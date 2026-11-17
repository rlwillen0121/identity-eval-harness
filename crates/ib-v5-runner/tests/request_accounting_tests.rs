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

#[tokio::test]
async fn all_python_request_state_machine_vectors_match() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/request_accounting_python.json")).unwrap();
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let config = RunConfigV5::from_value(case["config"].clone()).unwrap();
        let responses = case["responses"]
            .as_array()
            .unwrap()
            .iter()
            .map(response_from_fixture)
            .collect();
        let provider = Fake {
            responses: Mutex::new(responses),
            requests: Mutex::new(vec![]),
            retrieval_response: Mutex::new(
                case.get("retrieval_response").map(response_from_fixture),
            ),
            retrieved_ids: Mutex::new(vec![]),
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
        assert_eq!(
            ib_core::jcs_dumps(&actual).unwrap(),
            ib_core::jcs_dumps(&case["expected"]).unwrap(),
            "request accounting parity: {name}"
        );
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
            assert_eq!(requests.len(), 2);
            assert_eq!(requests[0].request_id, requests[1].request_id);
            assert_eq!(
                requests[0].request_status,
                ib_v5_schemas::ProviderRequestStatusV5::AmbiguousFailure
            );
            assert_eq!(
                requests[1].request_status,
                ib_v5_schemas::ProviderRequestStatusV5::Completed
            );
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
