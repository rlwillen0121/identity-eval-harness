//! Mechanical replay of literal score-trace delivery events.
//!
//! This helper delegates query, staging, co-sign, commit, phase opening, and
//! read-ledger behavior to `RuntimeV5`. Its private mechanics worlds are built
//! only from the public slices in the supplied `EpisodeV5`; they are never
//! returned or serialized and are not semantic gold states.

use std::collections::BTreeSet;

use ib_v5_runtime::{RuntimeOptionsV5, RuntimeV5};
use ib_v5_schemas::{
    AtomicMutationV5, CoSignScopeKindV5, CommitReceiptStatusV5, CommitReceiptV5, DeliveredSliceV5,
    EpisodeV5, PhaseManifestV5, QuerySourcesStatusV5, StrictV5Model, WorldStateV5,
};
use indexmap::IndexMap;
use serde_json::{Map, Value};

use super::SharedRuntimeError;

#[derive(Debug, Clone, PartialEq)]
pub struct LiteralTraceQueryReceipt {
    pub event_seq: i64,
    pub phase_index: i64,
    pub status: QuerySourcesStatusV5,
    pub remaining_query_budget: i64,
    pub already_delivered_ids: Vec<String>,
    /// Only newly accepted payloads returned by the runtime are included.
    pub slices: Vec<DeliveredSliceV5>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReplayOutcome {
    Complete {
        phase2_opened: bool,
    },
    Unknown {
        after_event_seq: Option<i64>,
        reason: String,
    },
}

#[derive(Debug, Clone, PartialEq)]
pub struct LiteralTraceDeliveryProjection {
    pub query_receipts: Vec<LiteralTraceQueryReceipt>,
    /// New P1 payloads returned by successful query receipts only.
    pub phase1_slices: IndexMap<String, DeliveredSliceV5>,
    /// New P2 payloads returned by successful query receipts only.
    pub phase2_slices: IndexMap<String, DeliveredSliceV5>,
    /// Public manifest returned by an actual successful P1 commit.
    pub phase2_manifest: Option<PhaseManifestV5>,
    /// Mechanical commit receipts returned by RuntimeV5 in event order.
    pub commit_receipts: Vec<CommitReceiptV5>,
    /// The P1 commit snapshot ledger, or the final P1 ledger if P1 stayed open.
    pub phase1_read_ledger: BTreeSet<String>,
    /// Effective final P2 ledger after P1 invalidation and accepted P2 queries.
    pub phase2_read_ledger: Option<BTreeSet<String>>,
    pub outcome: ReplayOutcome,
}

/// Replay literal trace events through the public runtime API.
///
/// Private mechanics states are reconstructed from the manifest-selected
/// public source slices, with P2 layered over the selected P1 view. The
/// runtime's author-visible view still starts empty, so only accepted
/// `query_sources` calls can add payloads to the result.
/// This proves mechanical delivery and phase opening only; it does not certify
/// reset-mode semantic scoring or G-PROP.
pub fn replay_literal_trace_delivery(
    episode: &EpisodeV5,
    events: &[Value],
    mode: &str,
) -> Result<LiteralTraceDeliveryProjection, SharedRuntimeError> {
    episode
        .validate()
        .map_err(|error| SharedRuntimeError(error.to_string()))?;
    if !matches!(mode, "COUPLED" | "ORACLE_PHASE_RESET") {
        return Err(SharedRuntimeError(
            "score_trace mode must be COUPLED or ORACLE_PHASE_RESET".to_owned(),
        ));
    }

    let mut projection = empty_projection();
    if episode.phase1_slices.is_empty() || episode.phase2_slices.is_empty() {
        projection.outcome = ReplayOutcome::Unknown {
            after_event_seq: None,
            reason: "public episode lacks complete P1/P2 slice context".to_owned(),
        };
        return Ok(projection);
    }

    let initial_world = match public_shadow_world(episode, 1) {
        Ok(world) => world,
        Err(error) => {
            projection.outcome = ReplayOutcome::Unknown {
                after_event_seq: None,
                reason: format!("public P1 mechanics context is unavailable: {error}"),
            };
            return Ok(projection);
        }
    };
    let phase2_environment_state = match public_shadow_world(episode, 2) {
        Ok(world) => world,
        Err(error) => {
            projection.outcome = ReplayOutcome::Unknown {
                after_event_seq: None,
                reason: format!("public P2 mechanics context is unavailable: {error}"),
            };
            return Ok(projection);
        }
    };
    let phase2_reset_state =
        (mode == "ORACLE_PHASE_RESET").then(|| phase2_environment_state.clone());
    let mut runtime = match RuntimeV5::with_options(
        episode.clone(),
        initial_world,
        RuntimeOptionsV5 {
            wave_execution_mode: mode.to_owned(),
            phase2_reset_state,
            phase2_environment_state: Some(phase2_environment_state),
            ..RuntimeOptionsV5::default()
        },
    ) {
        Ok(runtime) => runtime,
        Err(error) => {
            projection.outcome = ReplayOutcome::Unknown {
                after_event_seq: None,
                reason: format!("public mechanics runtime could not be initialized: {error}"),
            };
            return Ok(projection);
        }
    };

    let mut previous_seq = -1_i64;
    let mut phase1_commit_ledger = None;
    for event_value in events {
        let (seq, tool, args) = match parse_event(event_value, previous_seq) {
            Ok(parts) => parts,
            Err(error) => {
                return Err(SharedRuntimeError(error));
            }
        };
        previous_seq = seq;

        match tool {
            "query_sources" => {
                if !exact_keys(args, &["phase_index", "slice_ids"]) {
                    return Err(SharedRuntimeError(
                        "query_sources args must contain exactly phase_index and slice_ids"
                            .to_owned(),
                    ));
                }
                let phase_index = args["phase_index"]
                    .as_i64()
                    .filter(|phase| matches!(phase, 1 | 2))
                    .ok_or_else(|| {
                        SharedRuntimeError("query_sources phase_index must be 1 or 2".to_owned())
                    })?;
                let slice_ids = args["slice_ids"]
                    .as_array()
                    .and_then(|items| items.iter().map(Value::as_str).collect::<Option<Vec<_>>>())
                    .ok_or_else(|| {
                        SharedRuntimeError(
                            "query_sources slice_ids must contain only strings".to_owned(),
                        )
                    })?;
                let slice_ids: Vec<String> = slice_ids.into_iter().map(str::to_owned).collect();
                let response = match runtime.query_sources(phase_index, &slice_ids) {
                    Ok(response) => response,
                    Err(error) => {
                        projection.outcome = ReplayOutcome::Unknown {
                            after_event_seq: Some(seq),
                            reason: format!(
                                "runtime could not materialize an accepted query response: {error}"
                            ),
                        };
                        break;
                    }
                };
                let receipt = LiteralTraceQueryReceipt {
                    event_seq: seq,
                    phase_index,
                    status: response.status,
                    remaining_query_budget: response.remaining_query_budget,
                    already_delivered_ids: response.already_delivered_ids,
                    slices: response.slices,
                };
                if receipt.status == QuerySourcesStatusV5::Ok {
                    let delivered_map = if phase_index == 1 {
                        &mut projection.phase1_slices
                    } else {
                        &mut projection.phase2_slices
                    };
                    for slice in &receipt.slices {
                        delivered_map.insert(slice.slice_id.clone(), slice.clone());
                    }
                }
                projection.query_receipts.push(receipt);
            }
            "stage_phase_mutations" => {
                if !exact_keys(args, &["phase_index", "stage_id", "plan_source"]) {
                    return Err(SharedRuntimeError(
                        "stage_phase_mutations args do not match the literal trace contract"
                            .to_owned(),
                    ));
                }
                let seq_stage = match phase_stage_args(args) {
                    Ok(value) => value,
                    Err(error) => return Err(SharedRuntimeError(error)),
                };
                let source = args["plan_source"].as_object().ok_or_else(|| {
                    SharedRuntimeError("stage plan_source must be an object".to_owned())
                })?;
                if source.len() != 1 || !source.contains_key("submitted_plan") {
                    projection.outcome = ReplayOutcome::Unknown {
                        after_event_seq: Some(seq),
                        reason:
                            "symbolic recipe plan cannot be replayed as a literal mutation list"
                                .to_owned(),
                    };
                    break;
                }
                let mutations = match parse_literal_plan(&source["submitted_plan"]) {
                    Ok(mutations) => mutations,
                    Err(error) => {
                        projection.outcome = ReplayOutcome::Unknown {
                            after_event_seq: Some(seq),
                            reason: format!("literal stage plan could not be parsed: {error}"),
                        };
                        break;
                    }
                };
                if let Err(error) =
                    runtime.stage_phase_mutations(seq_stage.0, &seq_stage.1, &mutations)
                {
                    projection.outcome = ReplayOutcome::Unknown {
                        after_event_seq: Some(seq),
                        reason: format!("runtime could not replay literal staging: {error}"),
                    };
                    break;
                }
            }
            "request_cosign" => {
                if !exact_keys(
                    args,
                    &[
                        "phase_index",
                        "stage_id",
                        "scope_kind",
                        "target_entity_id",
                        "actor_account_id",
                    ],
                ) {
                    return Err(SharedRuntimeError(
                        "request_cosign args do not match the trace contract".to_owned(),
                    ));
                }
                let (phase_index, stage_id) = phase_stage_args(args).map_err(SharedRuntimeError)?;
                let scope_kind =
                    parse_cosign_scope_kind(args["scope_kind"].as_str().ok_or_else(|| {
                        SharedRuntimeError("request_cosign scope_kind must be text".to_owned())
                    })?)
                    .ok_or_else(|| {
                        SharedRuntimeError("request_cosign scope_kind is unsupported".to_owned())
                    })?;
                let target = args["target_entity_id"].as_str().ok_or_else(|| {
                    SharedRuntimeError("request_cosign target_entity_id must be text".to_owned())
                })?;
                let actor = args["actor_account_id"].as_str().ok_or_else(|| {
                    SharedRuntimeError("request_cosign actor_account_id must be text".to_owned())
                })?;
                let _receipt =
                    runtime.request_cosign(phase_index, &stage_id, scope_kind, target, actor);
            }
            "commit_phase_mutations" => {
                if !exact_keys(args, &["phase_index", "stage_id"]) {
                    return Err(SharedRuntimeError(
                        "commit_phase_mutations args do not match the trace contract".to_owned(),
                    ));
                }
                let (phase_index, stage_id) = phase_stage_args(args).map_err(SharedRuntimeError)?;
                let receipt = match runtime.commit_phase_mutations(phase_index, &stage_id) {
                    Ok(receipt) => receipt,
                    Err(error) => {
                        projection.outcome = ReplayOutcome::Unknown {
                            after_event_seq: Some(seq),
                            reason: format!("runtime could not replay commit: {error}"),
                        };
                        break;
                    }
                };
                projection.commit_receipts.push(receipt.clone());
                if phase_index == 1 && receipt.status == CommitReceiptStatusV5::CommittedAndAdvanced
                {
                    if !runtime.phase2_opened() {
                        projection.outcome = ReplayOutcome::Unknown {
                            after_event_seq: Some(seq),
                            reason: "P1 commit receipt did not open P2 in the runtime".to_owned(),
                        };
                        break;
                    }
                    let Some(phase2_manifest) = receipt.phase2_manifest else {
                        projection.outcome = ReplayOutcome::Unknown {
                            after_event_seq: Some(seq),
                            reason: "successful P1 commit receipt omitted its P2 manifest"
                                .to_owned(),
                        };
                        break;
                    };
                    projection.phase2_manifest = Some(phase2_manifest);
                    phase1_commit_ledger = runtime
                        .phase_snapshots(1)
                        .and_then(|snapshots| snapshots.commit.as_ref())
                        .map(|snapshot| snapshot.read_ledger.clone());
                    if phase1_commit_ledger.is_none() {
                        projection.outcome = ReplayOutcome::Unknown {
                            after_event_seq: Some(seq),
                            reason: "successful P1 commit omitted its commit snapshot ledger"
                                .to_owned(),
                        };
                        break;
                    }
                }
            }
            _ => {
                return Err(SharedRuntimeError(format!(
                    "unsupported literal score_trace tool {tool:?}"
                )));
            }
        }
    }

    if matches!(projection.outcome, ReplayOutcome::Complete { .. }) {
        projection.outcome = ReplayOutcome::Complete {
            phase2_opened: runtime.phase2_opened(),
        };
    }
    projection.phase1_read_ledger = phase1_commit_ledger.unwrap_or_else(|| {
        if runtime.phase2_opened() {
            runtime
                .phase_snapshots(1)
                .and_then(|snapshots| snapshots.commit.as_ref())
                .map_or_else(BTreeSet::new, |snapshot| snapshot.read_ledger.clone())
        } else {
            runtime.read_ledger().clone()
        }
    });
    if runtime.phase2_opened() {
        projection.phase2_read_ledger = Some(runtime.read_ledger().clone());
    }
    Ok(projection)
}

fn empty_projection() -> LiteralTraceDeliveryProjection {
    LiteralTraceDeliveryProjection {
        query_receipts: Vec::new(),
        phase1_slices: IndexMap::new(),
        phase2_slices: IndexMap::new(),
        phase2_manifest: None,
        commit_receipts: Vec::new(),
        phase1_read_ledger: BTreeSet::new(),
        phase2_read_ledger: None,
        outcome: ReplayOutcome::Complete {
            phase2_opened: false,
        },
    }
}

fn public_shadow_world(
    episode: &EpisodeV5,
    phase_index: i64,
) -> Result<WorldStateV5, SharedRuntimeError> {
    let (phase1_view, phase2_view) = crate::production::native_episode_source_views(episode)
        .map_err(|error| SharedRuntimeError(error.to_string()))?;
    let mut world = match phase_index {
        1 => phase1_view.to_policy_state(),
        2 => phase2_view.to_policy_state(),
        _ => {
            return Err(SharedRuntimeError(
                "public mechanics world phase_index must be 1 or 2".to_owned(),
            ));
        }
    };
    world.world_rev = 0;
    Ok(world)
}

fn parse_event(
    value: &Value,
    previous_seq: i64,
) -> Result<(i64, &str, &Map<String, Value>), String> {
    let object = value
        .as_object()
        .ok_or_else(|| "score_trace event must be an object".to_owned())?;
    if object
        .keys()
        .any(|key| !matches!(key.as_str(), "seq" | "tool" | "args" | "token_usage"))
    {
        return Err("score_trace event has unknown keys".to_owned());
    }
    let seq = object
        .get("seq")
        .and_then(Value::as_i64)
        .filter(|seq| *seq > previous_seq)
        .ok_or_else(|| "score_trace event seq values must increase".to_owned())?;
    let tool = object
        .get("tool")
        .and_then(Value::as_str)
        .ok_or_else(|| "score_trace event tool must be text".to_owned())?;
    let args = object
        .get("args")
        .and_then(Value::as_object)
        .ok_or_else(|| "score_trace event args must be an object".to_owned())?;
    Ok((seq, tool, args))
}

fn exact_keys(object: &Map<String, Value>, required: &[&str]) -> bool {
    object.len() == required.len() && required.iter().all(|key| object.contains_key(*key))
}

fn phase_stage_args(args: &Map<String, Value>) -> Result<(i64, String), String> {
    let phase = args
        .get("phase_index")
        .and_then(Value::as_i64)
        .filter(|phase| matches!(phase, 1 | 2))
        .ok_or_else(|| "event phase_index must be 1 or 2".to_owned())?;
    let stage_id = args
        .get("stage_id")
        .and_then(Value::as_str)
        .filter(|stage_id| !stage_id.is_empty())
        .ok_or_else(|| "event stage_id must be nonempty text".to_owned())?;
    Ok((phase, stage_id.to_owned()))
}

fn parse_literal_plan(value: &Value) -> Result<Vec<AtomicMutationV5>, String> {
    let rows = value
        .as_array()
        .ok_or_else(|| "submitted_plan must be an array".to_owned())?;
    rows.iter()
        .map(|row| AtomicMutationV5::from_strict_json_value(row).map_err(|error| error.to_string()))
        .collect()
}

fn parse_cosign_scope_kind(raw: &str) -> Option<CoSignScopeKindV5> {
    match raw {
        "UAR_CUSTODIAN" => Some(CoSignScopeKindV5::UarCustodian),
        "CRITICAL_JIT" => Some(CoSignScopeKindV5::CriticalJit),
        "AGENT_DELEGATION_COSIGN" => Some(CoSignScopeKindV5::AgentDelegationCosign),
        _ => None,
    }
}
