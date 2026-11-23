//! Shared machinery for the subscription-CLI providers (`claude`, `codex`, `grok`, `agy`, `cursor`, `muse`).

pub mod agy;
pub mod claude;
pub mod codex;
pub mod cursor;
pub mod grok;
pub mod muse;

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs::{self, OpenOptions};
use std::io::{ErrorKind, Read};
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock, Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime};

use async_trait::async_trait;
use ib_core::pyjson::{DumpOptions, dumps};
use nix::sys::signal::kill;
use nix::unistd::Pid;
use regex::Regex;
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use crate::base::{
    Message, ModelRequest, ModelResponse, Provider, ProviderCapabilities, Role, SystemChannel,
    ToolCall, hash_endpoint,
};
use crate::json::{
    bounded_token_count, load_object, safe_nonnegative_float_add, sanitize_response,
};
use crate::subprocess::{
    Command, DEFAULT_RETRY_BASE_DELAY_S, MAX_CAPTURE_BYTES, ProcessResult, ProcessRunner,
    SystemProcessRunner, is_transient_error, redact, retry_transient, run_command_with_cap, tail,
};

/// Header of the optional canonical system block (`SPEC-v5.2` §7.1 transcript grammar).
pub const SYSTEM_BEGIN: &str = "=== SYSTEM ===";
pub const MAX_ARG_BYTES: usize = 100_000;
pub const MAX_SIDECAR_BYTES: usize = MAX_CAPTURE_BYTES;
pub const NEUTRAL_SYSTEM: &str = "You are a helpful assistant.";
pub const ACTION_SCHEMA_NAME: &str = "tool_action";

pub const STDERR_EXCERPT_CHARS: usize = 300;
const MARK_CHARS: usize = 60;
const MIN_MARK_CHARS: usize = 16;
const MAX_MARKS: usize = 400;
const WINDOW_CHARS: usize = 24;

pub const OWNER_FILE: &str = ".owner";
pub const STALE_CALL_DIR_AGE_S: f64 = 2.0 * 60.0 * 60.0;

static EFFORT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$").expect("valid effort regex"));
static CALL_DIR_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^identitybench-(?:[a-z0-9]+_cli-(?:setup-)?|probe-)[A-Za-z0-9_.-]+$")
        .expect("valid call dir regex")
});

pub const COMMON_ENV_KEEP: &[&str] = &[
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "TERM",
    "COLORTERM",
    "USER",
    "LOGNAME",
    "SHELL",
    "TZ",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
];
pub const COMMON_ENV_KEEP_PREFIXES: &[&str] = &["LC_"];

pub const COMMON_ENV_DROP: &[&str] = &[
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CURSOR_API_KEY",
    "MINIMAX_API_KEY",
    "MOONSHOT_API_KEY",
    "KIMI_API_KEY",
    "HF_TOKEN",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "XAI_API_KEY",
    "OPENROUTER_API_KEY",
    "LOCAL_LLM_API_KEY",
    "OPENCODE_GO_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PYTHONPATH",
    "PYTHONSTARTUP",
];
pub const COMMON_ENV_DROP_PREFIXES: &[&str] =
    &["NODE_", "SSL_CERT_", "GIT_", "PYTHON", "DYLD_", "LD_"];
pub const COMMON_ENV_DROP_SUFFIXES: &[&str] = &["_PROXY"];
pub const HOME_RELATIVE_VARS: &[&str] = &["XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"];

/// Read a CLI-produced sidecar without allowing it to bypass the subprocess capture limit.
///
/// Sidecars are generated in a private per-call directory, but they are still controlled by the
/// child process. Require a single-link regular file, open it without following links or blocking
/// on special files, and enforce the same 16 MiB limit used for captured stdout and stderr.
pub fn read_sidecar_text(path: &Path) -> std::io::Result<String> {
    let before = fs::symlink_metadata(path)?;
    validate_sidecar_metadata(&before)?;

    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt as _;
        options.custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_NONBLOCK);
    }
    let mut file = options.open(path)?;
    let opened = file.metadata()?;
    validate_sidecar_metadata(&opened)?;

    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt as _;
        if before.dev() != opened.dev() || before.ino() != opened.ino() {
            return Err(std::io::Error::other(
                "CLI sidecar changed while it was opened",
            ));
        }
    }

    let mut bytes = Vec::with_capacity(
        usize::try_from(opened.len())
            .unwrap_or(MAX_SIDECAR_BYTES)
            .min(MAX_SIDECAR_BYTES),
    );
    file.by_ref()
        .take(MAX_SIDECAR_BYTES as u64 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > MAX_SIDECAR_BYTES {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "CLI sidecar exceeds the 16 MiB limit",
        ));
    }
    validate_sidecar_metadata(&file.metadata()?)?;
    String::from_utf8(bytes).map_err(|error| std::io::Error::new(ErrorKind::InvalidData, error))
}

fn validate_sidecar_metadata(metadata: &fs::Metadata) -> std::io::Result<()> {
    if !metadata.file_type().is_file() {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "CLI sidecar is not a regular file",
        ));
    }
    if metadata.len() > MAX_SIDECAR_BYTES as u64 {
        return Err(std::io::Error::new(
            ErrorKind::InvalidData,
            "CLI sidecar exceeds the 16 MiB limit",
        ));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt as _;
        if metadata.nlink() != 1 {
            return Err(std::io::Error::new(
                ErrorKind::InvalidData,
                "CLI sidecar must have exactly one link",
            ));
        }
    }
    Ok(())
}

const ALLOWED_SYNC_BACK_KEYS: &[&str] = &[
    "claudeAiOauth",
    "oauthAccount",
    "tokens",
    "access_token",
    "refresh_token",
    "expires_at",
    "id_token",
    "last_refresh",
];

// --------------------------------------------------------------------------- pure helpers

/// Prefix `body` with the canonical `SPEC-v5.2` §7.1 system block
/// (`=== SYSTEM ===\n<system_prompt>\n`) for transports without a system channel.
/// The system prompt is inserted after removing its trailing LF bytes.
#[must_use]
pub fn prepend_system(system: &str, body: &str) -> String {
    if system.is_empty() {
        body.to_string()
    } else {
        format!("{SYSTEM_BEGIN}\n{}\n{body}", system.trim_end_matches('\n'))
    }
}

#[must_use]
pub fn exceeds_arg_limit(text: &str) -> bool {
    text.len() > MAX_ARG_BYTES
}

