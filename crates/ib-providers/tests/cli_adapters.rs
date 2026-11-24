use std::collections::{BTreeMap, VecDeque};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use ib_providers::base::{Message, ModelRequest, Provider};
use ib_providers::cli::agy::{
    AGENT_FILE, AGY_CAPACITY_ERROR_CODE, AGY_MAX_USER_MESSAGE_BYTES, AgyAdapter, AgyCliProvider,
    agent_definition, recover_completed_agent_text, served_model,
};
use ib_providers::cli::claude::{ClaudeAdapter, ClaudeCliProvider};
use ib_providers::cli::codex::{
    CONFIG_OVERRIDES, CodexAdapter, CodexCliProvider, DEFAULT_EFFORT as CODEX_DEFAULT_EFFORT,
    DISABLED_FEATURES,
};
use ib_providers::cli::cursor::{CursorAdapter, CursorCliProvider, chat_dir, cursor_config_dir};
use ib_providers::cli::grok::{GrokAdapter, GrokCliProvider};
use ib_providers::cli::muse::{MuseAdapter, MuseCliProvider, trace_events};
use ib_providers::cli::{
    CliAdapter, CliConfig, CredentialLink, Invocation, Isolation, MAX_SIDECAR_BYTES,
    NEUTRAL_SYSTEM, read_sidecar_text,
};
use ib_providers::subprocess::{Command, ProcessResult, ProcessRunner};
use serde_json::{Value, json};
use tempfile::TempDir;

type RunnerClosure = Box<dyn FnMut(&Command) -> ProcessResult + Send>;

struct FakeRunner {
    handlers: Mutex<VecDeque<RunnerClosure>>,
    commands: Mutex<Vec<Command>>,
}

impl FakeRunner {
    fn new(handlers: Vec<RunnerClosure>) -> Arc<Self> {
        Arc::new(Self {
            handlers: Mutex::new(handlers.into_iter().collect()),
            commands: Mutex::new(Vec::new()),
        })
    }

    fn single<F>(f: F) -> Arc<Self>
    where
        F: FnMut(&Command) -> ProcessResult + Send + 'static,
    {
        Self::new(vec![Box::new(f)])
    }

    fn commands(&self) -> Vec<Command> {
        self.commands.lock().unwrap().clone()
    }
}

#[async_trait]
impl ProcessRunner for FakeRunner {
    async fn run(&self, command: &Command) -> ProcessResult {
        self.commands.lock().unwrap().push(command.clone());
        let mut h = self
            .handlers
            .lock()
            .unwrap()
            .pop_front()
            .expect("unexpected call");
        h(command)
    }
}

fn flag_value<'a>(argv: &'a [String], flag: &str) -> &'a str {
    let pos = argv.iter().position(|a| a == flag).expect("missing flag");
    &argv[pos + 1]
}

fn make_request(prompt: &str, system: &str) -> ModelRequest {
    ModelRequest::new(system, vec![Message::user(prompt)])
}

#[test]
fn caller_environment_reaches_cli_allowlist_without_global_mutation_or_debug_leak() {
    let mut config = CliConfig::new("offline-test", "never-executed", 1.0);
    config.environment = Some(BTreeMap::from([
        ("LANG".into(), "fixture-language".into()),
        ("OPENAI_API_KEY".into(), "synthetic-secret-canary".into()),
    ]));
    assert!(!format!("{config:?}").contains("synthetic-secret-canary"));
    let provider = ClaudeCliProvider::new(ClaudeAdapter, config);
    let child = provider.child_env(None);
    assert_eq!(
        child.get("LANG").map(String::as_str),
        Some("fixture-language")
    );
    assert!(!child.contains_key("OPENAI_API_KEY"));
    assert!(
        !provider
            .error_text("synthetic-secret-canary", None, None)
            .contains("synthetic-secret-canary")
    );
    let explicit = BTreeMap::from([("LANG".into(), "explicit-override".into())]);
    assert_eq!(
        provider.child_env(Some(&explicit))["LANG"],
        "explicit-override"
    );
}

#[test]
fn cli_sensitive_struct_debug_output_omits_prompts_credentials_and_paths() {
    let marker = "synthetic-cli-debug-marker";
    let invocation = Invocation {
        argv: vec!["cli".to_string(), marker.to_string()],
        stdin: Some(marker.to_string()),
        raw: serde_json::Map::from_iter([(
            "private".to_string(),
            Value::String(marker.to_string()),
        )]),
        home_files: BTreeMap::from([("config/private.json".to_string(), marker.to_string())]),
    };
    assert!(!format!("{invocation:?}").contains(marker));

    let link = CredentialLink {
        real: Path::new("/Users/private-user/.config/cli/auth.json").to_path_buf(),
        link: Path::new("/tmp/private-call/home/auth.json").to_path_buf(),
        sync_back: true,
    };
    let isolation = Isolation {
        env: BTreeMap::from([("CLAUDE_CODE_OAUTH_TOKEN".to_string(), marker.to_string())]),
        links: vec![link.clone()],
    };
    let isolation_debug = format!("{isolation:?}");
    assert!(!isolation_debug.contains(marker));
    assert!(!isolation_debug.contains("private-user"));
    assert!(!format!("{link:?}").contains("private-user"));
}

#[tokio::test]
async fn cli_diagnostics_redact_environment_secrets_without_changing_response_text() {
    let secret = "synthetic-openrouter-secret-marker";
    let runner = FakeRunner::single(move |_cmd| ProcessResult {
        returncode: 0,
        stdout: {
            let model_usage = serde_json::Map::from_iter([(secret.to_string(), json!({}))]);
            json!({
                "type": "result",
                "subtype": "success",
                "is_error": false,
                "result": "answer remains byte-for-byte",
                "stop_reason": secret,
                "modelUsage": model_usage
            })
            .to_string()
        },
        stderr: String::new(),
        duration_s: 0.01,
        timed_out: false,
        launch_error: None,
    });
    let mut config = CliConfig::new("fixture-model", "unused", 5.0);
    config.runner = runner;
    config.version = Some("fixture-version".to_string());
    config.environment = Some(BTreeMap::from([(
        "OPENROUTER".to_string(),
        secret.to_string(),
    )]));

    let provider = ClaudeCliProvider::new(ClaudeAdapter, config);
    let response = provider.complete(&make_request("prompt", "system")).await;

    assert_eq!(
        response.text.as_deref(),
        Some("answer remains byte-for-byte")
    );
    assert_eq!(response.stop_reason.as_deref(), Some("<redacted>"));
    assert_eq!(response.served_model.as_deref(), Some("<redacted>"));
    let raw = response.raw.unwrap();
    assert_eq!(raw["stop_reason"], "<redacted>");
    assert!(!Value::Object(raw.clone()).to_string().contains(secret));
}

