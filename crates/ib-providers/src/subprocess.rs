//! Subprocess plumbing for subscription-CLI providers.
//!
//! Everything a CLI provider does with the OS goes through [`ProcessRunner`]. Production uses
//! [`run_command`]; tests inject a fake runner or point at mock scripts in a temp dir.
//!
//! [`run_command`] never errors for process-level failures: a missing binary, crash, or timeout
//! comes back as a [`ProcessResult`] that the provider converts into [`ModelResponse::error`].
//! Captured stdout/stderr are capped at [`MAX_CAPTURE_BYTES`] per stream with [`TRUNCATION_MARKER`].

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use nix::sys::signal::{Signal, killpg};
use nix::unistd::Pid;
use regex::{Regex, RegexBuilder};
use serde_json::{Map, Value};
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, Command as TokioCommand};
use url::Url;

pub const DEFAULT_MAX_RETRIES: u32 = 3;
pub const DEFAULT_RETRY_BASE_DELAY_S: f64 = 2.0;
pub const MAX_CAPTURE_BYTES: usize = 16 * 1024 * 1024;
pub const TRUNCATION_MARKER: &str = "\n[identitybench: output truncated at 16 MiB]";
const READ_CHUNK: usize = 64 * 1024;

pub const REDACTED: &str = "<redacted>";
const LOCAL_HOSTS: &[&str] = &[
    "localhost",
    "localhost.localdomain",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
];
const MIN_SECRET_CHARS: usize = 8;
const MIN_HOST_CHARS: usize = 4;

static TRANSIENT_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)rate[ _-]?limit|too many requests|\b429\b|overloaded|at capacity|\b529\b|\b50[234]\b|temporarily unavailable|service unavailable|bad gateway|gateway timeout|stream idle timeout|context deadline exceeded|ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|socket hang up|fetch failed|network error|connection (?:reset|refused|error|closed|aborted)",
    )
    .expect("valid transient regex")
});

static AUTH_FAILURE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)\b401\b|unauthori[sz]ed|authentication_error|(?:wrong|invalid|incorrect) api key|not logged in|please (?:log ?in|run /login)",
    )
    .expect("valid auth failure regex")
});

static SECRET_NAME_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"(?i)(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?|_URL|_ENDPOINT|_HOST|_AUTH|_BASE|_PAT|_PASSWD|_COOKIE|_SESSION|_JWT|_PROXY|_ADDR|_URI|_DSN)$|(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)(?:_|$)",
    )
    .expect("valid secret name regex")
});

static SECRET_VALUE_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(
        r"\b(?:sk|xai|pk)-[A-Za-z0-9_\-]{12,}|\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}|(?i:Bearer)\s+[A-Za-z0-9._\-~+/=]{12,}|(?i:Authorization:\s*Basic)\s+[A-Za-z0-9+/=]{8,}|(?i:x-api-key:)\s*[^\s\x22',;]{8,}|\bxox[a-zA-Z]-[A-Za-z0-9-]{10,}|\bAIza[0-9A-Za-z_-]{30,}|\bya29\.[0-9A-Za-z_-]{20,}|\bAKIA[0-9A-Z]{16}\b|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    )
    .expect("valid secret value regex")
});

static URL_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?i)([A-Za-z][A-Za-z0-9+.-]*:)(//|(?:\\/){2})([^\s/\\?"'<>]+)([^\s"'<>]*)"#)
        .expect("valid url regex")
});

static QUERY_VALUE_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r#"([?&][^=&\s?#]+=)[^&\s#"']*"#).expect("valid query regex"));

static HOME_USER_PATH_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(/(?:Users|home)/)[^/\s"'<>:,)]+"#).expect("valid user path regex")
});