#[must_use]
pub fn system_override(
    system: &str,
    body: &str,
    prefix: &str,
) -> (String, String, Map<String, Value>) {
    if system.trim().is_empty() {
        let mut notes = Map::new();
        notes.insert(
            "substituted_system".to_string(),
            Value::String(NEUTRAL_SYSTEM.to_string()),
        );
        return (NEUTRAL_SYSTEM.to_string(), body.to_string(), notes);
    }
    // No pointer swap (SPEC-v5.2 §7.1): an over-long system argument is passed through
    // unchanged, and the argv limit check fails the call as a provider error instead of
    // rewriting what the model receives.
    let _ = prefix;
    (system.to_string(), body.to_string(), Map::new())
}

#[must_use]
pub fn render_transcript(messages: &[Message]) -> String {
    if messages.len() == 1 && messages[0].role == Role::User {
        return messages[0].content.clone();
    }
    let dump_opts = DumpOptions {
        sort_keys: true,
        ensure_ascii: false,
        ..DumpOptions::default()
    };
    let mut parts = Vec::with_capacity(messages.len());
    for message in messages {
        match message.role {
            Role::User => parts.push(format!("[User]\n{}", message.content)),
            Role::Assistant => {
                let mut lines = Vec::new();
                if !message.content.is_empty() {
                    lines.push(message.content.clone());
                }
                for call in &message.tool_calls {
                    let args_json = dumps(&Value::Object(call.arguments.clone()), &dump_opts)
                        .unwrap_or_else(|_| "{}".to_string());
                    lines.push(format!(
                        "(tool call {} id={}: {args_json})",
                        call.name, call.id
                    ));
                }
                parts.push(format!("[Assistant]\n{}", lines.join("\n")));
            }
            Role::Tool => {
                let call_id = message.tool_call_id.as_deref().unwrap_or("");
                parts.push(format!("[Tool result {call_id}]\n{}", message.content));
            }
        }
    }
    parts.join("\n\n")
}

#[must_use]
pub fn valid_effort(effort: &str) -> bool {
    EFFORT_RE.is_match(effort)
}

#[must_use]
pub fn parse_json_payload(stdout: &str) -> Option<Map<String, Value>> {
    let text = stdout.trim();
    if text.is_empty() {
        return None;
    }
    if let Some(whole) = load_object(text) {
        return Some(whole);
    }
    let mut candidates = Vec::new();
    for raw_line in text.lines() {
        let line = raw_line.trim();
        if line.starts_with('{')
            && let Some(val) = load_object(line)
        {
            candidates.push(val);
        }
    }
    if candidates.is_empty() {
        return None;
    }
    if let Some(result_obj) = candidates
        .iter()
        .rev()
        .find(|m| m.get("type").and_then(Value::as_str) == Some("result"))
    {
        return Some(result_obj.clone());
    }
    candidates.pop()
}

#[must_use]
pub fn parse_jsonl(stdout: &str) -> Vec<Map<String, Value>> {
    let mut events = Vec::new();
    for raw_line in stdout.lines() {
        let line = raw_line.trim();
        if line.starts_with('{')
            && let Some(val) = load_object(line)
        {
            events.push(val);
        }
    }
    events
}

#[must_use]
pub fn as_int(value: Option<&Value>) -> u64 {
    bounded_token_count(value)
}

#[must_use]
pub fn as_float(value: Option<&Value>) -> Option<f64> {
    let val = value?;
    if let Value::Number(n) = val {
        let num = n.as_f64()?;
        if num.is_finite() {
            return Some(num);
        }
    }
    None
}

#[must_use]
pub fn as_dict(value: Option<&Value>) -> Map<String, Value> {
    match value {
        Some(Value::Object(map)) => map.clone(),
        _ => Map::new(),
    }
}

#[must_use]
pub fn served_models(model_usage: Option<&Value>) -> Option<String> {
    let map = model_usage?.as_object()?;
    if map.is_empty() {
        return None;
    }
    let mut keys: Vec<&str> = map.keys().map(String::as_str).collect();
    keys.sort_unstable();
    Some(keys.join(","))
}

#[must_use]
pub fn structured_text(
    text: Option<&Value>,
    structured: Option<&Map<String, Value>>,
) -> (Option<String>, Option<String>) {
    let raw_str = text.and_then(Value::as_str);
    let cli_text = raw_str.filter(|s| !s.trim().is_empty()).map(str::to_string);
    let Some(structured_map) = structured else {
        return (raw_str.map(str::to_string), None);
    };
    if let Some(ref ct) = cli_text
        && load_object(ct).as_ref() == Some(structured_map)
    {
        return (Some(ct.clone()), None);
    }
    let serialized = dumps(
        &Value::Object(structured_map.clone()),
        &DumpOptions {
            ensure_ascii: false,
            ..DumpOptions::default()
        },
    )
    .unwrap_or_else(|_| "{}".to_string());
    (Some(serialized), cli_text)
}

// --------------------------------------------------------------------------- env filtering & probe

pub type EnvItems = Vec<(String, String)>;

#[must_use]
pub fn env_items(env: &BTreeMap<String, String>) -> EnvItems {
    env.iter().map(|(k, v)| (k.clone(), v.clone())).collect()
}

#[must_use]
pub fn always_dropped(name: &str) -> bool {
    let upper = name.to_uppercase();
    COMMON_ENV_DROP.contains(&name)
        || COMMON_ENV_DROP_PREFIXES.iter().any(|p| name.starts_with(p))
        || COMMON_ENV_DROP_SUFFIXES.iter().any(|s| upper.ends_with(s))
}

