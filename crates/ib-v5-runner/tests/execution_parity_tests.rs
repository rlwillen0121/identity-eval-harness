use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use ib_providers::base::{
    Message, ModelRequest, ModelResponse, Provider, ProviderCapabilities, ToolCall,
};
use ib_v5_oracle::evaluate_phase;
use ib_v5_runner::RunConfigV5;
use ib_v5_runner::execution::{
    RoundRequestSpecV5, RunEpisodeOptionsV5, TurnPolicyV5, run_v5_agent_episode_with_options,
    run_v5_scripted_episode,
};
use ib_v5_runtime::RuntimeV5;
use ib_v5_schemas::{EpisodeV5, SealedEpisodeOracleV5};
use serde_json::{Value, json};

fn fixture_pair() -> (EpisodeV5, SealedEpisodeOracleV5) {
    let episode: EpisodeV5 = serde_json::from_str(
        include_str!("../../../datasets/v5_dev/episodes.jsonl")
            .lines()
            .next()
            .expect("v5_dev episode fixture"),
    )
    .expect("valid frozen episode fixture");
    let oracle: SealedEpisodeOracleV5 = serde_json::from_str(
        include_str!("../../../datasets/v5_dev/oracles.jsonl")
            .lines()
            .next()
            .expect("v5_dev oracle fixture"),
    )
    .expect("valid frozen oracle fixture");
    assert_eq!(episode.episode_id, oracle.episode_id);
    (episode, oracle)
}

struct FakeProvider {
    responses: Mutex<VecDeque<ModelResponse>>,
    requests: Mutex<Vec<ModelRequest>>,
    native_tools: bool,
}

impl FakeProvider {
    fn new(responses: impl IntoIterator<Item = ModelResponse>, native_tools: bool) -> Self {
        Self {
            responses: Mutex::new(responses.into_iter().collect()),
            requests: Mutex::new(Vec::new()),
            native_tools,
        }
    }

    fn requests(&self) -> Vec<ModelRequest> {
        self.requests.lock().expect("request lock").clone()
    }
}

#[async_trait]
impl Provider for FakeProvider {
    fn kind(&self) -> &str {
        "offline-test"
    }

    fn model(&self) -> &str {
        "parity-fixture"
    }

    fn python_type_identity(&self) -> (&'static str, &'static str) {
        ("tests", "ExecutionParityFakeProvider")
    }

    fn endpoint_identity(&self) -> String {
        "offline-fixture".to_owned()
    }

    fn capabilities(&self) -> ProviderCapabilities {
        ProviderCapabilities {
            native_structured_output: true,
            native_tools: self.native_tools,
            supports_temperature: true,
            reports_usage: true,
        }
    }

    async fn complete(&self, request: &ModelRequest) -> ModelResponse {
        self.requests
            .lock()
            .expect("request lock")
            .push(request.clone());
        self.responses
            .lock()
            .expect("response lock")
            .pop_front()
            .unwrap_or_default()
    }
}

fn call(id: &str, name: &str, arguments: Value) -> ToolCall {
    ToolCall {
        id: id.to_owned(),
        name: name.to_owned(),
        arguments: arguments
            .as_object()
            .expect("tool arguments are an object")
            .clone(),
    }
}

fn response_with_calls(calls: Vec<ToolCall>) -> ModelResponse {
    ModelResponse {
        tool_calls: calls,
        ..ModelResponse::default()
    }
}

struct QueryThenNoop(Mutex<usize>);

impl TurnPolicyV5 for QueryThenNoop {
    fn next_turn(&self, _context: &ib_v5_runner::AgentTurnContextV5) -> Vec<ToolCall> {
        let mut turn = self.0.lock().expect("policy lock");
        let result = if *turn == 0 {
            vec![call(
                "query-1",
                "query_sources",
                json!({"phase_index": 1, "slice_ids": []}),
            )]
        } else {
            Vec::new()
        };
        *turn += 1;
        result
    }
}

#[tokio::test]
async fn scripted_noop_uses_legacy_reason_and_baseline_sequence_ordinal() {
    let (episode, oracle) = fixture_pair();
    let result = run_v5_scripted_episode(
        &episode,
        &oracle.omniscient_s1,
        &QueryThenNoop(Mutex::new(0)),
        RunConfigV5::default(),
    )
    .await
    .expect("scripted baseline execution");

    assert_eq!(result.termination_reason, "NO_TOOL_CALL");
    let ordinal = result
        .terminal_reservation_ordinal
        .as_ref()
        .expect("empty scripted turn has terminal ordinal");
    assert_eq!(ordinal.3, "baseline");
    assert_eq!(ordinal.4, 1, "baseline agent sequence advances per turn");
}

struct TooManyCalls;

