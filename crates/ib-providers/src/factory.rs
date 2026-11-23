//! Model specs (one `models:` entry of an experiment config, SPEC §16) and the provider factory.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use ib_core::pyjson::{PyNumber, classify_number};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::anthropic_api::{AnthropicConfig, AnthropicProvider};
use crate::base::{GenerationParams, Provider};
use crate::cli::CliConfig;
use crate::cli::agy::{AgyAdapter, AgyCliProvider};
use crate::cli::claude::{ClaudeAdapter, ClaudeCliProvider};
use crate::cli::codex::{CodexAdapter, CodexCliProvider, DEFAULT_EFFORT as CODEX_DEFAULT_EFFORT};
use crate::cli::cursor::{CursorAdapter, CursorCliProvider, resolve_cursor_binary};
use crate::cli::grok::{GrokAdapter, GrokCliProvider};
use crate::cli::muse::{DEFAULT_EFFORT as MUSE_DEFAULT_EFFORT, MuseAdapter, MuseCliProvider};
use crate::openai_compat::{
    MaxTokensParam, OpenAICompatConfig, OpenAICompatProvider, OpenAiApi, headers_problem,
    provider_routing_problem,
};
use crate::replay::{ReplayError, ReplayProvider};

pub const PROVIDER_KINDS: &[&str] = &[
    "anthropic",
    "anthropic_compatible",
    "openai_compatible",
    "claude_cli",
    "codex_cli",
    "grok_cli",
    "agy_cli",
    "cursor_cli",
    "muse_cli",
    "replay",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ProviderKind {
    Anthropic,
    AnthropicCompatible,
    OpenaiCompatible,
    ClaudeCli,
    CodexCli,
    GrokCli,
    AgyCli,
    CursorCli,
    MuseCli,
    Replay,
}

impl ProviderKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Anthropic => "anthropic",
            Self::AnthropicCompatible => "anthropic_compatible",
            Self::OpenaiCompatible => "openai_compatible",
            Self::ClaudeCli => "claude_cli",
            Self::CodexCli => "codex_cli",
            Self::GrokCli => "grok_cli",
            Self::AgyCli => "agy_cli",
            Self::CursorCli => "cursor_cli",
            Self::MuseCli => "muse_cli",
            Self::Replay => "replay",
        }
    }

    #[must_use]
    pub const fn is_api_kind(self) -> bool {
        matches!(
            self,
            Self::Anthropic | Self::AnthropicCompatible | Self::OpenaiCompatible
        )
    }

    #[must_use]
    pub const fn default_effort(self) -> Option<&'static str> {
        match self {
            Self::CodexCli => Some(CODEX_DEFAULT_EFFORT),
            Self::MuseCli => Some(MUSE_DEFAULT_EFFORT),
            _ => None,
        }
    }

    #[must_use]
    pub fn allowed_extra(self) -> &'static [&'static str] {
        match self {
            Self::Anthropic | Self::AnthropicCompatible => {
                &["max_retries", "allow_custom_endpoint"]
            }
            Self::OpenaiCompatible => &[
                "max_retries",
                "allow_custom_endpoint",
                "reasoning_effort_param",
                "max_tokens_param",
                "provider_routing",
                "headers",
                "api",
            ],
            Self::ClaudeCli
            | Self::CodexCli
            | Self::GrokCli
            | Self::AgyCli
            | Self::CursorCli
            | Self::MuseCli => &["binary", "max_retries"],
            Self::Replay => &["path"],
        }
    }
}

#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum FactoryError {
    #[error("{0}")]
    Validation(String),
    #[error("provider configuration failed: {0}")]
    Provider(String),
    #[error("replay load failed: {0}")]
    Replay(#[from] ReplayError),
}

fn default_max_output_tokens() -> u64 {
    4096
}

/// One model configuration under test (SPEC §16).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelSpec {
    pub provider: ProviderKind,
    pub model: String,
    #[serde(default)]
    pub label: Option<String>,
    #[serde(default)]
    pub base_url_env: Option<String>,
    #[serde(default)]
    pub api_key_env: Option<String>,
    #[serde(default)]
    pub cache_namespace: Option<String>,
    #[serde(default)]
    pub effort: Option<String>,
    #[serde(default)]
    pub temperature: Option<f64>,
    #[serde(default = "default_max_output_tokens")]
    pub max_output_tokens: u64,
    #[serde(default)]
    pub timeout_s: Option<f64>,
    #[serde(default)]
    pub concurrency: Option<u64>,
    #[serde(default)]
    pub extra: Map<String, Value>,
}

