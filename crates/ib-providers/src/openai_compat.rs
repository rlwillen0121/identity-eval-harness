//! OpenAI provider (`openai_compatible`): OpenAI itself or any compatible server
//! via Chat Completions (`chat_completions`) or the Responses API (`responses`).

use std::collections::BTreeMap;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use ib_core::pyjson::{DumpOptions, PyNumber, classify_number, dumps};
use reqwest::header::{AUTHORIZATION, CONTENT_TYPE, HeaderMap, HeaderName, HeaderValue};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use url::Url;

use crate::base::{
    Message, ModelRequest, ModelResponse, Provider, ProviderCapabilities, Role, ToolCall, Usage,
    hash_endpoint,
};
use crate::env::paired_api_key_env;
use crate::http::{
    MAX_PROVIDER_RESPONSE_BYTES, read_bounded_response_body, redirect_provider_error,
};
use crate::json::{bounded_token_count, load_object, raw_usage_payload, sanitize_response};
use crate::subprocess::{redact, redact_diagnostics_map};

pub const DEFAULT_BASE_URL: &str = "https://api.openai.com/v1";
pub const DEFAULT_BASE_URL_ENV: &str = "OPENAI_BASE_URL";
pub const DEFAULT_API_KEY_ENV: &str = "OPENAI_API_KEY";
pub const OPENROUTER_BASE_URL_ENV: &str = "OPENROUTER_BASE_URL";
pub const UNPARSED_ARGUMENTS_KEY: &str = "_unparsed_arguments";

const PLACEHOLDER_API_KEY: &str = "not-set";
const VENDOR_HOST: &str = "api.openai.com";
pub const NO_TEMPERATURE_MODELS: &[&str] = &["gpt-5", "o1", "o3", "o4"];

const CREDENTIAL_HEADERS: &[&str] = &[
    "authorization",
    "api-key",
    "x-api-key",
    "x-goog-api-key",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-auth-token",
    "x-access-token",
];

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MaxTokensParam {
    #[default]
    MaxCompletionTokens,
    MaxTokens,
}

impl MaxTokensParam {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::MaxCompletionTokens => "max_completion_tokens",
            Self::MaxTokens => "max_tokens",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OpenAiApi {
    #[default]
    ChatCompletions,
    Responses,
}

impl OpenAiApi {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ChatCompletions => "chat_completions",
            Self::Responses => "responses",
        }
    }
}

#[must_use]
pub fn headers_problem(value: &Value) -> Option<String> {
    let Some(map) = value.as_object() else {
        return Some("must be a non-empty mapping".to_string());
    };
    if map.is_empty() {
        return Some("must be a non-empty mapping".to_string());
    }
    for (name, val) in map {
        if name.trim().is_empty() || !val.is_string() {
            return Some("must map non-empty header names to string values".to_string());
        }
        let lower = name.to_lowercase();
        if CREDENTIAL_HEADERS.contains(&lower.as_str()) {
            return Some(format!(
                "must not set {name:?}: credentials come from api_key_env"
            ));
        }
    }
    None
}

#[must_use]
pub fn provider_routing_problem(value: &Value) -> Option<String> {
    let Some(map) = value.as_object() else {
        return Some("must be a non-empty mapping".to_string());
    };
    if map.is_empty() {
        return Some("must be a non-empty mapping".to_string());
    }
    if map.keys().any(|k| k.is_empty()) {
        return Some("keys must be non-empty strings".to_string());
    }
    let opts = DumpOptions {
        sort_keys: true,
        separators: Some((",".to_string(), ":".to_string())),
        allow_nan: false,
        ..DumpOptions::default()
    };
    if dumps(value, &opts).is_err() {
        return Some(
            "values must be JSON (strings, numbers, booleans, lists, mappings)".to_string(),
        );
    }
    if let Some(order) = map.get("order") {
        let valid_order = order.as_array().is_some_and(|arr| {
            arr.iter()
                .all(|item| item.as_str().is_some_and(|s| !s.is_empty()))
        });
        if !valid_order {
            return Some("order must be a list of host names".to_string());
        }
    }
    for flag in ["allow_fallbacks", "require_parameters"] {
        if let Some(v) = map.get(flag)
            && !v.is_boolean()
        {
            return Some(format!("{flag} must be true or false"));
        }
    }
    None
}

#[must_use]
pub fn accepts_temperature(model: &str) -> bool {
    let name = model.strip_prefix("ft:").unwrap_or(model);
    !NO_TEMPERATURE_MODELS.iter().any(|family| {
        name == *family
            || name.starts_with(&format!("{family}-"))
            || name.starts_with(&format!("{family}."))
    })
}

#[must_use]
pub fn is_vendor_endpoint(base_url: Option<&str>) -> bool {
    let Some(u) = base_url else {
        return false;
    };
    Url::parse(u)
        .ok()
        .and_then(|parsed| {
            parsed
                .host_str()
                .map(|h| h.eq_ignore_ascii_case(VENDOR_HOST))
        })
        .unwrap_or(false)
}

#[must_use]
pub fn default_api_key_env(base_url_env: Option<&str>) -> Option<String> {
    match base_url_env {
        None | Some(DEFAULT_BASE_URL_ENV) => Some(DEFAULT_API_KEY_ENV.to_string()),
        Some(other) => paired_api_key_env(other),
    }
}

fn usd_from_value(value: Option<&Value>) -> Option<f64> {
    let Value::Number(n) = value? else {
        return None;
    };
    let num = match classify_number(n) {
        PyNumber::Int(s) => s.parse::<f64>().ok()?,
        PyNumber::Float(f) => f,
    };
    if num.is_finite() && num >= 0.0 {
        Some(num)
    } else {
        None
    }
}

