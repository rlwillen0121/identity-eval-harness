use std::collections::BTreeMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicUsize, Ordering},
};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use ib_core::{hashing::sha256_hex, jcs_dumps};
use ib_providers::{base::Provider, replay::ReplayProvider};
use ib_v5_accounting::{
    OPERATOR_OR_GATEWAY_ATTESTATION_V5, USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
    V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA, V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA,
    provider_fingerprint_sha256_v5,
};
use serde_json::json;
use tempfile::tempdir;

const MAX_REQUEST_BYTES: usize = 2 * 1024 * 1024;

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_identitybench")
}

fn workspace_root() -> &'static Path {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
}

fn dataset() -> PathBuf {
    workspace_root().join("datasets/v5_draft5/v5_dev")
}

fn run_cli(root: &Path, args: &[&str]) -> Output {
    Command::new(binary())
        .arg("--root")
        .arg(root)
        .arg("--no-env-file")
        .args(args)
        .output()
        .expect("identitybench process starts")
}

fn write_pricing(path: &Path) {
    std::fs::write(
        path,
        br#"{"uncached_input_micros_per_million":0,"cached_input_micros_per_million":0,"output_micros_per_million":0,"reasoning_micros_per_million":0}"#,
    )
    .expect("pricing artifact");
}

fn snapshot_tree(root: &Path) -> BTreeMap<PathBuf, Option<Vec<u8>>> {
    let mut pending = vec![PathBuf::new()];
    let mut files = BTreeMap::new();
    while let Some(relative) = pending.pop() {
        let directory = root.join(&relative);
        for entry in std::fs::read_dir(directory).expect("artifact directory reads") {
            let entry = entry.expect("artifact directory entry reads");
            let child = relative.join(entry.file_name());
            let file_type = entry.file_type().expect("artifact type reads");
            if file_type.is_dir() {
                files.insert(child.clone(), None);
                pending.push(child);
            } else {
                assert!(file_type.is_file(), "run artifacts contain regular files");
                files.insert(
                    child.clone(),
                    Some(std::fs::read(root.join(child)).expect("artifact bytes read")),
                );
            }
        }
    }
    files
}

struct MockProvider {
    base_url: String,
    requests: Arc<AtomicUsize>,
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

impl MockProvider {
    fn start() -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("loopback listener binds");
        listener
            .set_nonblocking(true)
            .expect("mock listener is nonblocking");
        let address = listener.local_addr().expect("mock listener address");
        let requests = Arc::new(AtomicUsize::new(0));
        let stop = Arc::new(AtomicBool::new(false));
        let worker_requests = Arc::clone(&requests);
        let worker_stop = Arc::clone(&stop);
        let worker = thread::spawn(move || {
            while !worker_stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        worker_requests.fetch_add(1, Ordering::SeqCst);
                        respond_to_one_request(&mut stream);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                    }
                    Err(_) => break,
                }
            }
        });
        Self {
            base_url: format!("http://{address}/v1"),
            requests,
            stop,
            worker: Some(worker),
        }
    }

    fn request_count(&self) -> usize {
        self.requests.load(Ordering::SeqCst)
    }
}

impl Drop for MockProvider {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(worker) = self.worker.take() {
            worker.join().expect("mock provider worker joins");
        }
    }
}