impl TurnPolicyV5 for TooManyCalls {
    fn next_turn(&self, _context: &ib_v5_runner::AgentTurnContextV5) -> Vec<ToolCall> {
        (0..7)
            .map(|index| {
                call(
                    &format!("query-{index}"),
                    "query_sources",
                    json!({"phase_index": 1, "slice_ids": []}),
                )
            })
            .collect()
    }
}

#[tokio::test]
async fn scripted_call_limit_is_not_overwritten_as_generation_cap() {
    let (episode, oracle) = fixture_pair();
    let result = run_v5_scripted_episode(
        &episode,
        &oracle.omniscient_s1,
        &TooManyCalls,
        RunConfigV5::default(),
    )
    .await
    .expect("scripted baseline execution");

    assert_eq!(
        result.attempt_status.as_str(),
        "WITHIN_PROTOCOL_PARSE_FAILURE"
    );
    assert_eq!(
        result.terminal_cause.as_deref(),
        Some("TOOL_CALL_LIMIT_EXCEEDED")
    );
}

#[tokio::test]
async fn provider_allowance_terminal_status_survives_loop_exit() {
    let (episode, oracle) = fixture_pair();
    let provider = Arc::new(FakeProvider::new([], true));
    let provider_dyn: Arc<dyn Provider> = provider.clone();
    let config = RunConfigV5 {
        max_turns: Some(1),
        ..RunConfigV5::default()
    };
    let scheduler = Arc::new(|_round: u64, _runtime: &RuntimeV5, _config: &RunConfigV5| {
        vec![RoundRequestSpecV5 {
            role: "monolith".to_owned(),
            agent_id: "monolith".to_owned(),
            messages: vec![Message::user("padding ".repeat(250_000))],
            phase_index: 1,
            provider: None,
        }]
    });
    let result = run_v5_agent_episode_with_options(
        &episode,
        &oracle.omniscient_s1,
        provider_dyn,
        RunEpisodeOptionsV5 {
            run_config: config,
            round_scheduler: Some(scheduler),
            ..RunEpisodeOptionsV5::default()
        },
    )
    .await
    .expect("context-limited provider run");

    assert_eq!(
        result.attempt_status.as_str(),
        "WITHIN_PROTOCOL_CONTEXT_LIMIT"
    );
    assert_eq!(
        result.terminal_cause.as_deref(),
        Some("ALLOWANCE_BELOW_EFFORT_MIN")
    );
    assert!(
        provider.requests().is_empty(),
        "allowance is checked before provider call"
    );
}

#[tokio::test]
async fn scheduler_supplies_agent_messages_and_stable_ordinals() {
    let (episode, oracle) = fixture_pair();
    let provider = Arc::new(FakeProvider::new(
        [ModelResponse::default(), ModelResponse::default()],
        true,
    ));
    let provider_dyn: Arc<dyn Provider> = provider.clone();
    let config = RunConfigV5 {
        max_turns: Some(2),
        ..RunConfigV5::default()
    };
    let scheduler = Arc::new(|_round: u64, _runtime: &RuntimeV5, _config: &RunConfigV5| {
        ["agent_z", "agent_a"]
            .into_iter()
            .map(|agent_id| RoundRequestSpecV5 {
                role: "monolith".to_owned(),
                agent_id: agent_id.to_owned(),
                messages: vec![Message::user(format!("scheduled slot {agent_id}"))],
                phase_index: 1,
                provider: None,
            })
            .collect()
    });
    let result = run_v5_agent_episode_with_options(
        &episode,
        &oracle.omniscient_s1,
        provider_dyn,
        RunEpisodeOptionsV5 {
            run_config: config,
            round_scheduler: Some(scheduler),
            ..RunEpisodeOptionsV5::default()
        },
    )
    .await
    .expect("scheduled provider run");

    let requests = provider.requests();
    assert_eq!(requests.len(), 2);
    assert!(
        requests[0]
            .messages
            .iter()
            .any(|message| message.content == "scheduled slot agent_a")
    );
    assert!(
        requests[1]
            .messages
            .iter()
            .any(|message| message.content == "scheduled slot agent_z")
    );
    assert_eq!(result.provider_requests[0].agent_id, "agent_a");
    assert_eq!(result.provider_requests[1].agent_id, "agent_z");
    assert_eq!(result.provider_requests[0].reservation_ordinal.4, 0);
    assert_eq!(result.provider_requests[1].reservation_ordinal.4, 0);
}