fn chat_usage(usage_val: Option<&Value>) -> Usage {
    let Some(usage) = usage_val.and_then(Value::as_object) else {
        return Usage::default();
    };
    let prompt_details = usage
        .get("prompt_tokens_details")
        .and_then(Value::as_object);
    let completion_details = usage
        .get("completion_tokens_details")
        .and_then(Value::as_object);
    let cached = bounded_token_count(prompt_details.and_then(|d| d.get("cached_tokens")));
    let cache_write = bounded_token_count(prompt_details.and_then(|d| d.get("cache_write_tokens")));
    let reasoning = bounded_token_count(completion_details.and_then(|d| d.get("reasoning_tokens")));
    let prompt = bounded_token_count(usage.get("prompt_tokens"));
    Usage {
        input_tokens: prompt.saturating_sub(cached).saturating_sub(cache_write),
        output_tokens: bounded_token_count(usage.get("completion_tokens")),
        cache_read_tokens: cached,
        cache_write_tokens: cache_write,
        reasoning_tokens: reasoning,
    }
}

fn responses_usage(usage_val: Option<&Value>) -> Usage {
    let Some(usage) = usage_val.and_then(Value::as_object) else {
        return Usage::default();
    };
    let input_details = usage.get("input_tokens_details").and_then(Value::as_object);
    let output_details = usage
        .get("output_tokens_details")
        .and_then(Value::as_object);
    let cached = bounded_token_count(input_details.and_then(|d| d.get("cached_tokens")));
    let cache_write = bounded_token_count(input_details.and_then(|d| d.get("cache_write_tokens")));
    let reasoning = bounded_token_count(output_details.and_then(|d| d.get("reasoning_tokens")));
    let input = bounded_token_count(usage.get("input_tokens"));
    Usage {
        input_tokens: input.saturating_sub(cached).saturating_sub(cache_write),
        output_tokens: bounded_token_count(usage.get("output_tokens")),
        cache_read_tokens: cached,
        cache_write_tokens: cache_write,
        reasoning_tokens: reasoning,
    }
}

fn reported_cost(usage_val: Option<&Value>) -> (Option<f64>, bool) {
    let Some(usage) = usage_val.and_then(Value::as_object) else {
        return (None, false);
    };
    let is_byok = usage.get("is_byok") == Some(&Value::Bool(true));
    let cost = usd_from_value(usage.get("cost"));
    let Some(c) = cost else {
        return (None, is_byok);
    };
    if !is_byok {
        return (Some(c), false);
    }
    let upstream = usage
        .get("cost_details")
        .and_then(Value::as_object)
        .and_then(|d| usd_from_value(d.get("upstream_inference_cost")));
    (upstream.map(|u| c + u), true)
}

fn gateway_raw(reply: &Map<String, Value>) -> Map<String, Value> {
    let mut raw = Map::new();
    if let Some(upstream) = reply.get("provider").and_then(Value::as_str)
        && !upstream.is_empty()
    {
        raw.insert(
            "upstream_provider".to_string(),
            Value::String(upstream.to_string()),
        );
    }
    if reply
        .get("usage")
        .and_then(Value::as_object)
        .and_then(|u| u.get("is_byok"))
        == Some(&Value::Bool(true))
    {
        raw.insert("is_byok".to_string(), Value::Bool(true));
    }
    raw
}

#[derive(Debug, Clone)]
pub struct OpenAICompatConfig {
    pub model: String,
    pub api_key_env: Option<String>,
    pub base_url_env: Option<String>,
    pub cache_namespace: Option<String>,
    pub allow_custom_endpoint: bool,
    pub reasoning_effort_param: bool,
    pub max_tokens_param: MaxTokensParam,
    pub provider_routing: Option<Map<String, Value>>,
    pub headers: Option<BTreeMap<String, String>>,
    pub api: OpenAiApi,
    pub max_retries: u32,
    pub timeout_s: f64,
    pub retry_base_delay_s: f64,
    pub env_override: Option<BTreeMap<String, String>>,
}

impl OpenAICompatConfig {
    #[must_use]
    pub fn new(model: impl Into<String>) -> Self {
        Self {
            model: model.into(),
            api_key_env: None,
            base_url_env: Some(DEFAULT_BASE_URL_ENV.to_string()),
            cache_namespace: None,
            allow_custom_endpoint: false,
            reasoning_effort_param: true,
            max_tokens_param: MaxTokensParam::ChatCompletionsDefault,
            provider_routing: None,
            headers: None,
            api: OpenAiApi::ChatCompletions,
            // SPEC-v5.2 §7.4: no adapter-internal retries; the runner owns the retry cap.
            max_retries: 0,
            timeout_s: 600.0,
            retry_base_delay_s: 0.5,
            env_override: None,
        }
    }
}

impl MaxTokensParam {
    #[allow(non_upper_case_globals)]
    pub const ChatCompletionsDefault: Self = Self::MaxCompletionTokens;
}

pub struct OpenAICompatProvider {
    timeout: Duration,
    config: OpenAICompatConfig,
    resolved_api_key_env: Option<String>,
    routing_json: Option<String>,
    provider_routing: Option<Map<String, Value>>,
    base_url: Option<String>,
    drops_temperature: bool,
    client: OnceLock<Result<reqwest::Client, String>>,
}

