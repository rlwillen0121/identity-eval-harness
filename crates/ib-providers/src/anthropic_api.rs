//! Anthropic Messages API provider (`anthropic`) and Messages-compatible servers (`anthropic_compatible`).

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use async_trait::async_trait;
use reqwest::header::{AUTHORIZATION, CONTENT_TYPE, HeaderMap, HeaderValue};
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

pub const DEFAULT_BASE_URL: &str = "https://api.anthropic.com";
pub const DEFAULT_API_KEY_ENV: &str = "ANTHROPIC_API_KEY";
pub const STREAMING_THRESHOLD_TOKENS: u64 = 16_000;
const VENDOR_HOST: &str = "api.anthropic.com";
const PLACEHOLDER_API_KEY: &str = "not-set";
const TOOL_TURN_CACHE_SIZE: usize = 4096;
pub const ASSISTANT_CONTENT_KEY: &str = "assistant_content";

pub const NO_TEMPERATURE_MODELS: &[&str] = &[
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-haiku-5-5",
];

#[must_use]
pub fn accepts_temperature(model: &str) -> bool {
    !NO_TEMPERATURE_MODELS
        .iter()
        .any(|family| model.contains(family))
}

fn is_vendor_endpoint(base_url: Option<&str>) -> bool {
    let Some(base_url) = base_url else {
        return false;
    };
    Url::parse(base_url)
        .ok()
        .and_then(|parsed| {
            parsed
                .host_str()
                .map(|host| host.eq_ignore_ascii_case(VENDOR_HOST))
        })
        .unwrap_or(false)
}

fn parse_usage(usage_val: Option<&Value>) -> Usage {
    let Some(usage) = usage_val.and_then(Value::as_object) else {
        return Usage::default();
    };
    let details = usage
        .get("output_tokens_details")
        .and_then(Value::as_object);
    Usage {
        input_tokens: bounded_token_count(usage.get("input_tokens")),
        output_tokens: bounded_token_count(usage.get("output_tokens")),
        cache_read_tokens: bounded_token_count(usage.get("cache_read_input_tokens")),
        cache_write_tokens: bounded_token_count(usage.get("cache_creation_input_tokens")),
        reasoning_tokens: details.map_or(0, |d| bounded_token_count(d.get("thinking_tokens"))),
    }
}

#[derive(Debug, Default)]
struct ToolTurnCache {
    order: VecDeque<String>,
    map: HashMap<String, Vec<Value>>,
}

impl ToolTurnCache {
    fn get(&self, id: &str) -> Option<&Vec<Value>> {
        self.map.get(id)
    }

    fn insert_turn(&mut self, blocks: Vec<Value>, calls: &[ToolCall]) {
        for call in calls {
            if self.map.contains_key(&call.id) {
                self.order.retain(|x| x != &call.id);
            }
            self.order.push_back(call.id.clone());
            self.map.insert(call.id.clone(), blocks.clone());
        }
        while self.map.len() > TOOL_TURN_CACHE_SIZE {
            if let Some(oldest) = self.order.pop_front() {
                self.map.remove(&oldest);
            } else {
                break;
            }
        }
    }
}

#[derive(Debug, Clone)]
pub struct AnthropicConfig {
    pub model: String,
    pub api_key_env: Option<String>,
    pub base_url_env: Option<String>,
    pub cache_namespace: Option<String>,
    pub allow_custom_endpoint: bool,
    pub max_retries: u32,
    pub timeout_s: f64,
    pub retry_base_delay_s: f64,
    pub env_override: Option<BTreeMap<String, String>>,
}

impl AnthropicConfig {
    #[must_use]
    pub fn new(model: impl Into<String>) -> Self {
        Self {
            model: model.into(),
            api_key_env: None,
            base_url_env: None,
            cache_namespace: None,
            allow_custom_endpoint: false,
            // SPEC-v5.2 §7.4: no adapter-internal retries; the runner owns the retry cap.
            max_retries: 0,
            timeout_s: 600.0,
            retry_base_delay_s: 0.5,
            env_override: None,
        }
    }
}

pub struct AnthropicProvider {
    timeout: Duration,
    config: AnthropicConfig,
    kind: &'static str,
    resolved_api_key_env: Option<String>,
    base_url: Option<String>,
    tool_turns: Mutex<ToolTurnCache>,
    client: OnceLock<Result<reqwest::Client, String>>,
}

