//! Muse Code subscription calls, isolated from personal context and tools.

use std::collections::BTreeMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use ib_core::pyjson::{DumpOptions, dumps};
use serde_json::{Map, Value};

use crate::base::{ModelRequest, ModelResponse, Usage};
use crate::cli::{
    CliAdapter, CliConfig, CliProvider, Invocation, Isolation, NEUTRAL_SYSTEM, as_dict, as_int,
    link_credentials, parse_jsonl, render_transcript,
};
use crate::json::load_object;
use crate::subprocess::{ProcessResult, user_home_dir};

pub const DEFAULT_EFFORT: &str = "medium";
pub const SETTINGS: &str = ".config/muse/settings.json";
pub const BUNDLED_SKILLS: &[&str] = &[
    "blender",
    "browser-app-delivery",
    "create-plugin",
    "create-skill",
    "doctor",
    "durable-test-collateral",
    "git",
    "greenfield-project-scaffolding",
    "grill",
    "import",
    "manage-settings",
    "migrate",
    "plan",
    "python-env",
    "read-session",
    "requirements-clarification",
    "resume-claude",
    "resume-codex",
    "table-fit",
    "taste",
    "workflow-authoring",
];

#[must_use]
pub fn settings(system: &str) -> Map<String, Value> {
    let sys_prompt = {
        let t = system.trim();
        if t.is_empty() { NEUTRAL_SYSTEM } else { t }
    };

    let mut telemetry = Map::new();
    telemetry.insert("enabled".to_string(), Value::Bool(false));

    let mut run = Map::new();
    run.insert(
        "system_prompt".to_string(),
        Value::String(sys_prompt.to_string()),
    );
    run.insert("developer_prompt".to_string(), Value::String(String::new()));

    let mut bundled = Map::new();
    for name in BUNDLED_SKILLS {
        bundled.insert(
            format!("bundled://muse-core/skills/{name}/SKILL.md"),
            Value::String("off".to_string()),
        );
    }
    let mut plugin = Map::new();
    plugin.insert(
        "plugin://threejs/skills/threejs/SKILL.md".to_string(),
        Value::String("off".to_string()),
    );
    let mut activation = Map::new();
    activation.insert("bundled".to_string(), Value::Object(bundled));
    activation.insert("plugin".to_string(), Value::Object(plugin));
    let mut skills = Map::new();
    skills.insert("activation".to_string(), Value::Object(activation));

    let mut out = Map::new();
    out.insert("schema_version".to_string(), Value::from(1));
    out.insert("contributor_mode_enabled".to_string(), Value::Bool(false));
    out.insert("telemetry".to_string(), Value::Object(telemetry));
    out.insert("run".to_string(), Value::Object(run));
    out.insert("skills".to_string(), Value::Object(skills));
    out
}

#[must_use]
pub fn trace_events(stderr: &str) -> Vec<Map<String, Value>> {
    let mut events = Vec::new();
    for line in stderr.lines() {
        if !line.starts_with("SSE RESPONSE: ") {
            continue;
        }
        if let Some((_, data)) = line.split_once("data: ")
            && let Some(event) = load_object(data)
        {
            events.push(event);
        }
    }
    events
}

#[derive(Debug, Default, Clone)]
pub struct MuseAdapter {
    pub real_home: Option<PathBuf>,
}

