//! `grok` (Grok Build CLI, headless single-turn) provider.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use ib_core::pyjson::{DumpOptions, dumps};
use serde_json::{Map, Value};

use crate::base::{ModelRequest, ModelResponse, Usage};
use crate::cli::{
    ACTION_SCHEMA_NAME, CliAdapter, CliConfig, CliProvider, Invocation, Isolation, as_dict,
    as_float, as_int, link_credentials, parse_json_payload, render_transcript, served_models,
    structured_text, system_override,
};
use crate::subprocess::{ProcessResult, user_home_dir};

pub const PROMPT_FILE: &str = "prompt.md";
pub const SYSTEM_FLAG: &str = "--system-prompt-override=";
pub const ALLOWLISTED_TOOL: &str = "read_file";
pub const DISALLOWED_TOOLS: &[&str] = &[ALLOWLISTED_TOOL, "search_tool", "use_tool"];
const RAW_KEYS: &[&str] = &["num_turns", "session_id", "duration_ms"];

#[must_use]
pub fn real_grok_home() -> PathBuf {
    if let Ok(configured) = std::env::var("GROK_HOME")
        && !configured.is_empty()
    {
        return PathBuf::from(configured);
    }
    user_home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".grok")
}

#[derive(Debug, Default, Clone, Copy)]
pub struct GrokAdapter;

impl CliAdapter for GrokAdapter {
    fn kind(&self) -> &'static str {
        "grok_cli"
    }

    fn env_drop(&self) -> &'static [&'static str] {
        &["XAI_API_KEY"]
    }

    fn env_drop_prefixes(&self) -> &'static [&'static str] {
        &["GROK_"]
    }

    fn isolate(&self, home: &Path) -> std::io::Result<Isolation> {
        let private = home.join(".grok");
        let links = link_credentials(
            &real_grok_home(),
            &private,
            &["auth.json"],
            &["auth.json.lock"],
        )?;
        let bundled_skills = dumps(
            &Value::String(
                private
                    .join("bundled")
                    .join("skills")
                    .to_string_lossy()
                    .into_owned(),
            ),
            &DumpOptions::default(),
        )
        .unwrap_or_else(|_| "\"\"".to_string());
        fs::write(
            private.join("config.toml"),
            format!("[skills]\nignore = [{bundled_skills}]\n"),
        )?;

        let mut env = BTreeMap::new();
        env.insert("HOME".to_string(), home.to_string_lossy().into_owned());
        env.insert(
            "GROK_HOME".to_string(),
            private.to_string_lossy().into_owned(),
        );
        env.insert("GROK_MEMORY".to_string(), "0".to_string());
        env.insert("GROK_SUBAGENTS".to_string(), "0".to_string());
        env.insert("GROK_WEB_FETCH".to_string(), "0".to_string());
        Ok(Isolation { env, links })
    }

    fn build(
        &self,
        config: &CliConfig,
        request: &ModelRequest,
        effort: Option<&str>,
        io_dir: &Path,
        _work_dir: &Path,
    ) -> Invocation {
        let (system, body, mut notes) = system_override(
            &request.system,
            &render_transcript(&request.messages),
            SYSTEM_FLAG,
        );
        let prompt_path = io_dir.join(PROMPT_FILE);
        let _ = fs::write(&prompt_path, body);

        let mut argv = vec![
            config.binary.clone(),
            "-m".to_string(),
            config.model.clone(),
            "--prompt-file".to_string(),
            prompt_path.to_string_lossy().into_owned(),
            format!("{SYSTEM_FLAG}{system}"),
            "--tools".to_string(),
            ALLOWLISTED_TOOL.to_string(),
            "--disallowed-tools".to_string(),
            DISALLOWED_TOOLS.join(","),
            "--disable-web-search".to_string(),
            "--no-subagents".to_string(),
            "--no-plan".to_string(),
            "--no-auto-update".to_string(),
            "--max-turns".to_string(),
            "2".to_string(),
            "--verbatim".to_string(),
            "--output-format".to_string(),
            "json".to_string(),
        ];

        if let Some(schema) = &request.output_schema {
            if request.schema_name != ACTION_SCHEMA_NAME {
                let schema_json = dumps(&Value::Object(schema.clone()), &DumpOptions::default())
                    .unwrap_or_else(|_| "{}".to_string());
                argv.push(format!("--json-schema={schema_json}"));
            } else {
                notes.insert(
                    "json_schema".to_string(),
                    Value::String("withheld (JSON action protocol turn)".to_string()),
                );
            }
        }
        if let Some(eff) = effort {
            argv.push(format!("--reasoning-effort={eff}"));
        }

        Invocation {
            argv,
            stdin: None,
            raw: notes,
            home_files: BTreeMap::new(),
        }
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
        if payload.get("type").and_then(Value::as_str) == Some("error")
            || payload
                .get("is_error")
                .and_then(Value::as_bool)
                .unwrap_or(false)
        {
            let message = payload
                .get("message")
                .or_else(|| payload.get("error"))
                .and_then(Value::as_str)
                .unwrap_or("the CLI reported an error");
            return helper.fail(message, Some(result), Some(request));
        }

        let usage_in = as_dict(payload.get("usage"));
        let stop_reason = payload.get("stopReason").and_then(Value::as_str);
        let parsed = payload
            .get("structuredOutput")
            .and_then(Value::as_object)
            .cloned();
        let (text, cli_text) = structured_text(payload.get("text"), parsed.as_ref());

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

        let mut response = ModelResponse {
            text,
            parsed,
            stop_reason: stop_reason.map(str::to_string),
            refused: stop_reason == Some("refusal"),
            usage: Usage {
                input_tokens: as_int(usage_in.get("input_tokens")),
                output_tokens: as_int(usage_in.get("output_tokens")),
                cache_read_tokens: as_int(usage_in.get("cache_read_input_tokens")),
                cache_write_tokens: as_int(usage_in.get("cache_creation_input_tokens")),
                reasoning_tokens: as_int(usage_in.get("reasoning_tokens")),
            },
            served_model: served_models(payload.get("modelUsage")),
            reported_cost_usd: as_float(payload.get("total_cost_usd")),
            raw: if raw.is_empty() { None } else { Some(raw) },
            ..ModelResponse::default()
        };

        if result.returncode != 0 {
            response.error = Some(helper.error_text("nonzero exit", Some(result), Some(request)));
        }
        response
    }
}

pub type GrokCliProvider = CliProvider<GrokAdapter>;