impl AnthropicProvider {
    pub fn new(config: AnthropicConfig) -> Result<Self, String> {
        let timeout =
            crate::base::provider_timeout(config.timeout_s)?.max(Duration::from_millis(1));
        let kind = if config.base_url_env.is_some() {
            "anthropic_compatible"
        } else {
            "anthropic"
        };
        let resolved_api_key_env = if let Some(ref explicit) = config.api_key_env {
            Some(explicit.clone())
        } else if kind == "anthropic" {
            Some(DEFAULT_API_KEY_ENV.to_string())
        } else if let Some(ref b_env) = config.base_url_env {
            paired_api_key_env(b_env)
        } else {
            None
        };
        let base_url =
            resolve_base_url(config.base_url_env.as_deref(), config.env_override.as_ref());
        if kind == "anthropic"
            && !is_vendor_endpoint(base_url.as_deref())
            && !config.allow_custom_endpoint
        {
            return Err(
                "anthropic: custom endpoint requires extra.allow_custom_endpoint: true; vendor credentials and prompts would be sent to another host".to_string(),
            );
        }
        Ok(Self {
            timeout,
            config,
            kind,
            resolved_api_key_env,
            base_url,
            tool_turns: Mutex::new(ToolTurnCache::default()),
            client: OnceLock::new(),
        })
    }

    #[must_use]
    pub fn base_url(&self) -> Option<&str> {
        self.base_url.as_deref()
    }

    #[must_use]
    pub fn api_key_env(&self) -> Option<&str> {
        self.resolved_api_key_env.as_deref()
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
        let mut params = Map::new();
        params.insert(
            "model".to_string(),
            Value::String(self.config.model.clone()),
        );
        params.insert(
            "max_tokens".to_string(),
            Value::from(request.params.max_output_tokens),
        );
        params.insert(
            "messages".to_string(),
            Value::Array(self.api_messages(&request.messages)),
        );

        if !request.system.is_empty() {
            let mut cc = Map::new();
            cc.insert("type".to_string(), Value::String("ephemeral".to_string()));
            let mut block = Map::new();
            block.insert("type".to_string(), Value::String("text".to_string()));
            block.insert("text".to_string(), Value::String(request.system.clone()));
            block.insert("cache_control".to_string(), Value::Object(cc));
            params.insert(
                "system".to_string(),
                Value::Array(vec![Value::Object(block)]),
            );
        }

        let mut output_config = Map::new();
        if let Some(ref schema) = request.output_schema {
            let mut fmt = Map::new();
            fmt.insert("type".to_string(), Value::String("json_schema".to_string()));
            fmt.insert("schema".to_string(), Value::Object(schema.clone()));
            output_config.insert("format".to_string(), Value::Object(fmt));
        }
        if let Some(ref effort) = request.params.effort {
            output_config.insert("effort".to_string(), Value::String(effort.clone()));
        }
        if !output_config.is_empty() {
            params.insert("output_config".to_string(), Value::Object(output_config));
        }

        if !request.tools.is_empty() {
            let tools = request
                .tools
                .iter()
                .map(|tool| {
                    let mut m = Map::new();
                    m.insert("name".to_string(), Value::String(tool.name.clone()));
                    m.insert(
                        "description".to_string(),
                        Value::String(tool.description.clone()),
                    );
                    m.insert(
                        "input_schema".to_string(),
                        Value::Object(tool.input_schema.clone()),
                    );
                    m.insert("strict".to_string(), Value::Bool(true));
                    Value::Object(m)
                })
                .collect();
            params.insert("tools".to_string(), Value::Array(tools));
        }

        if let Some(temp) = request.params.temperature
            && accepts_temperature(&self.config.model)
        {
            params.insert("temperature".to_string(), Value::from(temp));
        }
        params
    }

    fn api_messages(&self, messages: &[Message]) -> Vec<Value> {
        let mut api: Vec<Map<String, Value>> = Vec::new();
        for message in messages {
            if message.role == Role::Assistant {
                let mut m = Map::new();
                m.insert("role".to_string(), Value::String("assistant".to_string()));
                m.insert(
                    "content".to_string(),
                    Value::Array(self.assistant_content(message)),
                );
                api.push(m);
                continue;
            }
            let block = if message.role == Role::Tool {
                let mut b = Map::new();
                b.insert("type".to_string(), Value::String("tool_result".to_string()));
                b.insert(
                    "tool_use_id".to_string(),
                    message
                        .tool_call_id
                        .as_ref()
                        .map_or(Value::Null, |id| Value::String(id.clone())),
                );
                b.insert(
                    "content".to_string(),
                    Value::String(message.content.clone()),
                );
                Value::Object(b)
            } else {
                let mut b = Map::new();
                b.insert("type".to_string(), Value::String("text".to_string()));
                b.insert("text".to_string(), Value::String(message.content.clone()));
                Value::Object(b)
            };

            if let Some(last) = api.last_mut()
                && last.get("role").and_then(Value::as_str) == Some("user")
                && let Some(Value::Array(arr)) = last.get_mut("content")
            {
                arr.push(block);
                continue;
            }
            let mut m = Map::new();
            m.insert("role".to_string(), Value::String("user".to_string()));
            m.insert("content".to_string(), Value::Array(vec![block]));
            api.push(m);
        }
        api.into_iter().map(Value::Object).collect()
    }

