//! Provider contract (SPEC §11.1).
//!
//! Every model access path implements [`Provider`]: native HTTP APIs (Anthropic, OpenAI-compatible,
//! Anthropic-compatible), subscription CLIs (`claude`, `codex`, `grok`, `agy`, `cursor`, `muse`),
//! and the replay provider. A provider never raises/errors for model-level failures: transport,
//! auth, and parse problems come back as a [`ModelResponse`] with `error` set.

use std::collections::BTreeMap;

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    User,
    Assistant,
    Tool,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolSpec {
    pub name: String,
    pub description: String,
    pub input_schema: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolCall {
    pub id: String,
    pub name: String,
    pub arguments: Map<String, Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Message {
    pub role: Role,
    pub content: String,
    #[serde(default)]
    pub tool_calls: Vec<ToolCall>,
    #[serde(default)]
    pub tool_call_id: Option<String>,
}

impl Message {
    #[must_use]
    pub fn user(content: impl Into<String>) -> Self {
        Self {
            role: Role::User,
            content: content.into(),
            tool_calls: Vec::new(),
            tool_call_id: None,
        }
    }

    #[must_use]
    pub fn assistant(content: impl Into<String>, tool_calls: Vec<ToolCall>) -> Self {
        Self {
            role: Role::Assistant,
            content: content.into(),
            tool_calls,
            tool_call_id: None,
        }
    }

    #[must_use]
    pub fn tool(tool_call_id: impl Into<String>, content: impl Into<String>) -> Self {
        Self {
            role: Role::Tool,
            content: content.into(),
            tool_calls: Vec::new(),
            tool_call_id: Some(tool_call_id.into()),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GenerationParams {
    pub max_output_tokens: u64,
    pub temperature: Option<f64>,
    pub effort: Option<String>,
    pub extra: Map<String, Value>,
}

impl Default for GenerationParams {
    fn default() -> Self {
        Self {
            max_output_tokens: 4096,
            temperature: None,
            effort: None,
            extra: Map::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModelRequest {
    pub system: String,
    pub messages: Vec<Message>,
    pub output_schema: Option<Map<String, Value>>,
    pub schema_name: String,
    pub tools: Vec<ToolSpec>,
    pub params: GenerationParams,
}

impl Default for ModelRequest {
    fn default() -> Self {
        Self {
            system: String::new(),
            messages: Vec::new(),
            output_schema: None,
            schema_name: "decision".to_string(),
            tools: Vec::new(),
            params: GenerationParams::default(),
        }
    }
}

impl ModelRequest {
    #[must_use]
    pub fn new(system: impl Into<String>, messages: Vec<Message>) -> Self {
        Self {
            system: system.into(),
            messages,
            ..Self::default()
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Usage {
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_read_tokens: u64,
    pub cache_write_tokens: u64,
    pub reasoning_tokens: u64,
}

impl Usage {
    pub fn add(&mut self, other: &Self) {
        self.input_tokens = self.input_tokens.saturating_add(other.input_tokens);
        self.output_tokens = self.output_tokens.saturating_add(other.output_tokens);
        self.cache_read_tokens = self.cache_read_tokens.saturating_add(other.cache_read_tokens);
        self.cache_write_tokens = self.cache_write_tokens.saturating_add(other.cache_write_tokens);
        self.reasoning_tokens = self.reasoning_tokens.saturating_add(other.reasoning_tokens);
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModelResponse {
    pub text: Option<String>,
    pub parsed: Option<Map<String, Value>>,
    pub tool_calls: Vec<ToolCall>,
    pub stop_reason: Option<String>,
    pub refused: bool,
    pub usage: Usage,
    pub latency_s: f64,
    pub served_model: Option<String>,
    pub reported_cost_usd: Option<f64>,
    pub error: Option<String>,
    pub raw: Option<Map<String, Value>>,
}

impl Default for ModelResponse {
    fn default() -> Self {
        Self {
            text: None,
            parsed: None,
            tool_calls: Vec::new(),
            stop_reason: None,
            refused: false,
            usage: Usage::default(),
            latency_s: 0.0,
            served_model: None,
            reported_cost_usd: None,
            error: None,
            raw: None,
        }
    }
}

impl ModelResponse {
    #[must_use]
    pub fn ok(&self) -> bool {
        self.error.is_none() && !self.refused
    }

    #[must_use]
    pub fn with_error(error: impl Into<String>) -> Self {
        Self {
            error: Some(error.into()),
            ..Self::default()
        }
    }
}

/// Structured adapter error metadata preserved across retries and converted into `ModelResponse`.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
#[error("{message}")]
pub struct ProviderError {
    pub message: String,
    pub error_code: Option<String>,
    pub error_class: Option<String>,
    pub http_status: Option<u16>,
    pub request_id: Option<String>,
    pub proven_unanswered: bool,
    pub ambiguous_timeout: bool,
    pub raw_usage: Option<Value>,
    pub metadata: BTreeMap<String, Value>,
}

impl ProviderError {
    #[must_use]
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            error_code: None,
            error_class: None,
            http_status: None,
            request_id: None,
            proven_unanswered: false,
            ambiguous_timeout: false,
            raw_usage: None,
            metadata: BTreeMap::new(),
        }
    }

    #[must_use]
    pub fn to_raw_map(&self) -> Option<Map<String, Value>> {
        let mut raw = Map::new();
        if let Some(status) = self.http_status {
            raw.insert("http_status".to_string(), Value::from(status));
        }
        if let Some(code) = &self.error_code {
            raw.insert("error_code".to_string(), Value::String(code.clone()));
        }
        if let Some(cls) = &self.error_class {
            raw.insert("error_class".to_string(), Value::String(cls.clone()));
        }
        if let Some(req_id) = &self.request_id {
            raw.insert("request_id".to_string(), Value::String(req_id.clone()));
        }
        if self.proven_unanswered {
            raw.insert("proven_unanswered".to_string(), Value::Bool(true));
        }
        if self.ambiguous_timeout {
            raw.insert("ambiguous_timeout".to_string(), Value::Bool(true));
        }
        if let Some(raw_usage) = &self.raw_usage {
            raw.insert("raw_usage".to_string(), raw_usage.clone());
        }
        for (k, v) in &self.metadata {
            raw.insert(k.clone(), v.clone());
        }
        if raw.is_empty() { None } else { Some(raw) }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct ProviderCapabilities {
    pub native_structured_output: bool,
    pub native_tools: bool,
    pub supports_temperature: bool,
    pub reports_usage: bool,
}

impl ProviderCapabilities {
    #[must_use]
    pub const fn new(
        native_structured_output: bool,
        native_tools: bool,
        supports_temperature: bool,
    ) -> Self {
        Self {
            native_structured_output,
            native_tools,
            supports_temperature,
            reports_usage: true,
        }
    }
}

#[async_trait]
pub trait Provider: Send + Sync {
    fn kind(&self) -> &str;
    fn model(&self) -> &str;

    /// Exact Python `(module, class)` tuple for v5 provider accounting parity.
    fn python_type_identity(&self) -> (&'static str, &'static str);

    /// Local retry limit configured on this adapter, if applicable to v5 accounting.
    fn local_retry_limit(&self) -> Option<u32> {
        None
    }

    /// Non-secret identity of where requests go (SPEC §11.4). Never includes keys or raw private URLs.
    fn endpoint_identity(&self) -> String;

    fn capabilities(&self) -> ProviderCapabilities;

    async fn complete(&self, request: &ModelRequest) -> ModelResponse;

    /// Optional hook for stateful providers (e.g., Anthropic thinking block replay cache).
    fn remember_response(&self, _response: &ModelResponse) {}
}

/// Short stable hash of endpoint parts (base URL, reported revision, cache namespace).
#[must_use]
pub fn hash_endpoint(parts: &[Option<&str>]) -> String {
    let joined = parts
        .iter()
        .map(|p| p.unwrap_or(""))
        .collect::<Vec<_>>()
        .join("\x1f");
    let digest = Sha256::digest(joined.as_bytes());
    let hex_str = hex::encode(digest);
    hex_str[..16].to_string()
}
