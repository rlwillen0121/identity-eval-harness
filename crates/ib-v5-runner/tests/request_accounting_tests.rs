use std::collections::VecDeque;
use std::sync::Mutex;

use async_trait::async_trait;
use ib_providers::base::{ModelRequest, ModelResponse, Provider, ProviderCapabilities};
use ib_v5_runner::request_accounting::{
    RequestContextV5, RequestParametersV5, execute_logical_request_v5,
};
use ib_v5_runner::{
    RunConfigV5, compute_effective_output_allowance_v5, select_actual_native_numeric_budget_v5,
};
use serde_json::{Value, json};

struct Fake {
    responses: Mutex<VecDeque<ModelResponse>>,
    requests: Mutex<Vec<ModelRequest>>,
    retrieval_response: Mutex<Option<ModelResponse>>,
    retrieved_ids: Mutex<Vec<String>>,
    local_retry_limit: Option<u32>,
    opaque_continuation: bool,
}

#[async_trait]
impl Provider for Fake {
    fn kind(&self) -> &str {
        "replay"
    }
    fn model(&self) -> &str {
        "offline-test"
    }
    fn python_type_identity(&self) -> (&'static str, &'static str) {
        ("tests", "Fake")
    }
    fn endpoint_identity(&self) -> String {
        "offline-fixture".into()
    }
    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities::new(true, true, true)
    }
    fn local_retry_limit(&self) -> Option<u32> {
        self.local_retry_limit
    }
    fn supports_opaque_continuation(&self) -> bool {
        self.opaque_continuation
    }
    async fn complete(&self, request: &ModelRequest) -> ModelResponse {
        self.requests.lock().unwrap().push(request.clone());
        self.responses
            .lock()
            .unwrap()
            .pop_front()
            .expect("unexpected extra request")
    }
    async fn retrieve_response(&self, request_id: &str) -> Option<ModelResponse> {
        self.retrieved_ids
            .lock()
            .unwrap()
            .push(request_id.to_owned());
        self.retrieval_response.lock().unwrap().clone()
    }
}

fn response_from_fixture(row: &Value) -> ModelResponse {
    let mut complete = serde_json::to_value(ModelResponse::default()).unwrap();
    for (key, value) in row.as_object().unwrap() {
        if key == "usage" {
            complete["usage"]
                .as_object_mut()
                .unwrap()
                .extend(value.as_object().unwrap().clone());
        } else {
            complete[key] = value.clone();
        }
    }
    serde_json::from_value(complete).unwrap()
}

fn context() -> RequestContextV5 {
    RequestContextV5 {
        attempt_id: "attempt:retry-proof".into(),
        logical_request_id: "attempt:retry-proof:logical:1".into(),
        role: "unified".into(),
        agent_id: "agent:unified".into(),
        dispatch_round: 1,
        reservation_ordinal: (1, 1, 0, "unified".into(), 0),
    }
}

fn fake(responses: impl IntoIterator<Item = ModelResponse>) -> Fake {
    Fake {
        responses: Mutex::new(responses.into_iter().collect()),
        requests: Mutex::new(vec![]),
        retrieval_response: Mutex::new(None),
        retrieved_ids: Mutex::new(vec![]),
        local_retry_limit: None,
        opaque_continuation: true,
    }
}

fn provider_error(class: &str, proof: Option<bool>) -> ModelResponse {
    let mut raw = json!({"error_class":class}).as_object().unwrap().clone();
    if let Some(proof) = proof {
        raw.insert("proven_unanswered".into(), json!(proof));
    }
    ModelResponse {
        error: Some("synthetic provider error".into()),
        raw: Some(raw),
        usage: ib_providers::base::Usage {
            input_tokens: 5,
            ..Default::default()
        },
        ..Default::default()
    }
}

async fn execute_one(
    provider: &Fake,
    config: &RunConfigV5,
) -> ib_v5_runner::LogicalRequestOutcomeV5 {
    execute_logical_request_v5(
        provider,
        config,
        ModelRequest::default(),
        context(),
        RequestParametersV5::default(),
    )
    .await
    .unwrap()
}

