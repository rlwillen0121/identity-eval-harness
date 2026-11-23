//! Shared limits, typed errors, environment lookup and redaction for the HTTP adapters.

use std::collections::BTreeMap;

use serde_json::{Map, Value};
use url::Url;

use crate::base::{ModelResponse, ProviderError};
use crate::subprocess::{redact, redact_diagnostics_map};

/// API key sent to a compatible endpoint when its key variable is unset.
pub(crate) const PLACEHOLDER_API_KEY: &str = "not-set";

/// `name` from `env_override` when one is configured, otherwise from the process environment.
pub(crate) fn env_var(
    env_override: Option<&BTreeMap<String, String>>,
    name: &str,
) -> Option<String> {
    match env_override {
        Some(map) => map.get(name).cloned(),
        None => std::env::var(name).ok(),
    }
}

/// Whether `base_url` parses and its host is `vendor_host` (ASCII case-insensitive).
pub(crate) fn is_vendor_host(base_url: Option<&str>, vendor_host: &str) -> bool {
    let Some(base_url) = base_url else {
        return false;
    };
    Url::parse(base_url)
        .ok()
        // A matching host alone does not prove this is the vendor's HTTPS endpoint. A
        // plaintext URL, alternate service port, or userinfo URL must go through the
        // custom-endpoint opt-in path just like any other operator-selected origin.
        .filter(|parsed| {
            parsed.scheme() == "https"
                && parsed.port_or_known_default() == Some(443)
                && parsed.username().is_empty()
                && parsed.password().is_none()
        })
        .and_then(|parsed| {
            parsed
                .host_str()
                .map(|host| host.eq_ignore_ascii_case(vendor_host))
        })
        .unwrap_or(false)
}

/// SDK-style error class name for an HTTP error status.
pub(crate) fn status_error_class(status: u16) -> &'static str {
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

/// Redacts an HTTP adapter's secrets: the values of its API-key and base-URL variables (looked up
/// in `env`) plus the generic secret patterns of [`redact`].
pub(crate) struct Redactor<'a> {
    env: Option<&'a BTreeMap<String, String>>,
    names: Vec<&'a str>,
}

impl<'a> Redactor<'a> {
    pub(crate) fn new(
        env: Option<&'a BTreeMap<String, String>>,
        api_key_env: Option<&'a str>,
        base_url_env: Option<&'a str>,
    ) -> Self {
        Self {
            env,
            names: api_key_env.into_iter().chain(base_url_env).collect(),
        }
    }

    pub(crate) fn text(&self, text: &str) -> String {
        redact(text, self.env, &self.names)
    }

    /// A failed response with error `"<prefix>: <message>"` and redacted diagnostics.
    pub(crate) fn fail(
        &self,
        prefix: &str,
        message: &str,
        raw: Option<Map<String, Value>>,
    ) -> ModelResponse {
        ModelResponse {
            error: Some(self.text(&format!("{prefix}: {message}"))),
            raw: raw.map(|r| redact_diagnostics_map(&r, self.env, &self.names)),
            ..ModelResponse::default()
        }
    }

    /// Redact a finished response's error text and diagnostics in place.
    pub(crate) fn response(&self, response: &mut ModelResponse) {
        if let Some(ref err) = response.error {
            response.error = Some(self.text(err));
        }
        // These fields are provider metadata rather than model-visible output. Redact
        // them even on successful responses; keep text, parsed output and tool calls
        // byte-for-byte intact for replay and model-request fidelity.
        for field in [&mut response.stop_reason, &mut response.served_model]
            .into_iter()
            .flatten()
        {
            *field = self.text(field);
        }
        if let Some(ref raw) = response.raw {
            response.raw = Some(redact_diagnostics_map(raw, self.env, &self.names));
        }
    }
}

/// Maximum buffered provider response body, including error and streaming responses.
///
/// Provider responses are parsed in memory, so adapters reject larger bodies rather than
/// truncating them or allowing an unbounded allocation.
pub(crate) const MAX_PROVIDER_RESPONSE_BYTES: usize = 20 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ResponseBodyError {
    TooLarge,
    ReadFailed,
    AllocationFailed,
}

impl ResponseBodyError {
    pub(crate) fn into_provider_error(
        self,
        status: u16,
        request_id: Option<String>,
    ) -> ProviderError {
        let (code, class, message) = match self {
            Self::TooLarge => (
                "response_body_too_large",
                "ResponseBodyTooLargeError",
                format!(
                    "response body exceeded the {} byte limit",
                    MAX_PROVIDER_RESPONSE_BYTES
                ),
            ),
            Self::ReadFailed => (
                "response_body_read_failed",
                "ResponseBodyReadError",
                "failed to read response body".to_string(),
            ),
            Self::AllocationFailed => (
                "response_body_allocation_failed",
                "ResponseBodyAllocationError",
                "failed to allocate bounded response body".to_string(),
            ),
        };
        let mut error = ProviderError::new(message);
        error.error_code = Some(code.to_string());
        error.error_class = Some(class.to_string());
        error.http_status = Some(status);
        error.request_id = request_id;
        error
    }
}

