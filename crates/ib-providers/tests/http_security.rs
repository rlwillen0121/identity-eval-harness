use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use ib_providers::anthropic_api::{AnthropicConfig, AnthropicProvider};
use ib_providers::base::{Message, ModelRequest, Provider};
use ib_providers::openai_compat::{OpenAICompatConfig, OpenAICompatProvider};
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

async fn spawn_server(response: Vec<u8>) -> (String, Arc<Mutex<Vec<Vec<u8>>>>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let requests = Arc::new(Mutex::new(Vec::new()));
    let recorded = Arc::clone(&requests);
    tokio::spawn(async move {
        loop {
            let Ok((mut socket, _)) = listener.accept().await else {
                break;
            };
            let response = response.clone();
            let recorded = Arc::clone(&recorded);
            tokio::spawn(async move {
                let mut request = Vec::new();
                let mut buf = [0_u8; 4096];
                let mut header_end = None;
                let mut content_length = 0_usize;
                loop {
                    let read = socket.read(&mut buf).await.unwrap_or(0);
                    if read == 0 {
                        break;
                    }
                    request.extend_from_slice(&buf[..read]);
                    if header_end.is_none()
                        && let Some(end) =
                            request.windows(4).position(|window| window == b"\r\n\r\n")
                    {
                        header_end = Some(end + 4);
                        let headers = String::from_utf8_lossy(&request[..end]);
                        for line in headers.lines().skip(1) {
                            if let Some((name, value)) = line.split_once(':')
                                && name.eq_ignore_ascii_case("content-length")
                            {
                                content_length = value.trim().parse().unwrap_or(0);
                            }
                        }
                    }
                    if let Some(end) = header_end
                        && request.len() >= end + content_length
                    {
                        break;
                    }
                }
                recorded.lock().unwrap().push(request);
                let _ = socket.write_all(&response).await;
                let _ = socket.shutdown().await;
            });
        }
    });
    (format!("http://{address}"), requests)
}