#[tokio::test]
async fn all_python_request_state_machine_vectors_match() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/request_accounting_python.json")).unwrap();
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let config = RunConfigV5::from_value(case["config"].clone()).unwrap();
        let mut responses: Vec<ModelResponse> = case["responses"]
            .as_array()
            .unwrap()
            .iter()
            .map(response_from_fixture)
            .collect();
        // The offline Python reference vectors predate these proof fields. Treat synthetic
        // no-progress transient/quota/context retries as explicitly proven unanswered, and give
        // synthetic segment boundaries a checkpoint. Missing and false proof are tested below.
        for response in &mut responses {
            let no_progress = !ib_v5_runner::request_accounting::has_decode_progress(response, 0);
            let class = response
                .raw
                .as_ref()
                .and_then(|raw| raw.get("error_class"))
                .and_then(Value::as_str);
            let reconciled_context_retry = response
                .raw
                .as_ref()
                .and_then(|raw| raw.get("context_reconciliation"))
                .and_then(Value::as_object)
                .and_then(|reconciliation| reconciliation.get("disposition"))
                .and_then(Value::as_str)
                == Some("RECONCILED_RESUMABLE");
            let is_positive_retry = no_progress
                && (matches!(class, Some("TRANSIENT_FAILURE" | "QUOTA_INTERRUPTION"))
                    || reconciled_context_retry);
            let is_segment_boundary = response.stop_reason.as_deref() == Some("pause_turn");
            if is_positive_retry || is_segment_boundary {
                let raw = response.raw.get_or_insert_with(Default::default);
                if is_positive_retry {
                    raw.insert("proven_unanswered".into(), json!(true));
                }
                if is_segment_boundary {
                    raw.entry("continuation_token")
                        .or_insert_with(|| json!("SYNTHETIC_OPAQUE"));
                }
            }
        }
        let provider = Fake {
            responses: Mutex::new(responses.into()),
            requests: Mutex::new(vec![]),
            retrieval_response: Mutex::new(
                case.get("retrieval_response").map(response_from_fixture),
            ),
            retrieved_ids: Mutex::new(vec![]),
            local_retry_limit: None,
            opaque_continuation: true,
        };
        let allowance = compute_effective_output_allowance_v5(
            config.base_effective_output_ceiling_tokens as i64,
            config.provider_max_output_tokens as i64,
            config.context_window_tokens as i64,
            100,
            config.provider_schema_overhead_tokens as i64,
            config.minimum_context_headroom_tokens as i64,
        );
        let params = RequestParametersV5 {
            rendered_input_context_tokens: 100,
            effective_output_allowance_tokens: allowance,
            actual_native_numeric_budget_tokens: select_actual_native_numeric_budget_v5(
                &config, allowance,
            )
            .unwrap(),
            ..Default::default()
        };
        let context = RequestContextV5 {
            attempt_id: "attempt:test".into(),
            logical_request_id: "attempt:test:logical:1".into(),
            role: "unified".into(),
            agent_id: "agent:unified".into(),
            dispatch_round: 1,
            reservation_ordinal: (1, 1, 0, "unified".into(), 0),
        };
        let outcome = execute_logical_request_v5(
            &provider,
            &config,
            ModelRequest::default(),
            context,
            params,
        )
        .await
        .unwrap();
        let actual = json!({
            "physical_requests": outcome.physical_requests, "response": outcome.response,
            "status": outcome.status, "terminal_cause": outcome.terminal_cause,
            "context_limit_cause": outcome.context_limit_cause,
            "completed_generation": outcome.completed_generation,
            "normalized_decode_status": outcome.normalized_decode_status,
            "usage_sum": outcome.usage_sum,"usage_problem_detected":outcome.usage_problem_detected,
            "provider_error_detected": outcome.provider_error_detected,
        });
        if name != "ambiguous_retrieved" {
            assert_eq!(
                ib_core::jcs_dumps(&actual).unwrap(),
                ib_core::jcs_dumps(&case["expected"]).unwrap(),
                "request accounting parity: {name}"
            );
        }
        assert!(
            provider.responses.lock().unwrap().is_empty(),
            "unconsumed response: {name}"
        );
        let requests = provider.requests.lock().unwrap().clone();
        if name == "segment_merge" {
            assert_eq!(
                requests[1].params.extra["continuation_token"],
                "SYNTHETIC_OPAQUE"
            );
            assert!(
                !serde_json::to_string(&outcome.physical_requests)
                    .unwrap()
                    .contains("SYNTHETIC_OPAQUE")
            );
        }
        if name == "native_segment_budget" {
            for request in requests.iter() {
                assert_eq!(request.params.max_output_tokens, 64000);
                assert_eq!(request.params.extra["native_numeric_budget_tokens"], 10000);
            }
        }
        if name == "ambiguous_retrieved" {
            let requests = &outcome.physical_requests;
            assert_eq!(requests.len(), 1);
            assert_eq!(
                requests[0].request_status,
                ib_v5_schemas::ProviderRequestStatusV5::Completed
            );
            let raw_usage: Value =
                serde_json::from_str(requests[0].raw_usage_fields_json.as_deref().unwrap())
                    .unwrap();
            assert_eq!(
                raw_usage["same_request_prior_failure_nonadditive"]["contribution"],
                "diagnostic_only_nonadditive"
            );
            assert_eq!(outcome.usage_sum.input_tokens, 11);
            assert_eq!(outcome.usage_sum.output_tokens, 7);
            assert!(!outcome.usage_problem_detected);
            assert_eq!(
                provider.retrieved_ids.lock().unwrap().as_slice(),
                &["provider:retrievable".to_owned()]
            );
            assert_eq!(provider.requests.lock().unwrap().len(), 1);
        }
        if name == "reconciled_native_segment_budget" {
            let requests = provider.requests.lock().unwrap();
            assert_eq!(requests.len(), 3);
            assert_eq!(requests[0].params.max_output_tokens, 64000);
            assert_eq!(
                requests[0].params.extra["native_numeric_budget_tokens"],
                10000
            );
            for request in &requests[1..] {
                assert_eq!(request.params.max_output_tokens, 6000);
                assert_eq!(request.params.extra["native_numeric_budget_tokens"], 5976);
            }
            assert_eq!(outcome.physical_requests[1].decode_segment_index, 0);
            assert_eq!(outcome.physical_requests[2].decode_segment_index, 1);
        }
    }
}