#[must_use]
pub fn base_child_env(base: Option<&BTreeMap<String, String>>) -> BTreeMap<String, String> {
    let owned: BTreeMap<String, String>;
    let source = match base {
        Some(m) => m,
        None => {
            owned = std::env::vars().collect();
            &owned
        }
    };
    source
        .iter()
        .filter(|(name, _)| {
            (COMMON_ENV_KEEP.contains(&name.as_str())
                || COMMON_ENV_KEEP_PREFIXES.iter().any(|p| name.starts_with(p)))
                && !always_dropped(name)
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect()
}

static VERSION_CACHE: OnceLock<Mutex<HashMap<(String, EnvItems), String>>> = OnceLock::new();
const VERSION_PROBE_TIMEOUT: Duration = Duration::from_secs(30);
const VERSION_PROBE_CAPTURE_BYTES: usize = 64 * 1024;

/// `<binary> --version` (first non-empty line), cached per binary and environment.
#[must_use]
pub fn probe_version(binary: &str, env: Option<&BTreeMap<String, String>>) -> String {
    let resolved_env = match env {
        Some(e) => e.clone(),
        None => base_child_env(None),
    };
    let cache_key = (binary.to_string(), env_items(&resolved_env));
    let cache = VERSION_CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    if let Ok(guard) = cache.lock()
        && let Some(cached) = guard.get(&cache_key)
    {
        return cached.clone();
    }

    let version = probe_version_uncached(binary, &resolved_env);
    if let Ok(mut guard) = cache.lock() {
        guard.insert(cache_key, version.clone());
    }
    version
}

fn probe_version_uncached(binary: &str, env: &BTreeMap<String, String>) -> String {
    probe_version_uncached_with_limits(
        binary,
        env,
        VERSION_PROBE_TIMEOUT,
        VERSION_PROBE_CAPTURE_BYTES,
    )
}

fn probe_version_uncached_with_limits(
    binary: &str,
    env: &BTreeMap<String, String>,
    timeout: Duration,
    max_capture_bytes: usize,
) -> String {
    let Ok(dir) = CallDirGuard::new("identitybench-probe-") else {
        return "unknown".to_string();
    };
    let command = Command {
        argv: vec![binary.to_string(), "--version".to_string()],
        cwd: dir.path().to_path_buf(),
        env: env.clone(),
        timeout_s: timeout.as_secs_f64(),
        stdin: None,
    };
    let Ok(worker) = std::thread::Builder::new()
        .name("identitybench-version-probe".to_string())
        .spawn(move || {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .ok()?;
            Some(runtime.block_on(run_command_with_cap(&command, max_capture_bytes)))
        })
    else {
        return "unknown".to_string();
    };
    let Ok(Some(result)) = worker.join() else {
        return "unknown".to_string();
    };
    if let Some(error) = result.launch_error {
        return if error.contains("No such file or directory") {
            "not-installed".to_string()
        } else {
            "unknown".to_string()
        };
    }
    // A direct child can exit successfully while a helper keeps its inherited pipes open. The
    // supervised runner then reaches its deadline to reap the process group, but preserves the
    // direct child's zero status and bounded output. Accept that completed probe result.
    if result.returncode != 0 {
        return "unknown".to_string();
    }
    for text in [result.stdout, result.stderr] {
        for line in text.lines() {
            let trimmed = line.trim();
            if !trimmed.is_empty() {
                return trimmed.to_string();
            }
        }
    }
    "unknown".to_string()
}

#[cfg(test)]
mod version_probe_tests {
    use super::*;

    #[cfg(unix)]
    #[test]
    fn version_probe_bounds_output_and_reaps_a_pipe_holding_descendant() {
        use std::os::unix::fs::PermissionsExt as _;

        let temp = tempfile::tempdir().expect("tempdir");
        let binary = temp.path().join("mock-version.sh");
        fs::write(
            &binary,
            concat!(
                "#!/bin/sh\n",
                "[ \"$1\" = \"--version\" ] || exit 2\n",
                "[ \"$PROBE_MARKER\" = \"visible\" ] || exit 3\n",
                "printf 'mock-version 1.2.3\\n'\n",
                "head -c 131072 /dev/zero | tr '\\0' 'x'\n",
                "sleep 30 &\n",
                "exit 0\n",
            ),
        )
        .expect("write mock version binary");
        fs::set_permissions(&binary, fs::Permissions::from_mode(0o755))
            .expect("make mock executable");
        let env = BTreeMap::from([
            ("PATH".to_string(), "/usr/bin:/bin".to_string()),
            ("PROBE_MARKER".to_string(), "visible".to_string()),
        ]);
        let started = Instant::now();
        let version = probe_version_uncached_with_limits(
            &binary.to_string_lossy(),
            &env,
            Duration::from_millis(250),
            64,
        );

        assert_eq!(version, "mock-version 1.2.3");
        assert!(started.elapsed() < Duration::from_secs(5));

        let failed_binary = temp.path().join("failed-version.sh");
        fs::write(&failed_binary, "#!/bin/sh\nexit 7\n").expect("write failing mock binary");
        fs::set_permissions(&failed_binary, fs::Permissions::from_mode(0o755))
            .expect("make failing mock executable");
        assert_eq!(
            probe_version_uncached_with_limits(
                &failed_binary.to_string_lossy(),
                &env,
                Duration::from_secs(1),
                64,
            ),
            "unknown"
        );
    }

    #[test]
    fn version_probe_keeps_not_installed_fallback_for_missing_binary() {
        let env = BTreeMap::new();
        assert_eq!(
            probe_version_uncached_with_limits(
                "/definitely/missing/identitybench-provider",
                &env,
                Duration::from_millis(100),
                64,
            ),
            "not-installed"
        );
    }
}

// --------------------------------------------------------------------------- stderr excerpts

fn normalize_ws(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn unescape_ws(line: &str) -> String {
    line.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
        .replace("\\t", " ")
}

fn windows_set(text: &str) -> HashSet<String> {
    let chars: Vec<char> = text.chars().collect();
    if chars.len() < WINDOW_CHARS {
        return HashSet::new();
    }
    (0..=chars.len() - WINDOW_CHARS)
        .map(|i| chars[i..i + WINDOW_CHARS].iter().collect())
        .collect()
}

#[must_use]
pub fn scrub_stderr(stderr: &str, request: Option<&ModelRequest>, limit: usize) -> String {
    if stderr.trim().is_empty() {
        return String::new();
    }
    let Some(req) = request else {
        return tail(stderr, limit);
    };

    let mut texts: Vec<&str> = Vec::with_capacity(1 + req.messages.len());
    texts.push(&req.system);
    for m in &req.messages {
        texts.push(&m.content);
    }

    let mut verbatim = HashSet::new();
    let mut marks = Vec::new();
    for text in &texts {
        for line in text.lines().map(str::trim) {
            if line.chars().count() < MIN_MARK_CHARS {
                continue;
            }
            verbatim.insert(line.to_string());
            if marks.len() < MAX_MARKS {
                marks.push(line.chars().take(MARK_CHARS).collect::<String>());
            }
        }
    }
    let joined = texts.join("\n");
    let windows = windows_set(&normalize_ws(&joined));

    let echoes_prompt = |raw: &str| -> bool {
        let line = unescape_ws(raw);
        if line.lines().any(|part| verbatim.contains(part.trim())) {
            return true;
        }
        if marks.iter().any(|mark| line.contains(mark)) {
            return true;
        }
        let norm = normalize_ws(&line);
        let norm_chars: Vec<char> = norm.chars().collect();
        if norm_chars.len() < WINDOW_CHARS {
            return false;
        }
        (0..=norm_chars.len() - WINDOW_CHARS).any(|i| {
            let sub: String = norm_chars[i..i + WINDOW_CHARS].iter().collect();
            windows.contains(&sub)
        })
    };

    let kept: Vec<&str> = stderr.lines().filter(|line| !echoes_prompt(line)).collect();
    tail(&kept.join("\n"), limit)
}

// --------------------------------------------------------------------------- call directories

static LIVE_CALL_DIRS: LazyLock<Mutex<BTreeSet<PathBuf>>> =
    LazyLock::new(|| Mutex::new(BTreeSet::new()));

pub fn register_call_dir(path: &Path) {
    if let Ok(mut guard) = LIVE_CALL_DIRS.lock() {
        guard.insert(path.to_path_buf());
    }
}

pub fn unregister_call_dir(path: &Path) {
    if let Ok(mut guard) = LIVE_CALL_DIRS.lock() {
        guard.remove(path);
    }
}

#[must_use]
pub fn live_call_dirs() -> BTreeSet<PathBuf> {
    LIVE_CALL_DIRS.lock().map(|g| g.clone()).unwrap_or_default()
}

pub fn remove_live_call_dirs() -> usize {
    let paths: Vec<PathBuf> = match LIVE_CALL_DIRS.lock() {
        Ok(mut guard) => {
            let items: Vec<PathBuf> = guard.iter().cloned().collect();
            guard.clear();
            items
        }
        Err(_) => return 0,
    };
    let mut removed = 0;
    for path in paths {
        let _ = fs::remove_dir_all(&path);
        if fs::symlink_metadata(&path).is_err() {
            removed += 1;
        }
    }
    removed
}

#[must_use]
pub fn owner_alive(path: &Path) -> Option<bool> {
    let content = fs::read_to_string(path.join(OWNER_FILE)).ok()?;
    let pid: i32 = content.trim().parse().ok()?;
    if pid <= 0 {
        return None;
    }
    #[cfg(unix)]
    {
        match kill(Pid::from_raw(pid), None) {
            Ok(()) | Err(nix::errno::Errno::EPERM) => Some(true),
            Err(nix::errno::Errno::ESRCH) => Some(false),
            Err(_) => Some(true),
        }
    }
    #[cfg(not(unix))]
    {
        let _ = pid;
        None
    }
}

pub struct CallDirGuard {
    raw_path: PathBuf,
    resolved_path: PathBuf,
}

impl CallDirGuard {
    pub fn new(prefix: &str) -> std::io::Result<Self> {
        let dir = tempfile::Builder::new().prefix(prefix).tempdir()?;
        let raw_path = dir.keep();
        let _ = fs::write(raw_path.join(OWNER_FILE), std::process::id().to_string());
        register_call_dir(&raw_path);
        let resolved_path = fs::canonicalize(&raw_path).unwrap_or_else(|_| raw_path.clone());
        Ok(Self {
            raw_path,
            resolved_path,
        })
    }

    #[must_use]
    pub fn path(&self) -> &Path {
        &self.resolved_path
    }
}

impl Drop for CallDirGuard {
    fn drop(&mut self) {
        unregister_call_dir(&self.raw_path);
        let _ = fs::remove_dir_all(&self.raw_path);
    }
}

pub fn sweep_stale_call_dirs(max_age_s: f64) -> usize {
    let root = std::env::temp_dir();
    let cutoff = SystemTime::now()
        .checked_sub(Duration::from_secs_f64(max_age_s.max(0.0)))
        .unwrap_or(SystemTime::UNIX_EPOCH);
    let live = live_call_dirs();
    let Ok(entries) = fs::read_dir(&root) else {
        return 0;
    };

    #[cfg(unix)]
    let current_uid = nix::unistd::getuid().as_raw();

    let mut removed = 0usize;
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name_str) = name.to_str() else {
            continue;
        };
        if !CALL_DIR_RE.is_match(name_str) {
            continue;
        }
        let path = entry.path();
        let Ok(meta) = fs::symlink_metadata(&path) else {
            continue;
        };
        if !meta.is_dir() || meta.file_type().is_symlink() {
            continue;
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt as _;
            if meta.uid() != current_uid || (meta.mode() & 0o777) != 0o700 {
                continue;
            }
        }
        if let Ok(mtime) = meta.modified()
            && mtime > cutoff
        {
            continue;
        }
        let real = fs::canonicalize(&path).unwrap_or_else(|_| path.clone());
        if live.contains(&path) || live.contains(&real) || owner_alive(&path) == Some(true) {
            continue;
        }
        let _ = fs::remove_dir_all(&path);
        if fs::symlink_metadata(&path).is_err() {
            removed += 1;
        }
    }
    removed
}

// --------------------------------------------------------------------------- isolation & credentials

#[derive(Debug, Clone, PartialEq)]
pub struct Invocation {
    pub argv: Vec<String>,
    pub stdin: Option<String>,
    pub raw: Map<String, Value>,
    pub home_files: BTreeMap<String, String>,
}

impl Invocation {
    #[must_use]
    pub fn new(argv: Vec<String>) -> Self {
        Self {
            argv,
            stdin: None,
            raw: Map::new(),
            home_files: BTreeMap::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CredentialLink {
    pub real: PathBuf,
    pub link: PathBuf,
    pub sync_back: bool,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Isolation {
    pub env: BTreeMap<String, String>,
    pub links: Vec<CredentialLink>,
}

pub fn link_credentials(
    real_dir: &Path,
    private_dir: &Path,
    files: &[&str],
    shared: &[&str],
) -> std::io::Result<Vec<CredentialLink>> {
    fs::create_dir_all(private_dir)?;
    let mut links = Vec::new();
    for (name, sync) in files
        .iter()
        .map(|&n| (n, true))
        .chain(shared.iter().map(|&n| (n, false)))
    {
        let real = real_dir.join(name);
        if !real.is_file() {
            continue;
        }
        let link = private_dir.join(name);
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(&real, &link)?;
        }
        #[cfg(not(unix))]
        {
            fs::copy(&real, &link)?;
        }
        links.push(CredentialLink {
            real,
            link,
            sync_back: sync,
        });
    }
    Ok(links)
}

fn json_type_tag(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(n) => {
            if n.as_str().contains(['.', 'e', 'E']) {
                "float"
            } else {
                "int"
            }
        }
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

pub fn sync_back(links: &[CredentialLink]) {
    for item in links {
        if !item.sync_back {
            continue;
        }
        let Ok(meta) = fs::symlink_metadata(&item.link) else {
            continue;
        };
        if meta.file_type().is_symlink() || !meta.is_file() {
            continue;
        }
        let Ok(content) = fs::read(&item.link) else {
            continue;
        };
        let Ok(text) = std::str::from_utf8(&content) else {
            continue;
        };
        let Some(parsed) = load_object(text) else {
            continue;
        };
        if parsed.is_empty() {
            continue;
        }
        if item.real.is_file() {
            let Ok(orig_bytes) = fs::read(&item.real) else {
                continue;
            };
            if orig_bytes == content {
                continue;
            }
            if let Ok(orig_str) = std::str::from_utf8(&orig_bytes)
                && let Some(orig) = load_object(orig_str)
            {
                let allowed_keys: HashSet<&str> = orig
                    .keys()
                    .map(String::as_str)
                    .chain(ALLOWED_SYNC_BACK_KEYS.iter().copied())
                    .collect();
                if !parsed.keys().all(|k| allowed_keys.contains(k.as_str())) {
                    continue;
                }
                let type_mismatch = parsed.iter().any(|(k, pv)| {
                    orig.get(k).is_some_and(|ov| {
                        !ov.is_null() && !pv.is_null() && json_type_tag(pv) != json_type_tag(ov)
                    })
                });
                if type_mismatch {
                    continue;
                }
            }
        }
        let Some(parent) = item.real.parent() else {
            continue;
        };
        let prefix = format!(
            ".{}.",
            item.real
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("cred")
        );
        let Ok(mut tmp) = tempfile::Builder::new()
            .prefix(&prefix)
            .suffix(".tmp")
            .tempfile_in(parent)
        else {
            continue;
        };
        use std::io::Write as _;
        if tmp.write_all(&content).is_ok() {
            let _ = tmp.persist(&item.real);
        }
    }
}

// --------------------------------------------------------------------------- CliAdapter & CliProvider

#[derive(Clone)]
pub struct CliConfig {
    pub model: String,
    pub effort: Option<String>,
    pub timeout_s: f64,
    pub binary: String,
    pub runner: Arc<dyn ProcessRunner>,
    pub version: Option<String>,
    pub max_retries: u32,
    pub retry_base_delay_s: f64,
    /// Effective process environment supplied by the caller. Never included in Debug or
    /// persisted provider identity; the adapter's existing allowlist still filters it.
    pub environment: Option<BTreeMap<String, String>>,
}

impl std::fmt::Debug for CliConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("CliConfig")
            .field("model", &self.model)
            .field("effort", &self.effort)
            .field("timeout_s", &self.timeout_s)
            .field("binary", &self.binary)
            .field("version", &self.version)
            .field("max_retries", &self.max_retries)
            .field("retry_base_delay_s", &self.retry_base_delay_s)
            .finish_non_exhaustive()
    }
}

impl CliConfig {
    #[must_use]
    pub fn new(model: impl Into<String>, binary: impl Into<String>, timeout_s: f64) -> Self {
        Self {
            model: model.into(),
            effort: None,
            timeout_s,
            binary: binary.into(),
            runner: Arc::new(SystemProcessRunner),
            version: None,
            // SPEC-v5.2 §7.4: adapters perform no internal retries; the runner owns the cap.
            max_retries: 0,
            retry_base_delay_s: DEFAULT_RETRY_BASE_DELAY_S,
            environment: None,
        }
    }
}

pub trait CliAdapter: Send + Sync {
    fn kind(&self) -> &'static str;
    fn python_type_identity(&self) -> (&'static str, &'static str);
    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities::new(true, false, false)
    }
    /// Whether this CLI receives the system prompt through a dedicated flag/file (`Native`) or
    /// inline as the canonical `=== SYSTEM ===` block of the user message (`Inline`).
    fn system_channel(&self) -> SystemChannel {
        SystemChannel::Native
    }
    fn supports_effort(&self) -> bool {
        true
    }
    fn max_arg_bytes(&self) -> usize {
        MAX_ARG_BYTES
    }
    fn env_drop(&self) -> &'static [&'static str] {
        &[]
    }
    fn env_drop_prefixes(&self) -> &'static [&'static str] {
        &[]
    }
    fn env_keep(&self) -> &'static [&'static str] {
        &[]
    }
    fn env_set(&self) -> &'static [(&'static str, &'static str)] {
        &[]
    }

    fn isolate(&self, _home: &Path) -> std::io::Result<Isolation> {
        Ok(Isolation::default())
    }

    fn after_call(&self, _work_dir: &Path, _env: &BTreeMap<String, String>) {}

    fn stale_outputs(&self, _io_dir: &Path) -> Vec<PathBuf> {
        Vec::new()
    }

    /// Refuse a request before anything is spawned, for example when the CLI would silently
    /// rewrite or truncate it. An `Err` becomes a provider failure carrying `(error_code,
    /// message)`; it never reaches the model.
    fn preflight(&self, _request: &ModelRequest) -> Result<(), (&'static str, String)> {
        Ok(())
    }

    fn build(
        &self,
        config: &CliConfig,
        request: &ModelRequest,
        effort: Option<&str>,
        io_dir: &Path,
        work_dir: &Path,
    ) -> Invocation;

    fn parse(
        &self,
        config: &CliConfig,
        result: &ProcessResult,
        request: &ModelRequest,
        io_dir: &Path,
    ) -> ModelResponse;
}