#[tokio::test]
async fn terminal_lower_ordinal_latches_other_completed_provider_turn_without_effects() {
    let (episode, oracle) = fixture_pair();
    let provider = Arc::new(FakeProvider::new(
        [response_with_calls(vec![call(
            "late-query",
            "query_sources",
            json!({"phase_index": 1, "slice_ids": []}),
        )])],
        true,
    ));
    let provider_dyn: Arc<dyn Provider> = provider.clone();
    let config = RunConfigV5 {
        max_turns: Some(2),
        ..RunConfigV5::default()
    };
    let scheduler = Arc::new(|_round: u64, _runtime: &RuntimeV5, _config: &RunConfigV5| {
        vec![
            RoundRequestSpecV5 {
                role: "monolith".to_owned(),
                agent_id: "agent_a_terminal".to_owned(),
                messages: vec![Message::user("padding ".repeat(250_000))],
                phase_index: 1,
                provider: None,
            },
            RoundRequestSpecV5 {
                role: "monolith".to_owned(),
                agent_id: "agent_z_inflight".to_owned(),
                messages: vec![Message::user("completed provider response must be latched")],
                phase_index: 1,
                provider: None,
            },
        ]
    });
    let result = run_v5_agent_episode_with_options(
        &episode,
        &oracle.omniscient_s1,
        provider_dyn,
        RunEpisodeOptionsV5 {
            run_config: config,
            round_scheduler: Some(scheduler),
            ..RunEpisodeOptionsV5::default()
        },
    )
    .await
    .expect("concurrent scheduled provider run");

    assert_eq!(
        result.attempt_status.as_str(),
        "WITHIN_PROTOCOL_CONTEXT_LIMIT"
    );
    assert_eq!(
        result.terminal_cause.as_deref(),
        Some("ALLOWANCE_BELOW_EFFORT_MIN")
    );
    assert_eq!(
        result.terminal_reservation_ordinal.as_ref().unwrap().3,
        "agent_a_terminal"
    );
    assert_eq!(
        provider.requests().len(),
        1,
        "the second slot completed physically"
    );
    let late_request = result
        .provider_requests
        .iter()
        .find(|request| request.agent_id == "agent_z_inflight")
        .expect("other dispatched slot is recorded");
    assert!(
        late_request.post_terminal,
        "later ordinal is marked post-terminal"
    );
    assert!(
        !result
            .tool_trace
            .iter()
            .any(|trace| trace.tool_name == "query_sources"),
        "the completed later response must not dispatch its tool call"
    );
    assert_eq!(result.tool_calls_requested, 0);
    assert_eq!(result.tool_calls_executed, 0);
}

#[tokio::test]
async fn non_native_tool_results_are_returned_as_user_observations() {
    let (episode, oracle) = fixture_pair();
    let provider = Arc::new(FakeProvider::new(
        [
            response_with_calls(vec![call(
                "query-1",
                "query_sources",
                json!({"phase_index": 1, "slice_ids": []}),
            )]),
            ModelResponse::default(),
        ],
        false,
    ));
    let provider_dyn: Arc<dyn Provider> = provider.clone();
    let config = RunConfigV5 {
        max_turns: Some(2),
        ..RunConfigV5::default()
    };
    let _ = run_v5_agent_episode_with_options(
        &episode,
        &oracle.omniscient_s1,
        provider_dyn,
        RunEpisodeOptionsV5 {
            run_config: config,
            ..RunEpisodeOptionsV5::default()
        },
    )
    .await
    .expect("non-native tool provider run");

    let requests = provider.requests();
    assert_eq!(requests.len(), 2);
    assert!(
        requests[1]
            .messages
            .iter()
            .any(|message| message.content.starts_with("## Tool Execution Results\n"))
    );
}

#[tokio::test]
async fn auditor_decision_json_with_actions_null_is_not_a_parse_failure() {
    let (episode, oracle) = fixture_pair();
    let provider = Arc::new(FakeProvider::new(
        [ModelResponse {
            text: Some(
                json!({
                    "actions": null,
                    "decision": "APPROVE",
                    "target_role": null,
                    "feedback": "checker receipt verified"
                })
                .to_string(),
            ),
            ..ModelResponse::default()
        }],
        false,
    ));
    let provider_dyn: Arc<dyn Provider> = provider.clone();
    let config = RunConfigV5 {
        topology_id: "T5_HIERARCHICAL_PIPELINE".to_owned(),
        max_turns: Some(1),
        ..RunConfigV5::default()
    };
    let scheduler = Arc::new(|_round: u64, _runtime: &RuntimeV5, _config: &RunConfigV5| {
        vec![RoundRequestSpecV5 {
            role: "auditor".to_owned(),
            agent_id: "auditor".to_owned(),
            messages: vec![Message::user("Review the staged work")],
            phase_index: 1,
            provider: None,
        }]
    });
    let result = run_v5_agent_episode_with_options(
        &episode,
        &oracle.omniscient_s1,
        provider_dyn,
        RunEpisodeOptionsV5 {
            run_config: config,
            diagnostic_oracle: Some(oracle.clone()),
            round_scheduler: Some(scheduler),
            ..RunEpisodeOptionsV5::default()
        },
    )
    .await
    .expect("auditor verdict provider run");

    assert_ne!(
        result.attempt_status.as_str(),
        "WITHIN_PROTOCOL_PARSE_FAILURE"
    );
}

