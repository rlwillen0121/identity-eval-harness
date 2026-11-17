//! `codex exec` (OpenAI Codex CLI, headless) provider.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use ib_core::pyjson::{DumpOptions, dumps};
use serde_json::{Map, Value};

use crate::base::{ModelRequest, ModelResponse, Usage};
use crate::cli::{
    ACTION_SCHEMA_NAME, CliAdapter, CliConfig, CliProvider, Invocation, Isolation, as_dict, as_int,
    link_credentials, parse_jsonl, prepend_system, render_transcript,
};
use crate::json::load_object;
use crate::subprocess::{ProcessResult, user_home_dir};

pub const OUTPUT_FILE: &str = "last_message.txt";
pub const SCHEMA_FILE: &str = "output_schema.json";
pub const DEFAULT_EFFORT: &str = "medium";

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

    fn python_type_identity(&self) -> (&'static str, &'static str) {
        ("identitybench.providers.codex_cli", "CodexCliProvider")
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

        let mut raw = Map::new();
        if let Some(schema) = &request.output_schema {
            if request.schema_name != ACTION_SCHEMA_NAME {
                let schema_path = io_dir.join(SCHEMA_FILE);
                let schema_json = dumps(&Value::Object(schema.clone()), &DumpOptions::default())
                    .unwrap_or_else(|_| "{}".to_string());
                let _ = fs::write(&schema_path, schema_json);
                argv.push("--output-schema".to_string());
                argv.push(schema_path.to_string_lossy().into_owned());
            } else {
                raw.insert(
                    "output_schema".to_string(),
                    Value::String("withheld (JSON action protocol turn)".to_string()),
                );
            }
        }
        argv.push("-C".to_string());
        argv.push(work_dir.to_string_lossy().into_owned());
        argv.push("-".to_string());

        Invocation {
            argv,
            stdin: Some(prompt),
            raw,
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
        let events = parse_jsonl(&result.stdout);
        let mut usage = Usage::default();
        let mut raw = Map::new();
        let mut raw_usage: Vec<Value> = Vec::new();
        let mut errors: Vec<String> = Vec::new();
        let mut streamed_text: Option<String> = None;
        let mut turn_completed = false;
        let mut turn_failed = false;

        for event in &events {
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
                raw_usage.push(Value::Object(counts.clone()));
                let total_in = as_int(counts.get("input_tokens"));
                let cached = as_int(counts.get("cached_input_tokens")).min(total_in);
                usage.add(&Usage {
                    input_tokens: total_in - cached,
                    cache_read_tokens: cached,
                    cache_write_tokens: 0,
                    output_tokens: as_int(counts.get("output_tokens")),
                    reasoning_tokens: as_int(counts.get("reasoning_output_tokens")),
                });
            } else if let Some(message) = agent_message(event) {
                streamed_text = Some(message);
            } else if let Some(err) = event_error(event) {
                if kind == Some("turn.failed") {
                    turn_failed = true;
                }
                errors.push(err);
            }
        }

        let mut text: Option<String> =
            fs::read_to_string(io_dir.join(OUTPUT_FILE))
                .ok()
                .and_then(|s| {
                    let t = s.trim();
                    if t.is_empty() {
                        None
                    } else {
                        Some(t.to_string())
                    }
                });
        if text.is_none()
            && let Some(ref st) = streamed_text
        {
            let t = st.trim();
            if !t.is_empty() {
                text = Some(t.to_string());
            }
        }
        if text.is_none()
            && turn_completed
            && !turn_failed
            && result.returncode == 0
            && request.schema_name == ACTION_SCHEMA_NAME
        {
            text = Some(String::new());
        }

        if !raw_usage.is_empty() {
            raw.insert("raw_usage".to_string(), Value::Array(raw_usage));
        }

        let mut response = ModelResponse {
            text: text.clone(),
            usage,
            stop_reason: if turn_completed {
                Some("completed".to_string())
            } else {
                None
            },
            raw: if raw.is_empty() { None } else { Some(raw) },
            ..ModelResponse::default()
        };

        let Some(ref final_text) = text else {
            let reason = errors
                .last()
                .map(String::as_str)
                .unwrap_or("no final message");
            response.error = Some(helper.error_text(reason, Some(result), Some(request)));
            return response;
        };

        if result.returncode != 0 && !turn_completed {
            let reason = errors
                .last()
                .map(String::as_str)
                .unwrap_or("exited without completing the turn");
            response.error = Some(helper.error_text(reason, Some(result), Some(request)));
            return response;
        }

        if request.output_schema.is_some() {
            response.parsed = load_object(final_text);
        }
        response
    }
}

pub type CodexCliProvider = CliProvider<CodexAdapter>;

#[must_use]
pub fn new_codex_provider(model: impl Into<String>) -> CodexCliProvider {
    let mut cfg = CliConfig::new(model, "codex", 900.0);
    cfg.effort = Some(DEFAULT_EFFORT.to_string());
    CliProvider::new(CodexAdapter, cfg)
}