#[test]
fn cli_error_redacts_before_taking_stderr_excerpt() {
    let marker = "opaque-test-diagnostic-marker";
    let mut config = CliConfig::new("fixture-model", "unused", 1.0);
    config.environment = Some(BTreeMap::from([("OPENROUTER".into(), marker.into())]));
    let provider = ClaudeCliProvider::new(ClaudeAdapter, config);
    let result = ProcessResult {
        returncode: 1,
        stderr: format!("{marker}{}", " ".repeat(285)),
        stdout: String::new(),
        duration_s: 0.0,
        timed_out: false,
        launch_error: None,
    };

    let message = provider.error_text("failed", Some(&result), None);
    assert!(!message.contains("ostic-marker"));
}

#[test]
fn cli_error_removes_prompt_echo_before_redacting_its_secret_value() {
    let secret = "synthetic-openrouter-secret-marker";
    let prompt = format!("private-prompt-prefix {secret} private-prompt-suffix");
    let mut config = CliConfig::new("fixture-model", "unused", 1.0);
    config.environment = Some(BTreeMap::from([("OPENROUTER".into(), secret.into())]));
    let provider = ClaudeCliProvider::new(ClaudeAdapter, config);
    let request = make_request(&prompt, "");
    let result = ProcessResult {
        returncode: 1,
        stderr: format!("diagnostic\n{prompt}"),
        stdout: String::new(),
        duration_s: 0.0,
        timed_out: false,
        launch_error: None,
    };

    let message = provider.error_text("failed", Some(&result), Some(&request));
    assert!(message.contains("diagnostic"));
    assert!(!message.contains(secret));
    assert!(!message.contains("private-prompt-prefix"));
    assert!(!message.contains("private-prompt-suffix"));
}

#[test]
fn jsonl_cli_adapters_reduce_irrelevant_events_without_changing_final_records() {
    let request = make_request("fixture", "SYS");
    let config = CliConfig::new("fixture-model", "unused", 5.0);
    let io_dir = TempDir::new().unwrap();

    let mut agy_stdout = String::new();
    for index in 0..2_048 {
        agy_stdout.push_str(&json!({"event":"unrelated","index":index}).to_string());
        agy_stdout.push('\n');
    }
    agy_stdout.push_str(
        &json!({
            "event":"result",
            "result":{"status":"SUCCESS","response":"agy answer","usage":{"input_tokens":3,"output_tokens":2}}
        })
        .to_string(),
    );
    let agy = AgyAdapter.parse(
        &config,
        &ProcessResult {
            returncode: 0,
            stdout: agy_stdout,
            stderr: String::new(),
            duration_s: 0.0,
            timed_out: false,
            launch_error: None,
        },
        &request,
        io_dir.path(),
    );
    assert_eq!(agy.text.as_deref(), Some("agy answer"));

    let mut codex_stdout = String::new();
    for index in 0..40 {
        codex_stdout.push_str(
            &json!({"type":"turn.completed","usage":{"input_tokens":index,"output_tokens":1}})
                .to_string(),
        );
        codex_stdout.push('\n');
    }
    codex_stdout.push_str(
        &json!({"type":"item.completed","item":{"type":"agent_message","text":"codex answer"}})
            .to_string(),
    );
    let codex = CodexAdapter.parse(
        &config,
        &ProcessResult {
            returncode: 0,
            stdout: codex_stdout,
            stderr: String::new(),
            duration_s: 0.0,
            timed_out: false,
            launch_error: None,
        },
        &request,
        io_dir.path(),
    );
    assert_eq!(codex.text.as_deref(), Some("codex answer"));
    assert_eq!(
        codex.raw.as_ref().unwrap()["raw_usage"]
            .as_array()
            .unwrap()
            .len(),
        32
    );

    let mut muse_stderr = String::new();
    for index in 0..2_048 {
        muse_stderr.push_str(&format!(
            "SSE RESPONSE: event: ignored\\ndata: {}\n",
            json!({"type":"ignored","index":index})
        ));
    }
    muse_stderr.push_str("REQUEST: {\"instructions\":\"SYS\",\"tools\":[]}\n");
    muse_stderr.push_str(&format!(
        "SSE RESPONSE: event: response.completed\\ndata: {}\n",
        json!({
            "type":"response.completed",
            "response":{"model":"fixture-model","status":"completed","usage":{"input_tokens":4,"output_tokens":2}}
        })
    ));
    muse_stderr.push_str(&format!(
        "SSE RESPONSE: event: response.subscription_usage\\ndata: {}\n",
        json!({"type":"response.subscription_usage","subscription":{"active":true,"used_percent":1.0}})
    ));
    let muse = MuseAdapter { real_home: None }.parse(
        &config,
        &ProcessResult {
            returncode: 0,
            stdout: json!({
                "payload_type":"run.terminal.completed",
                "payload":{"terminal":"completed","text":"muse answer"}
            })
            .to_string(),
            stderr: muse_stderr,
            duration_s: 0.0,
            timed_out: false,
            launch_error: None,
        },
        &request,
        io_dir.path(),
    );
    assert_eq!(muse.text.as_deref(), Some("muse answer"));
    assert!(muse.error.is_none());
    io_dir.close().unwrap();

    let mut legitimate_steps = vec![
        json!({
            "event":"step_update",
            "step_update":{"step_index":1,"step_type":"agent_response","text_delta":"first ","state":"IN_PROGRESS"}
        })
        .as_object()
        .unwrap()
        .clone(),
    ];
    legitimate_steps.extend((2..=257).map(|step_index| {
        json!({
            "event":"step_update",
            "step_update":{"step_index":step_index,"step_type":"agent_response","text_delta":"interleaved noise","state":"DONE"}
        })
        .as_object()
        .unwrap()
        .clone()
    }));
    legitimate_steps.push(
        json!({
            "event":"step_update",
            "step_update":{"step_index":1,"step_type":"agent_response","text_delta":"interleaved chunk","state":"DONE"}
        })
        .as_object()
        .unwrap()
        .clone(),
    );
    let expected_recovered = "first interleaved chunk";
    assert_eq!(
        recover_completed_agent_text(&legitimate_steps).as_deref(),
        Some(expected_recovered)
    );

    let mut agy_recovery_stdout = legitimate_steps
        .iter()
        .map(|event| Value::Object(event.clone()))
        .map(|event| event.to_string())
        .collect::<Vec<_>>()
        .join("\n");
    agy_recovery_stdout.push('\n');
    agy_recovery_stdout.push_str(
        &json!({
            "event":"result",
            "result":{"status":"FAILURE","error":"function call is empty - no input to parse"}
        })
        .to_string(),
    );
    let agy_recovery_io = TempDir::new().unwrap();
    let recovered = AgyAdapter.parse(
        &config,
        &ProcessResult {
            returncode: 0,
            stdout: agy_recovery_stdout,
            stderr: String::new(),
            duration_s: 0.0,
            timed_out: false,
            launch_error: None,
        },
        &request,
        agy_recovery_io.path(),
    );
    assert_eq!(recovered.text.as_deref(), Some(expected_recovered));
    assert!(recovered.error.is_none());
}

// --------------------------------------------------------------------------- agy_cli