impl OpenAICompatProvider {
    pub fn new(config: OpenAICompatConfig) -> Result<Self, String> {
        let timeout =
            crate::base::provider_timeout(config.timeout_s)?.max(Duration::from_millis(1));
        if config.api == OpenAiApi::Responses
            && config.max_tokens_param != MaxTokensParam::MaxCompletionTokens
        {
            return Err("max_tokens_param does not apply to api 'responses'".to_string());
        }

        let (routing_json, provider_routing) = if let Some(ref routing) = config.provider_routing {
            let val = Value::Object(routing.clone());
            if let Some(prob) = provider_routing_problem(&val) {
                return Err(format!("provider_routing {prob}"));
            }
            let opts = DumpOptions {
                sort_keys: true,
                separators: Some((",".to_string(), ":".to_string())),
                ..DumpOptions::default()
            };
            let canonical = dumps(&val, &opts).map_err(|e| format!("provider_routing {e}"))?;
            // The canonical (key-sorted) form feeds `endpoint_identity`; the request body carries
            // the configured object verbatim.
            (Some(canonical), Some(routing.clone()))
        } else {
            (None, None)
        };

        if let Some(ref headers) = config.headers {
            let mut hmap = Map::new();
            for (k, v) in headers {
                hmap.insert(k.clone(), Value::String(v.clone()));
            }
            if let Some(prob) = headers_problem(&Value::Object(hmap)) {
                return Err(format!("headers {prob}"));
            }
        }

        let resolved_api_key_env = config
            .api_key_env
            .clone()
            .or_else(|| default_api_key_env(config.base_url_env.as_deref()));

        let base_url =
            resolve_base_url(config.base_url_env.as_deref(), config.env_override.as_ref());
        if matches!(
            config.base_url_env.as_deref(),
            None | Some(DEFAULT_BASE_URL_ENV)
        ) && !is_vendor_endpoint(base_url.as_deref())
            && !config.allow_custom_endpoint
        {
            return Err(
                "openai_compatible: custom vendor endpoint requires extra.allow_custom_endpoint: true; vendor credentials and prompts would be sent to another host".to_string(),
            );
        }
        let drops_temperature =
            is_vendor_endpoint(base_url.as_deref()) && !accepts_temperature(&config.model);

        Ok(Self {
            timeout,
            config,
            resolved_api_key_env,
            routing_json,
            provider_routing,
            base_url,
            drops_temperature,
            client: OnceLock::new(),
        })
    }

    #[must_use]
    pub fn api(&self) -> OpenAiApi {
        self.config.api
    }

    #[must_use]
    pub fn base_url(&self) -> Option<&str> {
        self.base_url.as_deref()
    }

    #[must_use]
    pub fn api_key_env(&self) -> Option<&str> {
        self.resolved_api_key_env.as_deref()
    }

    #[must_use]
    pub fn max_tokens_param(&self) -> MaxTokensParam {
        self.config.max_tokens_param
    }

    #[must_use]
    pub fn reasoning_effort_param(&self) -> bool {
        self.config.reasoning_effort_param
    }

    fn get_env(&self, name: &str) -> Option<String> {
        if let Some(ref map) = self.config.env_override {
            map.get(name).cloned()
        } else {
            std::env::var(name).ok()
        }
    }

    fn redaction_env_names(&self) -> Vec<&str> {
        let mut names = Vec::new();
        if let Some(ref k) = self.resolved_api_key_env {
            names.push(k.as_str());
        }
        if let Some(ref b) = self.config.base_url_env {
            names.push(b.as_str());
        }
        names
    }

    #[must_use]
    pub fn request_params(&self, request: &ModelRequest) -> Map<String, Value> {
        if self.config.api == OpenAiApi::Responses {
            return self.responses_params(request);
        }
        let mut messages = Vec::new();
        if !request.system.is_empty() {
            let mut sys = Map::new();
            sys.insert("role".to_string(), Value::String("system".to_string()));
            sys.insert("content".to_string(), Value::String(request.system.clone()));
            messages.push(Value::Object(sys));
        }
        for m in &request.messages {
            messages.push(Value::Object(self.api_message(m)));
        }

        let mut params = Map::new();
        params.insert(
            "model".to_string(),
            Value::String(self.config.model.clone()),
        );
        params.insert("messages".to_string(), Value::Array(messages));
        params.insert(
            self.config.max_tokens_param.as_str().to_string(),
            Value::from(request.params.max_output_tokens),
        );

        if let Some(ref schema) = request.output_schema {
            let mut js = Map::new();
            js.insert(
                "name".to_string(),
                Value::String(request.schema_name.clone()),
            );
            js.insert("schema".to_string(), Value::Object(schema.clone()));
            js.insert("strict".to_string(), Value::Bool(true));

            let mut rf = Map::new();
            rf.insert("type".to_string(), Value::String("json_schema".to_string()));
            rf.insert("json_schema".to_string(), Value::Object(js));
            params.insert("response_format".to_string(), Value::Object(rf));
        }

        if !request.tools.is_empty() {
            let tools = request
                .tools
                .iter()
                .map(|tool| {
                    let mut fn_obj = Map::new();
                    fn_obj.insert("name".to_string(), Value::String(tool.name.clone()));
                    fn_obj.insert(
                        "description".to_string(),
                        Value::String(tool.description.clone()),
                    );
                    fn_obj.insert(
                        "parameters".to_string(),
                        Value::Object(tool.input_schema.clone()),
                    );
                    fn_obj.insert("strict".to_string(), Value::Bool(true));

                    let mut wrapper = Map::new();
                    wrapper.insert("type".to_string(), Value::String("function".to_string()));
                    wrapper.insert("function".to_string(), Value::Object(fn_obj));
                    Value::Object(wrapper)
                })
                .collect();
            params.insert("tools".to_string(), Value::Array(tools));
        }

        if let Some(temp) = request.params.temperature
            && !self.drops_temperature
        {
            params.insert("temperature".to_string(), Value::from(temp));
        }
        if let Some(ref effort) = request.params.effort
            && self.config.reasoning_effort_param
        {
            params.insert(
                "reasoning_effort".to_string(),
                Value::String(effort.clone()),
            );
        }
        if let Some(ref routing) = self.provider_routing {
            params.insert("provider".to_string(), Value::Object(routing.clone()));
        }
        params
    }