fn raw_response(status: u16, reason: &str, headers: &[(&str, &str)], body: &[u8]) -> Vec<u8> {
    let mut response = format!("HTTP/1.1 {status} {reason}\r\n");
    for (name, value) in headers {
        response.push_str(&format!("{name}: {value}\r\n"));
    }
    response.push_str(&format!(
        "Content-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    ));
    let mut bytes = response.into_bytes();
    bytes.extend_from_slice(body);
    bytes
}

fn prompt_request() -> ModelRequest {
    ModelRequest::new(
        "SYSTEM-REDIRECT-SECRET",
        vec![Message::user("PROMPT-REDIRECT-SECRET")],
    )
}

#[tokio::test]
async fn openai_cross_origin_302_is_rejected_without_forwarding_request() {
    let target_reply = raw_response(
        200,
        "OK",
        &[("Content-Type", "application/json")],
        br#"{"id":"ok","model":"test","choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}"#,
    );
    let (target, forwarded) = spawn_server(target_reply).await;
    let redirect_reply = raw_response(
        302,
        "Found",
        &[("Location", &format!("{target}/collect"))],
        b"",
    );
    let (source, received) = spawn_server(redirect_reply).await;

    let mut config = OpenAICompatConfig::new("test-model");
    config.base_url_env = Some("OPENAI_TEST_URL".to_string());
    config.api_key_env = Some("OPENAI_TEST_KEY".to_string());
    config.max_retries = 0;
    config.env_override = Some(BTreeMap::from([
        ("OPENAI_TEST_URL".to_string(), format!("{source}/v1")),
        ("OPENAI_TEST_KEY".to_string(), "openai-test-key".to_string()),
    ]));

    let provider = OpenAICompatProvider::new(config).unwrap();
    let response = provider.complete(&prompt_request()).await;

    assert!(
        response
            .error
            .as_deref()
            .unwrap()
            .contains("redirect responses are disabled")
    );
    let raw = response.raw.as_ref().unwrap();
    assert_eq!(raw["http_status"], json!(302));
    assert_eq!(raw["error_code"], json!("redirect_not_followed"));
    assert_eq!(raw["error_class"], json!("RedirectError"));
    let source_requests = received.lock().unwrap();
    assert_eq!(source_requests.len(), 1);
    let source_request = String::from_utf8_lossy(&source_requests[0]);
    assert!(source_request.contains("Bearer openai-test-key"));
    assert!(source_request.contains("PROMPT-REDIRECT-SECRET"));
    drop(source_requests);
    assert!(forwarded.lock().unwrap().is_empty());
}

#[tokio::test]
async fn anthropic_cross_origin_307_is_rejected_without_forwarding_request() {
    let target_reply = raw_response(
        200,
        "OK",
        &[("Content-Type", "application/json")],
        br#"{"id":"msg","type":"message","role":"assistant","model":"test","stop_reason":"end_turn","content":[{"type":"text","text":"ok"}],"usage":{"input_tokens":1,"output_tokens":1}}"#,
    );
    let (target, forwarded) = spawn_server(target_reply).await;
    let redirect_reply = raw_response(
        307,
        "Temporary Redirect",
        &[("Location", &format!("{target}/collect"))],
        b"",
    );
    let (source, received) = spawn_server(redirect_reply).await;

    let mut config = AnthropicConfig::new("test-model");
    config.base_url_env = Some("ANTHROPIC_TEST_URL".to_string());
    config.api_key_env = Some("ANTHROPIC_TEST_KEY".to_string());
    config.max_retries = 0;
    config.env_override = Some(BTreeMap::from([
        ("ANTHROPIC_TEST_URL".to_string(), source),
        (
            "ANTHROPIC_TEST_KEY".to_string(),
            "synthetic-anthropic-key".to_string(),
        ),
    ]));

    let provider = AnthropicProvider::new(config).unwrap();
    let response = provider.complete(&prompt_request()).await;

    assert!(
        response
            .error
            .as_deref()
            .unwrap()
            .contains("redirect responses are disabled")
    );
    let raw = response.raw.as_ref().unwrap();
    assert_eq!(raw["http_status"], json!(307));
    assert_eq!(raw["error_code"], json!("redirect_not_followed"));
    assert_eq!(raw["error_class"], json!("RedirectError"));
    let source_requests = received.lock().unwrap();
    assert_eq!(source_requests.len(), 1);
    let source_request = String::from_utf8_lossy(&source_requests[0]);
    assert!(source_request.contains("x-api-key: synthetic-anthropic-key"));
    assert!(source_request.contains("PROMPT-REDIRECT-SECRET"));
    drop(source_requests);
    assert!(forwarded.lock().unwrap().is_empty());
}

#[tokio::test]
async fn oversized_declared_success_and_error_bodies_fail_with_sanitized_metadata() {
    const OVER_LIMIT: usize = 20 * 1024 * 1024 + 1;
    for (status, reason, code, class) in [
        (
            200,
            "OK",
            "response_body_too_large",
            "ResponseBodyTooLargeError",
        ),
        (
            500,
            "Internal Server Error",
            "response_body_too_large",
            "ResponseBodyTooLargeError",
        ),
    ] {
        let response = format!(
            "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {OVER_LIMIT}\r\nx-request-id: private-key-value\r\nConnection: close\r\n\r\n"
        )
        .into_bytes();
        let (base, _) = spawn_server(response).await;
        let mut config = OpenAICompatConfig::new("test-model");
        config.base_url_env = Some("OPENAI_TEST_URL".to_string());
        config.api_key_env = Some("OPENAI_TEST_KEY".to_string());
        config.max_retries = 0;
        config.env_override = Some(BTreeMap::from([
            ("OPENAI_TEST_URL".to_string(), format!("{base}/v1")),
            (
                "OPENAI_TEST_KEY".to_string(),
                "private-key-value".to_string(),
            ),
        ]));
        let provider = OpenAICompatProvider::new(config).unwrap();
        let result = provider.complete(&prompt_request()).await;

        assert!(
            result
                .error
                .as_deref()
                .unwrap()
                .contains("response body exceeded")
        );
        assert!(
            !result
                .error
                .as_deref()
                .unwrap()
                .contains("private-key-value")
        );
        let raw = result.raw.as_ref().unwrap();
        assert_eq!(raw["http_status"], json!(status));
        assert_eq!(raw["error_code"], json!(code));
        assert_eq!(raw["error_class"], json!(class));
        assert!(
            !raw["request_id"]
                .as_str()
                .unwrap()
                .contains("private-key-value")
        );
        assert!(
            !raw.contains_key("proven_unanswered"),
            "HTTP status alone cannot prove that the request generated no response"
        );
    }

    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {OVER_LIMIT}\r\nrequest-id: anthropic-private-key\r\nConnection: close\r\n\r\n"
    )
    .into_bytes();
    let (base, _) = spawn_server(response).await;
    let mut config = AnthropicConfig::new("test-model");
    config.base_url_env = Some("ANTHROPIC_TEST_URL".to_string());
    config.api_key_env = Some("ANTHROPIC_TEST_KEY".to_string());
    config.max_retries = 0;
    config.env_override = Some(BTreeMap::from([
        ("ANTHROPIC_TEST_URL".to_string(), base),
        (
            "ANTHROPIC_TEST_KEY".to_string(),
            "anthropic-private-key".to_string(),
        ),
    ]));
    let provider = AnthropicProvider::new(config).unwrap();
    let result = provider.complete(&prompt_request()).await;
    assert!(
        result
            .error
            .as_deref()
            .unwrap()
            .contains("response body exceeded")
    );
    let raw = result.raw.as_ref().unwrap();
    assert_eq!(raw["http_status"], json!(200));
    assert_eq!(raw["error_code"], json!("response_body_too_large"));
    assert_eq!(raw["error_class"], json!("ResponseBodyTooLargeError"));
    assert!(
        !raw["request_id"]
            .as_str()
            .unwrap()
            .contains("anthropic-private-key")
    );
}

#[tokio::test]
async fn anthropic_normal_error_body_keeps_provider_message_and_request_id() {
    let body =
        br#"{"type":"error","error":{"type":"invalid_request_error","message":"invalid request"}}"#;
    let response = raw_response(
        400,
        "Bad Request",
        &[
            ("Content-Type", "application/json"),
            ("request-id", "ant-req-1"),
        ],
        body,
    );
    let (base, _) = spawn_server(response).await;
    let mut config = AnthropicConfig::new("test-model");
    config.base_url_env = Some("ANTHROPIC_TEST_URL".to_string());
    config.max_retries = 0;
    config.env_override = Some(BTreeMap::from([("ANTHROPIC_TEST_URL".to_string(), base)]));
    let provider = AnthropicProvider::new(config).unwrap();
    let result = provider
        .complete(&ModelRequest::new("system", vec![Message::user("prompt")]))
        .await;

    assert!(
        result
            .error
            .as_deref()
            .unwrap()
            .contains("BadRequestError 400: invalid request")
    );
    // Anthropic's existing HTTP error metadata includes the request identity.
    assert_eq!(
        result.raw.as_ref().unwrap()["request_id"],
        json!("ant-req-1")
    );
}