impl ModelSpec {
    #[must_use]
    pub fn new(provider: ProviderKind, model: impl Into<String>) -> Self {
        Self {
            provider,
            model: model.into(),
            label: None,
            base_url_env: None,
            api_key_env: None,
            cache_namespace: None,
            effort: None,
            temperature: None,
            max_output_tokens: default_max_output_tokens(),
            timeout_s: None,
            concurrency: None,
            extra: Map::new(),
        }
    }

    pub fn from_yaml_str(yaml_text: &str) -> Result<Self, FactoryError> {
        let spec: Self = serde_norway::from_str(yaml_text)
            .map_err(|e| FactoryError::Validation(e.to_string()))?;
        spec.validate()?;
        Ok(spec)
    }

    pub fn from_json_value(value: Value) -> Result<Self, FactoryError> {
        let spec: Self =
            serde_json::from_value(value).map_err(|e| FactoryError::Validation(e.to_string()))?;
        spec.validate()?;
        Ok(spec)
    }

    pub fn validate(&self) -> Result<(), FactoryError> {
        if self.model.is_empty() {
            return Err(FactoryError::Validation(
                "model must be a non-empty string".to_string(),
            ));
        }
        if self.model.starts_with('-')
            || self
                .model
                .chars()
                .any(|c| (c as u32) < 32 || (c as u32) == 127)
        {
            return Err(FactoryError::Validation(format!(
                "invalid model name {:?}",
                self.model
            )));
        }
        if let Some(ref eff) = self.effort
            && (eff.starts_with('-') || eff.chars().any(|c| (c as u32) < 32 || (c as u32) == 127))
        {
            return Err(FactoryError::Validation(format!(
                "invalid effort value {eff:?}"
            )));
        }
        if self.max_output_tokens == 0 {
            return Err(FactoryError::Validation(
                "max_output_tokens must be greater than 0".to_string(),
            ));
        }
        if let Some(t) = self.timeout_s {
            crate::base::provider_timeout(t).map_err(|err| FactoryError::Validation(err.into()))?;
        }
        if let Some(c) = self.concurrency
            && c == 0
        {
            return Err(FactoryError::Validation(
                "concurrency must be greater than 0".to_string(),
            ));
        }

        let kind = self.provider;
        let kind_str = kind.as_str();
        if !kind.is_api_kind() {
            if self.base_url_env.is_some() {
                return Err(FactoryError::Validation(format!(
                    "base_url_env does not apply to provider {kind_str:?}"
                )));
            }
            if self.api_key_env.is_some() {
                return Err(FactoryError::Validation(format!(
                    "api_key_env does not apply to provider {kind_str:?}"
                )));
            }
            if self.cache_namespace.is_some() {
                return Err(FactoryError::Validation(format!(
                    "cache_namespace does not apply to provider {kind_str:?}"
                )));
            }
        }
        if kind == ProviderKind::Anthropic && self.base_url_env.is_some() {
            return Err(FactoryError::Validation(
                "provider 'anthropic' takes no base_url_env; use anthropic_compatible".to_string(),
            ));
        }
        if kind == ProviderKind::AnthropicCompatible && self.base_url_env.is_none() {
            return Err(FactoryError::Validation(
                "provider 'anthropic_compatible' requires base_url_env".to_string(),
            ));
        }
        if kind == ProviderKind::CursorCli && self.effort.is_some() {
            return Err(FactoryError::Validation(
                "provider 'cursor_cli' has no effort setting".to_string(),
            ));
        }
        if kind == ProviderKind::Replay && !self.extra.contains_key("path") {
            return Err(FactoryError::Validation(
                "provider 'replay' requires extra.path (a responses.jsonl file)".to_string(),
            ));
        }

        let allowed: BTreeSet<&str> = kind.allowed_extra().iter().copied().collect();
        let mut unknown: Vec<&str> = self
            .extra
            .keys()
            .map(String::as_str)
            .filter(|k| !allowed.contains(k))
            .collect();
        if !unknown.is_empty() {
            unknown.sort_unstable();
            let mut allowed_sorted: Vec<&str> = allowed.into_iter().collect();
            allowed_sorted.sort_unstable();
            return Err(FactoryError::Validation(format!(
                "unknown extra options for {kind_str:?}: {unknown:?} (allowed={allowed_sorted:?})"
            )));
        }

        for (name, value) in &self.extra {
            if let Some(prob) = extra_problem(name, value) {
                return Err(FactoryError::Validation(format!(
                    "extra.{name} {prob} (got {value})"
                )));
            }
        }
        Ok(())
    }

