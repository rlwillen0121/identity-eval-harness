//! `codex exec` (OpenAI Codex CLI, headless) provider.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::ErrorKind;
use std::path::{Path, PathBuf};

use serde_json::{Map, Value};

use crate::base::{ModelRequest, ModelResponse, SystemChannel, Usage};
use crate::cli::{
    CliAdapter, CliConfig, CliProvider, Invocation, Isolation, as_dict, as_int, link_credentials,
    prepend_system, read_sidecar_text, render_transcript, visit_jsonl,
};
use crate::subprocess::{ProcessResult, user_home_dir};

pub const OUTPUT_FILE: &str = "last_message.txt";
pub const DEFAULT_EFFORT: &str = "medium";
const MAX_RETAINED_RAW_USAGE_EVENTS: usize = 32;

pub const DISABLED_FEATURES: &[&str] = &[
    "apps",
    "plugins",
    "remote_plugin",
    "plugin_hooks",
    "recommended_plugins",
    "tool_suggest",
    "shell_tool",
    "unified_exec",
    "js_repl",
    "view_image",
    "memories",
    "hooks",
    "skill_search",
    "skill_mcp_dependency_install",
    "multi_agent",
    "multi_agent_v2",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "web_search_request",
    "web_search_cached",
    "standalone_web_search",
    "search_tool",
    "tool_search",
    "sleep_tool",
    "request_permissions_tool",
    "goals",
    "workspace_dependencies",
];

pub const CONFIG_OVERRIDES: &[&str] = &[
    "skills.bundled.enabled=false",
    "skills.include_instructions=false",
    "web_search=\"disabled\"",
];

#[must_use]
pub fn real_codex_home() -> PathBuf {
    if let Ok(configured) = std::env::var("CODEX_HOME")
        && !configured.is_empty()
    {
        return PathBuf::from(configured);
    }
    user_home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".codex")
}

fn event_error(event: &Map<String, Value>) -> Option<String> {
    let kind = event.get("type").and_then(Value::as_str)?;
    if kind == "error" {
        let msg = event.get("message").and_then(Value::as_str).unwrap_or("");
        return Some(if msg.is_empty() {
            "error event".to_string()
        } else {
            msg.to_string()
        });
    }
    if kind == "turn.failed" {
        let err_map = as_dict(event.get("error"));
        let msg = err_map.get("message").and_then(Value::as_str).unwrap_or("");
        return Some(if msg.is_empty() {
            "turn failed".to_string()
        } else {
            msg.to_string()
        });
    }
    None
}

fn agent_message(event: &Map<String, Value>) -> Option<String> {
    if event.get("type").and_then(Value::as_str) != Some("item.completed") {
        return None;
    }
    let item = as_dict(event.get("item"));
    let item_kind = item
        .get("type")
        .or_else(|| item.get("item_type"))
        .and_then(Value::as_str);
    if matches!(item_kind, Some("agent_message" | "assistant_message")) {
        return item.get("text").and_then(Value::as_str).map(str::to_string);
    }
    None
}

#[derive(Debug, Default, Clone, Copy)]
pub struct CodexAdapter;