fn agy_stream_event(result: Value, log: Option<&str>) -> RunnerClosure {
    let log_owned = log.map(str::to_string);
    Box::new(move |cmd: &Command| {
        if let Some(ref log_txt) = log_owned {
            fs::write(flag_value(&cmd.argv, "--log-file"), log_txt).unwrap();
        }
        let events = [
            json!({"event": "init", "conversation_id": "c1"}),
            json!({"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "finish"}}),
            json!({"event": "result", "result": result}),
        ];
        let stdout = events
            .iter()
            .map(|e| e.to_string())
            .collect::<Vec<_>>()
            .join("\n")
            + "\n";
        ProcessResult {
            returncode: 0,
            stdout,
            stderr: String::new(),
            duration_s: 0.01,
            timed_out: false,
            launch_error: None,
        }
    })
}

#[tokio::test]
async fn agy_cli_argv_agent_definition_and_served_model() {
    let log = "I1014 21:05:45 model_resolver.go:155] model alias \"gemini-4-argon\" resolved to \"gemini-4-argon-medium\"\n";
    let result = json!({
        "conversation_id": "c1",
        "status": "SUCCESS",
        "response": "{\"decision\":\"DENY\",\"rules\":[\"sec-001\"],\"toolSummary\":\"done\"}\n",
        "duration_seconds": 1.0,
        "num_turns": 1,
        "structured_output": {"decision": "DENY", "rules": ["sec-001"]},
        "usage": {"input_tokens": 821, "output_tokens": 50, "thinking_tokens": 0, "cache_read_tokens": 0}
    });

    let runner = FakeRunner::single(agy_stream_event(result.clone(), Some(log)));
    let mut cfg = CliConfig::new("gemini-4-argon", "agy", 900.0);
    cfg.runner = Arc::clone(&runner) as Arc<dyn ProcessRunner>;
    cfg.version = Some("1.3.5".to_string());
    cfg.retry_base_delay_s = 0.0;
    let p = AgyCliProvider::new(AgyAdapter, cfg.clone());

    let resp = p.complete(&make_request("Decide REQ-1.", "SYS")).await;
    assert!(resp.ok());
    assert_eq!(resp.served_model.as_deref(), Some("gemini-4-argon-medium"));
    assert_eq!(
        resp.parsed.as_ref().unwrap().get("decision"),
        Some(&json!("DENY"))
    );
    assert_eq!(resp.usage.input_tokens, 821);
    assert_eq!(resp.usage.output_tokens, 50);

    // No --json-schema flag, and the private agent declares no tools.
    let seen_agent = Arc::new(Mutex::new(String::new()));
    let sa = Arc::clone(&seen_agent);
    let inner = agy_stream_event(
        json!({"status": "SUCCESS", "response": "{\"actions\":[]}", "usage": {"input_tokens": 10, "output_tokens": 5}}),
        None,
    );
    let mut inner = inner;
    let runner2 = FakeRunner::single(move |cmd: &Command| {
        let home = Path::new(&cmd.env["HOME"]);
        *sa.lock().unwrap() = fs::read_to_string(home.join(AGENT_FILE)).unwrap();
        inner(cmd)
    });
    cfg.runner = Arc::clone(&runner2) as Arc<dyn ProcessRunner>;
    let p2 = AgyCliProvider::new(AgyAdapter, cfg);
    let r2 = p2.complete(&make_request("Decide", "")).await;
    assert!(r2.ok());
    assert!(
        !runner2.commands()[0]
            .argv
            .contains(&"--json-schema".to_string())
    );
    assert!(seen_agent.lock().unwrap().contains("tools: []"));
    assert_eq!(
        r2.raw.as_ref().unwrap()["substituted_system"],
        json!(NEUTRAL_SYSTEM)
    );
    assert_eq!(served_model(log).as_deref(), Some("gemini-4-argon-medium"));
    assert!(agent_definition("SYS").contains("\ntools: []\n"));
    assert!(agent_definition("SYS").ends_with("---\n\nSYS"));
}

#[tokio::test]
async fn agy_rejects_oversized_log_without_discarding_completed_response_or_usage() {
    let runner = FakeRunner::single(|cmd: &Command| {
        fs::File::create(flag_value(&cmd.argv, "--log-file"))
            .unwrap()
            .set_len(MAX_SIDECAR_BYTES as u64 + 1)
            .unwrap();
        ProcessResult {
            returncode: 0,
            stdout: json!({"event": "result", "result": {
                "status": "SUCCESS", "response": "completed answer",
                "usage": {"input_tokens": 31, "output_tokens": 7, "cache_read_tokens": 11}
            }})
            .to_string(),
            stderr: String::new(),
            duration_s: 0.01,
            timed_out: false,
            launch_error: None,
        }
    });
    let mut config = CliConfig::new("offline-model", "never-executed", 1.0);
    config.runner = runner;
    config.version = Some("offline-test".to_owned());
    config.max_retries = 0;
    let provider = AgyCliProvider::new(AgyAdapter, config);

    let response = provider.complete(&make_request("x", "SYS")).await;
    assert!(response.ok(), "unexpected error: {:?}", response.error);
    assert_eq!(response.text.as_deref(), Some("completed answer"));
    assert_eq!(response.usage.input_tokens, 20);
    assert_eq!(response.usage.cache_read_tokens, 11);
    assert_eq!(response.usage.output_tokens, 7);
    assert!(response.served_model.is_none());
    assert_eq!(
        response.raw.as_ref().unwrap()["provider_log_rejected"],
        json!(true)
    );
}

#[tokio::test]
async fn agy_recovers_completed_action_response_on_empty_function_call_only_on_done() {
    let first_json = r#"{"actions":[{"tool":"get_identity","args":{"id":"u1"}}]}"#;
    let second_json = r#"{"decision":"DENY","rules":["sec-001"]}"#;
    let err_msg = "Malformed function call: Failed to parse function call: Function call is empty - no input to parse.";

    let events = [
        json!({"event": "init"}),
        json!({"event": "step_update", "step_update": {"step_index": 1, "state": "RUNNING", "step_type": "agent_response", "text_delta": first_json}}),
        json!({"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response"}}),
        json!({"event": "step_update", "step_update": {"step_index": 3, "state": "RUNNING", "step_type": "agent_response", "text_delta": &second_json[..18]}}),
        json!({"event": "step_update", "step_update": {"step_index": 3, "state": "RUNNING", "step_type": "agent_response", "text_delta": &second_json[18..]}}),
        json!({"event": "step_update", "step_update": {"step_index": 3, "state": "DONE", "step_type": "agent_response"}}),
        json!({"event": "result", "result": {"status": "ERROR", "error": err_msg, "response": format!("{first_json}\n{second_json}"), "usage": {"input_tokens": 1200, "output_tokens": 80, "thinking_tokens": 30, "cache_read_tokens": 200}}}),
    ];
    let stdout = events
        .iter()
        .map(Value::to_string)
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    let runner = FakeRunner::single(move |_cmd| ProcessResult {
        returncode: 0,
        stdout: stdout.clone(),
        stderr: String::new(),
        duration_s: 0.1,
        timed_out: false,
        launch_error: None,
    });
    let mut cfg = CliConfig::new("gemini-4-argon", "agy", 900.0);
    cfg.runner = runner;
    cfg.version = Some("1.3.5".to_string());
    cfg.max_retries = 0;
    let p = AgyCliProvider::new(AgyAdapter, cfg);

    let action_req = make_request("x", "SYS");
    let resp = p.complete(&action_req).await;
    assert!(resp.ok());
    assert!(resp.parsed.is_none());
    assert_eq!(resp.text.as_deref(), Some(second_json));
    assert_eq!(resp.usage.input_tokens, 1000);
    assert_eq!(resp.usage.cache_read_tokens, 200);
    assert_eq!(
        resp.raw.as_ref().unwrap()["recovered_empty_function_call"],
        json!(true)
    );
    assert!(!resp.raw.as_ref().unwrap().contains_key("error_code"));

    // Latest step NOT DONE -> must NOT backtrack!
    let not_done_events = [
        json!({"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": first_json}}),
        json!({"event": "step_update", "step_update": {"step_index": 3, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "{\"actions\":["}}),
        json!({"event": "result", "result": {"status": "ERROR", "error": err_msg, "usage": {"input_tokens": 100, "output_tokens": 10}}}),
    ];
    let stdout2 = not_done_events
        .iter()
        .map(Value::to_string)
        .collect::<Vec<_>>()
        .join("\n")
        + "\n";
    let runner2 = FakeRunner::single(move |_cmd| ProcessResult {
        returncode: 0,
        stdout: stdout2.clone(),
        stderr: String::new(),
        duration_s: 0.1,
        timed_out: false,
        launch_error: None,
    });
    let mut cfg2 = CliConfig::new("gemini-4-argon", "agy", 900.0);
    cfg2.runner = runner2;
    cfg2.version = Some("1.3.5".to_string());
    cfg2.max_retries = 0;
    let p2 = AgyCliProvider::new(AgyAdapter, cfg2);
    let r2 = p2.complete(&action_req).await;
    assert!(!r2.ok());
    assert_eq!(
        r2.raw.as_ref().unwrap()["error_code"],
        json!("CLI_MALFORMED_FUNCTION_CALL")
    );
}

