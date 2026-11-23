use std::collections::{BTreeMap, VecDeque};
use std::sync::{Arc, Mutex};

use ib_providers::anthropic_api::{
    ASSISTANT_CONTENT_KEY, AnthropicConfig, AnthropicProvider,
    accepts_temperature as anthropic_accepts_temperature,
};
use ib_providers::base::{Message, ModelRequest, Provider, ToolCall, ToolSpec};
use ib_providers::openai_compat::{
    DEFAULT_BASE_URL, MaxTokensParam, OpenAICompatConfig, OpenAICompatProvider, OpenAiApi,
    UNPARSED_ARGUMENTS_KEY, accepts_temperature as openai_accepts_temperature, default_api_key_env,
    headers_problem, is_vendor_endpoint, provider_routing_problem,
};
use serde_json::{Map, Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

#[derive(Clone)]
struct MockReply {
    status: u16,
    content_type: String,
    headers: Vec<(String, String)>,
    body: String,
}

impl MockReply {
    fn json(status: u16, body: Value) -> Self {
        Self {
            status,
            content_type: "application/json".to_string(),
            headers: Vec::new(),
            body: body.to_string(),
        }
    }

    fn raw(status: u16, content_type: &str, body: impl Into<String>) -> Self {
        Self {
            status,
            content_type: content_type.to_string(),
            headers: Vec::new(),
            body: body.into(),
        }
    }
}

#[derive(Debug, Clone)]
struct RecordedHttpRequest {
    path: String,
    headers: BTreeMap<String, String>,
    body: Value,
}

async fn spawn_mock_server(
    replies: Vec<MockReply>,
) -> (String, Arc<Mutex<Vec<RecordedHttpRequest>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let queue = Arc::new(Mutex::new(VecDeque::from(replies)));
    let recorded = Arc::new(Mutex::new(Vec::new()));
    let q_clone = Arc::clone(&queue);
    let rec_clone = Arc::clone(&recorded);

    tokio::spawn(async move {
        loop {
            let Ok((mut socket, _)) = listener.accept().await else {
                break;
            };
            let q = Arc::clone(&q_clone);
            let rec = Arc::clone(&rec_clone);
            tokio::spawn(async move {
                let mut buf = Vec::new();
                let mut tmp = [0u8; 4096];
                let mut header_end = None;
                let mut content_length = 0usize;

                loop {
                    let n = socket.read(&mut tmp).await.unwrap_or(0);
                    if n == 0 {
                        break;
                    }
                    buf.extend_from_slice(&tmp[..n]);
                    if header_end.is_none()
                        && let Some(pos) = buf.windows(4).position(|w| w == b"\r\n\r\n")
                    {
                        header_end = Some(pos + 4);
                        let hdr_str = String::from_utf8_lossy(&buf[..pos]);
                        for line in hdr_str.lines().skip(1) {
                            if let Some((k, v)) = line.split_once(':')
                                && k.trim().eq_ignore_ascii_case("content-length")
                            {
                                content_length = v.trim().parse().unwrap_or(0);
                            }
                        }
                    }
                    if let Some(he) = header_end
                        && buf.len() >= he + content_length
                    {
                        break;
                    }
                }

                let he = header_end.unwrap_or(buf.len());
                let hdr_str = String::from_utf8_lossy(&buf[..he]);
                let first_line = hdr_str.lines().next().unwrap_or("");
                let path = first_line
                    .split_whitespace()
                    .nth(1)
                    .unwrap_or("/")
                    .to_string();
                let mut hdrs = BTreeMap::new();
                for line in hdr_str.lines().skip(1) {
                    if let Some((k, v)) = line.split_once(':') {
                        hdrs.insert(k.trim().to_lowercase(), v.trim().to_string());
                    }
                }
                let body_bytes = &buf[he..buf.len().min(he + content_length)];
                let body_val = serde_json::from_slice(body_bytes).unwrap_or(Value::Null);
                rec.lock().unwrap().push(RecordedHttpRequest {
                    path,
                    headers: hdrs,
                    body: body_val,
                });

                let reply = q.lock().unwrap().pop_front().unwrap_or_else(|| {
                    MockReply::json(500, json!({"error": {"message": "exhausted"}}))
                });

                let mut resp_text = format!(
                    "HTTP/1.1 {} OK\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n",
                    reply.status,
                    reply.content_type,
                    reply.body.len()
                );
                for (k, v) in reply.headers {
                    resp_text.push_str(&format!("{k}: {v}\r\n"));
                }
                resp_text.push_str("\r\n");
                let _ = socket.write_all(resp_text.as_bytes()).await;
                let _ = socket.write_all(reply.body.as_bytes()).await;
                let _ = socket.shutdown().await;
            });
        }
    });

    (format!("http://{addr}"), recorded)
}

fn lookup_tool() -> ToolSpec {
    ToolSpec {
        name: "get_identity".to_string(),
        description: "Look up an identity by id.".to_string(),
        input_schema: json!({"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": false})
            .as_object()
            .unwrap()
            .clone(),
    }
}

fn decision_schema() -> Map<String, Value> {
    json!({"type": "object", "properties": {"decision": {"type": "string"}}, "required": ["decision"], "additionalProperties": false})
        .as_object()
        .unwrap()
        .clone()
}

#[test]
fn endpoint_helpers_and_temperature_tables() {
    assert!(is_vendor_endpoint(Some(DEFAULT_BASE_URL)));
    assert!(!is_vendor_endpoint(Some("http://127.0.0.1:8000/v1")));
    assert!(!openai_accepts_temperature("gpt-5"));
    assert!(!openai_accepts_temperature("ft:o3-mini"));
    assert!(openai_accepts_temperature("gpt-50"));
    assert!(openai_accepts_temperature("o1x"));

    assert_eq!(default_api_key_env(None).as_deref(), Some("OPENAI_API_KEY"));
    assert_eq!(
        default_api_key_env(Some("OPENROUTER_BASE_URL")).as_deref(),
        Some("OPENROUTER_API_KEY")
    );
    assert_eq!(default_api_key_env(Some("ANTHROPIC_BASE_URL")), None);

    assert!(!anthropic_accepts_temperature("claude-opus-5-5"));
    assert!(!anthropic_accepts_temperature(
        "us.anthropic.claude-opus-4-8-v1"
    ));
    assert!(anthropic_accepts_temperature("claude-haiku-4-5"));

    assert!(headers_problem(&json!({"Authorization": "Bearer k"})).is_some());
    assert!(headers_problem(&json!({"x-opencode-session": "id"})).is_none());
    assert!(provider_routing_problem(&json!({"allow_fallbacks": "false"})).is_some());
    assert!(
        provider_routing_problem(&json!({"order": ["Baidu"], "allow_fallbacks": false})).is_none()
    );
}

#[tokio::test]
async fn openai_chat_completions_byok_cost_routing_headers_and_usage() {
    let ok_reply = MockReply::json(
        200,
        json!({
            "id": "chatcmpl_1",
            "model": "deepseek/deepseek-v4",
            "provider": "Baidu",
            "system_fingerprint": "fp_1",
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 40,
                "prompt_tokens_details": {"cached_tokens": 20},
                "completion_tokens_details": {"reasoning_tokens": 10},
                "cost": 0.0001,
                "is_byok": true,
                "cost_details": {"upstream_inference_cost": 0.002}
            },
            "choices": [{
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "{\"decision\":\"DENY\"}",
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_identity", "arguments": "{bad-json"}
                    }]
                }
            }]
        }),
    );

    let (base, recorded) = spawn_mock_server(vec![ok_reply]).await;
    let env = BTreeMap::from([
        ("OPENROUTER_BASE_URL".to_string(), format!("{base}/v1")),
        (
            "OPENROUTER_API_KEY".to_string(),
            "or-secret-key-9999".to_string(),
        ),
        (
            "OPENAI_API_KEY".to_string(),
            "sk-should-never-be-sent".to_string(),
        ),
    ]);
    let mut cfg = OpenAICompatConfig::new("deepseek/deepseek-v4");
    cfg.base_url_env = Some("OPENROUTER_BASE_URL".to_string());
    cfg.headers = Some(BTreeMap::from([(
        "x-opencode-session".to_string(),
        "ib".to_string(),
    )]));
    cfg.provider_routing = Some(
        json!({"order": ["Baidu"], "allow_fallbacks": false})
            .as_object()
            .unwrap()
            .clone(),
    );
    cfg.max_tokens_param = MaxTokensParam::MaxTokens;
    cfg.retry_base_delay_s = 0.0;
    cfg.env_override = Some(env);

    let p = OpenAICompatProvider::new(cfg).unwrap();
    let mut req = ModelRequest::new("SYS", vec![Message::user("Decide.")]);
    req.output_schema = Some(decision_schema());
    req.tools = vec![lookup_tool()];
    req.params.temperature = Some(0.2);
    req.params.effort = Some("low".to_string());

    let resp = p.complete(&req).await;
    assert!(resp.ok());
    assert_eq!(resp.usage.input_tokens, 100);
    assert_eq!(resp.usage.cache_read_tokens, 20);
    assert_eq!(resp.usage.output_tokens, 40);
    assert_eq!(resp.usage.reasoning_tokens, 10);
    assert!((resp.reported_cost_usd.unwrap() - 0.0021).abs() < 1e-12);
    assert_eq!(resp.parsed.as_ref().unwrap()["decision"], json!("DENY"));
    assert_eq!(
        resp.tool_calls[0].arguments,
        json!({UNPARSED_ARGUMENTS_KEY: "{bad-json"})
            .as_object()
            .unwrap()
            .clone()
    );
    let raw = resp.raw.as_ref().unwrap();
    assert_eq!(raw["upstream_provider"], json!("Baidu"));
    assert_eq!(raw["is_byok"], json!(true));
    assert_eq!(raw["system_fingerprint"], json!("fp_1"));

    let reqs = recorded.lock().unwrap();
    assert_eq!(reqs.len(), 1);
    assert_eq!(reqs[0].path, "/v1/chat/completions");
    assert_eq!(
        reqs[0].headers["authorization"],
        "Bearer or-secret-key-9999"
    );
    assert_eq!(reqs[0].headers["x-opencode-session"], "ib");
    assert_eq!(reqs[0].body["provider"]["order"], json!(["Baidu"]));
    assert_eq!(reqs[0].body["max_tokens"], json!(4096));
}