    fn responses_params(&self, request: &ModelRequest) -> Map<String, Value> {
        let mut params = Map::new();
        params.insert(
            "model".to_string(),
            Value::String(self.config.model.clone()),
        );
        if !request.system.is_empty() {
            params.insert(
                "instructions".to_string(),
                Value::String(request.system.clone()),
            );
        }
        let input: Vec<Value> = request
            .messages
            .iter()
            .flat_map(Self::input_items)
            .map(Value::Object)
            .collect();
        params.insert("input".to_string(), Value::Array(input));
        params.insert(
            "max_output_tokens".to_string(),
            Value::from(request.params.max_output_tokens),
        );
        params.insert("store".to_string(), Value::Bool(false));

        if let Some(ref schema) = request.output_schema {
            let mut format = Map::new();
            format.insert("type".to_string(), Value::String("json_schema".to_string()));
            format.insert(
                "name".to_string(),
                Value::String(request.schema_name.clone()),
            );
            format.insert("schema".to_string(), Value::Object(schema.clone()));
            format.insert("strict".to_string(), Value::Bool(true));

            let mut text = Map::new();
            text.insert("format".to_string(), Value::Object(format));
            params.insert("text".to_string(), Value::Object(text));
        }

        if !request.tools.is_empty() {
            let tools = request
                .tools
                .iter()
                .map(|tool| {
                    let mut item = Map::new();
                    item.insert("type".to_string(), Value::String("function".to_string()));
                    item.insert("name".to_string(), Value::String(tool.name.clone()));
                    item.insert(
                        "description".to_string(),
                        Value::String(tool.description.clone()),
                    );
                    item.insert(
                        "parameters".to_string(),
                        Value::Object(tool.input_schema.clone()),
                    );
                    item.insert("strict".to_string(), Value::Bool(true));
                    Value::Object(item)
                })
                .collect();
            params.insert("tools".to_string(), Value::Array(tools));
        }

        if let Some(temp) = request.params.temperature
            && !self.drops_temperature
        {
            params.insert("temperature".to_string(), Value::from(temp));
        }
        if let Some(ref effort) = request.params.effort
            && self.config.reasoning_effort_param
        {
            let mut r = Map::new();
            r.insert("effort".to_string(), Value::String(effort.clone()));
            params.insert("reasoning".to_string(), Value::Object(r));
        }
        if let Some(ref routing) = self.provider_routing {
            params.insert("provider".to_string(), Value::Object(routing.clone()));
        }
        params
    }

    fn input_items(message: &Message) -> Vec<Map<String, Value>> {
        match message.role {
            Role::Tool => {
                let mut item = Map::new();
                item.insert(
                    "type".to_string(),
                    Value::String("function_call_output".to_string()),
                );
                item.insert(
                    "call_id".to_string(),
                    message
                        .tool_call_id
                        .as_ref()
                        .map_or(Value::Null, |id| Value::String(id.clone())),
                );
                item.insert("output".to_string(), Value::String(message.content.clone()));
                vec![item]
            }
            Role::Assistant => {
                let mut items = Vec::new();
                if !message.content.is_empty() {
                    let mut m = Map::new();
                    m.insert("role".to_string(), Value::String("assistant".to_string()));
                    m.insert(
                        "content".to_string(),
                        Value::String(message.content.clone()),
                    );
                    items.push(m);
                }
                for call in &message.tool_calls {
                    let args_str = dumps(
                        &Value::Object(call.arguments.clone()),
                        &DumpOptions {
                            ensure_ascii: false,
                            ..DumpOptions::default()
                        },
                    )
                    .unwrap_or_else(|_| "{}".to_string());
                    let mut m = Map::new();
                    m.insert(
                        "type".to_string(),
                        Value::String("function_call".to_string()),
                    );
                    m.insert("call_id".to_string(), Value::String(call.id.clone()));
                    m.insert("name".to_string(), Value::String(call.name.clone()));
                    m.insert("arguments".to_string(), Value::String(args_str));
                    items.push(m);
                }
                items
            }
            Role::User => {
                let mut m = Map::new();
                m.insert("role".to_string(), Value::String("user".to_string()));
                m.insert(
                    "content".to_string(),
                    Value::String(message.content.clone()),
                );
                vec![m]
            }
        }
    }