pub struct CliProvider<A: CliAdapter> {
    adapter: A,
    config: CliConfig,
    resolved_version: OnceLock<String>,
    setup_rev: OnceLock<String>,
}

impl<A: CliAdapter> CliProvider<A> {
    #[must_use]
    pub fn new(adapter: A, config: CliConfig) -> Self {
        let resolved_version = OnceLock::new();
        if let Some(ref v) = config.version {
            let _ = resolved_version.set(v.clone());
        }
        Self {
            adapter,
            config,
            resolved_version,
            setup_rev: OnceLock::new(),
        }
    }

    #[must_use]
    pub fn adapter(&self) -> &A {
        &self.adapter
    }

    #[must_use]
    pub fn config(&self) -> &CliConfig {
        &self.config
    }

    #[must_use]
    pub fn effort(&self) -> Option<&str> {
        self.config.effort.as_deref()
    }

    #[must_use]
    pub fn binary(&self) -> &str {
        &self.config.binary
    }

    #[must_use]
    pub fn timeout_s(&self) -> f64 {
        self.config.timeout_s
    }

    #[must_use]
    pub fn keeps(&self, name: &str) -> bool {
        let upper = name.to_uppercase();
        if self.adapter.env_drop().contains(&name)
            || COMMON_ENV_DROP_PREFIXES.iter().any(|p| name.starts_with(p))
            || COMMON_ENV_DROP_SUFFIXES.iter().any(|s| upper.ends_with(s))
        {
            return false;
        }
        if self.adapter.env_keep().contains(&name) {
            return !COMMON_ENV_DROP.contains(&name)
                || self
                    .adapter
                    .env_drop_prefixes()
                    .iter()
                    .any(|p| name.starts_with(p));
        }
        if always_dropped(name)
            || self
                .adapter
                .env_drop_prefixes()
                .iter()
                .any(|p| name.starts_with(p))
        {
            return false;
        }
        COMMON_ENV_KEEP.contains(&name)
            || COMMON_ENV_KEEP_PREFIXES.iter().any(|p| name.starts_with(p))
    }