#[tokio::test]
async fn missing_or_false_unanswered_proof_never_retries_transient_or_quota_errors() {
    for (class, proof) in [
        ("TRANSIENT_FAILURE", None),
        ("TRANSIENT_FAILURE", Some(false)),
        ("QUOTA_INTERRUPTION", None),
        ("QUOTA_INTERRUPTION", Some(false)),
    ] {
        let mut config = RunConfigV5::default();
        config.max_transient_retries = 2;
        config.max_quota_continuations = 2;
        config.max_recovery_continuations = 2;
        let provider = fake([
            provider_error(class, proof),
            ModelResponse {
                text: Some("must not be sent".into()),
                ..Default::default()
            },
        ]);

        let outcome = execute_one(&provider, &config).await;
        assert_eq!(
            provider.requests.lock().unwrap().len(),
            1,
            "{class} {proof:?}"
        );
        assert_eq!(outcome.physical_requests.len(), 1, "{class} {proof:?}");
        assert!(!outcome.completed_generation, "{class} {proof:?}");
        assert!(
            outcome.physical_requests[0]
                .raw_usage_fields_json
                .as_deref()
                .unwrap()
                .contains("\"input_tokens_uncached\":5"),
            "the failed physical request retains usage diagnostics"
        );
        assert_eq!(
            outcome.physical_requests[0].request_kind,
            ib_v5_schemas::ProviderRequestKindV5::Initial
        );
        assert_eq!(
            outcome.physical_requests[0].request_status,
            if class == "QUOTA_INTERRUPTION" {
                ib_v5_schemas::ProviderRequestStatusV5::QuotaInterruption
            } else {
                ib_v5_schemas::ProviderRequestStatusV5::TransientFailure
            }
        );
        if class == "TRANSIENT_FAILURE" {
            assert_eq!(
                outcome.terminal_cause.as_deref(),
                Some("UNANSWERED_REQUEST_NOT_PROVEN")
            );
        }
    }
}

#[tokio::test]
async fn explicit_unanswered_proof_allows_bounded_transient_and_quota_retries() {
    for (class, retry_kind) in [
        (
            "TRANSIENT_FAILURE",
            ib_v5_schemas::ProviderRequestKindV5::TransientRetry,
        ),
        (
            "QUOTA_INTERRUPTION",
            ib_v5_schemas::ProviderRequestKindV5::QuotaContinuation,
        ),
    ] {
        let mut config = RunConfigV5::default();
        config.max_transient_retries = 1;
        config.max_quota_continuations = 1;
        let provider = fake([
            provider_error(class, Some(true)),
            ModelResponse {
                text: Some("done".into()),
                ..Default::default()
            },
        ]);

        let outcome = execute_one(&provider, &config).await;
        let requests = provider.requests.lock().unwrap();
        assert_eq!(requests.len(), 2, "{class}");
        assert_eq!(outcome.physical_requests.len(), 2, "{class}");
        assert_eq!(outcome.physical_requests[1].request_kind, retry_kind);
        assert!(outcome.completed_generation, "{class}");
    }
}