    fn api_message(&self, message: &Message) -> Map<String, Value> {
        match message.role {
            Role::Tool => {
                let mut m = Map::new();
                m.insert("role".to_string(), Value::String("tool".to_string()));
                m.insert(
                    "tool_call_id".to_string(),
                    message
                        .tool_call_id
                        .as_ref()
                        .map_or(Value::Null, |id| Value::String(id.clone())),
                );
                m.insert(
                    "content".to_string(),
                    Value::String(message.content.clone()),
                );
                m
            }
            Role::Assistant => {
                let mut m = Map::new();
                m.insert("role".to_string(), Value::String("assistant".to_string()));
                m.insert(
                    "content".to_string(),
                    if message.content.is_empty() {
                        Value::Null
                    } else {
                        Value::String(message.content.clone())
                    },
                );
                if !message.tool_calls.is_empty() {
                    let tcs = message
                        .tool_calls
                        .iter()
                        .map(|call| {
                            let args_str = dumps(
                                &Value::Object(call.arguments.clone()),
                                &DumpOptions {
                                    ensure_ascii: false,
                                    ..DumpOptions::default()
                                },
                            )
                            .unwrap_or_else(|_| "{}".to_string());
                            let mut fn_obj = Map::new();
                            fn_obj.insert("name".to_string(), Value::String(call.name.clone()));
                            fn_obj.insert("arguments".to_string(), Value::String(args_str));
                            let mut wrapper = Map::new();
                            wrapper.insert("id".to_string(), Value::String(call.id.clone()));
                            wrapper
                                .insert("type".to_string(), Value::String("function".to_string()));
                            wrapper.insert("function".to_string(), Value::Object(fn_obj));
                            Value::Object(wrapper)
                        })
                        .collect();
                    m.insert("tool_calls".to_string(), Value::Array(tcs));
                }
                if self.config.model.contains("reflection/")
                    || self.config.model.contains("beam-1.0")
                {
                    let val = if message.content.is_empty() {
                        " "
                    } else {
                        &message.content
                    };
                    m.insert(
                        "reasoning_content".to_string(),
                        Value::String(val.to_string()),
                    );
                    m.insert("reasoning".to_string(), Value::String(val.to_string()));
                }
                if self.config.model.contains("cohere/")
                    && message.tool_calls.is_empty()
                    && message.content.trim().is_empty()
                {
                    m.insert("content".to_string(), Value::String(".".to_string()));
                }
                m
            }
            Role::User => {
                let mut m = Map::new();
                m.insert("role".to_string(), Value::String("user".to_string()));
                m.insert(
                    "content".to_string(),
                    Value::String(message.content.clone()),
                );
                m
            }
        }
    }

    fn ignored_params(&self, request: &ModelRequest) -> Vec<String> {
        let mut ignored = Vec::new();
        if request.params.temperature.is_some() && self.drops_temperature {
            ignored.push("temperature".to_string());
        }
        if request.params.effort.is_some() && !self.config.reasoning_effort_param {
            ignored.push("effort".to_string());
        }
        ignored
    }

    fn resolve_api_key(&self) -> Result<String, String> {
        let key = self
            .resolved_api_key_env
            .as_deref()
            .and_then(|name| self.get_env(name))
            .filter(|v| !v.is_empty());
        if let Some(k) = key {
            return Ok(k);
        }
        if self.base_url.as_deref() == Some(DEFAULT_BASE_URL) {
            let name = self
                .resolved_api_key_env
                .as_deref()
                .unwrap_or(DEFAULT_API_KEY_ENV);
            return Err(format!("environment variable {name} is not set"));
        }
        Ok(PLACEHOLDER_API_KEY.to_string())
    }

    fn get_client(&self) -> Result<&reqwest::Client, String> {
        self.client
            .get_or_init(|| {
                let key = self.resolve_api_key()?;
                let mut headers = HeaderMap::new();
                headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
                let auth_val = HeaderValue::from_str(&format!("Bearer {key}"))
                    .map_err(|e| format!("invalid api key header: {e}"))?;
                headers.insert(AUTHORIZATION, auth_val);
                if let Some(ref extra_headers) = self.config.headers {
                    for (name, val) in extra_headers {
                        let hn = HeaderName::from_bytes(name.as_bytes())
                            .map_err(|e| format!("invalid header name {name:?}: {e}"))?;
                        let hv = HeaderValue::from_str(val)
                            .map_err(|e| format!("invalid header value for {name:?}: {e}"))?;
                        headers.insert(hn, hv);
                    }
                }
                reqwest::Client::builder()
                    .timeout(self.timeout)
                    .redirect(reqwest::redirect::Policy::none())
                    .default_headers(headers)
                    .build()
                    .map_err(|e| format!("failed to build HTTP client: {e}"))
            })
            .as_ref()
            .map_err(Clone::clone)
    }

    fn fail(&self, message: &str, raw: Option<Map<String, Value>>) -> ModelResponse {
        let env_ref = self.config.env_override.as_ref();
        let names = self.redaction_env_names();
        ModelResponse {
            error: Some(redact(
                &format!("openai_compatible: {message}"),
                env_ref,
                &names,
            )),
            raw: raw.map(|r| redact_diagnostics_map(&r, env_ref, &names)),
            ..ModelResponse::default()
        }
    }