    #[must_use]
    pub fn child_env(&self, base: Option<&BTreeMap<String, String>>) -> BTreeMap<String, String> {
        let owned: BTreeMap<String, String>;
        let source = match base.or(self.config.environment.as_ref()) {
            Some(m) => m,
            None => {
                owned = std::env::vars().collect();
                &owned
            }
        };
        let mut env: BTreeMap<String, String> = source
            .iter()
            .filter(|(name, _)| self.keeps(name))
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect();
        for &(k, v) in self.adapter.env_set() {
            env.insert(k.to_string(), v.to_string());
        }
        env
    }

    pub fn setup_revision(&self) -> String {
        self.setup_rev
            .get_or_init(|| self.compute_setup_revision())
            .clone()
    }

    fn compute_setup_revision(&self) -> String {
        let mut args = Map::new();
        args.insert("<arg>".to_string(), Value::String("<value>".to_string()));
        let transcript = vec![
            Message::user("<prompt>"),
            Message::assistant(
                "<reply>",
                vec![ToolCall {
                    id: "<call-id>".to_string(),
                    name: "<tool>".to_string(),
                    arguments: args,
                }],
            ),
            Message::tool("<call-id>", "<result>"),
            Message::user("<follow-up>"),
        ];
        let mut empty_obj_schema = Map::new();
        empty_obj_schema.insert("type".to_string(), Value::String("object".to_string()));
        empty_obj_schema.insert("properties".to_string(), Value::Object(Map::new()));

        let templates: Vec<(ModelRequest, Option<&str>)> = vec![
            (ModelRequest::new("", vec![Message::user("<prompt>")]), None),
            (
                ModelRequest {
                    system: "<system>".to_string(),
                    messages: transcript.clone(),
                    output_schema: Some(empty_obj_schema.clone()),
                    ..ModelRequest::default()
                },
                if self.adapter.supports_effort() {
                    Some("<effort>")
                } else {
                    None
                },
            ),
            (
                ModelRequest {
                    system: "<system>".to_string(),
                    messages: transcript,
                    output_schema: Some(empty_obj_schema),
                    schema_name: ACTION_SCHEMA_NAME.to_string(),
                    ..ModelRequest::default()
                },
                None,
            ),
        ];

        let dir = CallDirGuard::new(&format!("identitybench-{}-setup-", self.adapter.kind()))
            .expect("setup revision scratch dir");
        let root = dir.path();

        let mut invocations = Vec::new();
        for (number, (req, effort)) in templates.iter().enumerate() {
            let io_dir = root.join(format!("t{number}")).join("io");
            let work_dir = root.join(format!("t{number}")).join("work");
            fs::create_dir_all(&io_dir).expect("io_dir");
            fs::create_dir_all(&work_dir).expect("work_dir");
            let built = self
                .adapter
                .build(&self.config, req, *effort, &io_dir, &work_dir);

            let mut argv = vec![Value::String("<binary>".to_string())];
            for arg in built.argv.iter().skip(1) {
                if arg == &self.config.model {
                    argv.push(Value::String("<model>".to_string()));
                } else {
                    argv.push(Value::String(arg.clone()));
                }
            }

            let mut inv = Map::new();
            inv.insert("argv".to_string(), Value::Array(argv));
            inv.insert(
                "stdin".to_string(),
                built.stdin.map_or(Value::Null, Value::String),
            );
            inv.insert("raw".to_string(), Value::Object(built.raw));
            inv.insert("files".to_string(), Value::Object(collect_files(&io_dir)));
            if !built.home_files.is_empty() {
                let mut hf = Map::new();
                for (k, v) in built.home_files {
                    hf.insert(k, Value::String(v));
                }
                inv.insert("home_files".to_string(), Value::Object(hf));
            }
            invocations.push(Value::Object(inv));
        }

        let home = root.join("home");
        fs::create_dir_all(&home).expect("home");
        let isolation = self.adapter.isolate(&home).expect("isolate");

        let mut iso_env = Map::new();
        for (k, v) in isolation.env {
            iso_env.insert(k, Value::String(v));
        }
        let mut iso_obj = Map::new();
        iso_obj.insert("env".to_string(), Value::Object(iso_env));
        iso_obj.insert("files".to_string(), Value::Object(collect_files(&home)));

        let mut env_obj = Map::new();
        env_obj.insert("allow".to_string(), sorted_str_array(COMMON_ENV_KEEP));
        env_obj.insert(
            "allow_prefixes".to_string(),
            str_array(COMMON_ENV_KEEP_PREFIXES),
        );
        env_obj.insert("common_drop".to_string(), sorted_str_array(COMMON_ENV_DROP));
        env_obj.insert(
            "common_drop_prefixes".to_string(),
            str_array(COMMON_ENV_DROP_PREFIXES),
        );
        env_obj.insert(
            "common_drop_suffixes".to_string(),
            str_array(COMMON_ENV_DROP_SUFFIXES),
        );
        env_obj.insert(
            "drop".to_string(),
            sorted_str_array(self.adapter.env_drop()),
        );
        env_obj.insert(
            "drop_prefixes".to_string(),
            str_array(self.adapter.env_drop_prefixes()),
        );
        env_obj.insert(
            "keep".to_string(),
            sorted_str_array(self.adapter.env_keep()),
        );
        let mut set_obj = Map::new();
        for &(k, v) in self.adapter.env_set() {
            set_obj.insert(k.to_string(), Value::String(v.to_string()));
        }
        env_obj.insert("set".to_string(), Value::Object(set_obj));
        env_obj.insert(
            "home_relative".to_string(),
            sorted_str_array(HOME_RELATIVE_VARS),
        );

        let large_system = Value::Array(vec![
            Value::from(MAX_ARG_BYTES as u64),
            Value::String(SYSTEM_BEGIN.to_string()),
            Value::String(self.adapter.system_channel().as_str().to_string()),
        ]);

        let mut setup = Map::new();
        setup.insert("invocations".to_string(), Value::Array(invocations));
        setup.insert("isolation".to_string(), Value::Object(iso_obj));
        setup.insert("env".to_string(), Value::Object(env_obj));
        setup.insert("large_system".to_string(), large_system);

        let dumped = dumps(
            &Value::Object(setup),
            &DumpOptions {
                sort_keys: true,
                ensure_ascii: false,
                ..DumpOptions::default()
            },
        )
        .unwrap_or_default()
        .replace(&root.to_string_lossy().into_owned(), "<tmp>");

        let hex_str = hex::encode(Sha256::digest(dumped.as_bytes()));
        hex_str[..16].to_string()
    }