impl CliAdapter for MuseAdapter {
    fn kind(&self) -> &'static str {
        "muse_cli"
    }

    fn env_drop_prefixes(&self) -> &'static [&'static str] {
        &["META_", "MUSE_", "OPENAI_", "ANTHROPIC_", "XDG_"]
    }

    fn env_set(&self) -> &'static [(&'static str, &'static str)] {
        &[("MUSE_TRANSPORT_TRACE", "1")]
    }

    fn isolate(&self, home: &Path) -> io::Result<Isolation> {
        let user_home = self
            .real_home
            .clone()
            .or_else(user_home_dir)
            .unwrap_or_default();
        let real = user_home.join(".config").join("muse");
        let auth = real.join("auth.json");
        if auth.exists() {
            let text = fs::read_to_string(&auth)?;
            let data = load_object(&text).unwrap_or_default();
            let providers = as_dict(data.get("providers"));
            let meta = as_dict(providers.get("meta"));
            if meta.get("mechanism").and_then(Value::as_str) != Some("oauth")
                || meta.get("subs_active").and_then(Value::as_bool) != Some(true)
            {
                return Err(io::Error::other(
                    "Muse requires an active OAuth subscription login",
                ));
            }
            if meta.get("api_base_url").and_then(Value::as_str) != Some("https://api.meta.ai/v1") {
                return Err(io::Error::other(
                    "Muse subscription login has an unexpected API endpoint",
                ));
            }
        }
        let mut links = link_credentials(
            &real,
            &home.join(".config").join("muse"),
            &["auth.json"],
            &[".auth.json.lock"],
        )?;
        let keychains = user_home.join("Library").join("Keychains");
        if keychains.is_dir() {
            let mut kc_links = link_credentials(
                &keychains,
                &home.join("Library").join("Keychains"),
                &[],
                &["login.keychain-db", "login.keychain"],
            )?;
            links.append(&mut kc_links);
        }
        let mut env = BTreeMap::new();
        env.insert("HOME".to_string(), home.to_string_lossy().into_owned());
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
        let prompt = io_dir.join("prompt.txt");
        let _ = fs::write(&prompt, render_transcript(&request.messages));
        let mut argv = vec![
            config.binary.clone(),
            "exec".to_string(),
            "--provider".to_string(),
            "meta".to_string(),
            "--model".to_string(),
            config.model.clone(),
            "--preset".to_string(),
            "empty".to_string(),
            "--json".to_string(),
            "--contributor-mode".to_string(),
            "off".to_string(),
            "--no-session-log".to_string(),
            "--disable-write".to_string(),
            "--disable-shell".to_string(),
            "--disable-web-tools".to_string(),
            "--disable-reminders".to_string(),
            "--disable-request-user-input".to_string(),
            "--no-foreign-personal-context".to_string(),
            "--approval-judge".to_string(),
            "off".to_string(),
            "--max-model-steps".to_string(),
            "1".to_string(),
            "--prompt-file".to_string(),
            prompt.to_string_lossy().into_owned(),
            "--reasoning-effort".to_string(),
            effort.unwrap_or(DEFAULT_EFFORT).to_string(),
        ];
        if let Some(schema) = &request.output_schema {
            let schema_path = io_dir.join("schema.json");
            let schema_json = dumps(&Value::Object(schema.clone()), &DumpOptions::default())
                .unwrap_or_else(|_| "{}".to_string());
            let _ = fs::write(&schema_path, schema_json);
            argv.push("--output-schema".to_string());
            argv.push(schema_path.to_string_lossy().into_owned());
        }

        let mut raw = Map::new();
        raw.insert(
            "billing_route".to_string(),
            Value::String("muse_code_subscription".to_string()),
        );
        raw.insert("contributor_mode".to_string(), Value::Bool(false));
        raw.insert(
            "system_outer_whitespace_normalized".to_string(),
            Value::Bool(true),
        );
        raw.insert(
            "cli_scaffolding".to_string(),
            Value::String("workspace, permissions, muse-core reminders".to_string()),
        );
        if request.system.trim().is_empty() {
            raw.insert(
                "substituted_system".to_string(),
                Value::String(NEUTRAL_SYSTEM.to_string()),
            );
        }

        let mut home_files = BTreeMap::new();
        let settings_json = dumps(
            &Value::Object(settings(&request.system)),
            &DumpOptions::default(),
        )
        .unwrap_or_else(|_| "{}".to_string());
        home_files.insert(SETTINGS.to_string(), settings_json);

        Invocation {
            argv,
            stdin: None,
            raw,
            home_files,
        }
    }

    fn parse(
        &self,
        config: &CliConfig,
        result: &ProcessResult,
        request: &ModelRequest,
        _io_dir: &Path,
    ) -> ModelResponse {
        let helper = CliProvider::new(self.clone(), CliConfig::new("", "", 0.0));
        let terminals: Vec<Map<String, Value>> = parse_jsonl(&result.stdout)
            .into_iter()
            .filter(|e| {
                e.get("payload_type")
                    .and_then(Value::as_str)
                    .is_some_and(|s| s.starts_with("run.terminal."))
            })
            .map(|e| as_dict(e.get("payload")))
            .collect();

        let events = trace_events(&result.stderr);
        let responses: Vec<Map<String, Value>> = events
            .iter()
            .filter(|e| {
                matches!(
                    e.get("type").and_then(Value::as_str),
                    Some("response.completed" | "response.incomplete")
                )
            })
            .map(|e| as_dict(e.get("response")))
            .collect();

        let terminal = terminals.last().cloned().unwrap_or_default();
        let payload = responses.last().cloned().unwrap_or_default();
        let usage = as_dict(payload.get("usage"));
        let cached = as_int(as_dict(usage.get("input_tokens_details")).get("cached_tokens"));
        let subscription: Vec<Map<String, Value>> = events
            .iter()
            .filter(|e| {
                e.get("type").and_then(Value::as_str) == Some("response.subscription_usage")
            })
            .map(|e| as_dict(e.get("subscription")))
            .collect();

        let text = terminal.get("text").and_then(Value::as_str);

        let sub_usage = subscription.last().map(|sub| {
            let mut m = Map::new();
            for (k, v) in sub {
                let keep = match v {
                    Value::Bool(_) => true,
                    Value::Number(n) => n.as_f64().is_none_or(f64::is_finite),
                    _ => false,
                };
                if keep {
                    m.insert(k.clone(), v.clone());
                }
            }
            Value::Object(m)
        });

        let mut raw = Map::new();
        raw.insert("raw_usage".to_string(), Value::Object(usage.clone()));
        raw.insert(
            "subscription_usage".to_string(),
            sub_usage.unwrap_or(Value::Null),
        );

        let mut response = ModelResponse {
            text: text.map(str::to_string),
            parsed: text.and_then(load_object),
            served_model: payload
                .get("model")
                .and_then(Value::as_str)
                .map(str::to_string),
            stop_reason: payload
                .get("status")
                .and_then(Value::as_str)
                .map(str::to_string),
            usage: Usage {
                input_tokens: as_int(usage.get("input_tokens")).saturating_sub(cached),
                output_tokens: as_int(usage.get("output_tokens")),
                cache_read_tokens: cached,
                cache_write_tokens: 0,
                reasoning_tokens: as_int(
                    as_dict(usage.get("output_tokens_details")).get("reasoning_tokens"),
                ),
            },
            raw: Some(raw),
            ..ModelResponse::default()
        };

        let mut problem: Option<String> = None;
        if result.returncode != 0
            || terminal.get("terminal").and_then(Value::as_str) != Some("completed")
        {
            let reason = terminal
                .get("reason")
                .and_then(Value::as_str)
                .unwrap_or("no completed terminal event");
            problem = Some(format!("exit {}: {reason}", result.returncode));
        } else if responses.len() != 1 {
            problem = Some("expected exactly one provider response".to_string());
        } else if payload.get("model").and_then(Value::as_str) != Some(config.model.as_str()) {
            problem = Some("served model did not match the requested model".to_string());
        } else if subscription.is_empty() {
            problem = Some("subscription accounting was not reported".to_string());
        } else if payload.get("status").and_then(Value::as_str) != Some("completed") {
            problem = Some("incomplete model response".to_string());
        }

        let requests: Vec<Option<Map<String, Value>>> = result
            .stderr
            .lines()
            .filter_map(|line| line.strip_prefix("REQUEST: "))
            .map(load_object)
            .collect();
        if requests.len() != 1 || requests[0].is_none() {
            if problem.is_none() {
                problem = Some("expected exactly one traced provider request".to_string());
            }
        } else if let Some(req_obj) = requests[0].as_ref() {
            let has_tools = req_obj.get("tools").is_some_and(|v| match v {
                Value::Array(a) => !a.is_empty(),
                Value::Bool(b) => *b,
                Value::Null => false,
                _ => true,
            });
            let expected_sys = {
                let t = request.system.trim();
                if t.is_empty() { NEUTRAL_SYSTEM } else { t }
            };
            if has_tools {
                problem = Some("unexpected tools in provider request".to_string());
            } else if req_obj.get("instructions").and_then(Value::as_str) != Some(expected_sys) {
                problem = Some("benchmark system instructions were changed".to_string());
            }
        }

        if let Some(prob) = problem {
            response.error = Some(helper.error_text(&prob, None, None));
        }
        response
    }
}

pub type MuseCliProvider = CliProvider<MuseAdapter>;
