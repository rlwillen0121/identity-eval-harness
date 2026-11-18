use std::collections::{BTreeMap, VecDeque};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use ib_providers::base::{Message, ModelRequest, Provider, ToolSpec};
use ib_providers::cli::agy::{
    AGENT_FILE, AgyAdapter, AgyCliProvider, agent_definition, served_model,
};
use ib_providers::cli::claude::{ClaudeAdapter, ClaudeCliProvider};
use ib_providers::cli::codex::{
    CONFIG_OVERRIDES, CodexAdapter, CodexCliProvider, DEFAULT_EFFORT as CODEX_DEFAULT_EFFORT,
    DISABLED_FEATURES,
};
use ib_providers::cli::cursor::{CursorAdapter, CursorCliProvider, chat_dir, cursor_config_dir};
use ib_providers::cli::grok::{GrokAdapter, GrokCliProvider};
use ib_providers::cli::muse::{MuseAdapter, MuseCliProvider, trace_events};
use ib_providers::cli::{ACTION_SCHEMA_NAME, CliConfig, NEUTRAL_SYSTEM};
use ib_providers::subprocess::{Command, ProcessResult, ProcessRunner};
use serde_json::{Map, Value, json};
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

fn decision_schema() -> Map<String, Value> {
    json!({
        "type": "object",
        "properties": {
            "decision": {"type": "string"},
            "rules": {"type": "array"}
        },
        "required": ["decision", "rules"]
    })
    .as_object()
    .unwrap()
    .clone()
}

fn make_request(prompt: &str, system: &str, schema: Option<Map<String, Value>>) -> ModelRequest {
    ModelRequest {
        system: system.to_string(),
        messages: vec![Message::user(prompt)],
        output_schema: schema,
        ..ModelRequest::default()
    }
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
async fn agy_cli_argv_agent_schema_withholding_and_served_model() {
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

    let resp = p
        .complete(&make_request(
            "Decide REQ-1.",
            "SYS",
            Some(decision_schema()),
        ))
        .await;
    assert!(resp.ok());
    assert_eq!(resp.served_model.as_deref(), Some("gemini-4-argon-medium"));
    assert_eq!(
        resp.parsed.as_ref().unwrap().get("decision"),
        Some(&json!("DENY"))
    );
    assert_eq!(resp.usage.input_tokens, 821);
    assert_eq!(resp.usage.output_tokens, 50);

    // Action protocol turn withholds --json-schema and sets tools: []
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
    let mut action_req = make_request("Decide", "", Some(decision_schema()));
    action_req.schema_name = ACTION_SCHEMA_NAME.to_string();
    let r2 = p2.complete(&action_req).await;
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
    assert_eq!(
        r2.raw.as_ref().unwrap()["json_schema"],
        json!("withheld (JSON action protocol turn)")
    );
    assert_eq!(served_model(log).as_deref(), Some("gemini-4-argon-medium"));
    assert!(agent_definition("SYS", true).contains("tools: [finish]"));
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

    let mut action_req = make_request("x", "SYS", Some(decision_schema()));
    action_req.schema_name = ACTION_SCHEMA_NAME.to_string();
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
        let response = provider.complete(&make_request("x", "SYS", None)).await;
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
    let response = provider.complete(&make_request("x", "SYS", None)).await;
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
async fn codex_cli_flags_usage_and_empty_completed_action_turn() {
    assert_eq!(DISABLED_FEATURES.len(), 29);
    assert_eq!(CONFIG_OVERRIDES.len(), 3);
    assert_eq!(CODEX_DEFAULT_EFFORT, "medium");

    let runner = FakeRunner::new(vec![
        Box::new(|cmd: &Command| {
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
        }),
        // Commit 05e0c79: turn.completed without turn.failed + rc=0 + ACTION_SCHEMA_NAME + empty output -> text = ""
        Box::new(|cmd: &Command| {
            assert!(!cmd.argv.contains(&"--output-schema".to_string()));
            let stdout = [
                json!({"type": "thread.started", "thread_id": "th_456"}),
                json!({"type": "turn.completed", "usage": {"input_tokens": 500, "cached_input_tokens": 0, "output_tokens": 10}}),
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
        }),
    ]);

    let mut cfg = CliConfig::new("gpt-test", "codex", 900.0);
    cfg.effort = Some(CODEX_DEFAULT_EFFORT.to_string());
    cfg.runner = Arc::clone(&runner) as Arc<dyn ProcessRunner>;
    cfg.version = Some("0.162.1".to_string());
    cfg.retry_base_delay_s = 0.0;
    let p = CodexCliProvider::new(CodexAdapter, cfg);

    let r1 = p
        .complete(&make_request("Decide", "SYS", Some(decision_schema())))
        .await;
    assert!(r1.ok());
    assert_eq!(r1.usage.input_tokens, 200);
    assert_eq!(r1.usage.cache_read_tokens, 800);
    assert_eq!(r1.usage.reasoning_tokens, 30);
    assert_eq!(r1.parsed.as_ref().unwrap()["decision"], json!("DENY"));

    let mut action_req = make_request("Decide", "SYS", Some(decision_schema()));
    action_req.schema_name = ACTION_SCHEMA_NAME.to_string();
    let r2 = p.complete(&action_req).await;
    assert!(r2.ok());
    assert!(r2.error.is_none());
    assert_eq!(r2.text.as_deref(), Some(""));
    assert!(r2.parsed.is_none());
    assert_eq!(r2.stop_reason.as_deref(), Some("completed"));
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
    let cr = claude_p
        .complete(&make_request("Decide.", "SYS", Some(decision_schema())))
        .await;
    assert!(cr.ok());
    assert_eq!(cr.usage.input_tokens, 10);
    assert_eq!(cr.usage.cache_read_tokens, 20);
    assert_eq!(cr.usage.cache_write_tokens, 30);
    assert_eq!(cr.usage.reasoning_tokens, 4);
    assert_eq!(cr.reported_cost_usd, Some(0.0012));
    assert_eq!(cr.served_model.as_deref(), Some("claude-haiku-5-5"));

    // Native tools rejected cleanly
    let mut bad_req = make_request("x", "y", None);
    bad_req.tools.push(ToolSpec {
        name: "t".to_string(),
        description: "d".to_string(),
        input_schema: Map::new(),
    });
    assert!(claude_p.complete(&bad_req).await.error.is_some());

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
    let mut g_req = make_request("hi", "- bullet system", Some(decision_schema()));
    g_req.schema_name = ACTION_SCHEMA_NAME.to_string();
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
    let cur_r = cur_p
        .complete(&make_request("- leading dash", "", None))
        .await;
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
    let mr = muse_p.complete(&make_request("hi", "SYS", None)).await;
    assert!(mr.ok(), "muse failed: {:?}", mr.error);
    assert_eq!(mr.usage.input_tokens, 60);
    assert_eq!(mr.usage.cache_read_tokens, 40);
    assert_eq!(mr.usage.reasoning_tokens, 5);
    assert_eq!(
        mr.raw.as_ref().unwrap()["subscription_usage"],
        json!({"used_percent": 2.5, "active": true})
    );
}