impl CliAdapter for CodexAdapter {
    fn kind(&self) -> &'static str {
        "codex_cli"
    }

    fn env_drop(&self) -> &'static [&'static str] {
        &["OPENAI_BASE_URL", "OPENAI_API_KEY", "CODEX_HOME"]
    }

    fn env_drop_prefixes(&self) -> &'static [&'static str] {
        &["CODEX_", "OPENAI_"]
    }

    fn stale_outputs(&self, io_dir: &Path) -> Vec<PathBuf> {
        vec![io_dir.join(OUTPUT_FILE)]
    }

    fn isolate(&self, home: &Path) -> std::io::Result<Isolation> {
        let private = home.join(".codex");
        let links = link_credentials(&real_codex_home(), &private, &["auth.json"], &[])?;
        let mut env = BTreeMap::new();
        env.insert("HOME".to_string(), home.to_string_lossy().into_owned());
        env.insert(
            "CODEX_HOME".to_string(),
            private.to_string_lossy().into_owned(),
        );
        Ok(Isolation { env, links })
    }

    fn system_channel(&self) -> SystemChannel {
        SystemChannel::Inline
    }

    fn build(
        &self,
        config: &CliConfig,
        request: &ModelRequest,
        effort: Option<&str>,
        io_dir: &Path,
        work_dir: &Path,
    ) -> Invocation {
        let prompt = prepend_system(&request.system, &render_transcript(&request.messages));
        let mut argv = vec![
            config.binary.clone(),
            "exec".to_string(),
            "-m".to_string(),
            config.model.clone(),
        ];
        if let Some(eff) = effort {
            argv.push("-c".to_string());
            argv.push(format!("model_reasoning_effort=\"{eff}\""));
        }
        argv.extend([
            "-s".to_string(),
            "read-only".to_string(),
            "--skip-git-repo-check".to_string(),
            "--ephemeral".to_string(),
            "--ignore-user-config".to_string(),
            "--ignore-rules".to_string(),
        ]);
        for feature in DISABLED_FEATURES {
            argv.push("--disable".to_string());
            argv.push((*feature).to_string());
        }
        for ov in CONFIG_OVERRIDES {
            argv.push("-c".to_string());
            argv.push((*ov).to_string());
        }
        argv.push("--json".to_string());
        argv.push("-o".to_string());
        argv.push(io_dir.join(OUTPUT_FILE).to_string_lossy().into_owned());

        argv.push("-C".to_string());
        argv.push(work_dir.to_string_lossy().into_owned());
        argv.push("-".to_string());

        Invocation {
            argv,
            stdin: Some(prompt),
            raw: Map::new(),
            home_files: BTreeMap::new(),
        }
    }

    fn parse(
        &self,
        _config: &CliConfig,
        result: &ProcessResult,
        request: &ModelRequest,
        io_dir: &Path,
    ) -> ModelResponse {
        let helper = CliProvider::new(*self, CliConfig::new("", "", 0.0));
        let mut usage = Usage::default();
        let mut raw = Map::new();
        let mut raw_usage: VecDeque<Value> = VecDeque::new();
        let mut last_error: Option<String> = None;
        let mut streamed_text: Option<String> = None;
        let mut turn_completed = false;
        let mut native_tool_events: BTreeSet<String> = BTreeSet::new();

        visit_jsonl(&result.stdout, |event| {
            let kind = event.get("type").and_then(Value::as_str);
            if kind == Some("thread.started")
                && let Some(tid) = event.get("thread_id")
                && !tid.is_null()
                && tid != &Value::String(String::new())
            {
                raw.insert("thread_id".to_string(), tid.clone());
            } else if kind == Some("turn.completed") {
                turn_completed = true;
                let counts = as_dict(event.get("usage"));
                if raw_usage.len() == MAX_RETAINED_RAW_USAGE_EVENTS {
                    raw_usage.pop_front();
                }
                raw_usage.push_back(Value::Object(counts.clone()));
                let total_in = as_int(counts.get("input_tokens"));
                let cached = as_int(counts.get("cached_input_tokens")).min(total_in);
                usage.add(&Usage {
                    input_tokens: total_in - cached,
                    cache_read_tokens: cached,
                    cache_write_tokens: 0,
                    output_tokens: as_int(counts.get("output_tokens")),
                    reasoning_tokens: as_int(counts.get("reasoning_output_tokens")),
                });
            } else if matches!(
                kind,
                Some("item.started" | "item.updated" | "item.completed")
            ) && let Some(item_kind) = event.get("item").and_then(|item| {
                item.get("type")
                    .or_else(|| item.get("item_type"))
                    .and_then(Value::as_str)
            }) && matches!(
                item_kind,
                "command_execution"
                    | "file_change"
                    | "mcp_tool_call"
                    | "dynamic_tool_call"
                    | "web_search"
                    | "collab_tool_call"
            ) {
                raw.insert(
                    crate::base::NATIVE_TOOL_ACTIVITY.to_owned(),
                    Value::Bool(true),
                );
                native_tool_events.insert(item_kind.to_owned());
            } else if let Some(message) = agent_message(event) {
                streamed_text = Some(message);
            } else if let Some(err) = event_error(event) {
                last_error = Some(err);
            }
        });

        let (sidecar_text, output_file_rejected) =
            match read_sidecar_text(&io_dir.join(OUTPUT_FILE)) {
                Ok(sidecar) => (Some(sidecar), false),
                Err(error) if error.kind() == ErrorKind::NotFound => (None, false),
                Err(_) => (None, true),
            };
        // Preserve the full visible reply without trimming, and keep a present empty or
        // whitespace-only message distinct from a missing transport event (SPEC-v5.2 §3.1, §7.1).
        let text = match (sidecar_text, streamed_text) {
            (Some(sidecar), Some(streamed)) if sidecar.is_empty() && !streamed.is_empty() => {
                Some(streamed)
            }
            (Some(sidecar), None) if sidecar.is_empty() && !turn_completed => None,
            (Some(sidecar), _) => Some(sidecar),
            (None, Some(streamed)) => Some(streamed),
            (None, None) => None,
        };
        if !native_tool_events.is_empty() {
            raw.insert(
                crate::base::NATIVE_TOOL_EVENTS.to_owned(),
                Value::Array(native_tool_events.into_iter().map(Value::String).collect()),
            );
        }
        if !raw_usage.is_empty() {
            raw.insert(
                "raw_usage".to_string(),
                Value::Array(raw_usage.into_iter().collect()),
            );
        }
        if output_file_rejected {
            raw.insert("output_file_rejected".to_string(), Value::Bool(true));
        }

        let missing_text = text.is_none();
        let mut response = ModelResponse {
            text,
            usage,
            stop_reason: if turn_completed {
                Some("completed".to_string())
            } else {
                None
            },
            raw: if raw.is_empty() { None } else { Some(raw) },
            ..ModelResponse::default()
        };

        if missing_text {
            let reason = if output_file_rejected {
                "final output sidecar was rejected"
            } else {
                last_error.as_deref().unwrap_or("no final message")
            };
            response.error = Some(helper.error_text(reason, Some(result), Some(request)));
            return response;
        }

        if result.returncode != 0 && !turn_completed {
            let reason = last_error
                .as_deref()
                .unwrap_or("exited without completing the turn");
            response.error = Some(helper.error_text(reason, Some(result), Some(request)));
            return response;
        }

        response
    }
}

pub type CodexCliProvider = CliProvider<CodexAdapter>;