#[derive(Debug, Clone, PartialEq)]
pub struct Command {
    pub argv: Vec<String>,
    pub cwd: PathBuf,
    pub env: BTreeMap<String, String>,
    pub timeout_s: f64,
    pub stdin: Option<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ProcessResult {
    pub returncode: i32,
    pub stdout: String,
    pub stderr: String,
    pub duration_s: f64,
    pub timed_out: bool,
    pub launch_error: Option<String>,
}

#[async_trait]
pub trait ProcessRunner: Send + Sync {
    async fn run(&self, command: &Command) -> ProcessResult;
}

#[derive(Debug, Default, Clone, Copy)]
pub struct SystemProcessRunner;

#[async_trait]
impl ProcessRunner for SystemProcessRunner {
    async fn run(&self, command: &Command) -> ProcessResult {
        run_command(command).await
    }
}

// --------------------------------------------------------------------------- live process groups

static LIVE_GROUPS: LazyLock<Mutex<BTreeSet<i32>>> = LazyLock::new(|| Mutex::new(BTreeSet::new()));

pub fn register_group(pgid: i32) {
    if let Ok(mut guard) = LIVE_GROUPS.lock() {
        guard.insert(pgid);
    }
}

pub fn unregister_group(pgid: i32) {
    if let Ok(mut guard) = LIVE_GROUPS.lock() {
        guard.remove(&pgid);
    }
}

#[must_use]
pub fn live_groups() -> BTreeSet<i32> {
    LIVE_GROUPS.lock().map(|g| g.clone()).unwrap_or_default()
}

pub fn kill_all_live() -> usize {
    let groups: Vec<i32> = match LIVE_GROUPS.lock() {
        Ok(mut guard) => {
            let items: Vec<i32> = guard.iter().copied().collect();
            guard.clear();
            items
        }
        Err(_) => return 0,
    };
    for &pgid in &groups {
        #[cfg(unix)]
        if pgid > 0 {
            let _ = killpg(Pid::from_raw(pgid), Signal::SIGKILL);
        }
        #[cfg(not(unix))]
        {
            let _ = pgid;
        }
    }
    groups.len()
}

struct LiveGroupGuard {
    pgid: i32,
    kill_on_drop: bool,
}

impl LiveGroupGuard {
    fn new(pgid: i32) -> Self {
        register_group(pgid);
        Self {
            pgid,
            kill_on_drop: true,
        }
    }

    fn disarm_kill(&mut self) {
        self.kill_on_drop = false;
    }
}

impl Drop for LiveGroupGuard {
    fn drop(&mut self) {
        if self.kill_on_drop {
            kill_stragglers(self.pgid);
        }
        unregister_group(self.pgid);
    }
}

fn kill_group(pgid: i32, child: &mut Child) {
    if child.try_wait().ok().flatten().is_none() {
        #[cfg(unix)]
        if pgid > 0 {
            let _ = killpg(Pid::from_raw(pgid), Signal::SIGKILL);
        }
        let _ = child.start_kill();
    }
}

fn kill_stragglers(pgid: i32) {
    #[cfg(unix)]
    if pgid > 0 {
        let _ = killpg(Pid::from_raw(pgid), Signal::SIGKILL);
    }
    #[cfg(not(unix))]
    {
        let _ = pgid;
    }
}

struct Capture {
    data: Vec<u8>,
    truncated: bool,
}

impl Capture {
    fn into_text(self, marker: &str) -> String {
        let decoded = String::from_utf8_lossy(&self.data).into_owned();
        if self.truncated {
            format!("{decoded}{marker}")
        } else {
            decoded
        }
    }
}

async fn drain_stream<R: AsyncRead + Unpin>(stream: Option<R>, limit: usize) -> Capture {
    let Some(mut reader) = stream else {
        return Capture {
            data: Vec::new(),
            truncated: false,
        };
    };
    let mut kept = Vec::new();
    let mut buf = vec![0u8; READ_CHUNK];
    let mut truncated = false;
    loop {
        match reader.read(&mut buf).await {
            Ok(0) | Err(_) => break,
            Ok(n) => {
                if kept.len() >= limit {
                    truncated = true;
                    continue;
                }
                let remaining = limit - kept.len();
                let keep_n = n.min(remaining);
                if keep_n < n {
                    truncated = true;
                }
                kept.extend_from_slice(&buf[..keep_n]);
            }
        }
    }
    Capture {
        data: kept,
        truncated,
    }
}

async fn feed_stdin(mut stdin: Option<tokio::process::ChildStdin>, data: &[u8]) {
    if let Some(ref mut writer) = stdin {
        let _ = writer.write_all(data).await;
        let _ = writer.shutdown().await;
    }
    drop(stdin);
}

pub async fn run_command(command: &Command) -> ProcessResult {
    run_command_with_cap(command, MAX_CAPTURE_BYTES).await
}

pub async fn run_command_with_cap(command: &Command, max_capture_bytes: usize) -> ProcessResult {
    let start = Instant::now();
    if command.argv.is_empty() {
        return ProcessResult {
            returncode: -1,
            stdout: String::new(),
            stderr: String::new(),
            duration_s: start.elapsed().as_secs_f64(),
            timed_out: false,
            launch_error: Some("could not start '': empty argv".to_string()),
        };
    }

    let mut cmd = TokioCommand::new(&command.argv[0]);
    cmd.args(&command.argv[1..])
        .current_dir(&command.cwd)
        .env_clear()
        .envs(&command.env)
        .stdin(if command.stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt as _;
        cmd.as_std_mut().process_group(0);
    }

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(err) => {
            return ProcessResult {
                returncode: -1,
                stdout: String::new(),
                stderr: String::new(),
                duration_s: start.elapsed().as_secs_f64(),
                timed_out: false,
                launch_error: Some(redact(
                    &format!("could not start {:?}: {err}", command.argv[0]),
                    None,
                    &[],
                )),
            };
        }
    };