#[tokio::test]
async fn exact_nonempty_provider_checkpoint_is_required_for_post_progress_resume() {
    let mut config = RunConfigV5 {
        segment_continuation_capability: "SUPPORTED".into(),
        ..RunConfigV5::default()
    };
    config.max_quota_continuations = 1;
    let provider = fake([
        ModelResponse {
            error: Some("quota interrupted a resumed decode".into()),
            raw: Some(
                json!({
                    "error_class":"QUOTA_INTERRUPTION",
                    "resumable":true,
                    "continuation_token":"SYNTHETIC_CHECKPOINT"
                })
                .as_object()
                .unwrap()
                .clone(),
            ),
            ..Default::default()
        },
        ModelResponse {
            text: Some("done".into()),
            ..Default::default()
        },
    ]);

    let outcome = execute_one(&provider, &config).await;
    {
        let requests = provider.requests.lock().unwrap();
        assert_eq!(requests.len(), 2);
        assert_eq!(
            requests[1].params.extra["continuation_token"],
            "SYNTHETIC_CHECKPOINT"
        );
        assert_eq!(
            requests[1].params.extra["request_kind"],
            json!(ib_v5_schemas::ProviderRequestKindV5::QuotaContinuation)
        );
    }
    assert!(outcome.completed_generation);

    let no_checkpoint = fake([ModelResponse {
        error: Some("quota interruption without a checkpoint".into()),
        raw: Some(
            json!({"error_class":"QUOTA_INTERRUPTION","resumable":true,"continuation_token":""})
                .as_object()
                .unwrap()
                .clone(),
        ),
        ..Default::default()
    }]);
    let outcome = execute_one(&no_checkpoint, &config).await;
    assert_eq!(no_checkpoint.requests.lock().unwrap().len(), 1);
    assert_eq!(outcome.physical_requests.len(), 1);
    assert!(!outcome.completed_generation);
}

#[tokio::test]
async fn native_segment_boundary_without_checkpoint_is_incomplete() {
    let config = RunConfigV5 {
        segment_continuation_capability: "SUPPORTED".into(),
        ..RunConfigV5::default()
    };
    let provider = fake([ModelResponse {
        stop_reason: Some("pause_turn".into()),
        text: Some("partial segment".into()),
        usage: ib_providers::base::Usage {
            input_tokens: 5,
            output_tokens: 2,
            ..Default::default()
        },
        ..Default::default()
    }]);

    let outcome = execute_one(&provider, &config).await;
    assert_eq!(provider.requests.lock().unwrap().len(), 1);
    assert_eq!(outcome.physical_requests.len(), 1);
    assert_eq!(
        outcome.status,
        Some(ib_v5_schemas::AttemptStatusV5::IncompleteProviderFailure)
    );
    assert_eq!(
        outcome.terminal_cause.as_deref(),
        Some("SEGMENT_CONTINUATION_CHECKPOINT_MISSING")
    );
}