#[tokio::test]
async fn agy_error_diagnostics_do_not_prove_an_unanswered_request() {
    for message in [
        None,
        Some("malformed function call"),
        Some("503 at capacity"),
        Some("quota exhausted"),
    ] {
        let stdout = message.map_or_else(String::new, |message| {
            json!({"event": "result", "result": {
                "status": "ERROR", "error": message, "response": "partial answer",
                "usage": {"input_tokens": 11, "output_tokens": 7, "thinking_tokens": 2}
            }})
            .to_string()
                + "\n"
        });
        let runner = FakeRunner::single(move |_| ProcessResult {
            returncode: 1,
            stdout: stdout.clone(),
            stderr: String::new(),
            duration_s: 0.1,
            timed_out: false,
            launch_error: None,
        });
        let mut config = CliConfig::new("offline-model", "never-executed", 1.0);
        config.runner = Arc::clone(&runner) as Arc<dyn ProcessRunner>;
        config.version = Some("offline-test".to_owned());
        config.max_retries = 0;
        let provider = AgyCliProvider::new(AgyAdapter, config);
        let response = provider.complete(&make_request("x", "SYS")).await;
        assert!(response.error.is_some());
        assert_eq!(runner.commands().len(), 1);
        let raw = response.raw.as_ref().unwrap();
        assert_ne!(raw.get("proven_unanswered"), Some(&json!(true)));
        if message.is_some() {
            assert_eq!(response.usage.input_tokens, 11);
            assert_eq!(response.usage.output_tokens, 7);
            assert_eq!(response.usage.reasoning_tokens, 2);
            assert_eq!(response.text.as_deref(), Some("partial answer"));
        } else {
            assert_eq!(raw["error_class"], json!("AMBIGUOUS_FAILURE"));
        }
    }
}

#[tokio::test]
async fn agy_capacity_guard_refuses_oversized_user_messages_without_spawning() {
    let success = || {
        agy_stream_event(
            json!({"status": "SUCCESS", "response": "{\"actions\":[]}", "usage": {"input_tokens": 10, "output_tokens": 5}}),
            None,
        )
    };
    // Exactly at the guard: delivered unchanged on stdin.
    let at_limit = "a".repeat(AGY_MAX_USER_MESSAGE_BYTES);
    let runner = FakeRunner::new(vec![success()]);
    let mut config = CliConfig::new("offline-model", "never-executed", 1.0);
    config.runner = Arc::clone(&runner) as Arc<dyn ProcessRunner>;
    config.version = Some("offline-test".to_owned());
    config.max_retries = 0;
    let provider = AgyCliProvider::new(AgyAdapter, config.clone());
    let response = provider.complete(&make_request(&at_limit, "SYS")).await;
    assert!(response.ok(), "{:?}", response.error);
    let commands = runner.commands();
    assert_eq!(commands.len(), 1);
    let stdin: Value =
        serde_json::from_str(commands[0].stdin.as_deref().unwrap().trim_end()).unwrap();
    assert_eq!(stdin["message"]["content"], json!(at_limit));

    // One byte over (multi-byte UTF-8 counts in bytes): refused before any process is spawned,
    // as a provider failure, never truncated and never delivered.
    for over in [
        "a".repeat(AGY_MAX_USER_MESSAGE_BYTES + 1),
        "é".repeat(AGY_MAX_USER_MESSAGE_BYTES / 2 + 1),
    ] {
        let idle = FakeRunner::new(Vec::new());
        let mut config = config.clone();
        config.runner = Arc::clone(&idle) as Arc<dyn ProcessRunner>;
        let provider = AgyCliProvider::new(AgyAdapter, config);
        let response = provider.complete(&make_request(&over, "SYS")).await;
        assert!(idle.commands().is_empty(), "nothing may be spawned");
        let error = response.error.as_deref().unwrap();
        assert!(error.contains("agy capacity guard"), "{error}");
        assert!(error.contains(&over.len().to_string()), "{error}");
        assert!(response.text.is_none() && response.parsed.is_none());
        let raw = response.raw.as_ref().unwrap();
        assert_eq!(raw["error_code"], json!(AGY_CAPACITY_ERROR_CODE));
        assert_eq!(raw["error_class"], json!("TRANSPORT_CAPACITY"));
        assert_eq!(raw["proven_unanswered"], json!(true));
        // Not the oversized-model-output path, which the runner scores as OUTPUT_LIMIT.
        assert!(raw.get("oversized_text_bytes").is_none());
    }
}