    let pgid = child.id().unwrap_or(0) as i32;
    let mut guard = if pgid > 0 {
        Some(LiveGroupGuard::new(pgid))
    } else {
        None
    };

    let stdout_pipe = child.stdout.take();
    let stderr_pipe = child.stderr.take();
    let stdin_pipe = child.stdin.take();
    let stdin_bytes = command.stdin.as_deref().map(str::as_bytes);

    let timeout = Duration::from_secs_f64(command.timeout_s.max(0.0));
    let work = async {
        let (out_cap, err_cap, ()) = tokio::join!(
            drain_stream(stdout_pipe, max_capture_bytes),
            drain_stream(stderr_pipe, max_capture_bytes),
            async {
                if let Some(bytes) = stdin_bytes {
                    feed_stdin(stdin_pipe, bytes).await;
                }
            }
        );
        let status = child.wait().await;
        kill_stragglers(pgid);
        (out_cap, err_cap, status)
    };

    match tokio::time::timeout(timeout, work).await {
        Ok((out_cap, err_cap, status_res)) => {
            kill_stragglers(pgid);
            if let Some(g) = guard.as_mut() {
                g.disarm_kill();
            }
            let returncode = status_res.ok().and_then(|s| s.code()).unwrap_or(-1);
            ProcessResult {
                returncode,
                stdout: out_cap.into_text(TRUNCATION_MARKER),
                stderr: err_cap.into_text(TRUNCATION_MARKER),
                duration_s: start.elapsed().as_secs_f64(),
                timed_out: false,
                launch_error: None,
            }
        }
        Err(_) => {
            kill_group(pgid, &mut child);
            let status = child.wait().await.ok().and_then(|s| s.code());
            if let Some(g) = guard.as_mut() {
                g.disarm_kill();
            }
            ProcessResult {
                returncode: status.unwrap_or(-9),
                stdout: String::new(),
                stderr: String::new(),
                duration_s: start.elapsed().as_secs_f64(),
                timed_out: true,
                launch_error: None,
            }
        }
    }
}

// --------------------------------------------------------------------------- retries

#[must_use]
pub fn is_transient_error(text: Option<&str>) -> bool {
    let Some(t) = text else {
        return false;
    };
    if t.is_empty() || AUTH_FAILURE_RE.is_match(t) {
        return false;
    }
    TRANSIENT_RE.is_match(t)
}

