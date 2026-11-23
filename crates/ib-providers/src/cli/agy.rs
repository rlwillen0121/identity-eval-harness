//! `agy -p` (Google Antigravity CLI, headless) provider.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use ib_core::pyjson::{DumpOptions, dumps};
use regex::Regex;
use serde_json::{Map, Value};

use crate::base::{ModelRequest, ModelResponse, Usage};
use crate::cli::{
    CliAdapter, CliConfig, CliProvider, Invocation, Isolation, NEUTRAL_SYSTEM, as_dict, as_int,
    read_sidecar_text, render_transcript, structured_text, visit_jsonl,
};
use crate::subprocess::{ProcessResult, user_home_dir};

pub const AGENT_NAME: &str = "identitybench";
pub const AGENT_FILE: &str = ".gemini/config/agents/identitybench/agent.md";
pub const LOG_FILE: &str = "agy.log";
/// Largest user message, in UTF-8 bytes, that `agy` delivers unmodified. The CLI truncates a
/// longer user message client-side to about `191_985` bytes and appends
/// `\n<truncated N bytes>`, so the adapter refuses to send one instead (`SPEC-v5.2` §7.2: silent
/// truncation is forbidden). The uniform `176_000`-byte request ceiling keeps every canonical
/// request below this bound; the guard is a defensive invariant.
pub const AGY_MAX_USER_MESSAGE_BYTES: usize = 191_000;
/// `ModelResponse::raw` `error_code` of a request refused by the capacity guard.
pub const AGY_CAPACITY_ERROR_CODE: &str = "CLI_MESSAGE_CAPACITY_EXCEEDED";

const RAW_KEYS: &[&str] = &["status", "num_turns", "duration_seconds", "conversation_id"];

static RESOLVED_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"model alias "[^"]+" resolved to "([^"]+)"|Resolving model (\S+)"#)
        .expect("valid resolved regex")
});

/// The private agent file: no tools, no inherited components, then the system prompt.
#[must_use]
pub fn agent_definition(system: &str) -> String {
    let front = [
        "---",
        &format!("name: {AGENT_NAME}"),
        "description: A single model call with no tools.",
        "mainAgent: true",
        "subagent: false",
        "excludeDefaultComponents: true",
        "inheritCustomizations: false",
        "inheritMcp: false",
        "tools: []",
        "---",
    ]
    .join("\n");
    format!("{front}\n\n{system}")
}

#[must_use]
pub fn served_model(log_text: &str) -> Option<String> {
    let mut found = None;
    for caps in RESOLVED_RE.captures_iter(log_text) {
        if let Some(m) = caps.get(1).or_else(|| caps.get(2)) {
            found = Some(m.as_str().to_string());
        }
    }
    found
}

#[must_use]
pub fn recover_completed_agent_text(events: &[Map<String, Value>]) -> Option<String> {
    let final_step_index = events.iter().rev().find_map(agent_response_step_index)?;
    let mut text = CompletedStepText::default();
    for event in events {
        if agent_response_step_index(event) == Some(final_step_index) {
            text.append(event);
        }
    }
    text.finish()
}

fn agent_response_step_index(event: &Map<String, Value>) -> Option<i64> {
    if event.get("event").and_then(Value::as_str) != Some("step_update") {
        return None;
    }
    let update = event.get("step_update")?.as_object()?;
    if update.get("step_type").and_then(Value::as_str) != Some("agent_response") {
        return None;
    }
    update.get("step_index").and_then(Value::as_i64)
}

#[derive(Default)]
struct CompletedStepText {
    chunks: String,
    done: bool,
}

impl CompletedStepText {
    fn append(&mut self, event: &Map<String, Value>) {
        let Some(update) = event.get("step_update").and_then(Value::as_object) else {
            return;
        };
        if let Some(delta) = update.get("text_delta").and_then(Value::as_str) {
            self.chunks.push_str(delta);
        }
        if update.get("state").and_then(Value::as_str) == Some("DONE") {
            self.done = true;
        }
    }

    fn finish(self) -> Option<String> {
        if !self.done {
            return None;
        }
        let candidate = self.chunks.trim();
        (!candidate.is_empty()).then(|| candidate.to_string())
    }
}

fn recover_agent_text_from_jsonl(stdout: &str, final_step_index: Option<i64>) -> Option<String> {
    let final_step_index = final_step_index?;
    let mut text = CompletedStepText::default();
    visit_jsonl(stdout, |event| {
        if agent_response_step_index(event) == Some(final_step_index) {
            text.append(event);
        }
    });
    text.finish()
}

#[derive(Debug, Default, Clone, Copy)]
pub struct AgyAdapter;