#[tokio::test]
async fn cli_timeout_preserves_buffered_usage_and_cost_without_claiming_completion() {
    let runner = FakeRunner::single(|_| ProcessResult {
        returncode: -9,
        stdout: json!({
            "type": "result", "subtype": "success", "is_error": false,
            "result": "buffered answer", "stop_reason": "end_turn",
            "usage": {"input_tokens": 11, "output_tokens": 7},
            "total_cost_usd": 0.00123
        })
        .to_string(),
        stderr: String::new(),
        duration_s: 1.0,
        timed_out: true,
        launch_error: None,
    });
    let mut config = CliConfig::new("offline-model", "never-executed", 1.0);
    config.runner = Arc::clone(&runner) as Arc<dyn ProcessRunner>;
    config.version = Some("offline-test".to_owned());
    config.max_retries = 0;
    let provider = ClaudeCliProvider::new(ClaudeAdapter, config);
    let response = provider.complete(&make_request("x", "SYS")).await;
    assert!(response.error.is_some());
    assert_eq!(runner.commands().len(), 1);
    assert_eq!(response.text.as_deref(), Some("buffered answer"));
    assert_eq!(response.usage.input_tokens, 11);
    assert_eq!(response.usage.output_tokens, 7);
    assert_eq!(response.reported_cost_usd, Some(0.00123));
    let raw = response.raw.as_ref().unwrap();
    assert_eq!(raw["error_class"], json!("AMBIGUOUS_FAILURE"));
    assert_eq!(raw["proven_unanswered"], json!(false));
    assert_eq!(raw["resumable"], json!(false));
}

// --------------------------------------------------------------------------- codex_cli

#[tokio::test]
async fn codex_cli_flags_and_usage() {
    assert_eq!(DISABLED_FEATURES.len(), 29);
    assert_eq!(CONFIG_OVERRIDES.len(), 3);
    assert_eq!(CODEX_DEFAULT_EFFORT, "medium");

    let runner = FakeRunner::new(vec![Box::new(|cmd: &Command| {
        fs::write(
            flag_value(&cmd.argv, "-o"),
            r#"{"decision":"DENY","rules":["LIFE-001"]}"#,
        )
        .unwrap();
        let stdout = [
                json!({"type": "thread.started", "thread_id": "th_123"}),
                json!({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 50, "reasoning_output_tokens": 30}}),
            ]
            .iter()
            .map(Value::to_string)
            .collect::<Vec<_>>()
            .join("\n");
        ProcessResult {
            returncode: 0,
            stdout,
            stderr: String::new(),
            duration_s: 0.01,
            timed_out: false,
            launch_error: None,
        }
    })]);

    let mut cfg = CliConfig::new("gpt-test", "codex", 900.0);
    cfg.effort = Some(CODEX_DEFAULT_EFFORT.to_string());
    cfg.runner = Arc::clone(&runner) as Arc<dyn ProcessRunner>;
    cfg.version = Some("0.162.1".to_string());
    cfg.retry_base_delay_s = 0.0;
    let p = CodexCliProvider::new(CodexAdapter, cfg);

    let r1 = p.complete(&make_request("Decide", "SYS")).await;
    assert!(r1.ok());
    assert_eq!(r1.usage.input_tokens, 200);
    assert_eq!(r1.usage.cache_read_tokens, 800);
    assert_eq!(r1.usage.reasoning_tokens, 30);
    assert_eq!(
        r1.text.as_deref(),
        Some(r#"{"decision":"DENY","rules":["LIFE-001"]}"#)
    );
    assert_eq!(r1.stop_reason.as_deref(), Some("completed"));
    assert!(
        !runner.commands()[0]
            .argv
            .contains(&"--output-schema".to_string())
    );
}

#[tokio::test]
async fn codex_rejects_oversized_sidecar_and_preserves_streamed_response_and_usage() {
    let runner = FakeRunner::single(|cmd: &Command| {
        fs::File::create(flag_value(&cmd.argv, "-o"))
            .unwrap()
            .set_len(MAX_SIDECAR_BYTES as u64 + 1)
            .unwrap();
        let stdout = [
            json!({"type": "item.completed", "item": {"type": "agent_message", "text": "streamed answer"}}),
            json!({"type": "turn.completed", "usage": {"input_tokens": 29, "cached_input_tokens": 9, "output_tokens": 6, "reasoning_output_tokens": 2}}),
        ]
        .iter()
        .map(Value::to_string)
        .collect::<Vec<_>>()
        .join("\n");
        ProcessResult {
            returncode: 0,
            stdout,
            stderr: String::new(),
            duration_s: 0.01,
            timed_out: false,
            launch_error: None,
        }
    });
    let mut config = CliConfig::new("offline-model", "never-executed", 1.0);
    config.runner = runner;
    config.version = Some("offline-test".to_owned());
    config.max_retries = 0;
    let provider = CodexCliProvider::new(CodexAdapter, config);

    let response = provider.complete(&make_request("x", "SYS")).await;
    assert!(response.ok(), "unexpected error: {:?}", response.error);
    assert_eq!(response.text.as_deref(), Some("streamed answer"));
    assert_eq!(response.usage.input_tokens, 20);
    assert_eq!(response.usage.cache_read_tokens, 9);
    assert_eq!(response.usage.output_tokens, 6);
    assert_eq!(response.usage.reasoning_tokens, 2);
    assert_eq!(
        response.raw.as_ref().unwrap()["output_file_rejected"],
        json!(true)
    );
}

#[tokio::test]
async fn codex_rejected_sidecar_without_streamed_fallback_is_not_an_empty_success() {
    let runner = FakeRunner::single(|cmd: &Command| {
        fs::File::create(flag_value(&cmd.argv, "-o"))
            .unwrap()
            .set_len(MAX_SIDECAR_BYTES as u64 + 1)
            .unwrap();
        ProcessResult {
            returncode: 0,
            stdout: json!({"type": "turn.completed", "usage": {
                "input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 1
            }})
            .to_string(),
            stderr: String::new(),
            duration_s: 0.01,
            timed_out: false,
            launch_error: None,
        }
    });
    let mut config = CliConfig::new("offline-model", "never-executed", 1.0);
    config.runner = runner;
    config.version = Some("offline-test".to_owned());
    config.max_retries = 0;
    let provider = CodexCliProvider::new(CodexAdapter, config);
    let request = make_request("x", "SYS");

    let response = provider.complete(&request).await;
    assert!(!response.ok());
    assert!(response.text.is_none());
    assert_eq!(response.usage.input_tokens, 5);
    assert!(
        response
            .error
            .as_deref()
            .unwrap()
            .contains("final output sidecar was rejected")
    );
    assert_eq!(
        response.raw.as_ref().unwrap()["output_file_rejected"],
        json!(true)
    );
}

#[cfg(unix)]
#[test]
fn sidecar_reader_rejects_links_and_oversized_files() {
    let temp = TempDir::new().unwrap();
    let ordinary = temp.path().join("ordinary.txt");
    fs::write(&ordinary, "ok").unwrap();
    assert_eq!(read_sidecar_text(&ordinary).unwrap(), "ok");

    let symlink = temp.path().join("symlink.txt");
    std::os::unix::fs::symlink(&ordinary, &symlink).unwrap();
    assert!(read_sidecar_text(&symlink).is_err());

    let hardlink = temp.path().join("hardlink.txt");
    fs::hard_link(&ordinary, &hardlink).unwrap();
    assert!(read_sidecar_text(&ordinary).is_err());
    assert!(read_sidecar_text(&hardlink).is_err());

    let oversized = temp.path().join("oversized.txt");
    fs::File::create(&oversized)
        .unwrap()
        .set_len(MAX_SIDECAR_BYTES as u64 + 1)
        .unwrap();
    assert!(read_sidecar_text(&oversized).is_err());
}