#[test]
fn fresh_run_rejects_an_occupied_target_before_provider_requests_or_artifact_changes() {
    let temp = tempdir().expect("temporary workspace");
    let replay = temp.path().join("responses.jsonl");
    std::fs::write(&replay, b"").expect("empty local replay log");
    let original_config = temp.path().join("original.yaml");
    std::fs::write(
        &original_config,
        format!(
            "model:\n  provider: replay\n  model: original-model\n  extra:\n    path: {:?}\n",
            replay
        ),
    )
    .expect("offline replay config");
    let pricing = temp.path().join("pricing.json");
    write_pricing(&pricing);
    let output = temp.path().join("run");
    let initial = run_cli(
        temp.path(),
        &[
            "v5",
            "run",
            "--config",
            original_config.to_str().unwrap(),
            "--pricing",
            pricing.to_str().unwrap(),
            "--suite",
            "v5_dev",
            "--dataset",
            dataset().to_str().unwrap(),
            "--out",
            output.to_str().unwrap(),
            "--repeats",
            "1",
        ],
    );
    assert!(
        initial.status.success(),
        "{}",
        String::from_utf8_lossy(&initial.stderr)
    );
    assert!(output.join("manifest.json").is_file());
    assert!(output.join("episodes.jsonl").is_file());
    let before = snapshot_tree(&output);

    let mock = MockProvider::start();
    let replacement_config = temp.path().join("replacement.yaml");
    std::fs::write(
        &replacement_config,
        "model:\n  provider: openai_compatible\n  model: replacement-model\n  base_url_env: MOCK_BASE_URL\n  api_key_env: MOCK_API_KEY\n  timeout_s: 10\n  concurrency: 1\n  extra:\n    api: chat_completions\n    max_retries: 0\n",
    )
    .expect("replacement model config");
    let rejected = Command::new(binary())
        .arg("--root")
        .arg(temp.path())
        .arg("--no-env-file")
        .env("MOCK_BASE_URL", &mock.base_url)
        .env("MOCK_API_KEY", "local-test-key")
        .env("NO_PROXY", "127.0.0.1,localhost")
        .env("no_proxy", "127.0.0.1,localhost")
        .args([
            "v5",
            "run",
            "--config",
            replacement_config.to_str().unwrap(),
            "--pricing",
            pricing.to_str().unwrap(),
            "--suite",
            "v5_dev",
            "--dataset",
            dataset().to_str().unwrap(),
            "--out",
            output.to_str().unwrap(),
            "--repeats",
            "1",
        ])
        .output()
        .expect("identitybench process starts");

    assert_eq!(rejected.status.code(), Some(3));
    assert!(
        String::from_utf8_lossy(&rejected.stderr)
            .contains("run output directory already contains a run")
    );
    assert_eq!(mock.request_count(), 0, "provider must not be called");
    assert_eq!(
        snapshot_tree(&output),
        before,
        "run artifact bytes are unchanged"
    );
}

fn respond_to_one_request(stream: &mut TcpStream) {
    if stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .is_err()
    {
        return;
    }
    let mut bytes = Vec::new();
    let mut chunk = [0_u8; 8192];
    let content_length = loop {
        let Ok(count) = stream.read(&mut chunk) else {
            return;
        };
        if count == 0 {
            return;
        }
        bytes.extend_from_slice(&chunk[..count]);
        if bytes.len() > MAX_REQUEST_BYTES {
            return;
        }
        let Some(header_end) = bytes.windows(4).position(|window| window == b"\r\n\r\n") else {
            continue;
        };
        let Ok(headers) = std::str::from_utf8(&bytes[..header_end]) else {
            return;
        };
        let Some(length) = headers.lines().find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().ok())
                .flatten()
        }) else {
            return;
        };
        if bytes.len() >= header_end + 4 + length {
            break length;
        }
    };
    let Some(header_end) = bytes.windows(4).position(|window| window == b"\r\n\r\n") else {
        return;
    };
    if bytes.len() < header_end + 4 + content_length {
        return;
    }
    let body = br#"{"id":"mock","object":"chat.completion","created":0,"model":"mock","choices":[{"index":0,"message":{"role":"assistant","content":""},"finish_reason":"length"}],"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}"#;
    let _ = write!(
        stream,
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    let _ = stream.write_all(body);
}