    fn assistant_content(&self, message: &Message) -> Vec<Value> {
        if !message.tool_calls.is_empty()
            && let Ok(guard) = self.tool_turns.lock()
            && let Some(original) = guard.get(&message.tool_calls[0].id)
        {
            return original.clone();
        }
        let mut blocks = Vec::new();
        if !message.content.is_empty() {
            let mut b = Map::new();
            b.insert("type".to_string(), Value::String("text".to_string()));
            b.insert("text".to_string(), Value::String(message.content.clone()));
            blocks.push(Value::Object(b));
        }
        for call in &message.tool_calls {
            let mut b = Map::new();
            b.insert("type".to_string(), Value::String("tool_use".to_string()));
            b.insert("id".to_string(), Value::String(call.id.clone()));
            b.insert("name".to_string(), Value::String(call.name.clone()));
            b.insert("input".to_string(), Value::Object(call.arguments.clone()));
            blocks.push(Value::Object(b));
        }
        blocks
    }

    fn get_client(&self) -> Result<&reqwest::Client, String> {
        self.client
            .get_or_init(|| {
                let mut headers = HeaderMap::new();
                headers.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
                headers.insert("anthropic-version", HeaderValue::from_static("2023-06-01"));

                let key = self
                    .resolved_api_key_env
                    .as_deref()
                    .and_then(|n| self.get_env(n))
                    .filter(|v| !v.is_empty());

                if let Some(k) = key {
                    let hv = HeaderValue::from_str(&k)
                        .map_err(|e| format!("invalid api key header: {e}"))?;
                    headers.insert("x-api-key", hv);
                } else if self.kind == "anthropic_compatible" {
                    headers.insert("x-api-key", HeaderValue::from_static(PLACEHOLDER_API_KEY));
                } else if self.resolved_api_key_env.as_deref() != Some(DEFAULT_API_KEY_ENV) {
                    let name = self
                        .resolved_api_key_env
                        .as_deref()
                        .unwrap_or(DEFAULT_API_KEY_ENV);
                    return Err(format!("environment variable {name} is not set"));
                } else if let Some(auth_tok) = self
                    .get_env("ANTHROPIC_AUTH_TOKEN")
                    .filter(|v| !v.is_empty())
                {
                    let hv = HeaderValue::from_str(&format!("Bearer {auth_tok}"))
                        .map_err(|e| format!("invalid auth token header: {e}"))?;
                    headers.insert(AUTHORIZATION, hv);
                } else {
                    return Err(format!(
                        "environment variable {DEFAULT_API_KEY_ENV} is not set"
                    ));
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
                &format!("{}: {message}", self.kind),
                env_ref,
                &names,
            )),
            raw: raw.map(|r| redact_diagnostics_map(&r, env_ref, &names)),
            ..ModelResponse::default()
        }
    }