    async fn complete_inner(&self, request: &ModelRequest) -> ModelResponse {
        let Some(ref base) = self.base_url else {
            let env_name = self
                .config
                .base_url_env
                .as_deref()
                .unwrap_or(DEFAULT_BASE_URL_ENV);
            return self.fail(&format!("environment variable {env_name} is not set"), None);
        };
        let client = match self.get_client() {
            Ok(c) => c,
            Err(err) => return self.fail(&format!("OpenAIError: {err}"), None),
        };
        let path = match self.config.api {
            OpenAiApi::ChatCompletions => "chat/completions",
            OpenAiApi::Responses => "responses",
        };
        let url = format!("{base}/{path}");
        let body = Value::Object(self.request_params(request));

        let mut last_resp = None;
        let max_attempts = self.config.max_retries.saturating_add(1);
        for attempt_idx in 0..max_attempts {
            match client.post(&url).json(&body).send().await {
                Ok(resp) => {
                    let status = resp.status().as_u16();
                    let req_id = resp
                        .headers()
                        .get("x-request-id")
                        .or_else(|| resp.headers().get("request-id"))
                        .and_then(|v| v.to_str().ok())
                        .map(str::to_string);
                    let should_retry_hdr = resp
                        .headers()
                        .get("x-should-retry")
                        .and_then(|v| v.to_str().ok())
                        .map(str::to_string);
                    if (300..400).contains(&status) {
                        let error = redirect_provider_error(status, req_id);
                        return self.fail(&error.message, error.to_raw_map());
                    }
                    let response_body =
                        match read_bounded_response_body(resp, MAX_PROVIDER_RESPONSE_BYTES).await {
                            Ok(body) => body,
                            Err(body_error) => {
                                let error = body_error.into_provider_error(status, req_id.clone());
                                return self.fail(&error.message, error.to_raw_map());
                            }
                        };
                    let text_body = String::from_utf8_lossy(&response_body).into_owned();

                    if status >= 400 {
                        let parsed_err = load_object(&text_body);
                        let err_obj = parsed_err
                            .as_ref()
                            .and_then(|m| m.get("error"))
                            .and_then(Value::as_object);
                        let code = err_obj
                            .and_then(|e| e.get("code").or_else(|| e.get("type")))
                            .or_else(|| parsed_err.as_ref().and_then(|m| m.get("code")))
                            .and_then(Value::as_str)
                            .unwrap_or_else(|| status_error_class(status));
                        let msg = err_obj
                            .and_then(|e| e.get("message"))
                            .or_else(|| parsed_err.as_ref().and_then(|m| m.get("message")))
                            .and_then(Value::as_str)
                            .unwrap_or(text_body.as_str());

                        let mut err_raw = Map::new();
                        err_raw.insert("http_status".to_string(), Value::from(status));
                        err_raw.insert("error_code".to_string(), Value::String(code.to_string()));
                        if let Some(rid) = req_id {
                            err_raw.insert("request_id".to_string(), Value::String(rid));
                        }
                        let cls = status_error_class(status);
                        let retryable = err_raw.get("proven_unanswered")
                            == Some(&Value::Bool(true))
                            && should_retry_hdr.as_deref() != Some("false")
                            && (matches!(status, 408 | 409 | 429) || status >= 500);
                        let failed = self.fail(&format!("{cls} {status}: {msg}"), Some(err_raw));
                        last_resp = Some(failed);
                        if !retryable || attempt_idx + 1 >= max_attempts {
                            return last_resp.expect("set");
                        }
                    } else {
                        let parsed_val: Value = match serde_json::from_str(&text_body) {
                            Ok(v) => v,
                            Err(err) => {
                                let label = if err.to_string().contains("recursion limit exceeded")
                                {
                                    "RecursionError"
                                } else {
                                    "JSONDecodeError"
                                };
                                return self.fail(
                                    &format!("unparseable response body ({label}: {err})"),
                                    None,
                                );
                            }
                        };
                        let Some(reply_map) = parsed_val.as_object() else {
                            return self.fail("malformed response (expected JSON object)", None);
                        };
                        let outcome = match self.config.api {
                            OpenAiApi::Responses => self.parse_responses_reply(reply_map, request),
                            OpenAiApi::ChatCompletions => self.to_response(reply_map, request),
                        };
                        return match outcome {
                            Ok(resp) => resp,
                            Err(problem) => {
                                let mut failed =
                                    self.fail(&format!("malformed response ({problem})"), None);
                                self.keep_accounting(&mut failed, reply_map);
                                failed
                            }
                        };
                    }
                }
                Err(err) => {
                    let failed = if err.is_timeout() {
                        let mut raw = Map::new();
                        raw.insert(
                            "error_code".to_string(),
                            Value::String("APITimeoutError".to_string()),
                        );
                        raw.insert("ambiguous_timeout".to_string(), Value::Bool(true));
                        self.fail(&format!("APITimeoutError: {err}"), Some(raw))
                    } else {
                        let mut raw = Map::new();
                        raw.insert(
                            "error_code".to_string(),
                            Value::String("APIConnectionError".to_string()),
                        );
                        raw.insert(
                            "error_class".to_string(),
                            Value::String("TRANSIENT_FAILURE".to_string()),
                        );
                        self.fail(&format!("APIConnectionError: {err}"), Some(raw))
                    };
                    let proven_unanswered = failed
                        .raw
                        .as_ref()
                        .and_then(|raw| raw.get("proven_unanswered"))
                        == Some(&Value::Bool(true));
                    last_resp = Some(failed);
                    if !proven_unanswered || attempt_idx + 1 >= max_attempts {
                        return last_resp.expect("set");
                    }
                }
            }

            let delay = self.config.retry_base_delay_s * 2.0_f64.powi(attempt_idx as i32);
            if delay > 0.0 {
                tokio::time::sleep(Duration::from_secs_f64(delay)).await;
            }
        }
        last_resp.unwrap_or_else(|| self.fail("no response", None))
    }

    fn keep_accounting(&self, response: &mut ModelResponse, reply: &Map<String, Value>) {
        let usage_val = reply.get("usage");
        response.usage = if self.config.api == OpenAiApi::Responses {
            responses_usage(usage_val)
        } else {
            chat_usage(usage_val)
        };
        let (cost, _) = reported_cost(usage_val);
        response.reported_cost_usd = cost;
        let mut raw = response.raw.take().unwrap_or_default();
        for (k, v) in gateway_raw(reply) {
            raw.insert(k, v);
        }
        if let Some(ru) = usage_val.and_then(raw_usage_payload) {
            raw.insert("raw_usage".to_string(), Value::Object(ru));
        }
        if !raw.is_empty() {
            response.raw = Some(raw);
        }
    }

    fn to_response(
        &self,
        completion: &Map<String, Value>,
        request: &ModelRequest,
    ) -> Result<ModelResponse, String> {
        let usage_val = completion.get("usage");
        let (cost, _) = reported_cost(usage_val);
        let mut raw = Map::new();
        if let Some(id) = completion.get("id") {
            raw.insert("id".to_string(), id.clone());
        }
        if let Some(ru) = usage_val.and_then(raw_usage_payload) {
            raw.insert("raw_usage".to_string(), Value::Object(ru));
        }
        if let Some(fp) = completion
            .get("system_fingerprint")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
        {
            raw.insert(
                "system_fingerprint".to_string(),
                Value::String(fp.to_string()),
            );
        }
        for (k, v) in gateway_raw(completion) {
            raw.insert(k, v);
        }

        let mut response = ModelResponse {
            usage: chat_usage(usage_val),
            served_model: completion
                .get("model")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_string),
            reported_cost_usd: cost,
            raw: Some(raw),
            ..ModelResponse::default()
        };