    pub fn error_text(
        &self,
        message: &str,
        result: Option<&ProcessResult>,
        request: Option<&ModelRequest>,
    ) -> String {
        let mut text = format!("{}: {message}", self.adapter.kind());
        if let Some(res) = result {
            let mut details = vec![format!("exit {}", res.returncode)];
            let excerpt = scrub_stderr(&res.stderr, request, STDERR_EXCERPT_CHARS);
            if !excerpt.is_empty() {
                details.push(format!("stderr: {excerpt}"));
            }
            text.push_str(&format!(" ({})", details.join("; ")));
        }
        redact(&text, self.config.environment.as_ref(), &[])
    }

    pub fn fail(
        &self,
        message: &str,
        result: Option<&ProcessResult>,
        request: Option<&ModelRequest>,
    ) -> ModelResponse {
        ModelResponse::with_error(self.error_text(message, result, request))
    }

    fn ignored_params(&self, request: &ModelRequest) -> Vec<String> {
        let mut ignored = Vec::new();
        if request.params.temperature.is_some() {
            ignored.push("temperature".to_string());
        }
        ignored.push("max_output_tokens".to_string());
        if request.params.effort.is_some() && !self.adapter.supports_effort() {
            ignored.push("effort".to_string());
        }
        ignored
    }