    async fn complete_inner(&self, request: &ModelRequest) -> ModelResponse {
        let Some(ref base) = self.base_url else {
            let env_name = self.config.base_url_env.as_deref().unwrap_or("");
            return self.fail(&format!("environment variable {env_name} is not set"), None);
        };
        let client = match self.get_client() {
            Ok(c) => c,
            Err(err) => return self.fail(&format!("AnthropicError: {err}"), None),
        };

        let mut params = self.request_params(request);
        let streaming = request.params.max_output_tokens > STREAMING_THRESHOLD_TOKENS;
        if streaming {
            params.insert("stream".to_string(), Value::Bool(true));
        }
        let url = format!("{base}/v1/messages");
        let body = Value::Object(params);

        let mut last_resp = None;
        let max_attempts = self.config.max_retries.saturating_add(1);
        for attempt_idx in 0..max_attempts {
            match client.post(&url).json(&body).send().await {
                Ok(resp) => {
                    let status = resp.status().as_u16();
                    let req_id = resp
                        .headers()
                        .get("request-id")
                        .or_else(|| resp.headers().get("x-request-id"))
                        .and_then(|v| v.to_str().ok())
                        .map(str::to_string);
                    let should_retry_hdr = resp
                        .headers()
                        .get("x-should-retry")
                        .and_then(|v| v.to_str().ok())
                        .map(str::to_string);
                    let content_type = resp
                        .headers()
                        .get(CONTENT_TYPE)
                        .and_then(|v| v.to_str().ok())
                        .unwrap_or("")
                        .to_lowercase();
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
                        let msg = parsed_err
                            .as_ref()
                            .and_then(|m| m.get("error"))
                            .and_then(Value::as_object)
                            .and_then(|e| e.get("message"))
                            .and_then(Value::as_str)
                            .unwrap_or(text_body.as_str());
                        let mut err_raw = Map::new();
                        if let Some(rid) = req_id {
                            err_raw.insert("request_id".to_string(), Value::String(rid));
                        }
                        let cls = status_error_class(status);
                        let failed = self.fail(
                            &format!("{cls} {status}: {msg}"),
                            if err_raw.is_empty() {
                                None
                            } else {
                                Some(err_raw)
                            },
                        );
                        let proven_unanswered = failed
                            .raw
                            .as_ref()
                            .and_then(|raw| raw.get("proven_unanswered"))
                            == Some(&Value::Bool(true));
                        let retryable = proven_unanswered
                            && should_retry_hdr.as_deref() != Some("false")
                            && (matches!(status, 408 | 409 | 429) || status >= 500);
                        last_resp = Some(failed);
                        if !retryable || attempt_idx + 1 >= max_attempts {
                            return last_resp.expect("set");
                        }
                    } else {
                        let msg_map = if streaming && content_type.contains("text/event-stream") {
                            match assemble_sse_message(&text_body) {
                                Ok(m) => m,
                                Err(err) => {
                                    return self.fail(
                                        &format!("unparseable response body (ValueError: {err})"),
                                        None,
                                    );
                                }
                            }
                        } else {
                            let val: Value = match serde_json::from_str(&text_body) {
                                Ok(v) => v,
                                Err(err) => {
                                    let label =
                                        if err.to_string().contains("recursion limit exceeded") {
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
                            match val {
                                Value::Object(m) => m,
                                _ => {
                                    return self
                                        .fail("malformed response (expected JSON object)", None);
                                }
                            }
                        };

                        return match self.to_response(&msg_map, request, req_id.as_deref()) {
                            Ok(r) => r,
                            Err(problem) => {
                                self.fail(&format!("malformed response ({problem})"), None)
                            }
                        };
                    }
                }
                Err(err) => {
                    let cls = if err.is_timeout() {
                        "APITimeoutError"
                    } else {
                        "APIConnectionError"
                    };
                    let failed = self.fail(&format!("{cls}: {err}"), None);
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

    fn to_response(
        &self,
        message: &Map<String, Value>,
        request: &ModelRequest,
        header_request_id: Option<&str>,
    ) -> Result<ModelResponse, String> {
        let content = message
            .get("content")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let mut raw = Map::new();
        raw.insert(
            "id".to_string(),
            message.get("id").cloned().unwrap_or(Value::Null),
        );
        let api_usage = message.get("usage");
        if let Some(ru) = api_usage.and_then(raw_usage_payload) {
            raw.insert("raw_usage".to_string(), Value::Object(ru));
        }
        if let Some(rid) = header_request_id {
            raw.insert("request_id".to_string(), Value::String(rid.to_string()));
        }

        let stop_reason = message
            .get("stop_reason")
            .and_then(Value::as_str)
            .map(str::to_string);
        let served_model = message
            .get("model")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(str::to_string);

        let mut response = ModelResponse {
            stop_reason: stop_reason.clone(),
            usage: parse_usage(api_usage),
            served_model,
            raw: Some(raw),
            ..ModelResponse::default()
        };

        let mut texts = Vec::new();
        for block in &content {
            let obj = block
                .as_object()
                .ok_or_else(|| "content block must be object".to_string())?;
            if obj.get("type").and_then(Value::as_str) == Some("text")
                && let Some(t) = obj.get("text").and_then(Value::as_str)
            {
                texts.push(t.to_string());
            }
        }

        if stop_reason.as_deref() == Some("refusal") {
            response.refused = true;
            let joined = texts.concat();
            response.text = if joined.is_empty() {
                None
            } else {
                Some(joined)
            };
            if let Some(sd) = message.get("stop_details")
                && !sd.is_null()
                && let Some(ref mut r) = response.raw
            {
                r.insert("stop_details".to_string(), sd.clone());
            }
            return Ok(response);
        }

        let mut calls = Vec::new();
        for block in &content {
            let obj = block
                .as_object()
                .ok_or_else(|| "content block must be object".to_string())?;
            if obj.get("type").and_then(Value::as_str) == Some("tool_use") {
                let id = obj
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "tool-call id and name must be strings".to_string())?;
                let name = obj
                    .get("name")
                    .and_then(Value::as_str)
                    .ok_or_else(|| "tool-call id and name must be strings".to_string())?;
                let input = obj
                    .get("input")
                    .and_then(Value::as_object)
                    .cloned()
                    .ok_or_else(|| "tool-call input must be an object".to_string())?;
                calls.push(ToolCall {
                    id: id.to_string(),
                    name: name.to_string(),
                    arguments: input,
                });
            }
        }
        if !calls.is_empty() {
            if let Ok(mut guard) = self.tool_turns.lock() {
                guard.insert_turn(content.clone(), &calls);
            }
            if let Some(ref mut r) = response.raw {
                r.insert(ASSISTANT_CONTENT_KEY.to_string(), Value::Array(content));
            }
        }
        response.tool_calls = calls;

        if request.output_schema.is_some() && !texts.is_empty() {
            response.text = Some(texts[0].clone());
            response.parsed = load_object(&texts[0]);
        } else {
            let joined = texts.concat();
            response.text = if joined.is_empty() {
                None
            } else {
                Some(joined)
            };
        }

        Ok(response)
    }
}

#[async_trait]
impl Provider for AnthropicProvider {
    fn kind(&self) -> &str {
        self.kind
    }

    fn model(&self) -> &str {
        &self.config.model
    }

    fn python_type_identity(&self) -> (&'static str, &'static str) {
        ("identitybench.providers.anthropic_api", "AnthropicProvider")
    }

    fn local_retry_limit(&self) -> Option<u32> {
        Some(self.config.max_retries)
    }

    fn endpoint_identity(&self) -> String {
        let fallback_unset;
        let base = match &self.base_url {
            Some(b) => b.as_str(),
            None => {
                let env_name = self.config.base_url_env.as_deref().unwrap_or("");
                fallback_unset = format!("unset:{env_name}");
                &fallback_unset
            }
        };
        let identity = hash_endpoint(&[
            Some(base),
            Some(self.config.model.as_str()),
            self.config.cache_namespace.as_deref(),
        ]);
        if self.config.allow_custom_endpoint {
            format!("custom-endpoint-opt-in:{identity}")
        } else {
            identity
        }
    }

    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities::new(true, true, accepts_temperature(&self.config.model))
    }

    async fn complete(&self, request: &ModelRequest) -> ModelResponse {
        let start = Instant::now();
        let mut response = sanitize_response(self.complete_inner(request).await, self.kind);
        response.latency_s = start.elapsed().as_secs_f64();
        if request.params.temperature.is_some() && !accepts_temperature(&self.config.model) {
            let mut raw = response.raw.take().unwrap_or_default();
            raw.insert(
                "ignored_params".to_string(),
                Value::Array(vec![Value::String("temperature".to_string())]),
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

    fn remember_response(&self, response: &ModelResponse) {
        if response.tool_calls.is_empty() {
            return;
        }
        let Some(Value::Array(blocks)) = response
            .raw
            .as_ref()
            .and_then(|r| r.get(ASSISTANT_CONTENT_KEY))
        else {
            return;
        };
        let valid: Vec<Value> = blocks.iter().filter(|b| b.is_object()).cloned().collect();
        if !valid.is_empty()
            && let Ok(mut guard) = self.tool_turns.lock()
        {
            guard.insert_turn(valid, &response.tool_calls);
        }
    }
}

fn assemble_sse_message(body: &str) -> Result<Map<String, Value>, String> {
    let mut message: Option<Map<String, Value>> = None;
    let mut blocks: Vec<Map<String, Value>> = Vec::new();
    let mut partial_json: HashMap<usize, String> = HashMap::new();

    for line in body.lines() {
        let Some(data) = line.strip_prefix("data: ") else {
            continue;
        };
        if data == "[DONE]" {
            continue;
        }
        let event = load_object(data).ok_or_else(|| "invalid SSE JSON event".to_string())?;
        match event.get("type").and_then(Value::as_str) {
            Some("message_start") => {
                if let Some(Value::Object(m)) = event.get("message") {
                    message = Some(m.clone());
                }
            }
            Some("content_block_start") => {
                if let Some(Value::Object(cb)) = event.get("content_block") {
                    blocks.push(cb.clone());
                }
            }
            Some("content_block_delta") => {
                let idx = event
                    .get("index")
                    .and_then(Value::as_u64)
                    .map(|u| u as usize)
                    .unwrap_or_else(|| blocks.len().saturating_sub(1));
                if let (Some(block), Some(Value::Object(delta))) =
                    (blocks.get_mut(idx), event.get("delta"))
                {
                    match delta.get("type").and_then(Value::as_str) {
                        Some("text_delta") => {
                            if let Some(t) = delta.get("text").and_then(Value::as_str) {
                                let cur = block
                                    .get("text")
                                    .and_then(Value::as_str)
                                    .unwrap_or("")
                                    .to_string();
                                block
                                    .insert("text".to_string(), Value::String(format!("{cur}{t}")));
                            }
                        }
                        Some("thinking_delta") => {
                            if let Some(t) = delta.get("thinking").and_then(Value::as_str) {
                                let cur = block
                                    .get("thinking")
                                    .and_then(Value::as_str)
                                    .unwrap_or("")
                                    .to_string();
                                block.insert(
                                    "thinking".to_string(),
                                    Value::String(format!("{cur}{t}")),
                                );
                            }
                        }
                        Some("signature_delta") => {
                            if let Some(s) = delta.get("signature").and_then(Value::as_str) {
                                let cur = block
                                    .get("signature")
                                    .and_then(Value::as_str)
                                    .unwrap_or("")
                                    .to_string();
                                block.insert(
                                    "signature".to_string(),
                                    Value::String(format!("{cur}{s}")),
                                );
                            }
                        }
                        Some("input_json_delta") => {
                            if let Some(j) = delta.get("partial_json").and_then(Value::as_str) {
                                partial_json.entry(idx).or_default().push_str(j);
                            }
                        }
                        _ => {}
                    }
                }
            }
            Some("content_block_stop") => {
                if let Some(idx) = event
                    .get("index")
                    .and_then(Value::as_u64)
                    .map(|u| u as usize)
                    && let Some(json_str) = partial_json.remove(&idx)
                    && let Some(block) = blocks.get_mut(idx)
                    && let Some(parsed) = load_object(&json_str)
                {
                    block.insert("input".to_string(), Value::Object(parsed));
                }
            }
            Some("message_delta") => {
                if let Some(ref mut msg) = message {
                    if let Some(Value::Object(delta)) = event.get("delta") {
                        for (k, v) in delta {
                            msg.insert(k.clone(), v.clone());
                        }
                    }
                    if let Some(Value::Object(delta_usage)) = event.get("usage") {
                        let mut existing = msg
                            .get("usage")
                            .and_then(Value::as_object)
                            .cloned()
                            .unwrap_or_default();
                        for (k, v) in delta_usage {
                            existing.insert(k.clone(), v.clone());
                        }
                        msg.insert("usage".to_string(), Value::Object(existing));
                    }
                }
            }
            Some("error") => {
                let err_msg = event
                    .get("error")
                    .and_then(Value::as_object)
                    .and_then(|e| e.get("message"))
                    .and_then(Value::as_str)
                    .unwrap_or("stream error");
                return Err(err_msg.to_string());
            }
            _ => {}
        }
    }

    let mut msg = message.ok_or_else(|| "missing message_start event".to_string())?;
    msg.insert(
        "content".to_string(),
        Value::Array(blocks.into_iter().map(Value::Object).collect()),
    );
    Ok(msg)
}

fn resolve_base_url(
    base_url_env: Option<&str>,
    env_override: Option<&BTreeMap<String, String>>,
) -> Option<String> {
    let get = |name: &str| -> Option<String> {
        if let Some(map) = env_override {
            map.get(name).cloned()
        } else {
            std::env::var(name).ok()
        }
    };
    if let Some(env_name) = base_url_env {
        let val = get(env_name).filter(|v| !v.is_empty())?;
        return Some(val.trim_end_matches('/').to_string());
    }
    let val = get("ANTHROPIC_BASE_URL")
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| DEFAULT_BASE_URL.to_string());
    Some(val.trim_end_matches('/').to_string())
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
