use std::collections::BTreeMap;
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

use ib_providers::base::{Message, ModelRequest, Provider};
use ib_providers::openai_compat::{OpenAICompatConfig, OpenAICompatProvider};
use ib_v5_runner::{
    RequestContextV5, RequestParametersV5, RunConfigV5, execute_logical_request_v5,
};
use ib_v5_schemas::{AttemptStatusV5, ProviderRequestStatusV5};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::oneshot;
use tokio::task::JoinHandle;

const MAX_PROVIDER_RESPONSE_BYTES: usize = 20 * 1024 * 1024;

struct TestServer {
    base_url: String,
    request_count: Arc<AtomicUsize>,
    stop: Option<oneshot::Sender<()>>,
    worker: JoinHandle<()>,
}

impl TestServer {
    async fn stop(mut self) -> usize {
        self.stop.take().unwrap().send(()).unwrap();
        self.worker.await.unwrap();
        self.request_count.load(Ordering::SeqCst)
    }
}

async fn serve_once_or_more(response: Option<Vec<u8>>) -> TestServer {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let request_count = Arc::new(AtomicUsize::new(0));
    let counted = Arc::clone(&request_count);
    let (stop, mut stop_rx) = oneshot::channel();
    let worker = tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = &mut stop_rx => break,
                accepted = listener.accept() => {
                    let Ok((mut socket, _)) = accepted else { break; };
                    counted.fetch_add(1, Ordering::SeqCst);
                    let mut request = Vec::new();
                    let mut chunk = [0_u8; 4096];
                    let mut header_end = None;
                    let mut content_length = 0_usize;
                    loop {
                        let read = socket.read(&mut chunk).await.unwrap_or(0);
                        if read == 0 {
                            break;
                        }
                        request.extend_from_slice(&chunk[..read]);
                        if header_end.is_none()
                            && let Some(end) = request.windows(4).position(|w| w == b"\r\n\r\n")
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
                    if let Some(response) = response.as_ref() {
                        let _ = socket.write_all(response).await;
                    }
                    let _ = socket.shutdown().await;
                }
            }
        }
    });
    TestServer {
        base_url: format!("http://{address}/v1"),
        request_count,
        stop: Some(stop),
        worker,
    }
}

fn raw_response(status: u16, reason: &str, content_length: usize, body: &[u8]) -> Vec<u8> {
    let mut bytes = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {content_length}\r\nConnection: close\r\n\r\n"
    )
    .into_bytes();
    bytes.extend_from_slice(body);
    bytes
}

fn openai_provider(base_url: &str) -> OpenAICompatProvider {
    let mut config = OpenAICompatConfig::new("retry-proof-test-model");
    config.base_url_env = Some("OPENAI_TEST_URL".into());
    config.api_key_env = Some("OPENAI_TEST_KEY".into());
    config.max_retries = 0;
    config.timeout_s = 2.0;
    config.env_override = Some(BTreeMap::from([
        ("OPENAI_TEST_URL".into(), base_url.into()),
        ("OPENAI_TEST_KEY".into(), "offline-test-key".into()),
    ]));
    OpenAICompatProvider::new(config).unwrap()
}

async fn run_one(provider: &dyn Provider) -> ib_v5_runner::LogicalRequestOutcomeV5 {
    let mut config = RunConfigV5::default();
    config.max_transient_retries = 2;
    config.max_recovery_continuations = 2;
    execute_logical_request_v5(
        provider,
        &config,
        ModelRequest::new("offline retry proof test", vec![Message::user("test")]),
        RequestContextV5 {
            attempt_id: "attempt:http-retry-proof".into(),
            logical_request_id: "attempt:http-retry-proof:logical:1".into(),
            role: "unified".into(),
            agent_id: "agent:unified".into(),
            dispatch_round: 1,
            reservation_ordinal: (1, 1, 0, "unified".into(), 0),
        },
        RequestParametersV5::default(),
    )
    .await
    .unwrap()
}

#[tokio::test]
async fn incomplete_500_bodies_and_post_dispatch_reset_are_not_resampled() {
    let oversized = raw_response(
        500,
        "Internal Server Error",
        MAX_PROVIDER_RESPONSE_BYTES + 1,
        b"",
    );
    let truncated = raw_response(500, "Internal Server Error", 20, b"{}");
    for response in [Some(oversized), Some(truncated), None] {
        let server = serve_once_or_more(response).await;
        let provider = openai_provider(&server.base_url);
        let outcome = run_one(&provider).await;
        let requests = server.stop().await;

        assert_eq!(
            requests, 1,
            "one failed physical dispatch is never resampled"
        );
        assert_eq!(outcome.physical_requests.len(), 1);
        assert_eq!(
            outcome.status,
            Some(AttemptStatusV5::IncompleteProviderFailure)
        );
        assert_eq!(
            outcome.physical_requests[0].request_status,
            ProviderRequestStatusV5::TransientFailure
        );
        assert!(!outcome.completed_generation);
    }
}