#[tokio::test]
async fn previous_segment_checkpoint_cannot_authorize_a_later_boundary_or_quota_resume() {
    let config = RunConfigV5 {
        segment_continuation_capability: "SUPPORTED".into(),
        max_segment_continuations: 3,
        max_quota_continuations: 2,
        ..RunConfigV5::default()
    };
    let first = ModelResponse {
        stop_reason: Some("pause_turn".into()),
        text: Some("first partial".into()),
        raw: Some(
            json!({"continuation_token":"OLD_CHECKPOINT"})
                .as_object()
                .unwrap()
                .clone(),
        ),
        ..Default::default()
    };
    let missing_segment_checkpoint = ModelResponse {
        stop_reason: Some("pause_turn".into()),
        text: Some("second partial".into()),
        ..Default::default()
    };
    let provider = fake([first.clone(), missing_segment_checkpoint]);
    let outcome = execute_one(&provider, &config).await;
    assert_eq!(provider.requests.lock().unwrap().len(), 2);
    assert_eq!(outcome.physical_requests.len(), 2);
    assert_eq!(
        outcome.terminal_cause.as_deref(),
        Some("SEGMENT_CONTINUATION_CHECKPOINT_MISSING")
    );

    let quota_without_current_checkpoint = ModelResponse {
        error: Some("quota interrupted continuation".into()),
        raw: Some(
            json!({"error_class":"QUOTA_INTERRUPTION","resumable":true})
                .as_object()
                .unwrap()
                .clone(),
        ),
        ..Default::default()
    };
    let provider = fake([first, quota_without_current_checkpoint]);
    let outcome = execute_one(&provider, &config).await;
    let requests = provider.requests.lock().unwrap();
    assert_eq!(requests.len(), 2);
    assert_eq!(
        requests[1].params.extra["continuation_token"],
        "OLD_CHECKPOINT"
    );
    assert_eq!(outcome.physical_requests.len(), 2);
    assert!(!outcome.completed_generation);
}

#[tokio::test]
async fn context_allowance_reconciliation_requires_explicit_unanswered_proof() {
    for proof in [None, Some(false)] {
        let mut raw = json!({
            "error_class":"CONTEXT_LIMIT_REJECTION",
            "context_reconciliation":{
                "disposition":"RECONCILED_RESUMABLE",
                "reconciled_output_allowance_tokens":32000
            }
        })
        .as_object()
        .unwrap()
        .clone();
        if let Some(proof) = proof {
            raw.insert("proven_unanswered".into(), json!(proof));
        }
        let mut config = RunConfigV5::default();
        config.max_recovery_continuations = 1;
        let provider = fake([ModelResponse {
            error: Some("synthetic context error".into()),
            raw: Some(raw),
            ..Default::default()
        }]);
        let outcome = execute_one(&provider, &config).await;
        assert_eq!(provider.requests.lock().unwrap().len(), 1);
        assert_eq!(
            outcome.terminal_cause.as_deref(),
            Some("UNANSWERED_REQUEST_NOT_PROVEN")
        );
    }

    let mut config = RunConfigV5::default();
    config.max_recovery_continuations = 1;
    let provider = fake([
        ModelResponse {
            error: Some("synthetic context error".into()),
            raw: Some(
                json!({
                    "error_class":"CONTEXT_LIMIT_REJECTION",
                    "proven_unanswered":true,
                    "context_reconciliation":{
                        "disposition":"RECONCILED_RESUMABLE",
                        "reconciled_output_allowance_tokens":32000
                    }
                })
                .as_object()
                .unwrap()
                .clone(),
            ),
            ..Default::default()
        },
        ModelResponse {
            text: Some("done".into()),
            ..Default::default()
        },
    ]);
    let outcome = execute_one(&provider, &config).await;
    let requests = provider.requests.lock().unwrap();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[1].params.max_output_tokens, 32000);
    assert!(outcome.completed_generation);
}

#[tokio::test]
async fn retrieved_response_reconciles_same_physical_usage_and_cost_once() {
    let provider = fake([ModelResponse {
        error: Some("ambiguous response with known accounting".into()),
        reported_cost_usd: Some(0.375),
        usage: ib_providers::base::Usage {
            input_tokens: 12,
            output_tokens: 3,
            ..Default::default()
        },
        raw: Some(
            json!({
                "error_class":"AMBIGUOUS_FAILURE",
                "provider_request_id":"provider:paid-request",
                "provider_error_code":"connection_lost",
                "continuation_token":"OPAQUE_PRIVATE_CANARY",
                "raw_usage":{"vendor_input":12,"vendor_output":3}
            })
            .as_object()
            .unwrap()
            .clone(),
        ),
        ..Default::default()
    }]);
    provider
        .retrieval_response
        .lock()
        .unwrap()
        .replace(ModelResponse {
            text: Some("done".into()),
            ..Default::default()
        });

    let outcome = execute_one(&provider, &RunConfigV5::default()).await;
    assert!(outcome.completed_generation);
    assert_eq!(provider.requests.lock().unwrap().len(), 1);
    assert_eq!(outcome.physical_requests.len(), 1);
    assert_eq!(outcome.usage_sum.input_tokens, 12);
    assert_eq!(outcome.usage_sum.output_tokens, 3);
    assert_eq!(outcome.reported_cost_sum, 0.375);
    assert_eq!(outcome.reported_cost_count, 1);
    let row = &outcome.physical_requests[0];
    assert_eq!(
        row.request_status,
        ib_v5_schemas::ProviderRequestStatusV5::Completed
    );
    assert_eq!(row.input_tokens_total, Some(12));
    assert_eq!(row.output_tokens_total, Some(3));
    assert_eq!(row.provider_reported_cost_micros, Some(375_000));
    let raw_usage: Value =
        serde_json::from_str(row.raw_usage_fields_json.as_deref().unwrap()).unwrap();
    assert_eq!(
        raw_usage["same_request_prior_failure_nonadditive"]["provider_reported_cost_micros"],
        375_000
    );
    assert_eq!(
        raw_usage["same_request_prior_failure_nonadditive"]["raw_usage_fields"]["provider_raw_usage"]
            ["vendor_input"],
        12
    );
    assert!(
        !serde_json::to_string(&outcome.physical_requests)
            .unwrap()
            .contains("OPAQUE_PRIVATE_CANARY")
    );
}