#[test]
fn conflicting_run_dataset_copy_is_rejected_before_provider_calls_or_output_writes() {
    let temp = tempdir().expect("temporary workspace");
    let output = temp.path().join("run");
    let copied_dataset = output.join("dataset");
    std::fs::create_dir_all(&copied_dataset).expect("existing copied dataset directory");
    let seal_sentinel = b"mismatched copied seal\n";
    let records_sentinel = b"preserve existing episode records\n";
    let checkpoint_sentinel = b"preserve existing checkpoint\n";
    std::fs::write(copied_dataset.join("SEAL.json"), seal_sentinel)
        .expect("conflicting copied seal");
    std::fs::write(output.join("episodes.jsonl"), records_sentinel)
        .expect("existing episode records");
    std::fs::write(output.join("checkpoint.json"), checkpoint_sentinel)
        .expect("existing checkpoint");

    let mock = MockProvider::start();
    let config = temp.path().join("model.yaml");
    std::fs::write(
        &config,
        "model:\n  provider: openai_compatible\n  model: mock\n  base_url_env: MOCK_BASE_URL\n  api_key_env: MOCK_API_KEY\n  timeout_s: 10\n  concurrency: 1\n  extra:\n    api: chat_completions\n    max_retries: 0\n",
    )
    .expect("model config");
    let pricing = temp.path().join("pricing.json");
    write_pricing(&pricing);

    let result = Command::new(binary())
        .arg("--root")
        .arg(temp.path())
        .arg("--no-env-file")
        .env("MOCK_BASE_URL", &mock.base_url)
        .env("MOCK_API_KEY", "local-test-key")
        .env("NO_PROXY", "127.0.0.1,localhost")
        .env("no_proxy", "127.0.0.1,localhost")
        .args([
            "v5",
            "run",
            "--config",
            config.to_str().unwrap(),
            "--pricing",
            pricing.to_str().unwrap(),
            "--suite",
            "v5_dev",
            "--dataset",
            dataset().to_str().unwrap(),
            "--out",
            output.to_str().unwrap(),
            "--repeats",
            "1",
        ])
        .output()
        .expect("identitybench process starts");

    assert_eq!(result.status.code(), Some(3));
    assert!(
        String::from_utf8_lossy(&result.stderr)
            .contains("existing copied dataset does not match the sealed input")
    );
    assert_eq!(mock.request_count(), 0, "provider must not be called");
    assert_eq!(
        std::fs::read(copied_dataset.join("SEAL.json")).unwrap(),
        seal_sentinel
    );
    assert_eq!(
        std::fs::read(output.join("episodes.jsonl")).unwrap(),
        records_sentinel
    );
    assert_eq!(
        std::fs::read(output.join("checkpoint.json")).unwrap(),
        checkpoint_sentinel
    );
}

