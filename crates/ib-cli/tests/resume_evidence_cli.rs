use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Output};
use std::sync::{
    Arc,
    atomic::{AtomicBool, AtomicUsize, Ordering},
    mpsc,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde_json::Value;
use tempfile::tempdir;

const MAX_REQUEST_BYTES: usize = 2 * 1024 * 1024;

#[derive(Clone, Copy)]
enum ReplyMode {
    ParseFailure,
    PaidThenFailure,
    HoldFirst,
}

struct FakeProvider {
    base_url: String,
    requests: Arc<AtomicUsize>,
    stop: Arc<AtomicBool>,
    held_request_release: Option<mpsc::Sender<()>>,
    worker: Option<JoinHandle<()>>,
}

impl FakeProvider {
    fn start(mode: ReplyMode) -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("local fake provider binds");
        listener
            .set_nonblocking(true)
            .expect("fake provider listener is nonblocking");
        let address = listener.local_addr().expect("fake provider address");
        let requests = Arc::new(AtomicUsize::new(0));
        let stop = Arc::new(AtomicBool::new(false));
        let (held_request_release, held_request_wait) = if matches!(mode, ReplyMode::HoldFirst) {
            let (release, wait) = mpsc::channel();
            (Some(release), Some(wait))
        } else {
            (None, None)
        };
        let worker_requests = Arc::clone(&requests);
        let worker_stop = Arc::clone(&stop);
        let worker = thread::spawn(move || {
            let mut held_request_wait = held_request_wait;
            while !worker_stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        if read_request(&mut stream) {
                            let request_index = worker_requests.fetch_add(1, Ordering::SeqCst);
                            match mode {
                                ReplyMode::ParseFailure => write_parse_failure(&mut stream),
                                ReplyMode::PaidThenFailure if request_index % 2 == 0 => {
                                    write_paid_continuation(&mut stream);
                                }
                                ReplyMode::PaidThenFailure => write_provider_failure(&mut stream),
                                ReplyMode::HoldFirst if request_index == 0 => {
                                    if let Some(wait) = held_request_wait.take() {
                                        let _ = wait.recv_timeout(Duration::from_secs(10));
                                    }
                                    write_parse_failure(&mut stream);
                                }
                                ReplyMode::HoldFirst => write_parse_failure(&mut stream),
                            }
                        }
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
            held_request_release,
            worker: Some(worker),
        }
    }

    fn request_count(&self) -> usize {
        self.requests.load(Ordering::SeqCst)
    }

    fn wait_for_requests(&self, expected: usize) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while self.request_count() < expected && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(
            self.request_count(),
            expected,
            "expected fake-provider requests"
        );
    }

    fn release_held_request(&mut self) {
        if let Some(release) = self.held_request_release.take() {
            let _ = release.send(());
        }
    }
}

impl Drop for FakeProvider {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        self.release_held_request();
        if let Some(worker) = self.worker.take() {
            worker.join().expect("fake provider worker joins");
        }
    }
}

fn read_request(stream: &mut TcpStream) -> bool {
    stream
        .set_nonblocking(false)
        .expect("accepted fake-provider connections use blocking reads");
    if stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .is_err()
    {
        return false;
    }
    let mut bytes = Vec::new();
    let mut chunk = [0_u8; 8192];
    let content_length = loop {
        let Ok(count) = stream.read(&mut chunk) else {
            return false;
        };
        if count == 0 {
            return false;
        }
        bytes.extend_from_slice(&chunk[..count]);
        if bytes.len() > MAX_REQUEST_BYTES {
            return false;
        }
        let Some(header_end) = bytes.windows(4).position(|window| window == b"\r\n\r\n") else {
            continue;
        };
        let Ok(headers) = std::str::from_utf8(&bytes[..header_end]) else {
            return false;
        };
        let Some(length) = headers.lines().find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().ok())
                .flatten()
        }) else {
            return false;
        };
        if bytes.len() >= header_end + 4 + length {
            break length;
        }
    };
    let Some(header_end) = bytes.windows(4).position(|window| window == b"\r\n\r\n") else {
        return false;
    };
    bytes.len() >= header_end + 4 + content_length
}