#[tokio::test]
async fn t5_checker_receipt_stays_with_auditor_after_approval() {
    let (episode, oracle) = fixture_pair();
    let q1_slices: Vec<String> = episode
        .phase1_manifest
        .source_directory
        .iter()
        .filter(|descriptor| {
            descriptor.authority_tier == ib_v5_schemas::AuthorityTierV5::AuthoritativePrimary
                && descriptor.is_complete_for_scope
                && (0..=episode.policy_card.overlay.freshness_sla_s)
                    .contains(&(episode.phase1_manifest.clock_epoch_s - descriptor.watermark_s))
        })
        .map(|descriptor| descriptor.slice_id.clone())
        .collect();
    assert!(!q1_slices.is_empty(), "fixture needs a fresh primary slice");
    let gold_mutations = evaluate_phase(
        &oracle.omniscient_s1,
        &episode.phase1_manifest.work_queue,
        &oracle.sealed_jml_adversarial_events,
    )
    .expect("fixture phase-one gold mutations");
    let mutation_values = serde_json::to_value(gold_mutations).expect("serialize gold mutations");
    let provider = Arc::new(FakeProvider::new(
        [
            ModelResponse {
                text: Some("plan phase one".to_owned()),
                ..ModelResponse::default()
            },
            response_with_calls(vec![call(
                "query-p1",
                "query_sources",
                json!({"phase_index": 1, "slice_ids": q1_slices}),
            )]),
            response_with_calls(vec![call(
                "stage-p1",
                "stage_phase_mutations",
                json!({
                    "phase_index": 1,
                    "stage_id": "stg_p1_01",
                    "mutations": mutation_values,
                }),
            )]),
            response_with_calls(vec![call(
                "checker-p1",
                "run_phase_checker",
                json!({"phase_index": 1, "stage_id": "stg_p1_01"}),
            )]),
            ModelResponse {
                text: Some(
                    json!({
                        "actions": null,
                        "decision": "APPROVE",
                        "target_role": null,
                        "feedback": "checker receipt verified"
                    })
                    .to_string(),
                ),
                ..ModelResponse::default()
            },
            ModelResponse::default(),
        ],
        false,
    ));
    let provider_dyn: Arc<dyn Provider> = provider.clone();
    let config = RunConfigV5 {
        topology_id: "T5_HIERARCHICAL_PIPELINE".to_owned(),
        max_turns: Some(6),
        ..RunConfigV5::default()
    };
    let _result = run_v5_agent_episode_with_options(
        &episode,
        &oracle.omniscient_s1,
        provider_dyn,
        RunEpisodeOptionsV5 {
            run_config: config,
            diagnostic_oracle: Some(oracle.clone()),
            ..RunEpisodeOptionsV5::default()
        },
    )
    .await
    .expect("T5 checker and approval run");

    let requests = provider.requests();
    assert_eq!(requests.len(), 6);
    let auditor_followup = &requests[4];
    let checker_observation = auditor_followup
        .messages
        .iter()
        .find_map(|message| {
            let (_heading, payload) = message.content.split_once("## Tool Execution Results\n")?;
            let observations: Value = serde_json::from_str(payload).ok()?;
            observations
                .as_array()?
                .iter()
                .find(|observation| {
                    observation.get("name").and_then(Value::as_str) == Some("run_phase_checker")
                })
                .cloned()
        })
        .expect("auditor receives checker tool result");
    let receipt = checker_observation
        .get("result")
        .expect("checker tool observation has a receipt");
    assert_eq!(receipt.get("status").and_then(Value::as_str), Some("PASS"));
    assert_eq!(
        receipt.get("stage_id").and_then(Value::as_str),
        Some("stg_p1_01")
    );
    assert!(
        receipt
            .get("stage_digest")
            .and_then(Value::as_str)
            .is_some_and(|digest| !digest.is_empty()),
        "auditor receives the checker receipt's content digest"
    );
    let receipt_text = serde_json::to_string(receipt).expect("serialize checker receipt");
    assert!(auditor_followup.messages.iter().any(|message| {
        message.content.contains("## Tool Execution Results")
            && message.content.contains(&receipt_text)
    }));
    let executor_history = requests[5]
        .messages
        .iter()
        .map(|message| message.content.as_str())
        .collect::<Vec<_>>()
        .join("\n");
    assert!(!executor_history.contains("checker-p1"));
    assert!(!executor_history.contains(&receipt_text));
    assert!(executor_history.contains("[Auditor APPROVED] checker receipt verified"));
}