    async fn complete_inner(&self, request: &ModelRequest) -> ModelResponse {
        if !request.tools.is_empty() {
            return self.fail(
                "native tools are not supported by CLI providers; use context mode",
                None,
                Some(request),
            );
        }
        if request.messages.is_empty() {
            return self.fail("request has no messages", None, Some(request));
        }
        if let Err((error_code, message)) = self.adapter.preflight(request) {
            let mut failed = self.fail(&message, None, Some(request));
            let mut meta = Map::new();
            meta.insert(
                "error_code".to_string(),
                Value::String(error_code.to_string()),
            );
            meta.insert(
                "error_class".to_string(),
                Value::String("TRANSPORT_CAPACITY".to_string()),
            );
            // Nothing was spawned, so the model provably never saw the request.
            meta.insert("proven_unanswered".to_string(), Value::Bool(true));
            failed.raw = Some(meta);
            return failed;
        }
        let effort: Option<String> = if self.adapter.supports_effort() {
            request
                .params
                .effort
                .clone()
                .or_else(|| self.config.effort.clone())
        } else {
            None
        };
        if let Some(ref eff) = effort
            && !valid_effort(eff)
        {
            return self.fail(
                &format!("invalid effort value {eff:?}"),
                None,
                Some(request),
            );
        }

        let dir = match CallDirGuard::new(&format!("identitybench-{}-", self.adapter.kind())) {
            Ok(d) => d,
            Err(err) => {
                return self.fail(
                    &format!("could not create call directory: {err}"),
                    None,
                    Some(request),
                );
            }
        };
        let root = dir.path();
        let io_dir = root.join("io");
        let work_dir = root.join("work");
        let home_dir = root.join("home");
        for directory in [&io_dir, &work_dir, &home_dir] {
            if let Err(err) = fs::create_dir_all(directory) {
                return self.fail(
                    &format!("could not create call subdir: {err}"),
                    None,
                    Some(request),
                );
            }
        }

        let built =
            self.adapter
                .build(&self.config, request, effort.as_deref(), &io_dir, &work_dir);
        let max_arg = self.adapter.max_arg_bytes();
        if let Some(too_long) = built.argv.iter().find(|arg| arg.len() > max_arg) {
            return self.fail(
                &format!(
                    "an argument is {} bytes, over the {max_arg}-byte argv limit",
                    too_long.len()
                ),
                None,
                Some(request),
            );
        }

        let isolation = match self.adapter.isolate(&home_dir) {
            Ok(iso) => iso,
            Err(err) => {
                return self.fail(
                    &format!("could not set up a private CLI home: {err}"),
                    None,
                    Some(request),
                );
            }
        };
        let resolved_home = fs::canonicalize(&home_dir).unwrap_or_else(|_| home_dir.clone());
        for (relative, text) in &built.home_files {
            let candidate = resolved_home.join(relative);
            if Path::new(relative).is_absolute()
                || relative.split(['/', '\\']).any(|seg| seg == "..")
            {
                return self.fail(
                    &format!("could not set up a private CLI home: home_files path escapes private home: {relative:?}"),
                    None,
                    Some(request),
                );
            }
            if let Some(parent) = candidate.parent()
                && let Err(err) = fs::create_dir_all(parent)
            {
                return self.fail(
                    &format!("could not set up a private CLI home: {err}"),
                    None,
                    Some(request),
                );
            }
            let target_dir = candidate
                .parent()
                .and_then(|p| fs::canonicalize(p).ok())
                .unwrap_or_else(|| resolved_home.clone());
            if !target_dir.starts_with(&resolved_home) {
                return self.fail(
                    &format!("could not set up a private CLI home: home_files path escapes private home: {relative:?}"),
                    None,
                    Some(request),
                );
            }
            if let Err(err) = fs::write(&candidate, text) {
                return self.fail(
                    &format!("could not set up a private CLI home: {err}"),
                    None,
                    Some(request),
                );
            }
        }

        let mut env = self.child_env(None);
        if isolation.env.contains_key("HOME") {
            for name in HOME_RELATIVE_VARS {
                env.remove(*name);
            }
        }
        for (k, v) in &isolation.env {
            env.insert(k.clone(), v.clone());
        }

        let command = Command {
            argv: built.argv.clone(),
            cwd: work_dir.clone(),
            env: env.clone(),
            timeout_s: self.config.timeout_s,
            stdin: built.stdin.clone(),
        };

        let attempts = Mutex::new(Vec::<ModelResponse>::new());
        let started = Instant::now();

        let mut response = retry_transient(
            || async {
                for stale in self.adapter.stale_outputs(&io_dir) {
                    let _ = fs::remove_file(stale);
                }
                let attempt_start = Instant::now();
                let res = self.config.runner.run(&command).await;
                let mut resp = if let Some(ref launch_err) = res.launch_error {
                    self.fail(launch_err, None, Some(request))
                } else if res.timed_out {
                    // A timeout does not prove that the CLI generated no response. Retain
                    // any buffered usage/output while refusing to call it a completed turn.
                    let mut r = self.adapter.parse(&self.config, &res, request, &io_dir);
                    r.error = Some(self.error_text(
                        &format!("no result within the {}s timeout", self.config.timeout_s),
                        None,
                        Some(request),
                    ));
                    let mut meta = r.raw.take().unwrap_or_default();
                    meta.insert("cli_buffered_error".to_string(), Value::Bool(true));
                    meta.insert(
                        "error_code".to_string(),
                        Value::String("CLI_SUBPROCESS_TIMEOUT".to_string()),
                    );
                    meta.insert(
                        "error_class".to_string(),
                        Value::String("AMBIGUOUS_FAILURE".to_string()),
                    );
                    meta.insert("proven_unanswered".to_string(), Value::Bool(false));
                    meta.insert("resumable".to_string(), Value::Bool(false));
                    r.raw = Some(meta);
                    r
                } else {
                    self.adapter.parse(&self.config, &res, request, &io_dir)
                };
                resp.latency_s = attempt_start.elapsed().as_secs_f64();
                if let Ok(mut guard) = attempts.lock() {
                    guard.push(resp.clone());
                }
                resp
            },
            |resp| is_transient_error(resp.error.as_deref()),
            self.config.max_retries,
            self.config.retry_base_delay_s,
        )
        .await;

        sync_back(&isolation.links);
        self.adapter.after_call(&work_dir, &env);

        let all_attempts = attempts.into_inner().unwrap_or_default();
        if all_attempts.len() > 1 {
            merge_attempts(
                &mut response,
                &all_attempts,
                started.elapsed().as_secs_f64(),
            );
        }
        if !built.raw.is_empty() {
            let mut merged = response.raw.take().unwrap_or_default();
            for (k, v) in built.raw {
                merged.insert(k, v);
            }
            response.raw = Some(merged);
        }
        if request.params.effort.is_none()
            && let Some(eff) = effort
        {
            let mut merged = response.raw.take().unwrap_or_default();
            merged.insert("effort".to_string(), Value::String(eff));
            response.raw = Some(merged);
        }
        response
    }
}

