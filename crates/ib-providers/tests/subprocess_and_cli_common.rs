use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Instant;
use tokio::sync::RwLock;

static LIVE_GROUP_RWLOCK: RwLock<()> = RwLock::const_new(());

use async_trait::async_trait;
use ib_providers::base::{Message, ModelRequest, ModelResponse, Usage};
use ib_providers::cli::claude::ClaudeAdapter;
use ib_providers::cli::{
    CallDirGuard, CliConfig, CliProvider, MAX_ARG_BYTES, NEUTRAL_SYSTEM, OWNER_FILE, SYSTEM_BEGIN,
    base_child_env, link_credentials, merge_attempts, owner_alive, parse_json_payload, parse_jsonl,
    prepend_system, probe_version, render_transcript, scrub_stderr, served_models, structured_text,
    sweep_stale_call_dirs, sync_back, system_override, valid_effort,
};
use ib_providers::subprocess::{
    Command, ProcessResult, ProcessRunner, TRUNCATION_MARKER, is_transient_error, kill_all_live,
    live_groups, redact, redact_diagnostics, register_group, retry_transient, run_command,
    run_command_with_cap, tail, unregister_group, url_hostname,
};
use serde_json::{Map, json};
use tempfile::TempDir;

fn sh_cmd(script: &str, stdin: Option<&str>, timeout_s: f64, cwd: PathBuf) -> Command {
    let mut env = BTreeMap::new();
    if let Ok(p) = std::env::var("PATH") {
        env.insert("PATH".to_string(), p);
    }
    env.insert("MARKER".to_string(), "visible".to_string());
    Command {
        argv: vec!["sh".to_string(), "-c".to_string(), script.to_string()],
        cwd,
        env,
        timeout_s,
        stdin: stdin.map(str::to_string),
    }
}

#[tokio::test]
async fn captures_stdout_stderr_exit_code_cwd_and_stdin() {
    let _guard = LIVE_GROUP_RWLOCK.read().await;
    let tmp = TempDir::new().unwrap();
    let cmd = sh_cmd(
        "read -r line; printf '%s\\n' \"$line\"; printf '%s\\n' \"$MARKER\" >&2; pwd; exit 3",
        Some("hello-stdin\n"),
        10.0,
        tmp.path().to_path_buf(),
    );
    let res = run_command(&cmd).await;
    assert!(
        res.launch_error.is_none(),
        "unexpected launch error: {res:?}"
    );
    assert_eq!(res.returncode, 3);
    let lines: Vec<&str> = res.stdout.lines().collect();
    assert_eq!(lines[0], "hello-stdin");
    assert_eq!(
        fs::canonicalize(lines[1]).unwrap(),
        fs::canonicalize(tmp.path()).unwrap()
    );
    assert_eq!(res.stderr.trim(), "visible");
    assert!(!res.timed_out);
    assert!(res.launch_error.is_none());
    assert!(res.duration_s > 0.0);
}

#[tokio::test]
async fn env_is_exactly_what_command_says_and_no_stdin_is_devnull() {
    let _guard = LIVE_GROUP_RWLOCK.read().await;
    let tmp = TempDir::new().unwrap();
    let cmd = sh_cmd(
        "printf 'M=%s H=%s\\n' \"${MARKER:-unset}\" \"${HOME:-unset}\"; cat",
        None,
        10.0,
        tmp.path().to_path_buf(),
    );
    let res = run_command(&cmd).await;
    assert_eq!(res.returncode, 0);
    assert_eq!(res.stdout.trim(), "M=visible H=unset");
}