    #[must_use]
    pub fn display_label(&self) -> String {
        self.label
            .clone()
            .unwrap_or_else(|| format!("{}:{}", self.provider.as_str(), self.model))
    }

    #[must_use]
    pub fn effective_effort(&self) -> Option<&str> {
        self.effort
            .as_deref()
            .or_else(|| self.provider.default_effort())
    }

    #[must_use]
    pub fn generation_params(&self) -> GenerationParams {
        GenerationParams {
            max_output_tokens: self.max_output_tokens,
            temperature: self.temperature,
            effort: self.effective_effort().map(str::to_string),
            extra: Map::new(),
        }
    }
}

fn extra_problem(name: &str, value: &Value) -> Option<String> {
    match name {
        "max_retries" => match value {
            Value::Number(n) => match classify_number(n) {
                PyNumber::Int(s) if !s.starts_with('-') && s.parse::<u32>().is_ok() => None,
                _ => Some("must be a non-negative integer".to_string()),
            },
            _ => Some("must be a non-negative integer".to_string()),
        },
        "reasoning_effort_param" => {
            if value.is_boolean() {
                None
            } else {
                Some("must be true or false".to_string())
            }
        }
        "allow_custom_endpoint" => {
            if value.is_boolean() {
                None
            } else {
                Some("must be true or false".to_string())
            }
        }
        "max_tokens_param" => {
            if matches!(value.as_str(), Some("max_completion_tokens" | "max_tokens")) {
                None
            } else {
                Some("must be one of ['max_completion_tokens', 'max_tokens']".to_string())
            }
        }
        "provider_routing" => provider_routing_problem(value),
        "headers" => headers_problem(value),
        "api" => {
            if matches!(value.as_str(), Some("chat_completions" | "responses")) {
                None
            } else {
                Some("must be one of ['chat_completions', 'responses']".to_string())
            }
        }
        _ => match value.as_str() {
            Some(s) if !s.trim().is_empty() => None,
            _ => Some("must be a non-empty string".to_string()),
        },
    }
}

