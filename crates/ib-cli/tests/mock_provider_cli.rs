use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::process::{Command, Output};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use serde_json::{Value, json};
use tempfile::tempdir;

const EXPECTED_EPISODES: usize = 12;
const MAX_REQUEST_BYTES: usize = 2 * 1024 * 1024;

#[derive(Debug)]
struct RequestSummary {
    request_line: String,
    authorization_present: bool,
    model: Option<String>,
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

fn run_cli(root: &Path, args: &[&str]) -> Output {
    Command::new(binary())
        .arg("--root")
        .arg(root)
        .arg("--no-env-file")
        .args(args)
        .output()
        .expect("identitybench process starts")
}

fn find_header_end(bytes: &[u8]) -> Option<usize> {
    bytes.windows(4).position(|window| window == b"\r\n\r\n")
}

fn read_request(stream: &mut TcpStream) -> Result<(String, bool, Value), String> {
    stream
        .set_nonblocking(false)
        .map_err(|_| "could not make mock request reads blocking".to_owned())?;
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .map_err(|_| "could not bound mock request read".to_owned())?;
    let mut bytes = Vec::new();
    let mut chunk = [0_u8; 8192];
    loop {
        let count = stream
            .read(&mut chunk)
            .map_err(|_| "mock endpoint could not read request".to_owned())?;
        if count == 0 {
            return Err("mock endpoint received an incomplete request".to_owned());
        }
        bytes.extend_from_slice(&chunk[..count]);
        if bytes.len() > MAX_REQUEST_BYTES {
            return Err("mock endpoint request exceeded its byte bound".to_owned());
        }
        if let Some(header_end) = find_header_end(&bytes) {
            let header = std::str::from_utf8(&bytes[..header_end])
                .map_err(|_| "mock endpoint request headers were not UTF-8".to_owned())?;
            let content_length = header
                .lines()
                .find_map(|line| {
                    let (name, value) = line.split_once(':')?;
                    name.eq_ignore_ascii_case("content-length")
                        .then(|| value.trim().parse::<usize>().ok())
                        .flatten()
                })
                .ok_or_else(|| "mock endpoint request omitted content length".to_owned())?;
            let body_start = header_end + 4;
            if bytes.len() >= body_start.saturating_add(content_length) {
                let request_line = header
                    .lines()
                    .next()
                    .ok_or_else(|| "mock endpoint request line was missing".to_owned())?
                    .to_owned();
                let authorization_present = header.lines().any(|line| {
                    line.split_once(':')
                        .is_some_and(|(name, _)| name.eq_ignore_ascii_case("authorization"))
                });
                let body = serde_json::from_slice(&bytes[body_start..body_start + content_length])
                    .map_err(|_| "mock endpoint request body was not JSON".to_owned())?;
                return Ok((request_line, authorization_present, body));
            }
        }
    }
}

fn spawn_mock_openai(
    expected_requests: usize,
) -> (String, JoinHandle<Result<Vec<RequestSummary>, String>>) {
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("loopback listener binds");
    listener
        .set_nonblocking(true)
        .expect("mock listener is nonblocking");
    let address = listener.local_addr().expect("mock listener address");
    let handle = thread::spawn(move || {
        let deadline = Instant::now() + Duration::from_secs(10);
        let mut requests = Vec::with_capacity(expected_requests);
        while requests.len() < expected_requests {
            match listener.accept() {
                Ok((mut stream, _)) => {
                    let (request_line, authorization_present, body) = read_request(&mut stream)?;
                    let response = json!({
                        "id": "chatcmpl-local-test",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "mock",
                        "choices": [{
                            "index": 0,
                            "message": {"role": "assistant", "content": ""},
                            "finish_reason": "length"
                        }],
                        "usage": {
                            "prompt_tokens": 11,
                            "completion_tokens": 7,
                            "total_tokens": 18
                        }
                    });
                    let response = serde_json::to_vec(&response)
                        .map_err(|_| "mock endpoint could not encode its response".to_owned())?;
                    write!(
                        stream,
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        response.len()
                    )
                    .and_then(|_| stream.write_all(&response))
                    .map_err(|_| "mock endpoint could not write its response".to_owned())?;
                    requests.push(RequestSummary {
                        request_line,
                        authorization_present,
                        model: body.get("model").and_then(Value::as_str).map(str::to_owned),
                    });
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    if Instant::now() >= deadline {
                        return Err("mock endpoint request deadline expired".to_owned());
                    }
                    thread::sleep(Duration::from_millis(10));
                }
                Err(_) => return Err("mock endpoint could not accept a connection".to_owned()),
            }
        }
        Ok(requests)
    });
    (format!("http://{address}/v1"), handle)
}

#[test]
fn v5_run_and_report_account_a_local_openai_compatible_response() {
    let temp = tempdir().expect("temporary workspace");
    let dataset = temp.path().join("dataset");
    let built = run_cli(
        workspace_root(),
        &[
            "v5",
            "build",
            "--suite",
            "v5_dev",
            "--out",
            dataset.to_str().unwrap(),
        ],
    );
    assert!(
        built.status.success(),
        "{}",
        String::from_utf8_lossy(&built.stderr)
    );

    let (base_url, server) = spawn_mock_openai(EXPECTED_EPISODES);
    let config = temp.path().join("model.yaml");
    std::fs::write(
        &config,
        "model:\n  provider: openai_compatible\n  model: mock\n  base_url_env: MOCK_BASE_URL\n  api_key_env: MOCK_API_KEY\n  timeout_s: 10\n  concurrency: 1\n  extra:\n    api: chat_completions\n    max_retries: 0\n",
    )
    .expect("local provider config");
    let pricing = temp.path().join("pricing.json");
    std::fs::write(
        &pricing,
        br#"{"uncached_input_micros_per_million":0,"cached_input_micros_per_million":0,"output_micros_per_million":0,"reasoning_micros_per_million":0}"#,
    )
    .expect("local pricing artifact");
    let run_dir = temp.path().join("run");
    let run = Command::new(binary())
        .arg("--root")
        .arg(temp.path())
        .arg("--no-env-file")
        .env("MOCK_BASE_URL", &base_url)
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
            dataset.to_str().unwrap(),
            "--out",
            run_dir.to_str().unwrap(),
            "--repeats",
            "1",
        ])
        .output()
        .expect("identitybench process starts");
    let requests = server.join().expect("mock server thread joins");
    assert!(
        run.status.success(),
        "{}",
        String::from_utf8_lossy(&run.stderr)
    );
    let requests = requests.expect("local mock endpoint served all expected calls");
    assert_eq!(requests.len(), EXPECTED_EPISODES);
    for request in &requests {
        assert_eq!(request.request_line, "POST /v1/chat/completions HTTP/1.1");
        assert!(request.authorization_present);
        assert_eq!(request.model.as_deref(), Some("mock"));
    }

    let run_manifest: Value = serde_json::from_slice(
        &std::fs::read(run_dir.join("manifest.json")).expect("run manifest"),
    )
    .expect("run manifest JSON");
    assert_eq!(run_manifest["run_episode_record_count"], EXPECTED_EPISODES);
    let rows = std::fs::read_to_string(run_dir.join("episodes.jsonl")).expect("run records");
    assert_eq!(rows.lines().count(), EXPECTED_EPISODES);
    for line in rows.lines() {
        let row: Value = serde_json::from_str(line).expect("episode record JSON");
        assert_eq!(row["provider_error"], false);
        assert_eq!(row["execution_valid"], true);
        assert_eq!(row["termination_reason"], "OUTPUT_BUDGET_STOP");

        let attempt = &row["attempt"];
        assert_eq!(attempt["attempt_status"], "WITHIN_PROTOCOL_OUTPUT_LIMIT");
        assert_eq!(
            attempt["terminal_cause"],
            "TOTAL_OUTPUT_ALLOWANCE_EXHAUSTED"
        );
        assert_eq!(attempt["completed_assistant_generations"], 1);
        assert_eq!(attempt["tool_call_count"], 0);
        assert!(attempt["terminal_reservation_ordinal"].is_array());
        assert!(attempt["terminal_outcome_elapsed_ms"].is_number());
        assert_eq!(attempt["provider_requests"].as_array().unwrap().len(), 1);

        let provider_request = &attempt["provider_requests"][0];
        assert_eq!(provider_request["request_status"], "COMPLETED");
        assert_eq!(
            provider_request["normalized_decode_status"],
            "TERMINAL_OUTPUT_LIMIT"
        );
        assert_eq!(provider_request["usage_scope"], "PHYSICAL_REQUEST");
        assert_eq!(provider_request["input_tokens_total"], 11);
        assert_eq!(provider_request["output_tokens_total"], 7);
        assert_eq!(provider_request["cost_basis"], "FROZEN_RATE_ESTIMATE");
    }

    // The listener was dropped when the server thread completed. A successful resume
    // must reuse every retained attempt without contacting the provider again.
    let resumed = Command::new(binary())
        .arg("--root")
        .arg(temp.path())
        .arg("--no-env-file")
        .env("MOCK_BASE_URL", &base_url)
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
            dataset.to_str().unwrap(),
            "--out",
            run_dir.to_str().unwrap(),
            "--repeats",
            "1",
            "--resume",
        ])
        .output()
        .expect("resume process starts");
    assert!(
        resumed.status.success(),
        "{}",
        String::from_utf8_lossy(&resumed.stderr)
    );
    assert_eq!(
        std::fs::read_to_string(run_dir.join("episodes.jsonl")).unwrap(),
        rows
    );

    let report_dir = temp.path().join("report");
    let report = run_cli(
        temp.path(),
        &[
            "v5",
            "report",
            "--run",
            run_dir.to_str().unwrap(),
            "--out",
            report_dir.to_str().unwrap(),
        ],
    );
    assert!(
        report.status.success(),
        "{}",
        String::from_utf8_lossy(&report.stderr)
    );
    let summary: Value = serde_json::from_slice(&report.stdout).expect("report command JSON");
    assert_eq!(summary["is_complete"], true);
    assert_eq!(summary["observed_slots"], EXPECTED_EPISODES);
    assert_eq!(summary["planned_slots"], EXPECTED_EPISODES);
    assert!(report_dir.join("report.md").is_file());
    assert!(report_dir.join("summary.json").is_file());
}
