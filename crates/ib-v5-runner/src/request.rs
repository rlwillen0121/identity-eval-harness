//! Outcome-blind context/output feasibility preflight for the canonical observation witness.

use ib_providers::base::{Message, ToolCall};
use ib_v5_runtime::{obs_strategy_q1, obs_strategy_q2};
use ib_v5_schemas::{EpisodeV5, PhaseManifestV5};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::config::RunConfigV5;
use crate::error::RunnerError;
use crate::prompts::{
    HARNESS_MSG_INVALID_ENVELOPE_V5, HARNESS_MSG_TOOL_SCHEMA_ERROR_V5,
    HARNESS_MSG_ZERO_CALL_CONTINUE_V5, make_request, render_v5_episode_opening,
    tool_specs_for_role,
};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WitnessPreflightStepV5 {
    pub step_index: u8,
    pub phase_index: u8,
    pub step_label: String,
    pub rendered_input_context_tokens: u64,
    pub effective_output_allowance_tokens: i64,
    pub fits: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ObsStrategyPreflightResultV5 {
    pub compatible: bool,
    pub episode_id: String,
    pub base_effective_output_ceiling_tokens: u64,
    pub effort_minimum_output_tokens: u64,
    pub segment_continuation_eligible: bool,
    pub min_witness_allowance_tokens: i64,
    pub witness_steps: Vec<WitnessPreflightStepV5>,
    #[serde(default)]
    pub failure_reasons: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContextReconciliationResultV5 {
    pub disposition: String,
    #[serde(default)]
    pub reconciled_input_context_tokens: Option<u64>,
    #[serde(default)]
    pub reconciled_output_allowance_tokens: Option<u64>,
    #[serde(default)]
    pub detail: String,
}

/// Run all six deterministic Q1/Q2/stage/commit witness requests without contacting a provider.
pub fn preflight_obs_strategy_witness_v5(
    episode: &EpisodeV5,
    run_config: &RunConfigV5,
    phase2_manifest: Option<&PhaseManifestV5>,
) -> Result<ObsStrategyPreflightResultV5, RunnerError> {
    let profile = run_config.resolved_topology_profile()?;
    let role = if profile.canonical_topology.as_str() == "T5_HIERARCHICAL_PIPELINE" {
        "executor"
    } else {
        profile
            .roles
            .first()
            .map_or("monolith", |r| r.role_id.as_str())
    };
    let manifest1 = &episode.phase1_manifest;
    let manifest2 = phase2_manifest.unwrap_or(&episode.phase2_base_manifest);
    let q1_p1 = obs_strategy_q1(manifest1, &episode.policy_card, 1)
        .map_err(|e| RunnerError::Configuration(e.to_string()))?;
    let q1_p1_slices = select_slices(&episode.phase1_slices, &q1_p1)?;
    let q2_p1 = obs_strategy_q2(
        manifest1,
        &q1_p1_slices,
        &manifest1.work_queue,
        &episode.policy_card,
        1,
    )
    .map_err(|e| RunnerError::Configuration(e.to_string()))?;
    let q1_p2 = obs_strategy_q1(manifest2, &episode.policy_card, 2)
        .map_err(|e| RunnerError::Configuration(e.to_string()))?;
    let q1_p2_slices = select_slices(&episode.phase2_slices, &q1_p2)?;
    let q2_p2 = obs_strategy_q2(
        manifest2,
        &q1_p2_slices,
        &manifest2.work_queue,
        &episode.policy_card,
        2,
    )
    .map_err(|e| RunnerError::Configuration(e.to_string()))?;

    let mut messages = vec![Message::user(render_v5_episode_opening(episode))];
    let mut steps = Vec::with_capacity(6);
    let mut failures = Vec::new();

    record_step(
        run_config,
        role,
        &messages,
        1,
        1,
        "P1_Q1",
        &mut steps,
        &mut failures,
    )?;
    append_query_turn(
        &mut messages,
        "wit_p1_q1",
        1,
        &q1_p1,
        &episode.phase1_slices,
    )?;
    record_step(
        run_config,
        role,
        &messages,
        2,
        1,
        "P1_Q2",
        &mut steps,
        &mut failures,
    )?;
    append_query_turn(
        &mut messages,
        "wit_p1_q2",
        1,
        &q2_p1,
        &episode.phase1_slices,
    )?;
    record_step(
        run_config,
        role,
        &messages,
        3,
        1,
        "P1_STAGE_COMMIT",
        &mut steps,
        &mut failures,
    )?;
    append_stage_commit_turn(&mut messages, 1, manifest2)?;
    record_step(
        run_config,
        role,
        &messages,
        4,
        2,
        "P2_Q1",
        &mut steps,
        &mut failures,
    )?;
    append_query_turn(
        &mut messages,
        "wit_p2_q1",
        2,
        &q1_p2,
        &episode.phase2_slices,
    )?;
    record_step(
        run_config,
        role,
        &messages,
        5,
        2,
        "P2_Q2",
        &mut steps,
        &mut failures,
    )?;
    append_query_turn(
        &mut messages,
        "wit_p2_q2",
        2,
        &q2_p2,
        &episode.phase2_slices,
    )?;
    record_step(
        run_config,
        role,
        &messages,
        6,
        2,
        "P2_STAGE_COSIGN_COMMIT",
        &mut steps,
        &mut failures,
    )?;

    let segment_eligible = run_config.is_segment_continuation_eligible();
    if !segment_eligible {
        failures.push(
            "segment_continuation_capability must be SUPPORTED when base_effective_output_ceiling_tokens exceeds provider_segment_output_limit_tokens".to_owned(),
        );
    }
    let min_allowance = steps
        .iter()
        .map(|s| s.effective_output_allowance_tokens)
        .min()
        .unwrap_or(0);
    Ok(ObsStrategyPreflightResultV5 {
        compatible: failures.is_empty(),
        episode_id: episode.episode_id.clone(),
        base_effective_output_ceiling_tokens: run_config.base_effective_output_ceiling_tokens,
        effort_minimum_output_tokens: run_config.effort_minimum_output_tokens,
        segment_continuation_eligible: segment_eligible,
        min_witness_allowance_tokens: min_allowance,
        witness_steps: steps,
        failure_reasons: failures,
    })
}

fn select_slices(
    slices: &indexmap::IndexMap<String, ib_v5_schemas::DeliveredSliceV5>,
    ids: &[String],
) -> Result<Vec<ib_v5_schemas::DeliveredSliceV5>, RunnerError> {
    ids.iter()
        .map(|id| {
            slices.get(id).cloned().ok_or_else(|| {
                RunnerError::Configuration(format!(
                    "observation witness references missing slice {id:?}"
                ))
            })
        })
        .collect()
}

fn record_step(
    config: &RunConfigV5,
    role: &str,
    messages: &[Message],
    step_index: u8,
    phase_index: u8,
    label: &str,
    steps: &mut Vec<WitnessPreflightStepV5>,
    failures: &mut Vec<String>,
) -> Result<(), RunnerError> {
    let _tools = tool_specs_for_role(config, role, Some(phase_index))?;
    let request = make_request(
        None,
        config,
        messages,
        role,
        phase_index,
        None,
        None,
        Map::new(),
    )?;
    let rendered_tokens = crate::prompts::count_model_request_tokens_v5(&request)?;
    let allowance = crate::config::compute_effective_output_allowance_v5(
        i64_from_u64(config.base_effective_output_ceiling_tokens),
        i64_from_u64(config.provider_max_output_tokens),
        i64_from_u64(config.context_window_tokens),
        i64_from_u64(rendered_tokens),
        i64_from_u64(config.provider_schema_overhead_tokens),
        i64_from_u64(config.minimum_context_headroom_tokens),
    );
    let fits = allowance >= i64_from_u64(config.effort_minimum_output_tokens);
    if !fits {
        failures.push(format!(
            "witness step {step_index} ({label}) allowance {allowance} < effort_minimum_output_tokens {}",
            config.effort_minimum_output_tokens
        ));
    }
    steps.push(WitnessPreflightStepV5 {
        step_index,
        phase_index,
        step_label: label.to_owned(),
        rendered_input_context_tokens: rendered_tokens,
        effective_output_allowance_tokens: allowance,
        fits,
    });
    Ok(())
}

fn i64_from_u64(value: u64) -> i64 {
    i64::try_from(value).unwrap_or(i64::MAX)
}

fn append_query_turn(
    messages: &mut Vec<Message>,
    call_id: &str,
    phase: u8,
    ids: &[String],
    slices: &indexmap::IndexMap<String, ib_v5_schemas::DeliveredSliceV5>,
) -> Result<(), RunnerError> {
    let mut args = Map::new();
    args.insert("phase_index".into(), Value::from(phase));
    args.insert("slice_ids".into(), serde_json::to_value(ids)?);
    let call = ToolCall {
        id: call_id.to_owned(),
        name: "query_sources".to_owned(),
        arguments: args,
    };
    messages.push(Message::assistant(" ", vec![call.clone()]));
    let slice_values: Vec<Value> = ids
        .iter()
        .map(|id| {
            slices
                .get(id)
                .map(serde_json::to_value)
                .transpose()
                .map(|v| v.unwrap_or(Value::Null))
        })
        .collect::<Result<_, _>>()?;
    let receipt = json!({
        "status": "OK",
        "phase_index": phase,
        "remaining_query_budget": 0,
        "already_delivered_ids": [],
        "slices": slice_values,
    });
    messages.push(Message::tool(
        call_id,
        crate::prompts::compact_json(&receipt),
    ));
    Ok(())
}

fn append_stage_commit_turn(
    messages: &mut Vec<Message>,
    phase: u8,
    phase2_manifest: &PhaseManifestV5,
) -> Result<(), RunnerError> {
    let stage_id = if phase == 1 { "stg_p1_01" } else { "stg_p2_01" };
    let mut stage_args = Map::new();
    stage_args.insert("phase_index".into(), Value::from(phase));
    stage_args.insert("stage_id".into(), Value::String(stage_id.to_owned()));
    stage_args.insert("mutations".into(), Value::Array(Vec::new()));
    let mut commit_args = Map::new();
    commit_args.insert("phase_index".into(), Value::from(phase));
    commit_args.insert("stage_id".into(), Value::String(stage_id.to_owned()));
    let stage_call = ToolCall {
        id: format!("wit_p{phase}_stage"),
        name: "stage_phase_mutations".to_owned(),
        arguments: stage_args,
    };
    let commit_call = ToolCall {
        id: format!("wit_p{phase}_commit"),
        name: "commit_phase_mutations".to_owned(),
        arguments: commit_args,
    };
    messages.push(Message::assistant(
        " ",
        vec![stage_call.clone(), commit_call.clone()],
    ));
    let stage_receipt = json!({
        "status": "STAGED",
        "phase_index": phase,
        "stage_id": stage_id,
        "mutation_count": 0,
        "stage_digest": "0".repeat(64),
        "dependency_digest": "0".repeat(64),
    });
    messages.push(Message::tool(
        &stage_call.id,
        crate::prompts::compact_json(&stage_receipt),
    ));
    let commit_receipt = json!({
        "status": if phase == 1 { "COMMITTED_AND_ADVANCED" } else { "EPISODE_COMPLETED" },
        "phase_index": phase,
        "stage_id": stage_id,
        "world_rev": phase,
        "phase2_manifest": if phase == 1 { serde_json::to_value(phase2_manifest)? } else { Value::Null },
    });
    messages.push(Message::tool(
        &commit_call.id,
        crate::prompts::compact_json(&commit_receipt),
    ));
    Ok(())
}

#[allow(dead_code)]
fn _fixed_messages() -> [&'static str; 3] {
    [
        HARNESS_MSG_INVALID_ENVELOPE_V5,
        HARNESS_MSG_TOOL_SCHEMA_ERROR_V5,
        HARNESS_MSG_ZERO_CALL_CONTINUE_V5,
    ]
}