pub async fn retry_transient<T, F, Fut, P>(
    mut attempt: F,
    is_transient: P,
    max_retries: u32,
    base_delay_s: f64,
) -> T
where
    F: FnMut() -> Fut,
    Fut: std::future::Future<Output = T>,
    P: Fn(&T) -> bool,
{
    let mut result = attempt().await;
    for retry in 0..max_retries {
        if !is_transient(&result) {
            return result;
        }
        let delay = base_delay_s * 2.0_f64.powi(retry as i32);
        if delay > 0.0 {
            tokio::time::sleep(Duration::from_secs_f64(delay)).await;
        }
        result = attempt().await;
    }
    result
}

// --------------------------------------------------------------------------- redaction

pub(crate) fn user_home_dir() -> Option<PathBuf> {
    if let Some(h) = std::env::var_os("HOME")
        && !h.is_empty()
    {
        return Some(PathBuf::from(h));
    }
    std::env::home_dir()
}

/// The host of `value` when it is a URL worth masking.
#[must_use]
pub fn url_hostname(value: &str) -> Option<String> {
    if !value.contains("://") {
        return None;
    }
    let parsed = Url::parse(value.trim()).ok()?;
    let raw_host = parsed.host_str()?;
    let host = raw_host
        .trim_start_matches('[')
        .trim_end_matches(']')
        .to_lowercase();
    if host.is_empty()
        || host.chars().count() < MIN_HOST_CHARS
        || host.chars().all(|c| c.is_ascii_digit())
        || LOCAL_HOSTS.contains(&host.as_str())
        || host.ends_with(".localhost")
    {
        return None;
    }
    Some(host)
}

fn is_url_scheme_boundary(prefix: &str) -> bool {
    match prefix.bytes().next_back() {
        None => true,
        Some(b) => !(b.is_ascii_alphanumeric() || b == b'+' || b == b'.' || b == b'-'),
    }
}

fn replace_urls(text: &str, collected_hosts: &mut BTreeSet<String>) -> String {
    let mut out = String::with_capacity(text.len());
    let mut pos = 0usize;
    while pos < text.len() {
        let Some(caps) = URL_RE.captures(&text[pos..]) else {
            out.push_str(&text[pos..]);
            break;
        };
        let full = caps.get(0).expect("full match");
        let abs_start = pos + full.start();
        let abs_end = pos + full.end();
        if !is_url_scheme_boundary(&text[..abs_start]) {
            let next_pos = abs_start + text[abs_start..].chars().next().map_or(1, char::len_utf8);
            out.push_str(&text[pos..next_pos]);
            pos = next_pos;
            continue;
        }
        let unescaped = full.as_str().replace(r"\/", "/");
        if let Some(host) = url_hostname(&unescaped)
            && host.contains('.')
        {
            collected_hosts.insert(host);
        }
        out.push_str(&text[pos..abs_start]);
        let scheme = caps.get(1).expect("scheme").as_str();
        let slashes = caps.get(2).expect("slashes").as_str();
        out.push_str(scheme);
        out.push_str(slashes);
        out.push_str(REDACTED);
        pos = abs_end;
    }
    out
}

fn replace_home_prefix(text: &str, home: &str) -> String {
    if home.is_empty() || home == "/" {
        return text.to_string();
    }
    let mut out = String::with_capacity(text.len());
    let mut pos = 0usize;
    while let Some(rel) = text[pos..].find(home) {
        let start = pos + rel;
        let end = start + home.len();
        let boundary_ok = match text[end..].chars().next() {
            None => true,
            Some(c) => c == '/' || c.is_whitespace() || c == '"' || c == '\'',
        };
        if boundary_ok {
            out.push_str(&text[pos..start]);
            out.push('~');
            pos = end;
        } else {
            out.push_str(&text[pos..end]);
            pos = end;
        }
    }
    out.push_str(&text[pos..]);
    out
}