#[test]
fn rejected_resume_does_not_replace_copied_usage_manifest() {
    let temp = tempdir().expect("temporary workspace");
    let replay = temp.path().join("responses.jsonl");
    std::fs::write(&replay, b"").expect("empty local replay log");
    let model = "offline-replay";
    let provider = ReplayProvider::from_path(&replay, model).expect("replay provider identity");
    let fingerprint = provider_fingerprint_sha256_v5(
        "unified",
        provider.kind(),
        provider.model(),
        &provider.endpoint_identity(),
    );
    let evidence = json!({
        "schema_version": V5_USAGE_ACCOUNTING_ATTESTATION_SCHEMA,
        "role": "unified",
        "provider_kind": provider.kind(),
        "model_id": provider.model(),
        "provider_fingerprint_sha256": fingerprint.clone(),
        "aggregate_usage_scope": USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
        "attestation": OPERATOR_OR_GATEWAY_ATTESTATION_V5,
    });
    let evidence_raw = jcs_dumps(&evidence).expect("canonical usage evidence");
    let evidence_sha = sha256_hex(&evidence_raw);
    std::fs::write(temp.path().join("a.json"), &evidence_raw).expect("first evidence file");
    std::fs::write(temp.path().join("b.json"), &evidence_raw).expect("second evidence file");

    let manifest_for = |evidence_path: &str| {
        let manifest = json!({
            "schema_version": V5_USAGE_ACCOUNTING_MANIFEST_SCHEMA,
            "entries": [{
                "role": "unified",
                "provider_kind": provider.kind(),
                "model_id": provider.model(),
                "provider_fingerprint_sha256": fingerprint.clone(),
                "aggregate_usage_scope": USAGE_SCOPE_ALL_COMPLETIONS_RETRIES_V5,
                "evidence_path": evidence_path,
                "evidence_sha256": evidence_sha.clone(),
                "adapter_retry_limit": 0,
            }],
        });
        jcs_dumps(&manifest).expect("canonical usage manifest")
    };
    let first_manifest = manifest_for("a.json");
    let second_manifest = manifest_for("b.json");
    let first_manifest_path = temp.path().join("manifest-a.json");
    let second_manifest_path = temp.path().join("manifest-b.json");
    std::fs::write(&first_manifest_path, &first_manifest).expect("first usage manifest");
    std::fs::write(&second_manifest_path, &second_manifest).expect("second usage manifest");

    let config = temp.path().join("model.yaml");
    std::fs::write(
        &config,
        format!(
            "model:\n  provider: replay\n  model: {model}\n  extra:\n    path: {:?}\n",
            replay
        ),
    )
    .expect("replay model config");
    let pricing = temp.path().join("pricing.json");
    write_pricing(&pricing);
    let output = temp.path().join("run");

    let args = |manifest: &Path, resume: bool| {
        let mut args = vec![
            "v5".to_owned(),
            "run".to_owned(),
            "--config".to_owned(),
            config.display().to_string(),
            "--pricing".to_owned(),
            pricing.display().to_string(),
            "--suite".to_owned(),
            "v5_dev".to_owned(),
            "--dataset".to_owned(),
            dataset().display().to_string(),
            "--out".to_owned(),
            output.display().to_string(),
            "--repeats".to_owned(),
            "1".to_owned(),
            "--usage-accounting-manifest".to_owned(),
            manifest.display().to_string(),
        ];
        if resume {
            args.push("--resume".to_owned());
        }
        args
    };
    let first_args = args(&first_manifest_path, false);
    let first_refs: Vec<_> = first_args.iter().map(String::as_str).collect();
    let initial = run_cli(temp.path(), &first_refs);
    assert!(
        initial.status.success(),
        "{}",
        String::from_utf8_lossy(&initial.stderr)
    );
    let copied_manifest_path = output.join("usage_accounting/manifest.json");
    let before = std::fs::read(&copied_manifest_path).expect("copied usage manifest");
    let original_run_manifest = std::fs::read(output.join("manifest.json")).expect("run manifest");
    let existing_records = std::fs::read(output.join("episodes.jsonl")).expect("run records");
    std::fs::remove_file(output.join(".identitybench-run.lock"))
        .expect("simulate a run directory created before target locking");

    let resume_args = args(&second_manifest_path, true);
    let resume_refs: Vec<_> = resume_args.iter().map(String::as_str).collect();
    let resumed = run_cli(temp.path(), &resume_refs);
    assert!(!resumed.status.success());
    assert!(
        String::from_utf8_lossy(&resumed.stderr)
            .contains("resume run config does not match existing run")
    );
    assert_eq!(std::fs::read(copied_manifest_path).unwrap(), before);
    assert_eq!(
        std::fs::read(output.join("manifest.json")).unwrap(),
        original_run_manifest
    );
    assert_eq!(
        std::fs::read(output.join("episodes.jsonl")).unwrap(),
        existing_records
    );
    assert!(
        !output.join(".identitybench-run.lock").exists(),
        "invalid legacy resume must not create a lock marker"
    );

    let invalid_records = b"{ malformed episode record\n";
    std::fs::write(output.join("episodes.jsonl"), invalid_records)
        .expect("malformed legacy episode records");
    let matching_resume_args = args(&first_manifest_path, true);
    let matching_resume_refs: Vec<_> = matching_resume_args.iter().map(String::as_str).collect();
    let rejected_rows = run_cli(temp.path(), &matching_resume_refs);
    assert!(!rejected_rows.status.success());
    assert!(
        String::from_utf8_lossy(&rejected_rows.stderr)
            .contains("resume episode record line 1 is invalid JSON")
    );
    assert_eq!(
        std::fs::read(output.join("manifest.json")).unwrap(),
        original_run_manifest
    );
    assert_eq!(
        std::fs::read(output.join("episodes.jsonl")).unwrap(),
        invalid_records
    );
    assert!(!output.join(".identitybench-run.lock").exists());
}