// --------------------------------------------------------------------------- claude, cursor, grok, muse

#[tokio::test]
async fn claude_grok_cursor_and_muse_via_mock_shell_script_and_runners() {
    // 1. Real temp-dir mock shell executable for ClaudeCliProvider!
    let tmp = TempDir::new().unwrap();
    let fake_claude = tmp.path().join("fake-claude.sh");
    fs::write(
        &fake_claude,
        r#"#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "2.1.296 (Claude Code)"
  exit 0
fi
cat >/dev/null
printf '%s\n' '{"type":"result","subtype":"success","is_error":false,"result":"{\"decision\":\"DENY\"}","structured_output":{"decision":"DENY"},"stop_reason":"end_turn","usage":{"input_tokens":10,"output_tokens":5,"cache_read_input_tokens":20,"cache_creation_input_tokens":30,"output_tokens_details":{"thinking_tokens":4}},"modelUsage":{"claude-haiku-5-5":{}},"total_cost_usd":0.0012}'
"#,
    )
    .unwrap();
    fs::set_permissions(&fake_claude, fs::Permissions::from_mode(0o755)).unwrap();

    let mut c_cfg = CliConfig::new(
        "claude-haiku-5-5",
        fake_claude.to_string_lossy().into_owned(),
        10.0,
    );
    c_cfg.retry_base_delay_s = 0.0;
    let claude_p = ClaudeCliProvider::new(ClaudeAdapter, c_cfg);
    assert!(!claude_p.endpoint_identity().is_empty());
    let cr = claude_p.complete(&make_request("Decide.", "SYS")).await;
    assert!(cr.ok());
    assert_eq!(cr.usage.input_tokens, 10);
    assert_eq!(cr.usage.cache_read_tokens, 20);
    assert_eq!(cr.usage.cache_write_tokens, 30);
    assert_eq!(cr.usage.reasoning_tokens, 4);
    assert_eq!(cr.reported_cost_usd, Some(0.0012));
    assert_eq!(cr.served_model.as_deref(), Some("claude-haiku-5-5"));

    // 2. Grok
    let g_runner = FakeRunner::single(|cmd| {
        assert!(
            cmd.argv
                .iter()
                .any(|a| a.starts_with("--system-prompt-override="))
        );
        assert!(!cmd.argv.iter().any(|a| a.starts_with("--json-schema=")));
        ProcessResult {
            returncode: 0,
            stdout: r#"{"type":"result","text":"ok","stopReason":"end_turn","usage":{"input_tokens":5,"output_tokens":3,"reasoning_tokens":1},"total_cost_usd":0.02}"#.to_string(),
            stderr: String::new(),
            duration_s: 0.01,
            timed_out: false,
            launch_error: None,
        }
    });
    let mut g_cfg = CliConfig::new("grok-4", "grok", 600.0);
    g_cfg.runner = g_runner;
    g_cfg.version = Some("1.0.46".to_string());
    let grok_p = GrokCliProvider::new(GrokAdapter, g_cfg);
    let g_req = make_request("hi", "- bullet system");
    let gr = grok_p.complete(&g_req).await;
    assert!(gr.ok());
    assert_eq!(gr.text.as_deref(), Some("ok"));
    assert_eq!(gr.usage.reasoning_tokens, 1);

    // 3. Cursor
    let cur_runner = FakeRunner::single(|cmd| {
        assert!(cmd.argv.last().unwrap().starts_with('\n'));
        ProcessResult {
            returncode: 0,
            stdout: r#"{"type":"result","subtype":"success","is_error":false,"result":"done","usage":{"inputTokens":7,"outputTokens":2,"cacheReadTokens":1,"cacheWriteTokens":0}}"#.to_string(),
            stderr: String::new(),
            duration_s: 0.01,
            timed_out: false,
            launch_error: None,
        }
    });
    let mut cur_cfg = CliConfig::new("auto", "cursor-agent", 600.0);
    cur_cfg.runner = cur_runner;
    cur_cfg.version = Some("1.0".to_string());
    let cur_p = CursorCliProvider::new(CursorAdapter, cur_cfg);
    let cur_r = cur_p.complete(&make_request("- leading dash", "")).await;
    assert!(cur_r.ok());
    assert_eq!(cur_r.usage.input_tokens, 7);
    let env_map = BTreeMap::from([("CURSOR_CONFIG_DIR".to_string(), "/tmp/cur".to_string())]);
    assert_eq!(cursor_config_dir(&env_map), Path::new("/tmp/cur"));
    assert!(chat_dir(&env_map, Path::new("/tmp/w")).starts_with("/tmp/cur/chats/"));

    // 4. Muse
    let muse_stderr = concat!(
        "REQUEST: {\"instructions\":\"SYS\",\"tools\":[]}\n",
        "SSE RESPONSE: event: response.completed\\ndata: {\"type\":\"response.completed\",\"response\":{\"model\":\"muse-m\",\"status\":\"completed\",\"usage\":{\"input_tokens\":100,\"input_tokens_details\":{\"cached_tokens\":40},\"output_tokens\":20,\"output_tokens_details\":{\"reasoning_tokens\":5}}}}\n",
        "SSE RESPONSE: event: response.subscription_usage\\ndata: {\"type\":\"response.subscription_usage\",\"subscription\":{\"used_percent\":2.5,\"active\":true,\"email\":\"private@example.invalid\",\"nested\":{\"a\":1}}}\n",
    );
    assert_eq!(trace_events(muse_stderr).len(), 2);
    let muse_runner = FakeRunner::single(move |_cmd| {
        ProcessResult {
        returncode: 0,
        stdout: r#"{"payload_type":"run.terminal.completed","payload":{"terminal":"completed","text":"{\"decision\":\"DENY\"}"}}"#.to_string(),
        stderr: muse_stderr.to_string(),
        duration_s: 0.01,
        timed_out: false,
        launch_error: None,
    }
    });
    let muse_home = TempDir::new().unwrap();
    fs::create_dir_all(muse_home.path().join(".config").join("muse")).unwrap();
    fs::write(
        muse_home.path().join(".config/muse/auth.json"),
        json!({
            "providers": {
                "meta": {
                    "mechanism": "oauth",
                    "subs_active": true,
                    "api_base_url": "https://api.meta.ai/v1"
                }
            }
        })
        .to_string(),
    )
    .unwrap();

    let mut m_cfg = CliConfig::new("muse-m", "muse", 900.0);
    m_cfg.runner = muse_runner;
    m_cfg.version = Some("1.4.10".to_string());
    m_cfg.max_retries = 0;
    let muse_p = MuseCliProvider::new(
        MuseAdapter {
            real_home: Some(muse_home.path().to_path_buf()),
        },
        m_cfg,
    );
    let mr = muse_p.complete(&make_request("hi", "SYS")).await;
    assert!(mr.ok(), "muse failed: {:?}", mr.error);
    assert_eq!(mr.usage.input_tokens, 60);
    assert_eq!(mr.usage.cache_read_tokens, 40);
    assert_eq!(mr.usage.reasoning_tokens, 5);
    assert_eq!(
        mr.raw.as_ref().unwrap()["subscription_usage"],
        json!({"used_percent": 2.5, "active": true})
    );
}