#[must_use]
pub fn redact(
    text: &str,
    env: Option<&BTreeMap<String, String>>,
    explicit_env_names: &[&str],
) -> String {
    let owned_env: BTreeMap<String, String>;
    let source = match env {
        Some(m) => m,
        None => {
            owned_env = std::env::vars().collect();
            &owned_env
        }
    };

    let mut values = BTreeSet::new();
    let mut hosts = BTreeSet::new();
    for (name, value) in source {
        let explicit = explicit_env_names.contains(&name.as_str());
        if !explicit && !SECRET_NAME_RE.is_match(name) {
            continue;
        }
        if !value.is_empty() && (explicit || value.chars().count() >= MIN_SECRET_CHARS) {
            values.insert(value.clone());
            if value.contains('/') {
                values.insert(value.replace('/', "\\/"));
            }
        }
        if let Some(host) = url_hostname(value) {
            hosts.insert(host);
        }
    }

    let mut current = replace_urls(text, &mut hosts);

    let mut sorted_values: Vec<String> = values.into_iter().collect();
    sorted_values.sort_by(|a, b| b.len().cmp(&a.len()).then_with(|| a.cmp(b)));
    for val in sorted_values {
        current = current.replace(&val, REDACTED);
    }

    current = QUERY_VALUE_RE
        .replace_all(&current, format!("${{1}}{REDACTED}"))
        .into_owned();

    let mut sorted_hosts: Vec<String> = hosts.into_iter().collect();
    sorted_hosts.sort_by(|a, b| b.len().cmp(&a.len()).then_with(|| a.cmp(b)));
    for host in sorted_hosts {
        let pattern = format!(r"\b{}\b", regex::escape(&host));
        if let Ok(re) = RegexBuilder::new(&pattern).case_insensitive(true).build() {
            current = re.replace_all(&current, REDACTED).into_owned();
        }
    }

    if let Some(home_path) = user_home_dir() {
        let resolved = std::fs::canonicalize(&home_path).unwrap_or(home_path);
        current = replace_home_prefix(&current, &resolved.to_string_lossy());
    }

    current = HOME_USER_PATH_RE
        .replace_all(&current, format!("${{1}}{REDACTED}"))
        .into_owned();

    SECRET_VALUE_RE.replace_all(&current, REDACTED).into_owned()
}

#[must_use]
pub fn redact_simple(text: &str) -> String {
    redact(text, None, &[])
}

#[must_use]
pub fn redact_diagnostics(
    value: &Value,
    env: Option<&BTreeMap<String, String>>,
    explicit_env_names: &[&str],
) -> Value {
    fn scrub(
        item: &Value,
        root: bool,
        env: Option<&BTreeMap<String, String>>,
        explicit: &[&str],
    ) -> Value {
        match item {
            Value::String(s) => Value::String(redact(s, env, explicit)),
            Value::Array(items) => Value::Array(
                items
                    .iter()
                    .map(|child| scrub(child, false, env, explicit))
                    .collect(),
            ),
            Value::Object(map) => {
                let mut out = Map::new();
                for (key, child) in map {
                    let safe_key = redact(key, env, explicit);
                    let preserve_replay_payload = root && key == "assistant_content";
                    let safe_val = if preserve_replay_payload {
                        child.clone()
                    } else {
                        scrub(child, false, env, explicit)
                    };
                    out.insert(safe_key, safe_val);
                }
                Value::Object(out)
            }
            _ => item.clone(),
        }
    }

    scrub(value, true, env, explicit_env_names)
}

#[must_use]
pub fn redact_diagnostics_map(
    map: &Map<String, Value>,
    env: Option<&BTreeMap<String, String>>,
    explicit_env_names: &[&str],
) -> Map<String, Value> {
    match redact_diagnostics(&Value::Object(map.clone()), env, explicit_env_names) {
        Value::Object(out) => out,
        _ => Map::new(),
    }
}

#[must_use]
pub fn tail(text: &str, limit: usize) -> String {
    let trimmed = text.trim();
    let count = trimmed.chars().count();
    if count <= limit {
        trimmed.to_string()
    } else {
        let skip = count - limit;
        let tail_str: String = trimmed.chars().skip(skip).collect();
        format!("…{tail_str}")
    }
}

pub fn cwd_or_dot(path: &Path) -> PathBuf {
    if path.as_os_str().is_empty() {
        PathBuf::from(".")
    } else {
        path.to_path_buf()
    }
}
