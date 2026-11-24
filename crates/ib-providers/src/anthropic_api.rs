//! Anthropic Messages API provider (`anthropic`) and Messages-compatible servers (`anthropic_compatible`).

use std::collections::{BTreeMap, HashMap};
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use reqwest::header::{AUTHORIZATION, CONTENT_TYPE, HeaderMap, HeaderValue};
use serde_json::{Map, Value};

use crate::base::{
    Message, ModelRequest, ModelResponse, Provider, ProviderCapabilities, Role, ToolCall, Usage,
    hash_endpoint,
};
use crate::env::paired_api_key_env;
use crate::http::{
    MAX_PROVIDER_RESPONSE_BYTES, PLACEHOLDER_API_KEY, Redactor, env_var, is_vendor_host,
    read_bounded_response_body, redirect_provider_error, status_error_class,
};
use crate::json::{
    BoundedJsonError, MAX_RESPONSE_DEPTH, MAX_RESPONSE_NODES, MAX_USAGE_DEPTH, MAX_USAGE_NODES,
    bounded_token_count, load_object, parse_bounded_json, raw_usage_payload, sanitize_response,
    value_node_count,
};

pub const DEFAULT_BASE_URL: &str = "https://api.anthropic.com";
pub const DEFAULT_API_KEY_ENV: &str = "ANTHROPIC_API_KEY";
pub const STREAMING_THRESHOLD_TOKENS: u64 = 16_000;
const VENDOR_HOST: &str = "api.anthropic.com";
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

#[derive(Clone)]
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

