//! Cursor Agent CLI (`cursor-agent -p`) provider.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use md5::{Digest, Md5};
use serde_json::{Map, Value};

use crate::base::{ModelRequest, ModelResponse, ProviderCapabilities, SystemChannel, Usage};
use crate::cli::{
    CliAdapter, CliConfig, CliProvider, Invocation, Isolation, as_dict, as_int, parse_json_payload,
    prepend_system, render_transcript,
};
use crate::subprocess::{ProcessResult, user_home_dir};

pub const DATA_DIR_NAME: &str = "cursor-data";
const RAW_KEYS: &[&str] = &["subtype", "duration_ms", "session_id", "request_id"];

fn is_cursor_install(path: &Path) -> bool {
    let real = fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
    real.components()
        .any(|comp| comp.as_os_str() == "cursor-agent")
}

fn find_on_path(binary: &str) -> Option<PathBuf> {
    let path_var = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path_var) {
        let candidate = dir.join(binary);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

#[must_use]
pub fn resolve_cursor_binary() -> String {
    if let Some(found) = find_on_path("cursor-agent") {
        return found.to_string_lossy().into_owned();
    }
    if let Some(found) = find_on_path("agent")
        && is_cursor_install(&found)
    {
        return found.to_string_lossy().into_owned();
    }
    if let Some(home) = user_home_dir() {
        let local_bin = home.join(".local").join("bin");
        for name in ["cursor-agent", "agent"] {
            let candidate = local_bin.join(name);
            if candidate.exists() && is_cursor_install(&candidate) {
                return candidate.to_string_lossy().into_owned();
            }
        }
    }
    "cursor-agent".to_string()
}

#[must_use]
pub fn cursor_config_dir(env: &BTreeMap<String, String>) -> PathBuf {
    if let Some(configured) = env.get("CURSOR_CONFIG_DIR").map(|s| s.trim())
        && !configured.is_empty()
    {
        return PathBuf::from(configured);
    }
    if let Some(xdg) = env.get("XDG_CONFIG_HOME").map(|s| s.trim())
        && !xdg.is_empty()
    {
        return PathBuf::from(xdg).join("cursor");
    }
    if let Some(home) = env.get("HOME").map(|s| s.trim())
        && !home.is_empty()
    {
        return PathBuf::from(home).join(".cursor");
    }
    user_home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".cursor")
}

#[must_use]
pub fn chat_dir(env: &BTreeMap<String, String>, work_dir: &Path) -> PathBuf {
    let digest = hex::encode(Md5::digest(work_dir.to_string_lossy().as_bytes()));
    cursor_config_dir(env).join("chats").join(digest)
}

#[derive(Debug, Default, Clone, Copy)]
pub struct CursorAdapter;

impl CliAdapter for CursorAdapter {
    fn kind(&self) -> &'static str {
        "cursor_cli"
    }

    fn python_type_identity(&self) -> (&'static str, &'static str) {
        ("identitybench.providers.cursor_cli", "CursorCliProvider")
    }

    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities::new(false, false, false)
    }

    fn supports_effort(&self) -> bool {
        false
    }

    fn max_arg_bytes(&self) -> usize {
        250_000
    }

    fn env_drop(&self) -> &'static [&'static str] {
        &["CURSOR_API_ENDPOINT", "CURSOR_API_KEY"]
    }

    fn env_drop_prefixes(&self) -> &'static [&'static str] {
        &["CURSOR_"]
    }

    fn env_keep(&self) -> &'static [&'static str] {
        &["CURSOR_CONFIG_DIR"]
    }

    fn isolate(&self, home: &Path) -> std::io::Result<Isolation> {
        let data_dir = home.join(DATA_DIR_NAME);
        fs::create_dir(&data_dir)?;
        let mut env = BTreeMap::new();
        env.insert(
            "CURSOR_DATA_DIR".to_string(),
            data_dir.to_string_lossy().into_owned(),
        );
        Ok(Isolation {
            env,
            links: Vec::new(),
        })
    }

    fn after_call(&self, work_dir: &Path, env: &BTreeMap<String, String>) {
        let chats = chat_dir(env, work_dir);
        if chats.is_dir() {
            let _ = fs::remove_dir_all(chats);
        }
    }

    fn system_channel(&self) -> SystemChannel {
        SystemChannel::Inline
    }

    fn build(
        &self,
        config: &CliConfig,
        request: &ModelRequest,
        _effort: Option<&str>,
        _io_dir: &Path,
        _work_dir: &Path,
    ) -> Invocation {
        let mut prompt = prepend_system(&request.system, &render_transcript(&request.messages));
        if prompt.starts_with('-') {
            prompt = format!("\n{prompt}");
        }
        Invocation::new(vec![
            config.binary.clone(),
            "-p".to_string(),
            "--trust".to_string(),
            "--mode".to_string(),
            "ask".to_string(),
            "--output-format".to_string(),
            "json".to_string(),
            "--model".to_string(),
            config.model.clone(),
            prompt,
        ])
    }

    fn parse(
        &self,
        _config: &CliConfig,
        result: &ProcessResult,
        request: &ModelRequest,
        _io_dir: &Path,
    ) -> ModelResponse {
        let helper = CliProvider::new(*self, CliConfig::new("", "", 0.0));
        let Some(payload) = parse_json_payload(&result.stdout) else {
            return helper.fail("no JSON result on stdout", Some(result), Some(request));
        };

        let usage_in = as_dict(payload.get("usage"));
        let text = payload.get("result").and_then(Value::as_str);

        let mut raw = Map::new();
        raw.insert("raw_usage".to_string(), Value::Object(usage_in.clone()));
        for key in RAW_KEYS {
            if let Some(val) = payload.get(*key) {
                raw.insert((*key).to_string(), val.clone());
            }
        }

        let mut response = ModelResponse {
            text: text.map(str::to_string),
            stop_reason: payload
                .get("subtype")
                .and_then(Value::as_str)
                .map(str::to_string),
            usage: Usage {
                input_tokens: as_int(usage_in.get("inputTokens")),
                output_tokens: as_int(usage_in.get("outputTokens")),
                cache_read_tokens: as_int(usage_in.get("cacheReadTokens")),
                cache_write_tokens: as_int(usage_in.get("cacheWriteTokens")),
                reasoning_tokens: 0,
            },
            raw: Some(raw),
            ..ModelResponse::default()
        };

        let is_err = payload
            .get("is_error")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if is_err || result.returncode != 0 {
            let message = text
                .filter(|s| !s.is_empty())
                .unwrap_or("the CLI reported an error");
            response.error = Some(helper.error_text(message, Some(result), Some(request)));
        }
        response
    }
}

pub type CursorCliProvider = CliProvider<CursorAdapter>;

#[must_use]
pub fn new_cursor_provider(model: Option<&str>, binary: Option<&str>) -> CursorCliProvider {
    let bin = binary
        .map(str::to_string)
        .unwrap_or_else(resolve_cursor_binary);
    CliProvider::new(
        CursorAdapter,
        CliConfig::new(model.unwrap_or("auto"), bin, 600.0),
    )
}