        let choices = completion
            .get("choices")
            .and_then(Value::as_array)
            .ok_or_else(|| "missing choices".to_string())?;
        if choices.is_empty() {
            let env_ref = self.config.env_override.as_ref();
            let names = self.redaction_env_names();
            response.error = Some(redact(
                "openai_compatible: response has no choices",
                env_ref,
                &names,
            ));
            return Ok(response);
        }

        let choice = choices[0]
            .as_object()
            .ok_or_else(|| "choice must be an object".to_string())?;
        let message = choice
            .get("message")
            .and_then(Value::as_object)
            .ok_or_else(|| "choice missing message".to_string())?;
        response.stop_reason = choice
            .get("finish_reason")
            .and_then(Value::as_str)
            .map(str::to_string);

        if let Some(content) = message.get("content")
            && !content.is_null()
        {
            let text = content
                .as_str()
                .ok_or_else(|| "message.content must be text or null".to_string())?;
            if !text.is_empty() {
                response.text = Some(text.to_string());
            }
        }

        let refusal = message
            .get("refusal")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty());
        if refusal.is_some() || response.stop_reason.as_deref() == Some("content_filter") {
            response.refused = true;
            if let Some(ref_str) = refusal
                && let Some(ref mut r) = response.raw
            {
                r.insert("refusal".to_string(), Value::String(ref_str.to_string()));
            }
            return Ok(response);
        }

        if let Some(tc_arr) = message.get("tool_calls").and_then(Value::as_array) {
            for item in tc_arr {
                let Some(obj) = item.as_object() else {
                    continue;
                };
                let Some(function) = obj.get("function").and_then(Value::as_object) else {
                    continue;
                };
                let id = obj
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "tool call missing id".to_string())?;
                let name = function
                    .get("name")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "tool call missing name".to_string())?;
                let raw_args = function
                    .get("arguments")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let arguments = if raw_args.trim().is_empty() {
                    Map::new()
                } else if let Some(obj_args) = load_object(raw_args) {
                    obj_args
                } else {
                    let mut m = Map::new();
                    m.insert(
                        UNPARSED_ARGUMENTS_KEY.to_string(),
                        Value::String(raw_args.to_string()),
                    );
                    m
                };
                response.tool_calls.push(ToolCall {
                    id: id.to_string(),
                    name: name.to_string(),
                    arguments,
                });
            }
        }

        if request.output_schema.is_some()
            && let Some(ref text) = response.text
        {
            response.parsed = load_object(text);
        }
        Ok(response)
    }

    fn parse_responses_reply(
        &self,
        reply: &Map<String, Value>,
        request: &ModelRequest,
    ) -> Result<ModelResponse, String> {
        let usage_val = reply.get("usage");
        let (cost, _) = reported_cost(usage_val);
        let mut raw = Map::new();
        if let Some(id) = reply.get("id") {
            raw.insert("id".to_string(), id.clone());
        }
        for (k, v) in gateway_raw(reply) {
            raw.insert(k, v);
        }
        if let Some(ru) = usage_val.and_then(raw_usage_payload) {
            raw.insert("raw_usage".to_string(), Value::Object(ru));
        }

        let mut response = ModelResponse {
            usage: responses_usage(usage_val),
            served_model: reply
                .get("model")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_string),
            reported_cost_usd: cost,
            raw: Some(raw),
            ..ModelResponse::default()
        };

        let status = reply.get("status").and_then(Value::as_str);
        let err_obj = reply.get("error").filter(|v| !v.is_null());
        if status == Some("failed") || err_obj.is_some() {
            let detail = err_obj
                .and_then(Value::as_object)
                .map(|e| {
                    let code = e.get("code").and_then(Value::as_str).unwrap_or("error");
                    let msg = e.get("message").and_then(Value::as_str).unwrap_or("");
                    format!("{code}: {msg}")
                })
                .unwrap_or_else(|| "no error given".to_string());
            let env_ref = self.config.env_override.as_ref();
            let names = self.redaction_env_names();
            response.error = Some(redact(
                &format!("openai_compatible: response failed ({detail})"),
                env_ref,
                &names,
            ));
            return Ok(response);
        }

        let output = reply
            .get("output")
            .and_then(Value::as_array)
            .ok_or_else(|| "response missing output".to_string())?;
        let mut texts = Vec::new();
        let mut refusals = Vec::new();
        let mut calls = Vec::new();
        for item_val in output {
            let item = item_val
                .as_object()
                .ok_or_else(|| "output item must be an object".to_string())?;
            let item_type = item
                .get("type")
                .and_then(Value::as_str)
                .ok_or_else(|| "output item missing type".to_string())?;
            if item_type == "message" {
                let parts = item
                    .get("content")
                    .and_then(Value::as_array)
                    .ok_or_else(|| "message item missing content".to_string())?;
                for part_val in parts {
                    let part = part_val
                        .as_object()
                        .ok_or_else(|| "content part must be object".to_string())?;
                    match part.get("type").and_then(Value::as_str) {
                        Some("output_text") => {
                            if let Some(t) = part.get("text").and_then(Value::as_str) {
                                texts.push(t.to_string());
                            }
                        }
                        Some("refusal") => {
                            if let Some(r) = part.get("refusal").and_then(Value::as_str) {
                                refusals.push(r.to_string());
                            }
                        }
                        _ => {}
                    }
                }
            } else if item_type == "function_call" {
                let call_id = item
                    .get("call_id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "function_call missing call_id".to_string())?;
                let name = item
                    .get("name")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "function_call missing name".to_string())?;
                let raw_args = item.get("arguments").and_then(Value::as_str).unwrap_or("");
                let arguments = if raw_args.trim().is_empty() {
                    Map::new()
                } else if let Some(obj_args) = load_object(raw_args) {
                    obj_args
                } else {
                    let mut m = Map::new();
                    m.insert(
                        UNPARSED_ARGUMENTS_KEY.to_string(),
                        Value::String(raw_args.to_string()),
                    );
                    m
                };
                calls.push(ToolCall {
                    id: call_id.to_string(),
                    name: name.to_string(),
                    arguments,
                });
            }
        }

        let joined_text = texts.concat();
        response.text = if joined_text.is_empty() {
            None
        } else {
            Some(joined_text)
        };

        let reason = reply
            .get("incomplete_details")
            .and_then(Value::as_object)
            .and_then(|d| d.get("reason"))
            .and_then(Value::as_str);

        if status == Some("incomplete") {
            response.stop_reason = Some(match reason {
                Some("max_output_tokens") => "length".to_string(),
                Some(r) => r.to_string(),
                None => "incomplete".to_string(),
            });
        } else {
            response.stop_reason = Some(if calls.is_empty() {
                "stop".to_string()
            } else {
                "tool_calls".to_string()
            });
        }

        if !refusals.is_empty() || reason == Some("content_filter") {
            response.refused = true;
            if reason == Some("content_filter") {
                response.stop_reason = Some("content_filter".to_string());
            }
            if !refusals.is_empty()
                && let Some(ref mut r) = response.raw
            {
                r.insert("refusal".to_string(), Value::String(refusals.concat()));
            }
            return Ok(response);
        }

        response.tool_calls = calls;
        if request.output_schema.is_some()
            && let Some(ref text) = response.text
        {
            response.parsed = load_object(text);
        }
        Ok(response)
    }
}