impl CliAdapter for AgyAdapter {
    fn kind(&self) -> &'static str {
        "agy_cli"
    }

    fn env_drop(&self) -> &'static [&'static str] {
        &[
            "GOOGLE_API_KEY",
            "GOOGLE_GEMINI_BASE_URL",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_QUOTA_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_CLOUD_REGION",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_GENAI_USE_ENTERPRISE",
            "CLOUD_CODE_URL",
        ]
    }

    fn env_drop_prefixes(&self) -> &'static [&'static str] {
        &["GEMINI_", "AGY_", "ANTIGRAVITY_", "JETSKI_", "GOOGLE_"]
    }

    fn env_set(&self) -> &'static [(&'static str, &'static str)] {
        &[("AGY_CLI_DISABLE_AUTO_UPDATE", "true")]
    }

    fn isolate(&self, home: &Path) -> std::io::Result<Isolation> {
        if let Some(user_home) = user_home_dir() {
            let real_keychains = user_home.join("Library").join("Keychains");
            if real_keychains.is_dir() {
                let target = home.join("Library").join("Keychains");
                if let Some(parent) = target.parent() {
                    fs::create_dir_all(parent)?;
                }
                #[cfg(unix)]
                {
                    std::os::unix::fs::symlink(&real_keychains, &target)?;
                }
            }
        }
        let mut env = BTreeMap::new();
        env.insert("HOME".to_string(), home.to_string_lossy().into_owned());
        Ok(Isolation {
            env,
            links: Vec::new(),
        })
    }

    fn stale_outputs(&self, io_dir: &Path) -> Vec<PathBuf> {
        vec![io_dir.join(LOG_FILE)]
    }

    fn preflight(&self, request: &ModelRequest) -> Result<(), (&'static str, String)> {
        let bytes = render_transcript(&request.messages).len();
        if bytes > AGY_MAX_USER_MESSAGE_BYTES {
            return Err((
                AGY_CAPACITY_ERROR_CODE,
                format!(
                    "user message is {bytes} bytes, over the {AGY_MAX_USER_MESSAGE_BYTES}-byte agy capacity guard; refusing to send a message the CLI would truncate"
                ),
            ));
        }
        Ok(())
    }

    fn build(
        &self,
        config: &CliConfig,
        request: &ModelRequest,
        effort: Option<&str>,
        io_dir: &Path,
        _work_dir: &Path,
    ) -> Invocation {
        let mut notes = Map::new();
        let system = if request.system.trim().is_empty() {
            notes.insert(
                "substituted_system".to_string(),
                Value::String(NEUTRAL_SYSTEM.to_string()),
            );
            NEUTRAL_SYSTEM
        } else {
            &request.system
        };
        let mut msg_inner = Map::new();
        msg_inner.insert("role".to_string(), Value::String("user".to_string()));
        msg_inner.insert(
            "content".to_string(),
            Value::String(render_transcript(&request.messages)),
        );
        let mut msg_outer = Map::new();
        msg_outer.insert("event".to_string(), Value::String("user".to_string()));
        msg_outer.insert("message".to_string(), Value::Object(msg_inner));

        let mut argv = vec![
            config.binary.clone(),
            "--model".to_string(),
            config.model.clone(),
            "--agent".to_string(),
            AGENT_NAME.to_string(),
            "--disable-slash-commands".to_string(),
            "--input-format".to_string(),
            "stream-json".to_string(),
            "--output-format".to_string(),
            "stream-json".to_string(),
            "--log-file".to_string(),
            io_dir.join(LOG_FILE).to_string_lossy().into_owned(),
        ];
        if let Some(eff) = effort {
            argv.push("--effort".to_string());
            argv.push(eff.to_string());
        }
        argv.push("-p".to_string());
        argv.push(String::new());

        let stdin_line = dumps(
            &Value::Object(msg_outer),
            &DumpOptions {
                ensure_ascii: false,
                ..DumpOptions::default()
            },
        )
        .unwrap_or_else(|_| "{}".to_string());

        let mut home_files = BTreeMap::new();
        home_files.insert(AGENT_FILE.to_string(), agent_definition(system));

        Invocation {
            argv,
            stdin: Some(format!("{stdin_line}\n")),
            raw: notes,
            home_files,
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
        let mut result_payload: Option<Map<String, Value>> = None;
        let mut final_step_index = None;
        visit_jsonl(&result.stdout, |event| {
            if let Some(step_index) = agent_response_step_index(event) {
                final_step_index = Some(step_index);
            }
            if event.get("event").and_then(Value::as_str) == Some("result") {
                result_payload = Some(as_dict(event.get("result")));
            }
        });

        let Some(payload) = result_payload.as_ref() else {
            let mut failed = helper.fail("no result event on stdout", Some(result), Some(request));
            let mut meta = Map::new();
            meta.insert("cli_buffered_error".to_string(), Value::Bool(true));
            meta.insert(
                "error_code".to_string(),
                Value::String("CLI_NO_RESULT_EVENT".to_string()),
            );
            meta.insert(
                "error_class".to_string(),
                Value::String("AMBIGUOUS_FAILURE".to_string()),
            );
            failed.raw = Some(meta);
            return failed;
        };

        let usage_in = as_dict(payload.get("usage"));
        let cache_read = as_int(usage_in.get("cache_read_tokens"));
        let parsed = payload
            .get("structured_output")
            .and_then(Value::as_object)
            .cloned();
        let (text, cli_text) = structured_text(payload.get("response"), parsed.as_ref());

        let mut raw = Map::new();
        raw.insert("raw_usage".to_string(), Value::Object(usage_in.clone()));
        for key in RAW_KEYS {
            if let Some(val) = payload.get(*key) {
                raw.insert((*key).to_string(), val.clone());
            }
        }
        if let Some(ct) = cli_text {
            raw.insert("cli_text".to_string(), Value::String(ct));
        }

        let log_path = io_dir.join(LOG_FILE);
        let (log_text, provider_log_rejected) = match read_sidecar_text(&log_path) {
            Ok(text) => (text, false),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => (String::new(), false),
            Err(_) => (String::new(), true),
        };
        if provider_log_rejected {
            raw.insert("provider_log_rejected".to_string(), Value::Bool(true));
        }

        let mut response = ModelResponse {
            text,
            parsed,
            usage: Usage {
                input_tokens: as_int(usage_in.get("input_tokens")).saturating_sub(cache_read),
                output_tokens: as_int(usage_in.get("output_tokens")),
                cache_read_tokens: cache_read,
                cache_write_tokens: 0,
                reasoning_tokens: as_int(usage_in.get("thinking_tokens")),
            },
            served_model: served_model(&log_text),
            raw: if raw.is_empty() { None } else { Some(raw) },
            ..ModelResponse::default()
        };

        let status = payload
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("None");

        if status != "SUCCESS" {
            let err_str = payload.get("error").and_then(Value::as_str).unwrap_or("");
            if result.returncode == 0
                && err_str
                    .to_lowercase()
                    .contains("function call is empty - no input to parse")
                && let Some(recovered) =
                    recover_agent_text_from_jsonl(&result.stdout, final_step_index)
            {
                response.text = Some(recovered);
                response.parsed = None;
                let mut meta = response.raw.take().unwrap_or_default();
                meta.insert(
                    "recovered_empty_function_call".to_string(),
                    Value::Bool(true),
                );
                response.raw = Some(meta);
                return response;
            }

            let message = payload
                .get("error")
                .and_then(Value::as_str)
                .map(str::to_string)
                .unwrap_or_else(|| format!("status {status:?}"));
            response.error = Some(helper.error_text(&message, Some(result), Some(request)));
            let haystack = format!("{message}\n{log_text}").to_lowercase();
            let mut meta = response.raw.take().unwrap_or_default();
            meta.insert("cli_buffered_error".to_string(), Value::Bool(true));

            if [
                "malformed function call",
                "improperly formatted function call",
                "function call",
                "unknown tool",
            ]
            .iter()
            .any(|needle| haystack.contains(needle))
            {
                meta.insert(
                    "error_code".to_string(),
                    Value::String("CLI_MALFORMED_FUNCTION_CALL".to_string()),
                );
            } else if [
                "429",
                "rate limit",
                "resource_exhausted",
                "at capacity",
                "overloaded",
                "503",
                "unavailable",
            ]
            .iter()
            .any(|needle| haystack.contains(needle))
            {
                meta.insert(
                    "error_code".to_string(),
                    Value::String("CLI_TRANSIENT_CAPACITY".to_string()),
                );
                meta.insert(
                    "error_class".to_string(),
                    Value::String("TRANSIENT_FAILURE".to_string()),
                );
            } else if haystack.contains("quota") || haystack.contains("billing") {
                meta.insert(
                    "error_code".to_string(),
                    Value::String("CLI_QUOTA".to_string()),
                );
                meta.insert(
                    "error_class".to_string(),
                    Value::String("QUOTA_INTERRUPTION".to_string()),
                );
            } else {
                meta.insert(
                    "error_code".to_string(),
                    Value::String(format!("CLI_STATUS_{status}")),
                );
            }
            response.raw = Some(meta);
        } else if result.returncode != 0 {
            response.error = Some(helper.error_text(
                "nonzero exit after a success result",
                Some(result),
                Some(request),
            ));
        }

        response
    }
}

pub type AgyCliProvider = CliProvider<AgyAdapter>;