impl std::fmt::Debug for AnthropicConfig {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AnthropicConfig").finish_non_exhaustive()
    }
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
            && !is_vendor_host(base_url.as_deref(), VENDOR_HOST)
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
        env_var(self.config.env_override.as_ref(), name)
    }

    fn redactor(&self) -> Redactor<'_> {
        Redactor::new(
            self.config.env_override.as_ref(),
            self.resolved_api_key_env.as_deref(),
            self.config.base_url_env.as_deref(),
        )
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
            Value::Array(Self::api_messages(&request.messages)),
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
        if let Some(ref effort) = request.params.effort {
            output_config.insert("effort".to_string(), Value::String(effort.clone()));
        }
        if !output_config.is_empty() {
            params.insert("output_config".to_string(), Value::Object(output_config));
        }

        if let Some(temp) = request.params.temperature
            && accepts_temperature(&self.config.model)
        {
            params.insert("temperature".to_string(), Value::from(temp));
        }
        params
    }

    fn api_messages(messages: &[Message]) -> Vec<Value> {
        let mut api: Vec<Map<String, Value>> = Vec::new();
        for message in messages {
            let text_block = || {
                let mut b = Map::new();
                b.insert("type".to_string(), Value::String("text".to_string()));
                b.insert("text".to_string(), Value::String(message.content.clone()));
                Value::Object(b)
            };
            if message.role == Role::Assistant {
                let blocks = if message.content.is_empty() {
                    Vec::new()
                } else {
                    vec![text_block()]
                };
                let mut m = Map::new();
                m.insert("role".to_string(), Value::String("assistant".to_string()));
                m.insert("content".to_string(), Value::Array(blocks));
                api.push(m);
                continue;
            }
            let block = text_block();
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
        self.redactor().fail(self.kind, message, raw)
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
                            let val: Value = match parse_bounded_json(
                                &text_body,
                                MAX_RESPONSE_NODES,
                                MAX_RESPONSE_DEPTH,
                            ) {
                                Ok((value, _)) => value,
                                Err(BoundedJsonError::TooManyNodes | BoundedJsonError::TooDeep) => {
                                    return self.fail(
                                        "malformed response (response exceeds structural limits)",
                                        None,
                                    );
                                }
                                Err(BoundedJsonError::Invalid) => {
                                    let err = serde_json::from_str::<Value>(&text_body)
                                        .expect_err("bounded JSON scan rejected invalid JSON");
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

                        return match self.to_response(&msg_map, req_id.as_deref()) {
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
        if !calls.is_empty()
            && let Some(ref mut r) = response.raw
        {
            r.insert(ASSISTANT_CONTENT_KEY.to_string(), Value::Array(content));
        }
        response.tool_calls = calls;

        let joined = texts.concat();
        response.text = if joined.is_empty() {
            None
        } else {
            Some(joined)
        };
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
        self.redactor().response(&mut response);
        response
    }
}

fn assemble_sse_message(body: &str) -> Result<Map<String, Value>, String> {
    assemble_sse_message_with_limit(body, MAX_RESPONSE_NODES)
}

fn assemble_sse_message_with_limit(
    body: &str,
    max_response_nodes: usize,
) -> Result<Map<String, Value>, String> {
    let mut message: Option<Map<String, Value>> = None;
    let mut blocks: Vec<Map<String, Value>> = Vec::new();
    let mut partial_json: HashMap<usize, String> = HashMap::new();
    let mut retained_nodes = 0usize;

    for line in body.lines() {
        let Some(data) = line.strip_prefix("data: ") else {
            continue;
        };
        if data == "[DONE]" {
            continue;
        }
        let (event_value, _) = parse_bounded_json(data, max_response_nodes, MAX_RESPONSE_DEPTH)
            .map_err(|error| match error {
                BoundedJsonError::TooManyNodes | BoundedJsonError::TooDeep => {
                    "SSE response exceeds structural limits".to_string()
                }
                BoundedJsonError::Invalid => "invalid SSE JSON event".to_string(),
            })?;
        let Some(event) = event_value.as_object() else {
            return Err("invalid SSE JSON event".to_string());
        };
        match event.get("type").and_then(Value::as_str) {
            Some("message_start") => {
                if let Some(message_value) = event.get("message").filter(|v| v.is_object()) {
                    if message.is_some() {
                        return Err("duplicate SSE message_start event".to_string());
                    }
                    retained_nodes =
                        value_node_count(message_value, max_response_nodes, MAX_RESPONSE_DEPTH)
                            .map_err(|_| "SSE response exceeds structural limits".to_string())?;
                    let Value::Object(m) = message_value else {
                        return Err("invalid SSE message_start event".to_string());
                    };
                    if let Some(usage) = m.get("usage") {
                        value_node_count(usage, MAX_USAGE_NODES, MAX_USAGE_DEPTH)
                            .map_err(|_| "SSE usage exceeds structural limits".to_string())?;
                    }
                    message = Some(m.clone());
                }
            }
            Some("content_block_start") => {
                if let Some(block_value) = event.get("content_block").filter(|v| v.is_object()) {
                    if blocks.len() >= max_response_nodes {
                        return Err("SSE response exceeds structural limits".to_string());
                    }
                    let block_nodes = value_node_count(
                        block_value,
                        max_response_nodes.saturating_sub(retained_nodes),
                        MAX_RESPONSE_DEPTH,
                    )
                    .map_err(|_| "SSE response exceeds structural limits".to_string())?;
                    retained_nodes = retained_nodes.saturating_add(block_nodes);
                    let Value::Object(cb) = block_value else {
                        return Err("invalid SSE content block".to_string());
                    };
                    blocks.push(cb.clone());
                }
            }
            Some("content_block_delta") => {
                let idx = match event.get("index") {
                    Some(index) => index.as_u64().and_then(|value| usize::try_from(value).ok()),
                    None => Some(blocks.len().saturating_sub(1)),
                };
                if let Some(idx) = idx
                    && let (Some(block), Some(Value::Object(delta))) =
                        (blocks.get_mut(idx), event.get("delta"))
                {
                    match delta.get("type").and_then(Value::as_str) {
                        Some("text_delta") => {
                            if let Some(t) = delta.get("text").and_then(Value::as_str) {
                                retained_nodes +=
                                    usize::from(append_string_field(block, "text", t));
                            }
                        }
                        Some("thinking_delta") => {
                            if let Some(t) = delta.get("thinking").and_then(Value::as_str) {
                                retained_nodes +=
                                    usize::from(append_string_field(block, "thinking", t));
                            }
                        }
                        Some("signature_delta") => {
                            if let Some(s) = delta.get("signature").and_then(Value::as_str) {
                                retained_nodes +=
                                    usize::from(append_string_field(block, "signature", s));
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
                    .and_then(|value| usize::try_from(value).ok())
                    && let Some(json_str) = partial_json.remove(&idx)
                    && let Some(block) = blocks.get_mut(idx)
                    && json_str.chars().count() <= crate::json::MAX_JSON_TEXT
                    && let Ok((Value::Object(parsed), parsed_nodes)) = parse_bounded_json(
                        &json_str,
                        max_response_nodes.saturating_sub(retained_nodes),
                        MAX_RESPONSE_DEPTH,
                    )
                {
                    retained_nodes = retained_nodes.saturating_add(parsed_nodes);
                    block.insert("input".to_string(), Value::Object(parsed));
                }
            }
            Some("message_delta") => {
                if let Some(ref mut msg) = message {
                    if let Some(Value::Object(delta)) = event.get("delta") {
                        merge_object(msg, delta, &mut retained_nodes, max_response_nodes)?;
                    }
                    if let Some(Value::Object(delta_usage)) = event.get("usage") {
                        let usage_value = event.get("usage").expect("matched usage object");
                        value_node_count(usage_value, MAX_USAGE_NODES, MAX_USAGE_DEPTH)
                            .map_err(|_| "SSE usage exceeds structural limits".to_string())?;
                        if let Some(Value::Object(existing)) = msg.get_mut("usage") {
                            merge_object(
                                existing,
                                delta_usage,
                                &mut retained_nodes,
                                max_response_nodes,
                            )?;
                            let usage_value =
                                msg.get("usage").expect("usage object remains present");
                            value_node_count(usage_value, MAX_USAGE_NODES, MAX_USAGE_DEPTH)
                                .map_err(|_| "SSE usage exceeds structural limits".to_string())?;
                        } else {
                            let usage_nodes = value_node_count(
                                usage_value,
                                max_response_nodes.saturating_sub(retained_nodes),
                                MAX_RESPONSE_DEPTH,
                            )
                            .map_err(|_| "SSE response exceeds structural limits".to_string())?;
                            retained_nodes = retained_nodes.saturating_add(usage_nodes);
                            msg.insert("usage".to_string(), Value::Object(delta_usage.clone()));
                        }
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
    if retained_nodes >= max_response_nodes {
        return Err("SSE response exceeds structural limits".to_string());
    }
    msg.insert(
        "content".to_string(),
        Value::Array(blocks.into_iter().map(Value::Object).collect()),
    );
    Ok(msg)
}

fn append_string_field(map: &mut Map<String, Value>, key: &str, suffix: &str) -> bool {
    match map.entry(key.to_string()) {
        serde_json::map::Entry::Vacant(entry) => {
            entry.insert(Value::String(suffix.to_string()));
            true
        }
        serde_json::map::Entry::Occupied(mut entry) => {
            if let Value::String(value) = entry.get_mut() {
                value.push_str(suffix);
            }
            false
        }
    }
}

fn merge_object(
    target: &mut Map<String, Value>,
    source: &Map<String, Value>,
    retained_nodes: &mut usize,
    max_nodes: usize,
) -> Result<(), String> {
    for (key, value) in source {
        let old_nodes = target.get(key).map_or(0, |old| {
            value_node_count(old, max_nodes, MAX_RESPONSE_DEPTH).unwrap_or(max_nodes)
        });
        let new_nodes = value_node_count(
            value,
            max_nodes.saturating_sub(retained_nodes.saturating_sub(old_nodes)),
            MAX_RESPONSE_DEPTH,
        )
        .map_err(|_| "SSE response exceeds structural limits".to_string())?;
        *retained_nodes = retained_nodes
            .saturating_sub(old_nodes)
            .saturating_add(new_nodes);
        if *retained_nodes > max_nodes {
            return Err("SSE response exceeds structural limits".to_string());
        }
        target.insert(key.clone(), value.clone());
    }
    Ok(())
}

fn resolve_base_url(
    base_url_env: Option<&str>,
    env_override: Option<&BTreeMap<String, String>>,
) -> Option<String> {
    let get = |name: &str| env_var(env_override, name);
    if let Some(env_name) = base_url_env {
        let val = get(env_name).filter(|v| !v.is_empty())?;
        return Some(val.trim_end_matches('/').to_string());
    }
    let val = get("ANTHROPIC_BASE_URL")
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| DEFAULT_BASE_URL.to_string());
    Some(val.trim_end_matches('/').to_string())
}

#[cfg(test)]
mod sse_security_tests {
    use super::assemble_sse_message_with_limit;

    #[test]
    fn sse_events_share_a_preparse_node_budget_and_deltas_append_in_order() {
        let mut body =
            String::from("data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg\"}}\n\n");
        body.push_str(
            "data: {\"type\":\"content_block_start\",\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n",
        );
        for _ in 0..1_000 {
            body.push_str(
                "data: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"x\"}}\n\n",
            );
        }
        let assembled = assemble_sse_message_with_limit(&body, 64).unwrap();
        assert_eq!(
            assembled["content"][0]["text"].as_str().unwrap(),
            "x".repeat(1_000)
        );

        let invalid_index = concat!(
            "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg\"}}\n\n",
            "data: {\"type\":\"content_block_start\",\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n",
            "data: {\"type\":\"content_block_delta\",\"index\":18446744073709551615,\"delta\":{\"type\":\"text_delta\",\"text\":\"ignored\"}}\n\n",
            "data: {\"type\":\"content_block_delta\",\"index\":\"bad\",\"delta\":{\"type\":\"text_delta\",\"text\":\"ignored\"}}\n\n",
        );
        let assembled = assemble_sse_message_with_limit(invalid_index, 64).unwrap();
        assert_eq!(assembled["content"][0]["text"], "");

        let small_budget = "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg\"}}\n\n";
        assert!(matches!(
            assemble_sse_message_with_limit(small_budget, 2),
            Err(message) if message == "SSE response exceeds structural limits"
        ));
    }
}