#[async_trait]
impl<A: CliAdapter> Provider for CliProvider<A> {
    fn kind(&self) -> &str {
        self.adapter.kind()
    }

    fn model(&self) -> &str {
        &self.config.model
    }

    fn python_type_identity(&self) -> (&'static str, &'static str) {
        self.adapter.python_type_identity()
    }

    fn local_retry_limit(&self) -> Option<u32> {
        Some(self.config.max_retries)
    }

    fn endpoint_identity(&self) -> String {
        let ver = self.resolved_version.get_or_init(|| {
            probe_version(
                &self.config.binary,
                Some(&base_child_env(self.config.environment.as_ref())),
            )
        });
        let setup_rev = self.setup_revision();
        hash_endpoint(&[
            Some(self.adapter.kind()),
            Some(ver.as_str()),
            Some(&self.config.model),
            Some(&setup_rev),
            self.config.effort.as_deref(),
        ])
    }

    fn capabilities(&self) -> ProviderCapabilities {
        self.adapter.capabilities()
    }

    fn system_channel(&self) -> SystemChannel {
        self.adapter.system_channel()
    }

    async fn complete(&self, request: &ModelRequest) -> ModelResponse {
        let mut response =
            sanitize_response(self.complete_inner(request).await, self.adapter.kind());
        let ignored = self.ignored_params(request);
        if !ignored.is_empty() {
            let mut raw = response.raw.take().unwrap_or_default();
            raw.insert(
                "ignored_params".to_string(),
                Value::Array(ignored.into_iter().map(Value::String).collect()),
            );
            response.raw = Some(raw);
        }
        response
    }
}

pub fn merge_attempts(final_resp: &mut ModelResponse, attempts: &[ModelResponse], wall_s: f64) {
    if attempts.len() <= 1 {
        return;
    }
    for earlier in &attempts[..attempts.len() - 1] {
        final_resp.usage.add(&earlier.usage);
    }
    let costs: Vec<f64> = attempts
        .iter()
        .filter_map(|item| item.reported_cost_usd)
        .filter(|c| c.is_finite() && *c >= 0.0)
        .collect();
    let mut reported_cost_total = 0.0_f64;
    let mut reported_cost_saturated = false;
    for cost in &costs {
        if let Ok((sum, sat)) = safe_nonnegative_float_add(reported_cost_total, *cost) {
            reported_cost_total = sum;
            reported_cost_saturated = reported_cost_saturated || sat;
        }
    }
    final_resp.reported_cost_usd = if costs.is_empty() {
        None
    } else {
        Some(reported_cost_total)
    };
    let mut raw = final_resp.raw.take().unwrap_or_default();
    raw.insert("attempts".to_string(), Value::from(attempts.len() as u64));
    let rounded_wall = (wall_s * 1000.0).round() / 1000.0;
    raw.insert("wall_s".to_string(), Value::from(rounded_wall));
    if reported_cost_saturated {
        raw.insert("reported_cost_saturated".to_string(), Value::Bool(true));
    }
    final_resp.raw = Some(raw);
}

fn sorted_str_array(items: &[&str]) -> Value {
    let mut sorted: Vec<&str> = items.to_vec();
    sorted.sort_unstable();
    Value::Array(
        sorted
            .into_iter()
            .map(|s| Value::String(s.to_string()))
            .collect(),
    )
}

fn str_array(items: &[&str]) -> Value {
    Value::Array(
        items
            .iter()
            .map(|s| Value::String((*s).to_string()))
            .collect(),
    )
}

fn collect_files_recursive(base: &Path, dir: &Path, out: &mut Vec<(String, String)>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let Ok(meta) = fs::symlink_metadata(&path) else {
            continue;
        };
        if meta.file_type().is_symlink() {
            continue;
        }
        if meta.is_dir() {
            collect_files_recursive(base, &path, out);
        } else if meta.is_file()
            && let Ok(rel) = path.strip_prefix(base)
        {
            let bytes = fs::read(&path).unwrap_or_default();
            let text = String::from_utf8_lossy(&bytes).into_owned();
            out.push((rel.to_string_lossy().into_owned(), text));
        }
    }
}

fn collect_files(directory: &Path) -> Map<String, Value> {
    let mut items = Vec::new();
    collect_files_recursive(directory, directory, &mut items);
    items.sort_by(|a, b| a.0.cmp(&b.0));
    let mut map = Map::new();
    for (k, v) in items {
        map.insert(k, Value::String(v));
    }
    map
}