#[tokio::test]
async fn openai_rate_limit_without_explicit_no_response_proof_is_not_retried() {
    let mut rate_limited = MockReply::json(
        429,
        json!({"error": {"code": "rate_limit_exceeded", "message": "slow down"}}),
    );
    rate_limited
        .headers
        .push(("x-request-id".to_string(), "req_429".to_string()));
    let success = MockReply::json(
        200,
        json!({
            "id":"chatcmpl_1",
            "model":"test-model",
            "choices":[{"message":{"role":"assistant","content":"done"},"finish_reason":"stop"}]
        }),
    );
    let (base, recorded) = spawn_mock_server(vec![rate_limited, success]).await;
    let mut config = OpenAICompatConfig::new("test-model");
    config.base_url_env = Some("OPENAI_TEST_URL".to_string());
    config.api_key_env = Some("OPENAI_TEST_KEY".to_string());
    config.retry_base_delay_s = 0.0;
    config.env_override = Some(BTreeMap::from([
        ("OPENAI_TEST_URL".to_string(), format!("{base}/v1")),
        ("OPENAI_TEST_KEY".to_string(), "test-key".to_string()),
    ]));

    let provider = OpenAICompatProvider::new(config).unwrap();
    let response = provider.complete(&ModelRequest::default()).await;
    assert!(response.error.is_some());
    let raw = response.raw.as_ref().unwrap();
    assert_eq!(raw["http_status"], json!(429));
    assert!(!raw.contains_key("proven_unanswered"));
    assert_eq!(recorded.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn anthropic_rate_limit_without_explicit_no_response_proof_is_not_retried() {
    let mut rate_limited = MockReply::json(
        429,
        json!({"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}),
    );
    rate_limited
        .headers
        .push(("request-id".to_string(), "req_429".to_string()));
    let success = MockReply::json(
        200,
        json!({
            "id":"msg_1",
            "type":"message",
            "role":"assistant",
            "model":"test-model",
            "stop_reason":"end_turn",
            "content":[{"type":"text","text":"done"}],
            "usage":{"input_tokens":1,"output_tokens":1}
        }),
    );
    let (base, recorded) = spawn_mock_server(vec![rate_limited, success]).await;
    let mut config = AnthropicConfig::new("test-model");
    config.base_url_env = Some("ANTHROPIC_TEST_URL".into());
    config.api_key_env = Some("ANTHROPIC_TEST_KEY".into());
    config.retry_base_delay_s = 0.0;
    config.env_override = Some(BTreeMap::from([
        ("ANTHROPIC_TEST_URL".into(), base),
        ("ANTHROPIC_TEST_KEY".into(), "test-key".into()),
    ]));

    let provider = AnthropicProvider::new(config).unwrap();
    let response = provider.complete(&ModelRequest::default()).await;
    assert!(response.error.is_some());
    assert!(
        response
            .error
            .as_deref()
            .unwrap()
            .contains("RateLimitError 429")
    );
    let raw = response.raw.as_ref().unwrap();
    assert!(!raw.contains_key("proven_unanswered"));
    assert_eq!(recorded.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn openai_responses_api_malformed_body_and_redacted_errors() {
    let ok_resp = MockReply::json(
        200,
        json!({
            "id": "resp_1",
            "model": "responses-model",
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "content": [{"type": "output_text", "text": "{\"decision\":\"APPROVE\"}"}]},
                {"type": "function_call", "call_id": "fc_1", "name": "get_identity", "arguments": "{\"id\":\"U1\"}"}
            ],
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 300,
                "input_tokens_details": {"cached_tokens": 600},
                "output_tokens_details": {"reasoning_tokens": 200}
            }
        }),
    );
    let truncated_json = MockReply::raw(200, "application/json", "{\"id\":");
    let mut bad_req = MockReply::json(
        400,
        json!({"error": {"code": "BadCode", "message": "failed with my-secret-token-123 at https://secret-gw.invalid/v1"}}),
    );
    bad_req.headers.push((
        "x-request-id".to_string(),
        "req-my-secret-token-123".to_string(),
    ));

    let (base, recorded) = spawn_mock_server(vec![ok_resp, truncated_json, bad_req]).await;
    let env = BTreeMap::from([
        ("GW_URL".to_string(), format!("{base}/v1")),
        ("GW_KEY".to_string(), "my-secret-token-123".to_string()),
    ]);
    let mut cfg = OpenAICompatConfig::new("responses-model");
    cfg.base_url_env = Some("GW_URL".to_string());
    cfg.api_key_env = Some("GW_KEY".to_string());
    cfg.api = OpenAiApi::Responses;
    cfg.max_retries = 0;
    cfg.env_override = Some(env);

    let p = OpenAICompatProvider::new(cfg).unwrap();
    let mut req = ModelRequest::new("POLICY", vec![Message::user("Decide.")]);
    req.output_schema = Some(decision_schema());
    let r1 = p.complete(&req).await;
    assert!(r1.ok());
    assert_eq!(r1.stop_reason.as_deref(), Some("tool_calls"));
    assert_eq!(r1.usage.input_tokens, 400);
    assert_eq!(r1.usage.cache_read_tokens, 600);
    assert_eq!(r1.usage.reasoning_tokens, 200);
    assert_eq!(recorded.lock().unwrap()[0].path, "/v1/responses");
    assert_eq!(recorded.lock().unwrap()[0].body["store"], json!(false));

    let r2 = p.complete(&req).await;
    assert!(
        r2.error
            .as_deref()
            .unwrap()
            .contains("unparseable response body")
    );

    let r3 = p.complete(&req).await;
    let err3 = r3.error.as_deref().unwrap();
    assert!(!err3.contains("my-secret-token-123"));
    assert_eq!(r3.raw.as_ref().unwrap()["http_status"], json!(400));
    assert_eq!(r3.raw.as_ref().unwrap()["error_code"], json!("BadCode"));
    assert!(
        !r3.raw.as_ref().unwrap()["request_id"]
            .as_str()
            .unwrap()
            .contains("my-secret-token-123")
    );
}

#[tokio::test]
async fn anthropic_messages_tool_turn_replay_sse_stream_and_isolation() {
    let first_msg = MockReply::json(
        200,
        json!({
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "stop_reason": "tool_use",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig-abc"},
                {"type": "tool_use", "id": "toolu_9", "name": "get_identity", "input": {"id": "U9"}}
            ],
            "usage": {"input_tokens": 12, "output_tokens": 40, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 200, "output_tokens_details": {"thinking_tokens": 25}}
        }),
    );
    let sse_stream = MockReply::raw(
        200,
        "text/event-stream",
        concat!(
            "event: message_start\ndata: {\"type\":\"message_start\",\"message\":{\"id\":\"msg_2\",\"model\":\"claude-opus-5-5\",\"usage\":{\"input_tokens\":50,\"output_tokens\":0}}}\n\n",
            "event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"text\",\"text\":\"\"}}\n\n",
            "event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"{\\\"decision\\\":\\\"DENY\\\"}\"}}\n\n",
            "event: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\n",
            "event: message_delta\ndata: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"},\"usage\":{\"output_tokens\":15}}\n\n",
            "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
        ),
    );

    let (base, recorded) = spawn_mock_server(vec![first_msg, sse_stream]).await;
    let env = BTreeMap::from([
        ("LOCAL_ANTHROPIC_URL".to_string(), base),
        (
            "ANTHROPIC_API_KEY".to_string(),
            "sk-ant-real-never-sent".to_string(),
        ),
    ]);
    let mut cfg = AnthropicConfig::new("claude-opus-5-5");
    cfg.base_url_env = Some("LOCAL_ANTHROPIC_URL".to_string());
    cfg.max_retries = 0;
    cfg.env_override = Some(env);

    let p = AnthropicProvider::new(cfg).unwrap();
    assert_eq!(p.kind(), "anthropic_compatible");
    let mut req1 = ModelRequest::new("POLICY", vec![Message::user("Decide.")]);
    req1.tools = vec![lookup_tool()];
    req1.params.temperature = Some(0.0); // dropped for claude-opus-5-5!

    let r1 = p.complete(&req1).await;
    assert!(r1.ok());
    assert_eq!(
        r1.tool_calls,
        vec![ToolCall {
            id: "toolu_9".to_string(),
            name: "get_identity".to_string(),
            arguments: json!({"id": "U9"}).as_object().unwrap().clone(),
        }]
    );
    assert_eq!(
        r1.raw.as_ref().unwrap()["ignored_params"],
        json!(["temperature"])
    );
    assert!(r1.raw.as_ref().unwrap().contains_key(ASSISTANT_CONTENT_KEY));

    // Follow-up request replays original thinking block and uses SSE (>16k tokens)
    let mut req2 = ModelRequest::new(
        "POLICY",
        vec![
            Message::user("Decide."),
            Message::assistant("", r1.tool_calls.clone()),
            Message::tool("toolu_9", "U9 record"),
        ],
    );
    req2.output_schema = Some(decision_schema());
    req2.params.max_output_tokens = 20_000;

    let r2 = p.complete(&req2).await;
    assert!(r2.ok());
    assert_eq!(r2.parsed.as_ref().unwrap()["decision"], json!("DENY"));

    let reqs = recorded.lock().unwrap();
    assert_eq!(reqs.len(), 2);
    // Must send placeholder key, never ANTHROPIC_API_KEY!
    assert_eq!(reqs[0].headers["x-api-key"], "not-set");
    assert_eq!(reqs[0].path, "/v1/messages");
    assert!(reqs[0].body.get("temperature").is_none());
    // Follow-up replayed the thinking block unchanged!
    assert_eq!(
        reqs[1].body["messages"][1]["content"][0]["signature"],
        json!("sig-abc")
    );
    assert_eq!(reqs[1].body["stream"], json!(true));
}

#[test]
fn provider_routing_is_sent_verbatim_and_bound_into_endpoint_identity() {
    let make = |routing: Option<Value>| {
        let mut cfg = OpenAICompatConfig::new("deepseek/deepseek-v4");
        cfg.base_url_env = Some("OPENROUTER_BASE_URL".to_string());
        cfg.env_override = Some(BTreeMap::from([(
            "OPENROUTER_BASE_URL".to_string(),
            "https://openrouter.ai/api/v1".to_string(),
        )]));
        cfg.provider_routing = routing.map(|r| r.as_object().unwrap().clone());
        OpenAICompatProvider::new(cfg).unwrap()
    };
    // Members deliberately out of sorted order: the body keeps the configured object verbatim.
    let pinned = json!({"order": ["deepinfra"], "allow_fallbacks": false});
    let reordered = json!({"allow_fallbacks": false, "order": ["deepinfra"]});
    let other = json!({"order": ["together"], "allow_fallbacks": false});
    let req = ModelRequest::new("SYS", vec![Message::user("x")]);

    let unpinned = make(None);
    assert!(unpinned.request_params(&req).get("provider").is_none());
    let p = make(Some(pinned.clone()));
    let body = p.request_params(&req);
    assert_eq!(body["provider"], pinned);
    assert_eq!(
        serde_json::to_string(&body["provider"]).unwrap(),
        r#"{"order":["deepinfra"],"allow_fallbacks":false}"#
    );

    // Upstream routing is part of the transport identity; member order is not.
    assert_ne!(p.endpoint_identity(), unpinned.endpoint_identity());
    assert_ne!(p.endpoint_identity(), make(Some(other)).endpoint_identity());
    assert_eq!(
        p.endpoint_identity(),
        make(Some(reordered)).endpoint_identity()
    );
}