#[tokio::test]
async fn retrieved_accounting_prefers_current_values_without_double_counting() {
    let provider = fake([ModelResponse {
        error: Some("ambiguous response".into()),
        reported_cost_usd: Some(0.2),
        usage: ib_providers::base::Usage {
            input_tokens: 20,
            output_tokens: 4,
            ..Default::default()
        },
        raw: Some(
            json!({"error_class":"AMBIGUOUS_FAILURE","provider_request_id":"provider:current"})
                .as_object()
                .unwrap()
                .clone(),
        ),
        ..Default::default()
    }]);
    provider
        .retrieval_response
        .lock()
        .unwrap()
        .replace(ModelResponse {
            text: Some("done".into()),
            reported_cost_usd: Some(0.3),
            usage: ib_providers::base::Usage {
                input_tokens: 8,
                output_tokens: 2,
                ..Default::default()
            },
            ..Default::default()
        });

    let outcome = execute_one(&provider, &RunConfigV5::default()).await;
    assert_eq!(outcome.usage_sum.input_tokens, 8);
    assert_eq!(outcome.usage_sum.output_tokens, 2);
    assert_eq!(outcome.reported_cost_sum, 0.3);
    assert_eq!(outcome.reported_cost_count, 1);
    assert_eq!(outcome.physical_requests.len(), 1);
    assert_eq!(outcome.physical_requests[0].input_tokens_total, Some(8));
    assert_eq!(
        outcome.physical_requests[0].provider_reported_cost_micros,
        Some(300_000)
    );
}

