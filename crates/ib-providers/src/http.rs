//! Shared limits and typed errors for provider HTTP responses.

use crate::base::ProviderError;

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
}