fn write_response(stream: &mut TcpStream, status: &str, body: &[u8]) {
    let _ = write!(
        stream,
        "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    let _ = stream.write_all(body);
}

fn write_parse_failure(stream: &mut TcpStream) {
    write_response(
        stream,
        "200 OK",
        br#"{"id":"mock","object":"chat.completion","created":0,"model":"local-resume-test","choices":[{"index":0,"message":{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"unknown_tool","arguments":"{}"}}]},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,"cost":0.000123}}"#,
    );
}

fn write_paid_continuation(stream: &mut TcpStream) {
    write_response(
        stream,
        "200 OK",
        br#"{"id":"mock","object":"chat.completion","created":0,"model":"local-resume-test","choices":[{"index":0,"message":{"role":"assistant","content":"I will inspect the available state before choosing a policy.","tool_calls":[]},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,"cost":0.000123}}"#,
    );
}

fn write_provider_failure(stream: &mut TcpStream) {
    write_response(
        stream,
        "400 Bad Request",
        br#"{"error":{"type":"invalid_request_error","message":"local test provider failure fixture"}}"#,
    );
}

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

fn write_inputs(temp: &Path) -> (PathBuf, PathBuf, PathBuf) {
    let config = temp.join("model.yaml");
    std::fs::write(
        &config,
        "model:\n  provider: openai_compatible\n  model: local-resume-test\n  base_url_env: RESUME_FAKE_BASE_URL\n  api_key_env: RESUME_FAKE_API_KEY\n  timeout_s: 60\n  concurrency: 1\n  extra:\n    api: chat_completions\n    max_retries: 0\nrun_config:\n  max_transient_retries: 0\n",
    )
    .expect("local fake-provider config");
    let pricing = temp.join("pricing.json");
    std::fs::write(
        &pricing,
        br#"{"uncached_input_micros_per_million":1000000,"cached_input_micros_per_million":1000000,"output_micros_per_million":1000000,"reasoning_micros_per_million":1000000}"#,
    )
    .expect("local pricing artifact");
    let output = temp.join("run");
    (config, pricing, output)
}

fn run_args(config: &Path, pricing: &Path, output: &Path, resume: bool) -> Vec<String> {
    let mut args = vec![
        "v5".to_owned(),
        "run".to_owned(),
        "--config".to_owned(),
        config.to_string_lossy().into_owned(),
        "--pricing".to_owned(),
        pricing.to_string_lossy().into_owned(),
        "--suite".to_owned(),
        "v5_dev".to_owned(),
        "--dataset".to_owned(),
        workspace_root()
            .join("datasets/v5_draft5/v5_dev")
            .to_string_lossy()
            .into_owned(),
        "--out".to_owned(),
        output.to_string_lossy().into_owned(),
        "--repeats".to_owned(),
        "1".to_owned(),
    ];
    if resume {
        args.push("--resume".to_owned());
    }
    args
}

fn run_cli(temp: &Path, mock: &FakeProvider, args: &[String]) -> Output {
    Command::new(binary())
        .arg("--root")
        .arg(temp)
        .arg("--no-env-file")
        .env("RESUME_FAKE_BASE_URL", &mock.base_url)
        .env("RESUME_FAKE_API_KEY", "local-test-key")
        .env("NO_PROXY", "127.0.0.1,localhost")
        .env("no_proxy", "127.0.0.1,localhost")
        .args(args)
        .output()
        .expect("identitybench process starts")
}

fn run_command(temp: &Path, mock: &FakeProvider, args: &[String]) -> Child {
    Command::new(binary())
        .arg("--root")
        .arg(temp)
        .arg("--no-env-file")
        .env("RESUME_FAKE_BASE_URL", &mock.base_url)
        .env("RESUME_FAKE_API_KEY", "local-test-key")
        .env("NO_PROXY", "127.0.0.1,localhost")
        .env("no_proxy", "127.0.0.1,localhost")
        .args(args)
        .spawn()
        .expect("identitybench process starts")
}

#[test]
fn parse_failure_rows_survive_resume_without_new_provider_requests() {
    let temp = tempdir().expect("temporary workspace");
    let mock = FakeProvider::start(ReplyMode::ParseFailure);
    let (config, pricing, output) = write_inputs(temp.path());
    let args = run_args(&config, &pricing, &output, false);

    let initial = run_cli(temp.path(), &mock, &args);
    assert!(
        initial.status.success(),
        "{}",
        String::from_utf8_lossy(&initial.stderr)
    );
    let before = std::fs::read(output.join("episodes.jsonl")).expect("recorded attempt rows");
    let rows: Vec<Value> = std::str::from_utf8(&before)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(rows.len(), 12);
    assert_eq!(
        mock.request_count(),
        12,
        "attempt outcomes: {:?}",
        rows.iter()
            .map(|row| (
                row["episode_id"].clone(),
                row["attempt_status"].clone(),
                row["attempt"]["terminal_cause"].clone(),
                row["attempt"]["provider_requests"].as_array().map(Vec::len),
            ))
            .collect::<Vec<_>>()
    );
    assert!(rows.iter().all(|row| {
        row["attempt_status"] == "WITHIN_PROTOCOL_PARSE_FAILURE"
            && row["execution_valid"] == false
            && row["provider_error"] == false
            && row["attempt"]["provider_requests"][0]["provider_reported_cost_micros"] == 123
    }));
    let manifest_before = std::fs::read(output.join("manifest.json")).unwrap();
    let manifest: Value = serde_json::from_slice(&manifest_before).unwrap();
    assert_eq!(
        manifest["descriptive_usage_summary"]["accounting_coverage_status"],
        "COMPLETE"
    );
    assert_eq!(
        manifest["descriptive_usage_summary"]["provider_billed_cost_micros"],
        1476
    );
    assert_eq!(mock.request_count(), 12);

    let report = run_cli(
        temp.path(),
        &mock,
        &[
            "v5".to_owned(),
            "report".to_owned(),
            "--run".to_owned(),
            output.to_string_lossy().into_owned(),
        ],
    );
    assert!(
        report.status.success(),
        "{}",
        String::from_utf8_lossy(&report.stderr)
    );
    let report_before = ["summary.json", "report.md"].map(|name| {
        std::fs::read(output.join("report").join(name)).expect("interim report artifact")
    });

    let resumed = run_cli(
        temp.path(),
        &mock,
        &run_args(&config, &pricing, &output, true),
    );
    assert!(
        resumed.status.success(),
        "{}",
        String::from_utf8_lossy(&resumed.stderr)
    );
    assert_eq!(
        mock.request_count(),
        12,
        "observed parse failures are terminal"
    );
    assert_eq!(
        std::fs::read(output.join("episodes.jsonl")).unwrap(),
        before,
        "recorded attempt evidence is unchanged"
    );
    assert_eq!(
        std::fs::read(output.join("manifest.json")).unwrap(),
        manifest_before
    );
    for (index, name) in ["summary.json", "report.md"].iter().enumerate() {
        assert_eq!(
            std::fs::read(output.join("report").join(name)).unwrap(),
            report_before[index]
        );
    }
}

#[test]
fn unsupported_provider_controls_fail_before_start_journal_or_http_request() {
    let temp = tempdir().expect("temporary workspace");
    let mock = FakeProvider::start(ReplyMode::ParseFailure);
    let (config, pricing, output) = write_inputs(temp.path());
    let config_text = std::fs::read_to_string(&config).unwrap();

    let continuation_config = temp.path().join("continuation.yaml");
    std::fs::write(
        &continuation_config,
        config_text.replace(
            "run_config:\n  max_transient_retries: 0\n",
            "run_config:\n  max_transient_retries: 0\n  segment_continuation_capability: SUPPORTED\n",
        ),
    )
    .unwrap();
    let continuation_output = output.with_file_name("continuation-run");
    let continuation = run_cli(
        temp.path(),
        &mock,
        &run_args(&continuation_config, &pricing, &continuation_output, false),
    );
    assert!(!continuation.status.success());
    assert!(String::from_utf8_lossy(&continuation.stderr).contains("requires a provider adapter"));
    assert!(!continuation_output.join("started_slots.jsonl").exists());
    assert_eq!(mock.request_count(), 0);

    let retry_config = temp.path().join("provider-retries.yaml");
    std::fs::write(
        &retry_config,
        config_text.replace("    max_retries: 0\n", "    max_retries: 1\n"),
    )
    .unwrap();
    let retry_output = output.with_file_name("provider-retries-run");
    let retry = run_cli(
        temp.path(),
        &mock,
        &run_args(&retry_config, &pricing, &retry_output, false),
    );
    assert!(!retry.status.success());
    assert!(
        String::from_utf8_lossy(&retry.stderr)
            .contains("v5 run inputs are invalid; check the YAML config"),
        "{}",
        String::from_utf8_lossy(&retry.stderr)
    );
    assert!(!retry_output.join("started_slots.jsonl").exists());
    assert_eq!(mock.request_count(), 0);
}

#[test]
fn infrastructure_incomplete_rows_are_preserved_and_not_retried_on_resume() {
    let temp = tempdir().expect("temporary workspace");
    let mock = FakeProvider::start(ReplyMode::PaidThenFailure);
    let (config, pricing, output) = write_inputs(temp.path());
    let initial = run_cli(
        temp.path(),
        &mock,
        &run_args(&config, &pricing, &output, false),
    );
    assert!(
        initial.status.success(),
        "{}",
        String::from_utf8_lossy(&initial.stderr)
    );
    let before = std::fs::read(output.join("episodes.jsonl")).expect("incomplete attempt rows");
    let rows: Vec<Value> = std::str::from_utf8(&before)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(rows.len(), 12);
    assert_eq!(
        mock.request_count(),
        24,
        "attempt outcomes: {:?}",
        rows.iter()
            .map(|row| (
                row["episode_id"].clone(),
                row["attempt_status"].clone(),
                row["attempt"]["terminal_cause"].clone(),
                row["attempt"]["provider_requests"].as_array().map(Vec::len),
            ))
            .collect::<Vec<_>>()
    );
    assert!(rows.iter().all(|row| {
        row["attempt_status"] == "INCOMPLETE_PROVIDER_FAILURE"
            && row["execution_valid"] == false
            && row["provider_error"] == true
    }));
    assert!(rows.iter().all(|row| {
        row["attempt"]["provider_requests"][0]["provider_reported_cost_micros"] == 123
    }));
    let manifest_before = std::fs::read(output.join("manifest.json")).unwrap();
    let manifest: Value = serde_json::from_slice(&manifest_before).unwrap();
    assert_eq!(
        manifest["descriptive_usage_summary"]["accounting_coverage_status"],
        "PARTIAL"
    );
    assert!(
        manifest["descriptive_usage_summary"]["provider_billed_cost_micros"]
            .as_i64()
            .is_some_and(|cost| cost > 0)
    );
    let started_before = std::fs::read(output.join("started_slots.jsonl")).unwrap();

    let resumed = run_cli(
        temp.path(),
        &mock,
        &run_args(&config, &pricing, &output, true),
    );
    assert!(!resumed.status.success());
    assert!(String::from_utf8_lossy(&resumed.stderr).contains("infrastructure-incomplete slot"));
    assert_eq!(
        mock.request_count(),
        24,
        "incomplete slots are not resampled"
    );
    assert_eq!(
        std::fs::read(output.join("episodes.jsonl")).unwrap(),
        before,
        "refused resume leaves recorded attempt evidence unchanged"
    );
    assert_eq!(
        std::fs::read(output.join("manifest.json")).unwrap(),
        manifest_before
    );
    assert_eq!(
        std::fs::read(output.join("started_slots.jsonl")).unwrap(),
        started_before,
        "refused resume keeps slot history unchanged"
    );
}

#[test]
fn hard_crash_records_started_slot_before_provider_request_and_resume_fails_closed() {
    let temp = tempdir().expect("temporary workspace");
    let mut mock = FakeProvider::start(ReplyMode::HoldFirst);
    let (config, pricing, output) = write_inputs(temp.path());
    let mut child = run_command(
        temp.path(),
        &mock,
        &run_args(&config, &pricing, &output, false),
    );
    mock.wait_for_requests(1);

    let journal = std::fs::read_to_string(output.join("started_slots.jsonl"))
        .expect("started-slot journal exists before provider requests");
    let mut journal_lines = journal.lines();
    let header: Value = serde_json::from_str(journal_lines.next().unwrap()).unwrap();
    assert_eq!(
        header["schema_version"],
        "identitybench-v5-started-slots-v1"
    );
    let journal_value: Value = serde_json::from_str(journal_lines.next().unwrap()).unwrap();
    assert_eq!(journal_value["repeat_index"], 1);
    assert!(journal_value["episode_id"].as_str().is_some());

    child.kill().expect("terminate in-flight run process");
    let _ = child.wait().expect("crashed run process exits");
    mock.release_held_request();
    let before = std::fs::read(output.join("episodes.jsonl")).expect("checkpointed row file");
    assert!(before.is_empty(), "no fake completed attempt is written");
    assert_eq!(mock.request_count(), 1);

    let resumed = run_cli(
        temp.path(),
        &mock,
        &run_args(&config, &pricing, &output, true),
    );
    assert!(!resumed.status.success());
    assert!(
        String::from_utf8_lossy(&resumed.stderr)
            .contains("started slot without exact-state recovery")
    );
    assert_eq!(
        mock.request_count(),
        1,
        "hard-crashed work is not restarted"
    );
    assert_eq!(
        std::fs::read(output.join("episodes.jsonl")).unwrap(),
        before,
        "hard-crash evidence remains unchanged"
    );
}