#[tokio::test]
async fn retrieval_uses_only_the_current_request_id_and_checkpoint() {
    let mut config = RunConfigV5 {
        segment_continuation_capability: "SUPPORTED".into(),
        ..RunConfigV5::default()
    };
    config.max_segment_continuations = 1;
    let provider = fake([
        ModelResponse {
            stop_reason: Some("pause_turn".into()),
            text: Some("first partial".into()),
            raw: Some(
                json!({"continuation_token":"STALE_TOKEN","provider_request_id":"provider:old"})
                    .as_object()
                    .unwrap()
                    .clone(),
            ),
            ..Default::default()
        },
        ModelResponse {
            error: Some("ambiguous current request".into()),
            raw: Some(
                json!({"error_class":"AMBIGUOUS_FAILURE"})
                    .as_object()
                    .unwrap()
                    .clone(),
            ),
            ..Default::default()
        },
    ]);
    provider
        .retrieval_response
        .lock()
        .unwrap()
        .replace(ModelResponse {
            text: Some("retrieved segment".into()),
            stop_reason: Some("pause_turn".into()),
            ..Default::default()
        });
    let outcome = execute_one(&provider, &config).await;
    assert!(provider.retrieved_ids.lock().unwrap().is_empty());
    assert_eq!(provider.requests.lock().unwrap().len(), 2);
    assert!(!outcome.completed_generation);
    assert_eq!(
        outcome.terminal_cause.as_deref(),
        Some("AMBIGUOUS_OR_PARTIAL_DECODE_UNRECOVERABLE")
    );

    let provider = fake([ModelResponse {
        error: Some("ambiguous with stale checkpoint".into()),
        raw: Some(
            json!({
                "error_class":"AMBIGUOUS_FAILURE",
                "provider_request_id":"provider:retrievable",
                "resumable":true,
                "continuation_token":"STALE_TOKEN"
            })
            .as_object()
            .unwrap()
            .clone(),
        ),
        ..Default::default()
    }]);
    provider
        .retrieval_response
        .lock()
        .unwrap()
        .replace(ModelResponse {
            text: Some("retrieved segment".into()),
            stop_reason: Some("pause_turn".into()),
            ..Default::default()
        });
    let outcome = execute_one(&provider, &config).await;
    assert_eq!(provider.requests.lock().unwrap().len(), 1);
    assert_eq!(outcome.physical_requests.len(), 1);
    assert_eq!(
        outcome.terminal_cause.as_deref(),
        Some("SEGMENT_CONTINUATION_CHECKPOINT_MISSING")
    );

    let provider = fake([ModelResponse {
        error: Some("ambiguous with stale checkpoint".into()),
        raw: Some(
            json!({
                "error_class":"AMBIGUOUS_FAILURE",
                "provider_request_id":"provider:retrievable",
                "resumable":true,
                "continuation_token":"STALE_TOKEN"
            })
            .as_object()
            .unwrap()
            .clone(),
        ),
        ..Default::default()
    }]);
    provider
        .retrieval_response
        .lock()
        .unwrap()
        .replace(ModelResponse {
            text: Some("retrieved segment".into()),
            stop_reason: Some("pause_turn".into()),
            raw: Some(
                json!({"continuation_token":"CURRENT_TOKEN"})
                    .as_object()
                    .unwrap()
                    .clone(),
            ),
            ..Default::default()
        });
    provider.responses.lock().unwrap().push_back(ModelResponse {
        text: Some("final".into()),
        ..Default::default()
    });
    let outcome = execute_one(&provider, &config).await;
    let requests = provider.requests.lock().unwrap();
    assert_eq!(requests.len(), 2);
    assert_eq!(
        requests[1].params.extra["continuation_token"],
        "CURRENT_TOKEN"
    );
    assert!(!requests[1].params.extra.contains_key("STALE_TOKEN"));
    assert!(outcome.completed_generation);
}

#[tokio::test]
async fn continuation_requires_provider_opt_in_and_local_retries_are_rejected() {
    let mut supported = RunConfigV5 {
        segment_continuation_capability: "SUPPORTED".into(),
        ..RunConfigV5::default()
    };
    let mut provider = fake([ModelResponse::default()]);
    provider.opaque_continuation = false;
    let error = execute_logical_request_v5(
        &provider,
        &supported,
        ModelRequest::default(),
        context(),
        RequestParametersV5::default(),
    )
    .await
    .unwrap_err();
    assert!(error.to_string().contains("exact opaque checkpoints"));
    assert!(provider.requests.lock().unwrap().is_empty());

    supported.segment_continuation_capability = "NOT_REQUIRED".into();
    let mut provider = fake([ModelResponse::default()]);
    provider.local_retry_limit = Some(1);
    let error = execute_logical_request_v5(
        &provider,
        &supported,
        ModelRequest::default(),
        context(),
        RequestParametersV5::default(),
    )
    .await
    .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("provider-local retries must be disabled")
    );
    assert!(provider.requests.lock().unwrap().is_empty());
}

#[tokio::test]
async fn buffered_cli_error_with_partial_output_is_not_retried() {
    let mut raw = json!({
        "cli_buffered_error":true,
        "error_class":"TRANSIENT_FAILURE",
        "proven_unanswered":true
    })
    .as_object()
    .unwrap()
    .clone();
    raw.insert("decode_progress".into(), json!(true));
    let provider = fake([
        ModelResponse {
            error: Some("CLI reported malformed output".into()),
            text: Some("partial answer".into()),
            raw: Some(raw),
            usage: ib_providers::base::Usage {
                input_tokens: 7,
                output_tokens: 2,
                ..Default::default()
            },
            ..Default::default()
        },
        ModelResponse {
            text: Some("must not restart the observed output".into()),
            ..Default::default()
        },
    ]);
    let mut config = RunConfigV5::default();
    config.max_transient_retries = 2;
    config.max_recovery_continuations = 2;

    let outcome = execute_one(&provider, &config).await;
    assert_eq!(provider.requests.lock().unwrap().len(), 1);
    assert_eq!(outcome.physical_requests.len(), 1);
    assert_eq!(
        outcome.physical_requests[0].request_status,
        ib_v5_schemas::ProviderRequestStatusV5::AmbiguousFailure
    );
    assert_eq!(
        outcome.physical_requests[0].normalized_decode_status,
        ib_v5_schemas::NormalizedDecodeStatusV5::Unknown
    );
    assert_eq!(outcome.physical_requests[0].input_tokens_total, Some(7));
    assert_eq!(outcome.physical_requests[0].output_tokens_total, Some(2));
    assert_eq!(outcome.usage_sum.input_tokens, 7);
    assert_eq!(outcome.usage_sum.output_tokens, 2);
    assert!(!outcome.completed_generation);
}