#[tokio::test]
async fn native_probe_observes_cli_cwd_marker_and_structured_tool_events() {
    for (cwd_marker, event_kind, expected) in [
        (true, "agent_message", true),
        (false, "file_change", true),
        (false, "agent_message", false),
    ] {
        let runner =
            FakeRunner::single(move |cmd| {
                if cwd_marker {
                    fs::write(cmd.cwd.join("canary.txt"), b"fixture").unwrap();
                }
                let stdout = [
                json!({"type":"item.completed","item":{"type":event_kind,"text":"file_change"}}),
                json!({"type":"item.completed","item":{"type":"agent_message","text":"done"}}),
                json!({"type":"turn.completed","usage":{}}),
            ].map(|event| event.to_string()).join("\n");
                ProcessResult {
                    stdout,
                    returncode: 0,
                    stderr: String::new(),
                    duration_s: 0.0,
                    timed_out: false,
                    launch_error: None,
                }
            });
        let mut config = CliConfig::new("fixture", "never-executed", 1.0);
        config.runner = runner;
        config.version = Some("offline".into());
        config.max_retries = 0;
        let mut req = make_request("fixture", "SYS");
        req.params.extra.insert(
            ib_providers::replay::CACHE_KEY_PARAM.into(),
            ib_providers::base::NATIVE_TOOL_PROBE_KEY.into(),
        );
        let result = CodexCliProvider::new(CodexAdapter, config)
            .complete(&req)
            .await;
        assert!(result.ok());
        assert_eq!(result.text.as_deref(), Some("done"));
        assert_eq!(
            result
                .raw
                .as_ref()
                .and_then(|r| r.get(ib_providers::base::NATIVE_TOOL_ACTIVITY))
                .and_then(Value::as_bool)
                .unwrap_or(false),
            expected
        );
    }
}

