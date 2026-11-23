//! `claude -p` (Claude Code, headless) provider.

use std::path::Path;

use ib_core::pyjson::{DumpOptions, dumps};
use serde_json::{Map, Value};

use crate::base::{ModelRequest, ModelResponse, Usage};
use crate::cli::{
    ACTION_SCHEMA_NAME, CliAdapter, CliConfig, CliProvider, Invocation, as_dict, as_float, as_int,
    parse_json_payload, render_transcript, served_models, structured_text, system_override,
};
use crate::subprocess::ProcessResult;

const RAW_KEYS: &[&str] = &[
    "subtype",
    "num_turns",
    "duration_ms",
    "stop_reason",
    "session_id",
];

#[derive(Debug, Default, Clone, Copy)]
pub struct ClaudeAdapter;

impl CliAdapter for ClaudeAdapter {
    fn kind(&self) -> &'static str {
        "claude_cli"
    }

    fn env_drop(&self) -> &'static [&'static str] {
        &[
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDECODE",
            "CLAUDE_PID",
            "CLAUDE_EFFORT",
        ]
    }

    fn env_drop_prefixes(&self) -> &'static [&'static str] {
        &["ANTHROPIC_", "CLAUDE_"]
    }

    fn env_keep(&self) -> &'static [&'static str] {
        &["CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"]
    }

    fn env_set(&self) -> &'static [(&'static str, &'static str)] {
        &[("DISABLE_AUTOUPDATER", "1")]
    }

    fn build(
        &self,
        config: &CliConfig,
        request: &ModelRequest,
        effort: Option<&str>,
        _io_dir: &Path,
        _work_dir: &Path,
    ) -> Invocation {
        let (system, body, mut notes) =
            system_override(&request.system, &render_transcript(&request.messages), "");
        let mut argv = vec![
            config.binary.clone(),
            "-p".to_string(),
            "--model".to_string(),
            config.model.clone(),
            "--system-prompt".to_string(),
            system,
            "--tools".to_string(),
            String::new(),
            "--strict-mcp-config".to_string(),
            "--setting-sources".to_string(),
            String::new(),
            "--no-session-persistence".to_string(),
            "--safe-mode".to_string(),
            "--disable-slash-commands".to_string(),
            "--no-chrome".to_string(),
            "--output-format".to_string(),
            "json".to_string(),
        ];
        if let Some(schema) = &request.output_schema {
            if request.schema_name != ACTION_SCHEMA_NAME {
                let schema_json = dumps(&Value::Object(schema.clone()), &DumpOptions::default())
                    .unwrap_or_else(|_| "{}".to_string());
                argv.push("--json-schema".to_string());
                argv.push(schema_json);
            } else {
                notes.insert(
                    "json_schema".to_string(),
                    Value::String("withheld (JSON action protocol turn)".to_string()),
                );
            }
        }
        if let Some(eff) = effort {
            argv.push("--effort".to_string());
            argv.push(eff.to_string());
        }
        Invocation {
            argv,
            stdin: Some(body),
            raw: notes,
            home_files: Default::default(),
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

        let usage_in = as_dict(payload.get("usage"));
        let usage = Usage {
            input_tokens: as_int(usage_in.get("input_tokens")),
            output_tokens: as_int(usage_in.get("output_tokens")),
            cache_read_tokens: as_int(usage_in.get("cache_read_input_tokens")),
            cache_write_tokens: as_int(usage_in.get("cache_creation_input_tokens")),
            reasoning_tokens: as_int(
                as_dict(usage_in.get("output_tokens_details")).get("thinking_tokens"),
            ),
        };
        let subtype = payload.get("subtype").and_then(Value::as_str);
        let stop_reason = payload
            .get("stop_reason")
            .and_then(Value::as_str)
            .map(str::to_string);

        let mut raw = Map::new();
        raw.insert("raw_usage".to_string(), Value::Object(usage_in));
        for key in RAW_KEYS {
            if let Some(val) = payload.get(*key) {
                raw.insert((*key).to_string(), val.clone());
            }
        }

        let mut response = ModelResponse {
            usage,
            stop_reason,
            served_model: served_models(payload.get("modelUsage")),
            reported_cost_usd: as_float(payload.get("total_cost_usd")),
            raw: Some(raw),
            ..ModelResponse::default()
        };

        let is_err = payload
            .get("is_error")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if is_err || subtype.is_some_and(|s| s != "success") {
            let message = payload
                .get("result")
                .or_else(|| payload.get("error"))
                .and_then(Value::as_str)
                .unwrap_or("the CLI reported an error");
            let label = subtype.unwrap_or("error");
            response.error = Some(helper.error_text(
                &format!("{label}: {message}"),
                Some(result),
                Some(request),
            ));
            return response;
        }
        if response.stop_reason.as_deref() == Some("refusal") {
            response.refused = true;
        }

        response.parsed = payload
            .get("structured_output")
            .and_then(Value::as_object)
            .cloned();
        let (text, cli_text) = structured_text(payload.get("result"), response.parsed.as_ref());
        response.text = text;
        if let Some(ct) = cli_text
            && let Some(ref mut r) = response.raw
        {
            r.insert("cli_text".to_string(), Value::String(ct));
        }
        if result.returncode != 0 {
            response.error = Some(helper.error_text(
                "nonzero exit after a success result",
                Some(result),
                Some(request),
            ));
        }
        response
    }
}

pub type ClaudeCliProvider = CliProvider<ClaudeAdapter>;