#[tokio::test]
async fn malformed_reconciliation_cannot_change_allowance_or_leak_provider_details() {
    for reconciliation in [
        json!({"disposition":"RECONCILED_RESUMABLE","reconciled_output_allowance_tokens":true}),
        json!({"disposition":"CONFIRMED_LIMIT","extra":"synthetic-private-canary"}),
        json!({"disposition":"invented-provider-policy"}),
        json!({"disposition":"CONFIRMED_LIMIT","reconciled_output_allowance_tokens":-1}),
        json!({"disposition":"CONFIRMED_LIMIT","reconciled_input_context_tokens":u64::MAX}),
    ] {
        let response = ModelResponse {
            error: Some("synthetic-private-canary".into()),
            raw: Some(
                json!({"error_class":"CONTEXT_LIMIT_REJECTION",
                "context_reconciliation":reconciliation})
                .as_object()
                .unwrap()
                .clone(),
            ),
            ..Default::default()
        };
        let provider = Fake {
            responses: Mutex::new(VecDeque::from([response])),
            requests: Mutex::new(vec![]),
            retrieval_response: Mutex::new(None),
            retrieved_ids: Mutex::new(vec![]),
            local_retry_limit: None,
            opaque_continuation: true,
        };
        let context = RequestContextV5 {
            attempt_id: "attempt:test".into(),
            logical_request_id: "logical:test".into(),
            role: "unified".into(),
            agent_id: "agent:unified".into(),
            dispatch_round: 0,
            reservation_ordinal: (0, 0, 0, "unified".into(), 0),
        };
        let error = execute_logical_request_v5(
            &provider,
            &RunConfigV5::default(),
            ModelRequest::default(),
            context,
            RequestParametersV5::default(),
        )
        .await
        .unwrap_err();
        assert!(!error.to_string().contains("synthetic-private-canary"));
        assert_eq!(provider.requests.lock().unwrap().len(), 1);
    }
}

#[tokio::test]
async fn unrepresentable_cost_is_an_explicit_error_instead_of_missing_accounting() {
    for billed in [None, Some(1e30), Some(-1.0)] {
        let response = ModelResponse {
            text: Some("done".into()),
            reported_cost_usd: billed,
            usage: ib_providers::base::Usage {
                input_tokens: 1_000_000_000,
                output_tokens: 1_000_000_000,
                ..Default::default()
            },
            ..Default::default()
        };
        let mut config = RunConfigV5::default();
        config.pricing = Some(ib_v5_runner::PricingRatesV5 {
            pricing_manifest_sha256: "a".repeat(64),
            uncached_input_micros_per_million: i64::MAX,
            cached_input_micros_per_million: 0,
            output_micros_per_million: 0,
            reasoning_micros_per_million: 0,
        });
        let provider = Fake {
            responses: Mutex::new(VecDeque::from([response])),
            requests: Mutex::new(vec![]),
            retrieval_response: Mutex::new(None),
            retrieved_ids: Mutex::new(vec![]),
            local_retry_limit: None,
            opaque_continuation: true,
        };
        let context = RequestContextV5 {
            attempt_id: "attempt:test".into(),
            logical_request_id: "logical:test".into(),
            role: "unified".into(),
            agent_id: "agent:unified".into(),
            dispatch_round: 0,
            reservation_ordinal: (0, 0, 0, "unified".into(), 0),
        };
        let error = execute_logical_request_v5(
            &provider,
            &config,
            ModelRequest::default(),
            context,
            RequestParametersV5::default(),
        )
        .await
        .unwrap_err();
        assert!(error.to_string().contains("supported integer range"));
    }
}