#[tokio::test]
async fn all_cli_adapters_preserve_untrimmed_visible_replies_and_present_empty_messages() {
    use ib_providers::cli::claude::{ClaudeAdapter, ClaudeCliProvider};
    use ib_providers::cli::cursor::{CursorAdapter, CursorCliProvider};
    use ib_providers::cli::grok::{GrokAdapter, GrokCliProvider};
    use ib_providers::cli::muse::{MuseAdapter, MuseCliProvider};

    let trailing = format!("{}{}", "{\"actions\":[]}", " ".repeat(20));

    // 1. Codex: sidecar untrimmed, sidecar whitespace-only, sidecar empty (with turn.completed),
    //    streamed untrimmed, streamed whitespace-only, streamed empty, and missing -> error.
    for (label, sidecar, streamed, turn_done, expected_text) in [
        (
            "sidecar_untrimmed",
            Some(trailing.as_str()),
            None,
            true,
            Some(trailing.as_str()),
        ),
        ("sidecar_ws", Some("   \n\t "), None, true, Some("   \n\t ")),
        ("sidecar_empty_completed", Some(""), None, true, Some("")),
        (
            "streamed_untrimmed",
            None,
            Some(trailing.as_str()),
            true,
            Some(trailing.as_str()),
        ),
        ("streamed_ws", None, Some("  \n "), true, Some("  \n ")),
        ("streamed_empty", None, Some(""), true, Some("")),
        ("missing_message", None, None, true, None),
        ("empty_sidecar_uncompleted", Some(""), None, false, None),
    ] {
        let sidecar_owned = sidecar.map(str::to_owned);
        let streamed_owned = streamed.map(str::to_owned);
        let runner = FakeRunner::single(move |cmd: &Command| {
            if let Some(ref content) = sidecar_owned {
                fs::write(flag_value(&cmd.argv, "-o"), content).unwrap();
            }
            let mut lines = Vec::new();
            if let Some(ref msg) = streamed_owned {
                lines.push(
                    json!({
                        "type": "item.completed",
                        "item": {"id": "item_1", "type": "agent_message", "text": msg}
                    })
                    .to_string(),
                );
            }
            if turn_done {
                lines.push(
                    json!({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
                        .to_string(),
                );
            }
            ProcessResult {
                returncode: 0,
                stdout: lines.join("\n"),
                stderr: String::new(),
                duration_s: 0.01,
                timed_out: false,
                launch_error: None,
            }
        });
        let mut cfg = CliConfig::new("offline-model", "never-executed", 1.0);
        cfg.runner = runner;
        cfg.version = Some("offline".into());
        cfg.max_retries = 0;
        let resp = CodexCliProvider::new(CodexAdapter, cfg)
            .complete(&make_request("x", "SYS"))
            .await;
        match expected_text {
            Some(exp) => {
                assert!(resp.ok(), "{label}: expected ok, got {:?}", resp.error);
                assert_eq!(resp.text.as_deref(), Some(exp), "{label}");
            }
            None => {
                assert!(!resp.ok(), "{label}: expected provider error");
                assert!(resp.text.is_none(), "{label}");
            }
        }
    }

    // 2. Claude, Cursor, Grok, Agy (normal + recovered step), and Muse: preserve untrimmed text
    //    and present empty/whitespace replies without raising a provider error.
    for sample in [trailing.as_str(), "   \n", ""] {
        // Claude
        let stdout = json!({"type": "result", "subtype": "success", "is_error": false, "result": sample, "usage": {}}).to_string();
        let mut cfg = CliConfig::new("claude-test", "never-executed", 1.0);
        cfg.runner = FakeRunner::single(move |_| ProcessResult {
            returncode: 0,
            stdout: stdout.clone(),
            stderr: String::new(),
            duration_s: 0.01,
            timed_out: false,
            launch_error: None,
        });
        cfg.version = Some("offline".into());
        cfg.max_retries = 0;
        let resp = ClaudeCliProvider::new(ClaudeAdapter, cfg)
            .complete(&make_request("x", "SYS"))
            .await;
        assert!(resp.ok(), "claude sample={sample:?}: {:?}", resp.error);
        assert_eq!(resp.text.as_deref(), Some(sample));

        // Cursor
        let stdout = json!({"type": "result", "subtype": "success", "is_error": false, "result": sample, "usage": {}}).to_string();
        let mut cfg = CliConfig::new("cursor-test", "never-executed", 1.0);
        cfg.runner = FakeRunner::single(move |_| ProcessResult {
            returncode: 0,
            stdout: stdout.clone(),
            stderr: String::new(),
            duration_s: 0.01,
            timed_out: false,
            launch_error: None,
        });
        cfg.version = Some("offline".into());
        cfg.max_retries = 0;
        let resp = CursorCliProvider::new(CursorAdapter, cfg)
            .complete(&make_request("x", "SYS"))
            .await;
        assert!(resp.ok(), "cursor sample={sample:?}: {:?}", resp.error);
        assert_eq!(resp.text.as_deref(), Some(sample));

        // Grok
        let stdout =
            json!({"type": "result", "is_error": false, "text": sample, "usage": {}}).to_string();
        let mut cfg = CliConfig::new("grok-test", "never-executed", 1.0);
        cfg.runner = FakeRunner::single(move |_| ProcessResult {
            returncode: 0,
            stdout: stdout.clone(),
            stderr: String::new(),
            duration_s: 0.01,
            timed_out: false,
            launch_error: None,
        });
        cfg.version = Some("offline".into());
        cfg.max_retries = 0;
        let resp = GrokCliProvider::new(GrokAdapter, cfg)
            .complete(&make_request("x", "SYS"))
            .await;
        assert!(resp.ok(), "grok sample={sample:?}: {:?}", resp.error);
        assert_eq!(resp.text.as_deref(), Some(sample));

        // Agy (normal result event)
        let stdout = format!(
            "{}\n",
            json!({"event": "result", "result": {"status": "SUCCESS", "response": sample, "usage": {}}})
        );
        let mut cfg = CliConfig::new("agy-test", "never-executed", 1.0);
        cfg.runner = FakeRunner::single(move |_| ProcessResult {
            returncode: 0,
            stdout: stdout.clone(),
            stderr: String::new(),
            duration_s: 0.01,
            timed_out: false,
            launch_error: None,
        });
        cfg.version = Some("offline".into());
        cfg.max_retries = 0;
        let resp = AgyCliProvider::new(AgyAdapter, cfg)
            .complete(&make_request("x", "SYS"))
            .await;
        assert!(resp.ok(), "agy sample={sample:?}: {:?}", resp.error);
        assert_eq!(resp.text.as_deref(), Some(sample));

        // Agy recovered completed step must also not trim
        let step_events = vec![
            json!({"event": "step_update", "step_update": {"step_index": 1, "state": "DONE", "step_type": "agent_response", "text_delta": sample}})
                .as_object()
                .unwrap()
                .clone(),
        ];
        assert_eq!(
            recover_completed_agent_text(&step_events).as_deref(),
            Some(sample)
        );

        // Muse
        let stdout = format!(
            "{}\n",
            json!({"payload_type": "run.terminal.completed", "payload": {"terminal": "completed", "text": sample}})
        );
        let stderr = format!(
            "REQUEST: {}\nSSE RESPONSE: data: {}\nSSE RESPONSE: data: {}\n",
            json!({"instructions": "SYS", "tools": []}),
            json!({"type": "response.completed", "response": {"model": "muse-test", "status": "completed", "usage": {"input_tokens": 1, "output_tokens": 1}}}),
            json!({"type": "response.subscription_usage", "subscription": {"used": 1}})
        );
        let mut cfg = CliConfig::new("muse-test", "never-executed", 1.0);
        cfg.runner = FakeRunner::single(move |_| ProcessResult {
            returncode: 0,
            stdout: stdout.clone(),
            stderr: stderr.clone(),
            duration_s: 0.01,
            timed_out: false,
            launch_error: None,
        });
        cfg.version = Some("offline".into());
        cfg.max_retries = 0;
        // A fake subscription login keeps this test independent of the machine's real Muse home.
        let muse_home = tempfile::tempdir().unwrap();
        fs::create_dir_all(muse_home.path().join(".config").join("muse")).unwrap();
        fs::write(
            muse_home.path().join(".config/muse/auth.json"),
            json!({
                "providers": {
                    "meta": {
                        "mechanism": "oauth",
                        "subs_active": true,
                        "api_base_url": "https://api.meta.ai/v1"
                    }
                }
            })
            .to_string(),
        )
        .unwrap();
        let resp = MuseCliProvider::new(
            MuseAdapter {
                real_home: Some(muse_home.path().to_path_buf()),
            },
            cfg,
        )
        .complete(&make_request("x", "SYS"))
        .await;
        assert!(resp.ok(), "muse sample={sample:?}: {:?}", resp.error);
        assert_eq!(resp.text.as_deref(), Some(sample));
    }
}

#[tokio::test]
async fn codex_records_structured_native_tool_events_and_ignores_assistant_prose() {
    let prose = r#"{"actions":[{"action":"commit_deployment","arguments":{"summary_notes":"command_execution file_change mcp_tool_call web_search native_tool_activity"}}]}"#;
    let runner = FakeRunner::single(move |_| {
        ProcessResult {
        returncode: 0,
        stdout: [
            json!({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": prose}}),
            json!({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ]
        .map(|e| e.to_string())
        .join("\n"),
        stderr: String::new(),
        duration_s: 0.01,
        timed_out: false,
        launch_error: None,
    }
    });
    let mut cfg = CliConfig::new("offline-model", "never-executed", 1.0);
    cfg.runner = runner;
    cfg.version = Some("offline".into());
    cfg.max_retries = 0;
    let resp = CodexCliProvider::new(CodexAdapter, cfg)
        .complete(&make_request("x", "SYS"))
        .await;
    assert!(resp.ok());
    assert!(
        resp.raw
            .as_ref()
            .and_then(|r| r.get(ib_providers::base::NATIVE_TOOL_ACTIVITY))
            .is_none(),
        "assistant prose must never set native_tool_activity"
    );

    // Structured event records both the flag and event kinds without storing commands.
    let runner_tool = FakeRunner::single(move |_| {
        ProcessResult {
        returncode: 0,
        stdout: [
            json!({"type": "item.started", "item": {"id": "t1", "type": "command_execution", "command": "secret_cmd"}}),
            json!({"type": "item.completed", "item": {"id": "t2", "type": "web_search", "query": "secret_q"}}),
            json!({"type": "item.completed", "item": {"id": "a", "type": "agent_message", "text": prose}}),
            json!({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ]
        .map(|e| e.to_string())
        .join("\n"),
        stderr: String::new(),
        duration_s: 0.01,
        timed_out: false,
        launch_error: None,
    }
    });
    let mut cfg2 = CliConfig::new("offline-model", "never-executed", 1.0);
    cfg2.runner = runner_tool;
    cfg2.version = Some("offline".into());
    cfg2.max_retries = 0;
    let resp2 = CodexCliProvider::new(CodexAdapter, cfg2)
        .complete(&make_request("x", "SYS"))
        .await;
    let raw = resp2.raw.as_ref().unwrap();
    assert_eq!(raw[ib_providers::base::NATIVE_TOOL_ACTIVITY], json!(true));
    assert_eq!(
        raw[ib_providers::base::NATIVE_TOOL_EVENTS],
        json!(["command_execution", "web_search"])
    );
    let raw_str = serde_json::to_string(raw).unwrap();
    assert!(!raw_str.contains("secret_cmd") && !raw_str.contains("secret_q"));
}