pub fn make_provider_with_env(
    spec: &ModelSpec,
    env_override: Option<BTreeMap<String, String>>,
) -> Result<Arc<dyn Provider>, FactoryError> {
    spec.validate()?;
    let extra = &spec.extra;
    let retries_opt = extra
        .get("max_retries")
        .and_then(|v| v.as_u64())
        .map(|n| n as u32);
    let binary_opt = extra
        .get("binary")
        .and_then(Value::as_str)
        .map(str::to_string);

    match spec.provider {
        ProviderKind::Anthropic | ProviderKind::AnthropicCompatible => {
            let mut cfg = AnthropicConfig::new(&spec.model);
            cfg.api_key_env = spec.api_key_env.clone();
            cfg.base_url_env = spec.base_url_env.clone();
            cfg.cache_namespace = spec.cache_namespace.clone();
            cfg.allow_custom_endpoint = extra
                .get("allow_custom_endpoint")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            cfg.env_override = env_override;
            if let Some(t) = spec.timeout_s {
                cfg.timeout_s = t;
            }
            if let Some(r) = retries_opt {
                cfg.max_retries = r;
            }
            let provider = AnthropicProvider::new(cfg).map_err(FactoryError::Provider)?;
            Ok(Arc::new(provider))
        }
        ProviderKind::OpenaiCompatible => {
            let mut cfg = OpenAICompatConfig::new(&spec.model);
            cfg.api_key_env = spec.api_key_env.clone();
            cfg.base_url_env = Some(
                spec.base_url_env
                    .clone()
                    .unwrap_or_else(|| "OPENAI_BASE_URL".to_string()),
            );
            cfg.cache_namespace = spec.cache_namespace.clone();
            cfg.allow_custom_endpoint = extra
                .get("allow_custom_endpoint")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            cfg.env_override = env_override;
            if let Some(t) = spec.timeout_s {
                cfg.timeout_s = t;
            }
            if let Some(r) = retries_opt {
                cfg.max_retries = r;
            }
            if let Some(v) = extra.get("reasoning_effort_param").and_then(Value::as_bool) {
                cfg.reasoning_effort_param = v;
            }
            if let Some(v) = extra.get("max_tokens_param").and_then(Value::as_str) {
                cfg.max_tokens_param = if v == "max_tokens" {
                    MaxTokensParam::MaxTokens
                } else {
                    MaxTokensParam::MaxCompletionTokens
                };
            }
            if let Some(Value::Object(m)) = extra.get("provider_routing") {
                cfg.provider_routing = Some(m.clone());
            }
            if let Some(Value::Object(m)) = extra.get("headers") {
                let mut hdrs = BTreeMap::new();
                for (k, v) in m {
                    if let Some(s) = v.as_str() {
                        hdrs.insert(k.clone(), s.to_string());
                    }
                }
                cfg.headers = Some(hdrs);
            }
            if let Some(v) = extra.get("api").and_then(Value::as_str) {
                cfg.api = if v == "responses" {
                    OpenAiApi::Responses
                } else {
                    OpenAiApi::ChatCompletions
                };
            }
            let provider = OpenAICompatProvider::new(cfg).map_err(FactoryError::Provider)?;
            Ok(Arc::new(provider))
        }
        ProviderKind::ClaudeCli => {
            let mut cfg = CliConfig::new(
                &spec.model,
                binary_opt.unwrap_or_else(|| "claude".to_string()),
                spec.timeout_s.unwrap_or(600.0),
            );
            cfg.effort = spec.effort.clone();
            if let Some(r) = retries_opt {
                cfg.max_retries = r;
            }
            cfg.environment = env_override;
            Ok(Arc::new(ClaudeCliProvider::new(ClaudeAdapter, cfg)))
        }
        ProviderKind::CodexCli => {
            let mut cfg = CliConfig::new(
                &spec.model,
                binary_opt.unwrap_or_else(|| "codex".to_string()),
                spec.timeout_s.unwrap_or(900.0),
            );
            cfg.effort = spec.effective_effort().map(str::to_string);
            if let Some(r) = retries_opt {
                cfg.max_retries = r;
            }
            cfg.environment = env_override;
            Ok(Arc::new(CodexCliProvider::new(CodexAdapter, cfg)))
        }
        ProviderKind::GrokCli => {
            let mut cfg = CliConfig::new(
                &spec.model,
                binary_opt.unwrap_or_else(|| "grok".to_string()),
                spec.timeout_s.unwrap_or(600.0),
            );
            cfg.effort = spec.effort.clone();
            if let Some(r) = retries_opt {
                cfg.max_retries = r;
            }
            cfg.environment = env_override;
            Ok(Arc::new(GrokCliProvider::new(GrokAdapter, cfg)))
        }
        ProviderKind::AgyCli => {
            let mut cfg = CliConfig::new(
                &spec.model,
                binary_opt.unwrap_or_else(|| "agy".to_string()),
                spec.timeout_s.unwrap_or(900.0),
            );
            cfg.effort = spec.effort.clone();
            if let Some(r) = retries_opt {
                cfg.max_retries = r;
            }
            cfg.environment = env_override;
            Ok(Arc::new(AgyCliProvider::new(AgyAdapter, cfg)))
        }
        ProviderKind::MuseCli => {
            let mut cfg = CliConfig::new(
                &spec.model,
                binary_opt.unwrap_or_else(|| "muse".to_string()),
                spec.timeout_s.unwrap_or(900.0),
            );
            cfg.effort = spec.effective_effort().map(str::to_string);
            cfg.max_retries = retries_opt.unwrap_or(0);
            cfg.environment = env_override;
            Ok(Arc::new(MuseCliProvider::new(MuseAdapter::default(), cfg)))
        }
        ProviderKind::CursorCli => {
            let mut cfg = CliConfig::new(
                &spec.model,
                binary_opt.unwrap_or_else(resolve_cursor_binary),
                spec.timeout_s.unwrap_or(600.0),
            );
            if let Some(r) = retries_opt {
                cfg.max_retries = r;
            }
            cfg.environment = env_override;
            Ok(Arc::new(CursorCliProvider::new(CursorAdapter, cfg)))
        }
        ProviderKind::Replay => {
            let path = extra
                .get("path")
                .and_then(Value::as_str)
                .ok_or_else(|| FactoryError::Validation("missing extra.path".to_string()))?;
            let provider = ReplayProvider::from_path(path, &spec.model)?;
            Ok(Arc::new(provider))
        }
    }
}