#[tokio::test]
async fn timeout_kills_process_group_including_grandchild() {
    let _guard = LIVE_GROUP_RWLOCK.read().await;
    let tmp = TempDir::new().unwrap();
    let pid_file = tmp.path().join("grandchild.pid");
    let script = format!("sleep 30 & echo $! > '{}'; sleep 30", pid_file.display());
    let start = Instant::now();
    let res = run_command(&sh_cmd(&script, None, 0.5, tmp.path().to_path_buf())).await;
    assert!(res.timed_out);
    assert!(start.elapsed().as_secs_f64() < 10.0);

    let grandchild: i32 = fs::read_to_string(&pid_file)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    let mut dead = false;
    for _ in 0..50 {
        let alive = nix::sys::signal::kill(nix::unistd::Pid::from_raw(grandchild), None).is_ok();
        if !alive {
            dead = true;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(dead, "grandchild survived timeout kill");
}

#[tokio::test]
async fn timeout_preserves_captured_output_with_the_configured_cap() {
    let _guard = LIVE_GROUP_RWLOCK.read().await;
    let tmp = TempDir::new().unwrap();
    let stdout = "o".repeat(2048);
    let stderr = "e".repeat(2048);
    let script = format!("printf '%s' '{stdout}'; printf '%s' '{stderr}' >&2; sleep 30");
    let cap = 128;
    let res =
        run_command_with_cap(&sh_cmd(&script, None, 0.25, tmp.path().to_path_buf()), cap).await;

    assert!(res.timed_out);
    assert_eq!(
        res.stdout,
        format!("{}{TRUNCATION_MARKER}", "o".repeat(cap))
    );
    assert_eq!(
        res.stderr,
        format!("{}{TRUNCATION_MARKER}", "e".repeat(cap))
    );
}

#[tokio::test]
async fn timeout_kills_group_when_direct_child_exited_but_descendant_holds_pipes() {
    let _guard = LIVE_GROUP_RWLOCK.read().await;
    let tmp = TempDir::new().unwrap();
    let pid_file = tmp.path().join("grandchild.pid");
    let script = format!("sleep 30 & echo $! > '{}'; exit 0", pid_file.display());
    let res = run_command(&sh_cmd(&script, None, 0.25, tmp.path().to_path_buf())).await;
    assert!(res.timed_out);

    let grandchild: i32 = fs::read_to_string(&pid_file)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    let mut dead = false;
    for _ in 0..50 {
        let alive = nix::sys::signal::kill(nix::unistd::Pid::from_raw(grandchild), None).is_ok();
        if !alive {
            dead = true;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(
        dead,
        "grandchild survived timeout after direct child exited"
    );
}

#[tokio::test]
async fn malformed_and_unrepresentable_timeouts_are_rejected_before_launch() {
    let _guard = LIVE_GROUP_RWLOCK.read().await;
    let tmp = TempDir::new().unwrap();
    let marker = tmp.path().join("command-started");
    let script = format!("printf ran > '{}'", marker.display());
    for timeout_s in [f64::NAN, f64::INFINITY, f64::MAX] {
        let res = run_command(&sh_cmd(&script, None, timeout_s, tmp.path().to_path_buf())).await;
        assert_eq!(res.returncode, -1);
        assert!(
            res.launch_error
                .as_deref()
                .unwrap()
                .contains("invalid process timeout")
        );
        assert!(!res.timed_out);
        assert!(!marker.exists());
    }

    let negative_timeout =
        run_command(&sh_cmd("sleep 30", None, -1.0, tmp.path().to_path_buf())).await;
    assert!(negative_timeout.timed_out);
}

#[tokio::test]
async fn helpers_left_running_after_normal_exit_are_killed() {
    let _guard = LIVE_GROUP_RWLOCK.read().await;
    let tmp = TempDir::new().unwrap();
    let pid_file = tmp.path().join("helper.pid");
    let script = format!(
        "sleep 30 </dev/null >/dev/null 2>&1 & echo $! > '{}'",
        pid_file.display()
    );
    let res = run_command(&sh_cmd(&script, None, 10.0, tmp.path().to_path_buf())).await;
    assert_eq!(res.returncode, 0);
    assert!(!res.timed_out);

    let helper: i32 = fs::read_to_string(&pid_file)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    let mut dead = false;
    for _ in 0..50 {
        let alive = nix::sys::signal::kill(nix::unistd::Pid::from_raw(helper), None).is_ok();
        if !alive {
            dead = true;
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    assert!(dead, "helper outlived finished command");
}

#[tokio::test]
async fn missing_binary_is_launch_error_and_output_is_capped() {
    let _guard = LIVE_GROUP_RWLOCK.read().await;
    let tmp = TempDir::new().unwrap();
    let missing = Command {
        argv: vec!["/nonexistent/claude-binary".to_string()],
        cwd: tmp.path().to_path_buf(),
        env: BTreeMap::new(),
        timeout_s: 5.0,
        stdin: None,
    };
    let res = run_command(&missing).await;
    assert!(
        res.launch_error
            .as_deref()
            .unwrap()
            .contains("could not start")
    );
    assert_eq!(res.returncode, -1);

    // Output cap test
    let big_script = "awk 'BEGIN { for (i=0;i<5000;i++) printf \"o\"; for (i=0;i<5000;i++) printf \"e\" > \"/dev/stderr\" }'";
    let capped = run_command_with_cap(
        &sh_cmd(big_script, None, 10.0, tmp.path().to_path_buf()),
        1000,
    )
    .await;
    assert_eq!(capped.returncode, 0);
    assert_eq!(
        capped.stdout,
        format!("{}{TRUNCATION_MARKER}", "o".repeat(1000))
    );
    assert_eq!(
        capped.stderr,
        format!("{}{TRUNCATION_MARKER}", "e".repeat(1000))
    );

    // Flooding stdout before reading stdin must not deadlock
    let flood_script = "head -c 200000 /dev/zero | tr '\\0' 'x'; cat >/dev/null";
    let flood = run_command(&sh_cmd(
        flood_script,
        Some(&"s".repeat(200_000)),
        15.0,
        tmp.path().to_path_buf(),
    ))
    .await;
    assert_eq!(flood.returncode, 0);
    assert_eq!(flood.stdout.len(), 200_000);
}

#[test]
fn transient_errors_and_redaction_and_tail() {
    for t in [
        "API Error: 429 Too Many Requests",
        "rate_limit_error: slow down",
        "Overloaded",
        "HTTP 503 Service Unavailable",
        "Error: read ECONNRESET",
        "socket hang up",
    ] {
        assert!(is_transient_error(Some(t)), "expected transient: {t}");
    }
    for t in [
        "",
        "claude_cli: no result within the 600s timeout",
        "Unauthorized (401): missing or wrong API key. Authentication is temporarily unavailable",
        "authentication_error: invalid x-api-key (503 upstream)",
        "Not logged in · Please run /login",
    ] {
        assert!(!is_transient_error(Some(t)), "expected non-transient: {t}");
    }
    assert!(!is_transient_error(None));

    // Redaction
    let env = BTreeMap::from([
        ("MY_API_KEY".to_string(), "abcdef123456".to_string()),
        ("SHORT_TOKEN".to_string(), "abc".to_string()),
        ("PATH".to_string(), "/usr/bin:/bin".to_string()),
        (
            "SERVICE_ADDR".to_string(),
            "https://synthetic-gateway.invalid/v1".to_string(),
        ),
        ("AUTH_BLOB".to_string(), "synthetic-cred-7319".to_string()),
    ]);
    let text = "key=abcdef123456 short=abc path=/usr/bin:/bin sk-ant-api03-ABCDEFGHIJKLMNOP Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig url=https://user:pass@example.invalid/v1?api_key=s#f auth=synthetic-cred-7319 host=synthetic-gateway.invalid";
    let r = redact(text, Some(&env), &["AUTH_BLOB", "SERVICE_ADDR"]);
    assert!(!r.contains("abcdef123456"));
    assert!(!r.contains("sk-ant-api03-ABCDEFGHIJKLMNOP"));
    assert!(!r.contains("eyJhbGciOiJIUzI1NiJ9"));
    assert!(!r.contains("user:pass"));
    assert!(!r.contains("example.invalid"));
    assert!(!r.contains("synthetic-cred-7319"));
    assert!(!r.contains("synthetic-gateway.invalid"));
    assert!(r.contains("short=abc"));
    assert!(r.contains("/usr/bin:/bin"));

    let openrouter = "synthetic-openrouter-marker-7319";
    let openrouter_env = BTreeMap::from([("OPENROUTER".to_string(), openrouter.to_string())]);
    let openrouter_redacted = redact(
        &format!("credential={openrouter}"),
        Some(&openrouter_env),
        &[],
    );
    assert!(!openrouter_redacted.contains(openrouter));
    assert!(openrouter_redacted.contains("<redacted>"));
    let lowercase_openrouter_env =
        BTreeMap::from([("openrouter".to_string(), openrouter.to_string())]);
    assert!(
        !redact(
            &format!("credential={openrouter}"),
            Some(&lowercase_openrouter_env),
            &[],
        )
        .contains(openrouter)
    );

    // Mixed-case inline URL redaction
    let mixed_host = ["MixedCase-GW", "Internal", "Lan"].join(".");
    let mixed = redact(
        &format!(
            "Failed at https://{mixed_host}:8443/v1 on {}",
            mixed_host.to_uppercase()
        ),
        Some(&BTreeMap::new()),
        &[],
    );
    assert_eq!(mixed, "Failed at https://<redacted> on <redacted>");

    // Redact diagnostics preserves root assistant_content
    let payload = json!({
        "request": {"message": "failed for synthetic-cred-7319"},
        "diagnostic-synthetic-cred-7319": "safe",
        "metadata": {"assistant_content": [{"text": "diagnostic synthetic-cred-7319"}]},
        "assistant_content": [{"text": "preserve synthetic-cred-7319"}],
    });
    let red_diag = redact_diagnostics(&payload, Some(&env), &["AUTH_BLOB"]);
    assert!(
        !red_diag["request"]
            .to_string()
            .contains("synthetic-cred-7319")
    );
    assert!(
        !red_diag["metadata"]
            .to_string()
            .contains("synthetic-cred-7319")
    );
    assert_eq!(
        red_diag["assistant_content"],
        json!([{"text": "preserve synthetic-cred-7319"}])
    );

    assert_eq!(url_hostname("https://127.0.0.1:8000/v1"), None);
    assert_eq!(url_hostname("https://localhost:8000/v1"), None);
    assert_eq!(
        url_hostname("https://api.example.com/v1").as_deref(),
        Some("api.example.com")
    );

    assert_eq!(tail("short", 2000), "short");
    let long = format!("{}END", "x".repeat(50));
    assert_eq!(tail(&long, 10), format!("…{}", "x".repeat(7) + "END"));

    let _wguard = LIVE_GROUP_RWLOCK.blocking_write();
    register_group(999_991);
    assert!(live_groups().contains(&999_991));
    unregister_group(999_991);
    assert_eq!(kill_all_live(), 0);
}

#[test]
fn json_payload_parser_keeps_selection_semantics_with_many_candidates() {
    let mut stdout = format!("{}\n", json!({"type":"result","sequence":1}));
    for sequence in 0..2_048 {
        stdout.push_str(&json!({"type":"event","sequence":sequence}).to_string());
        stdout.push('\n');
    }
    stdout.push_str(&json!({"type":"result","sequence":2}).to_string());
    stdout.push('\n');
    stdout.push_str(&json!({"type":"event","sequence":3}).to_string());

    let parsed = parse_json_payload(&stdout).unwrap();
    assert_eq!(parsed["sequence"], json!(2));

    let fallback = parse_json_payload(
        "{\"type\":\"event\",\"sequence\":4}\n{\"type\":\"event\",\"sequence\":5}",
    )
    .unwrap();
    assert_eq!(fallback["sequence"], json!(5));
}

#[test]
fn subprocess_debug_output_omits_prompts_environment_and_captured_text() {
    let marker = "synthetic-debug-secret-marker";
    let command = Command {
        argv: vec!["cli".to_string(), marker.to_string()],
        cwd: PathBuf::from("/tmp/private-test"),
        env: BTreeMap::from([("API_TOKEN".to_string(), marker.to_string())]),
        timeout_s: 1.0,
        stdin: Some(marker.to_string()),
    };
    let command_debug = format!("{command:?}");
    assert!(!command_debug.contains(marker));
    assert!(!command_debug.contains("/tmp/private-test"));

    let result = ProcessResult {
        returncode: 1,
        stdout: marker.to_string(),
        stderr: marker.to_string(),
        duration_s: 0.0,
        timed_out: false,
        launch_error: Some(marker.to_string()),
    };
    assert!(!format!("{result:?}").contains(marker));
}

#[test]
fn sync_back_rejects_hard_linked_cli_sidecars() {
    use ib_providers::cli::{CredentialLink, sync_back};

    let tmp = TempDir::new().unwrap();
    let original = tmp.path().join("original.json");
    let unrelated = tmp.path().join("unrelated.json");
    let cli_sidecar = tmp.path().join("cli-sidecar.json");
    fs::write(&original, br#"{"value":"original"}"#).unwrap();
    fs::write(&unrelated, br#"{"value":"changed"}"#).unwrap();
    fs::hard_link(&unrelated, &cli_sidecar).unwrap();

    sync_back(&[CredentialLink {
        real: original.clone(),
        link: cli_sidecar,
        sync_back: true,
    }]);
    assert_eq!(fs::read(original).unwrap(), br#"{"value":"original"}"#);
}

#[cfg(unix)]
#[test]
fn owner_marker_reader_accepts_only_bounded_single_link_files() {
    let tmp = TempDir::new().unwrap();
    let owner = tmp.path().join(OWNER_FILE);
    let target = tmp.path().join("target");
    fs::write(&target, std::process::id().to_string()).unwrap();

    std::os::unix::fs::symlink(&target, &owner).unwrap();
    assert_eq!(owner_alive(tmp.path()), None);
    fs::remove_file(&owner).unwrap();

    fs::hard_link(&target, &owner).unwrap();
    assert_eq!(owner_alive(tmp.path()), None);
    fs::remove_file(&owner).unwrap();

    fs::write(&owner, std::process::id().to_string()).unwrap();
    assert_eq!(owner_alive(tmp.path()), Some(true));
    fs::write(&owner, "1".repeat(33)).unwrap();
    assert_eq!(owner_alive(tmp.path()), None);
}

#[tokio::test]
async fn retry_transient_retries_and_gives_up() {
    let calls = Arc::new(Mutex::new(0u32));
    let c2 = Arc::clone(&calls);
    let res = retry_transient(
        move || {
            let c = Arc::clone(&c2);
            async move {
                let mut g = c.lock().unwrap();
                *g += 1;
                if *g < 3 { "429 rate limit" } else { "ok" }
            }
        },
        |r| *r != "ok",
        3,
        0.0,
    )
    .await;
    assert_eq!(res, "ok");
    assert_eq!(*calls.lock().unwrap(), 3);
}

#[test]
fn cli_common_pure_helpers_and_credentials_and_call_dir() {
    assert_eq!(
        render_transcript(&[Message::user("exact  text\n")]),
        "exact  text\n"
    );
    let rendered = render_transcript(&[
        Message::user("Decide REQ-1."),
        Message::assistant("Looking it up."),
        Message::user("Now decide."),
    ]);
    assert_eq!(
        rendered,
        "[User]\nDecide REQ-1.\n\n[Assistant]\nLooking it up.\n\n[User]\nNow decide."
    );

    assert_eq!(prepend_system("", "body"), "body");
    // Canonical SPEC-v5.2 §7.1 system block: `=== SYSTEM ===\n<system_prompt>\n` then the body.
    assert_eq!(
        prepend_system("rules\n\n", "=== ENGAGEMENT ===\nbody"),
        format!("{SYSTEM_BEGIN}\nrules\n=== ENGAGEMENT ===\nbody")
    );
    assert_eq!(SYSTEM_BEGIN, "=== SYSTEM ===");
    // No pointer swap: an over-long system prompt is passed through unchanged.
    let big = "x".repeat(MAX_ARG_BYTES + 1);
    let (system, body, _) = system_override(&big, "body", "");
    assert_eq!((system.as_str(), body.as_str()), (big.as_str(), "body"));
    assert_eq!(system_override(" \n\t", "body", "").0, NEUTRAL_SYSTEM);
    assert!(valid_effort("xhigh"));
    assert!(!valid_effort("-bad"));

    let lines = "{\"type\": \"system\"}\n{\"type\": \"result\", \"n\": 1}\n{\"type\": \"event\"}\n";
    assert_eq!(parse_json_payload(lines).unwrap()["n"], json!(1));
    assert_eq!(
        parse_jsonl("{\"type\":\"a\"}\nnoise\n[1]\n{\"type\":\"b\"}\n").len(),
        2
    );
    assert_eq!(
        served_models(Some(&json!({"b": {}, "a": {}}))).as_deref(),
        Some("a,b")
    );
    let mut s_map = Map::new();
    s_map.insert("a".to_string(), json!(1));
    assert_eq!(
        structured_text(Some(&json!("{\"a\":1}")), Some(&s_map)),
        (Some("{\"a\":1}".to_string()), None)
    );
    assert_eq!(
        structured_text(Some(&json!("Here you go")), Some(&s_map)),
        (
            Some("{\"a\": 1}".to_string()),
            Some("Here you go".to_string())
        )
    );

    // scrub_stderr removes prompt echoes
    let req = ModelRequest::new(
        "Secret policy line number one that is long enough",
        vec![Message::user(
            "User prompt line that must not appear in errors",
        )],
    );
    let stderr = "Secret policy line number one that is long enough\nUser prompt line that must not appear in errors\nactual CLI failure";
    assert_eq!(scrub_stderr(stderr, Some(&req), 300), "actual CLI failure");

    // link_credentials + sync_back
    let tmp = TempDir::new().unwrap();
    let real = tmp.path().join("real");
    let priv_dir = tmp.path().join("priv");
    fs::create_dir_all(&real).unwrap();
    fs::write(real.join("auth.json"), r#"{"token": "old"}"#).unwrap();
    fs::write(real.join("auth.json.lock"), "").unwrap();
    let links = link_credentials(
        &real,
        &priv_dir,
        &["auth.json", "missing.json"],
        &["auth.json.lock"],
    )
    .unwrap();
    assert_eq!(links.len(), 2);
    // Replace symlink atomically as a CLI refreshing OAuth would
    let staged = priv_dir.join("auth.json.tmp");
    fs::write(&staged, r#"{"token": "new"}"#).unwrap();
    fs::rename(&staged, priv_dir.join("auth.json")).unwrap();
    sync_back(&links);
    assert_eq!(
        fs::read_to_string(real.join("auth.json")).unwrap(),
        r#"{"token": "new"}"#
    );
    assert_eq!(
        fs::metadata(real.join("auth.json"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );

    // call_dir + owner_alive + sweep
    {
        let dir = CallDirGuard::new("identitybench-claude_cli-").unwrap();
        assert!(dir.path().join(OWNER_FILE).is_file());
        assert_eq!(
            fs::metadata(dir.path()).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(dir.path().join(OWNER_FILE))
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
        assert_eq!(owner_alive(dir.path()), Some(true));
        let _ = sweep_stale_call_dirs(0.0);
        assert!(dir.path().is_dir(), "sweeper removed a live call directory");
    }

    // base_child_env + keeps
    let base = BTreeMap::from([
        ("PATH".to_string(), "/usr/bin".to_string()),
        ("OPENAI_API_KEY".to_string(), "secret".to_string()),
        ("HTTPS_PROXY".to_string(), "http://proxy".to_string()),
        ("NODE_OPTIONS".to_string(), "--inspect".to_string()),
        ("LC_ALL".to_string(), "C".to_string()),
    ]);
    let child = base_child_env(Some(&base));
    assert_eq!(
        child.keys().cloned().collect::<BTreeSet<_>>(),
        BTreeSet::from(["LC_ALL".to_string(), "PATH".to_string()])
    );

    // merge_attempts
    let mut final_resp = ModelResponse {
        usage: Usage {
            input_tokens: 10,
            output_tokens: 5,
            ..Usage::default()
        },
        reported_cost_usd: Some(0.2),
        ..ModelResponse::default()
    };
    let earlier = ModelResponse {
        usage: Usage {
            input_tokens: 4,
            output_tokens: 1,
            ..Usage::default()
        },
        reported_cost_usd: Some(0.1),
        error: Some("429".to_string()),
        ..ModelResponse::default()
    };
    let snapshot = final_resp.clone();
    merge_attempts(&mut final_resp, &[earlier, snapshot], 1.2345);
    assert_eq!(final_resp.usage.input_tokens, 14);
    assert!((final_resp.reported_cost_usd.unwrap() - 0.3).abs() < 1e-12);
    assert_eq!(final_resp.raw.as_ref().unwrap()["attempts"], json!(2));

    // probe_version missing binary
    assert_eq!(
        probe_version("/definitely/missing/binary-xyz", Some(&BTreeMap::new())),
        "not-installed"
    );

    // setup_revision deterministic
    let p1 = CliProvider::new(ClaudeAdapter, CliConfig::new("m1", "claude", 600.0));
    let p2 = CliProvider::new(ClaudeAdapter, CliConfig::new("m2", "claude", 600.0));
    assert_eq!(p1.setup_revision(), p2.setup_revision());
    assert_eq!(
        p1.config().max_retries,
        0,
        "SPEC-v5.2 §7.4: no adapter retries"
    );

    // Mock Runner integration
    struct DummyRunner;
    #[async_trait]
    impl ProcessRunner for DummyRunner {
        async fn run(&self, _command: &Command) -> ProcessResult {
            ProcessResult {
                returncode: 0,
                stdout: r#"{"type":"result","subtype":"success","is_error":false,"result":"ok","usage":{"input_tokens":1,"output_tokens":1}}"#.to_string(),
                stderr: String::new(),
                duration_s: 0.01,
                timed_out: false,
                launch_error: None,
            }
        }
    }
    let _ = Arc::new(DummyRunner);
}