#[async_trait]
impl Provider for OpenAICompatProvider {
    fn kind(&self) -> &str {
        "openai_compatible"
    }

    fn model(&self) -> &str {
        &self.config.model
    }

    fn python_type_identity(&self) -> (&'static str, &'static str) {
        (
            "identitybench.providers.openai_compat",
            "OpenAICompatProvider",
        )
    }

    fn local_retry_limit(&self) -> Option<u32> {
        Some(self.config.max_retries)
    }

    fn endpoint_identity(&self) -> String {
        let fallback_unset;
        let base = match &self.base_url {
            Some(b) => b.as_str(),
            None => {
                let env_name = self
                    .config
                    .base_url_env
                    .as_deref()
                    .unwrap_or(DEFAULT_BASE_URL_ENV);
                fallback_unset = format!("unset:{env_name}");
                &fallback_unset
            }
        };
        let mut parts: Vec<Option<&str>> = vec![
            Some(base),
            Some(self.config.model.as_str()),
            self.config.cache_namespace.as_deref(),
        ];
        if let Some(ref rj) = self.routing_json {
            parts.push(Some(rj.as_str()));
        }
        let api_part;
        if self.config.api != OpenAiApi::ChatCompletions {
            api_part = format!("api={}", self.config.api.as_str());
            parts.push(Some(&api_part));
        }
        let identity = hash_endpoint(&parts);
        if self.config.allow_custom_endpoint {
            format!("custom-endpoint-opt-in:{identity}")
        } else {
            identity
        }
    }

    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities::new(true, true, !self.drops_temperature)
    }

    async fn complete(&self, request: &ModelRequest) -> ModelResponse {
        let start = Instant::now();
        let mut response = sanitize_response(self.complete_inner(request).await, self.kind());
        response.latency_s = start.elapsed().as_secs_f64();
        let ignored = self.ignored_params(request);
        if !ignored.is_empty() {
            let mut raw = response.raw.take().unwrap_or_default();
            raw.insert(
                "ignored_params".to_string(),
                Value::Array(ignored.into_iter().map(Value::String).collect()),
            );
            response.raw = Some(raw);
        }
        let env_ref = self.config.env_override.as_ref();
        let names = self.redaction_env_names();
        if let Some(ref err) = response.error {
            response.error = Some(redact(err, env_ref, &names));
        }
        if let Some(ref raw) = response.raw {
            response.raw = Some(redact_diagnostics_map(raw, env_ref, &names));
        }
        response
    }
}

fn resolve_base_url(
    base_url_env: Option<&str>,
    env_override: Option<&BTreeMap<String, String>>,
) -> Option<String> {
    let Some(env_name) = base_url_env else {
        return Some(DEFAULT_BASE_URL.to_string());
    };
    let val = if let Some(map) = env_override {
        map.get(env_name).cloned()
    } else {
        std::env::var(env_name).ok()
    };
    if let Some(v) = val
        && !v.is_empty()
    {
        return Some(v.trim_end_matches('/').to_string());
    }
    if env_name == DEFAULT_BASE_URL_ENV {
        Some(DEFAULT_BASE_URL.to_string())
    } else {
        None
    }
}

fn status_error_class(status: u16) -> &'static str {
    match status {
        400 => "BadRequestError",
        401 => "AuthenticationError",
        403 => "PermissionDeniedError",
        404 => "NotFoundError",
        409 => "ConflictError",
        422 => "UnprocessableEntityError",
        429 => "RateLimitError",
        500..=599 => "InternalServerError",
        _ => "APIStatusError",
    }
}