pub(crate) fn redirect_provider_error(status: u16, request_id: Option<String>) -> ProviderError {
    let mut error = ProviderError::new(format!("redirect responses are disabled (HTTP {status})"));
    error.error_code = Some("redirect_not_followed".to_string());
    error.error_class = Some("RedirectError".to_string());
    error.http_status = Some(status);
    error.request_id = request_id;
    error
}

/// Read a response incrementally, stopping as soon as its byte limit would be exceeded.
pub(crate) async fn read_bounded_response_body(
    mut response: reqwest::Response,
    max_bytes: usize,
) -> Result<Vec<u8>, ResponseBodyError> {
    if response
        .content_length()
        .is_some_and(|length| length > max_bytes as u64)
    {
        return Err(ResponseBodyError::TooLarge);
    }

    // Avoid reserving from an untrusted Content-Length; grow only for chunks already
    // verified to fit under the cap.
    let mut body = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| ResponseBodyError::ReadFailed)?
    {
        if chunk.len() > max_bytes.saturating_sub(body.len()) {
            return Err(ResponseBodyError::TooLarge);
        }
        let required = body.len() + chunk.len();
        if required > body.capacity() {
            let geometric = body.capacity().saturating_mul(2).max(4096);
            let target = required.max(geometric).min(max_bytes);
            body.try_reserve_exact(target - body.len())
                .map_err(|_| ResponseBodyError::AllocationFailed)?;
        }
        body.extend_from_slice(&chunk);
    }
    Ok(body)
}

#[cfg(test)]
mod tests {
    use crate::base::ModelResponse;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    use super::{ResponseBodyError, read_bounded_response_body};

    async fn response_with(raw_response: &'static [u8]) -> reqwest::Response {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = [0_u8; 1024];
            let _ = socket.read(&mut request).await;
            let _ = socket.write_all(raw_response).await;
            let _ = socket.shutdown().await;
        });
        reqwest::Client::new()
            .get(format!("http://{address}/"))
            .send()
            .await
            .unwrap()
    }

    #[tokio::test]
    async fn bounded_reader_rejects_declared_chunked_and_close_delimited_bodies() {
        let declared =
            response_with(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\n")
                .await;
        assert_eq!(
            read_bounded_response_body(declared, 5).await,
            Err(ResponseBodyError::TooLarge)
        );

        let chunked = response_with(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n3\r\n123\r\n3\r\n456\r\n0\r\n\r\n",
        )
        .await;
        assert_eq!(
            read_bounded_response_body(chunked, 5).await,
            Err(ResponseBodyError::TooLarge)
        );

        let close_delimited =
            response_with(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n123456").await;
        assert_eq!(
            read_bounded_response_body(close_delimited, 5).await,
            Err(ResponseBodyError::TooLarge)
        );

        let incomplete =
            response_with(b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\n123")
                .await;
        assert_eq!(
            read_bounded_response_body(incomplete, 8).await,
            Err(ResponseBodyError::ReadFailed)
        );
    }

    #[tokio::test]
    async fn bounded_reader_returns_complete_small_body() {
        let response = response_with(
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello",
        )
        .await;
        assert_eq!(
            read_bounded_response_body(response, 5).await,
            Ok(b"hello".to_vec())
        );
    }

    #[test]
    fn response_redacts_provider_metadata_but_preserves_model_output() {
        use std::collections::BTreeMap;

        use serde_json::{Map, Value};

        let marker = "synthetic-provider-secret-marker";
        let env = BTreeMap::from([
            ("TEST_API_KEY".to_string(), marker.to_string()),
            (
                "TEST_BASE_URL".to_string(),
                "https://private.example/tenant".to_string(),
            ),
        ]);
        let redactor =
            super::Redactor::new(Some(&env), Some("TEST_API_KEY"), Some("TEST_BASE_URL"));
        let mut raw = Map::new();
        raw.insert("id".to_string(), Value::String(marker.to_string()));
        raw.insert(
            "assistant_content".to_string(),
            Value::String(marker.to_string()),
        );
        let mut response = ModelResponse {
            text: Some(marker.to_string()),
            stop_reason: Some(format!("stopped at {marker}")),
            served_model: Some(format!("{marker}/model")),
            tool_calls: vec![crate::base::ToolCall {
                id: marker.to_string(),
                name: "lookup".to_string(),
                arguments: Map::new(),
            }],
            raw: Some(raw),
            ..ModelResponse::default()
        };

        redactor.response(&mut response);

        assert_eq!(response.text.as_deref(), Some(marker));
        assert_eq!(response.tool_calls[0].id, marker);
        assert!(!response.stop_reason.as_deref().unwrap().contains(marker));
        assert!(!response.served_model.as_deref().unwrap().contains(marker));
        let raw = response.raw.unwrap();
        assert!(!raw["id"].as_str().unwrap().contains(marker));
        // Anthropic assistant content is request-round-trip data, so the diagnostic
        // redactor deliberately preserves it along with the model-visible tool output.
        assert_eq!(raw["assistant_content"], marker);
    }
}
