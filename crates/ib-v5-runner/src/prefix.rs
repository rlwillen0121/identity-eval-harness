//! Exact first-50-generation prefix reconstruction and the `v5_dev` cap selection rule.

use std::collections::BTreeSet;

use ib_v5_accounting::EpisodeAttemptV5;
use ib_v5_schemas::{
    AttemptStatusV5, EpisodeV5, NormalizedDecodeStatusV5, ProviderRequestStatusV5,
    ProviderRequestUsageV5, SealedEpisodeOracleV5, StrictV5Model,
};
use ib_v5_scoring::{
    CompletionTraceV5, EpisodeExecutionV5, ScoreExecutionOptionsV5, TrajectorySnapshotV5,
    score_execution_v5,
};
use serde::{Deserialize, Serialize};

use crate::RunnerError;

pub const DEFAULT_CAP_SELECTION_TRACKS_V5: [&str; 1] = ["PRIMARY_HIGHEST"];
const CAP_SELECTION_TRACKS_V5: [&str; 3] = [
    "PRIMARY_HIGHEST",
    "PREREGISTERED_LOWER",
    "PREREGISTERED_ABLATION",
];

/// One resolved 100-cap `v5_dev` slot and its optional paired 50-cap prefix result.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DevCapSensitivitySlotV5 {
    pub attempt_100: EpisodeAttemptV5,
    pub effort_track: String,
    pub attempt_50: Option<EpisodeAttemptV5>,
}

/// Result of the preregistered `v5_dev` generation-cap selection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DevCapSelectionResultV5 {
    pub selected_cap: u8,
    pub cap_selection_tracks: Vec<String>,
    pub planned_dev_slots: usize,
    pub resolved_dev_slots: usize,
    pub triggering_attempt_ids: Vec<String>,
}

/// Optional additional execution evidence needed to derive exact prefix metrics.
#[derive(Debug, Clone, Copy, Default)]
pub struct PrefixOutcomeOptionsV5<'a> {
    pub execution: Option<&'a EpisodeExecutionV5>,
    pub episode: Option<&'a EpisodeV5>,
    pub oracle: Option<&'a SealedEpisodeOracleV5>,
    pub prefix_tool_call_count: Option<i64>,
    pub prefix_elapsed_wall_time_ms: Option<i64>,
    pub prefix_terminal_outcome_elapsed_ms: Option<i64>,
    pub prefix_active_elapsed_ms: Option<i64>,
    pub prefix_overgrant_count: Option<i64>,
    pub prefix_avail_breach_count: Option<i64>,
}

/// Derive the paired 50-generation cap outcome from the exact first 50 provider ordinals.
pub fn derive_50_cap_prefix_outcome(
    attempt_100: &EpisodeAttemptV5,
) -> Result<EpisodeAttemptV5, RunnerError> {
    derive_50_cap_prefix_outcome_with_options(attempt_100, PrefixOutcomeOptionsV5::default())
}

/// Derive a 50-generation outcome using optional trace, score, and explicit prefix metrics.
pub fn derive_50_cap_prefix_outcome_with_options(
    attempt_100: &EpisodeAttemptV5,
    options: PrefixOutcomeOptionsV5<'_>,
) -> Result<EpisodeAttemptV5, RunnerError> {
    attempt_100
        .validate()
        .map_err(|_| invalid_prefix("100-cap attempt is invalid"))?;

    let mut ordered_non_post_completed = Vec::new();
    let mut seen_non_post = BTreeSet::new();
    let mut ordered_all_completed = Vec::new();
    let mut seen_all = BTreeSet::new();
    for request in sorted_requests(&attempt_100.provider_requests) {
        if is_terminal_completed(request) {
            if seen_all.insert(request.logical_request_id.as_str()) {
                ordered_all_completed.push(request.reservation_ordinal.clone());
            }
            if !request.post_terminal && seen_non_post.insert(request.logical_request_id.as_str()) {
                ordered_non_post_completed.push(request.reservation_ordinal.clone());
            }
        }
    }
    if ordered_all_completed.len() < 50 {
        return Ok(attempt_100.clone());
    }

    let ordinal_50 = ordered_non_post_completed
        .get(49)
        .or_else(|| ordered_all_completed.get(49))
        .expect("at least 50 completed requests were checked above")
        .clone();
    let prefix_requests: Vec<ProviderRequestUsageV5> = attempt_100
        .provider_requests
        .iter()
        .filter(|request| request.reservation_ordinal <= ordinal_50)
        .cloned()
        .collect();
    let prefix_retry_count = i64::try_from(
        prefix_requests
            .iter()
            .filter(|request| {
                request.request_kind == ib_v5_schemas::ProviderRequestKindV5::TransientRetry
            })
            .count(),
    )
    .map_err(|_| invalid_prefix("prefix retry count is outside supported range"))?;
    let prefix_completed_gens = i64::try_from(
        prefix_requests
            .iter()
            .filter(|request| is_terminal_completed(request))
            .map(|request| request.logical_request_id.as_str())
            .collect::<BTreeSet<_>>()
            .len(),
    )
    .map_err(|_| invalid_prefix("prefix generation count is outside supported range"))?;

    let terminated_within_50 = (attempt_100.attempt_status
        != AttemptStatusV5::WithinProtocolStepLimit
        && (attempt_100
            .terminal_reservation_ordinal
            .as_ref()
            .is_some_and(|ordinal| ordinal <= &ordinal_50)
            || (attempt_100.terminal_reservation_ordinal.is_none()
                && attempt_100.completed_assistant_generations <= 50)))
        || (attempt_100.attempt_status == AttemptStatusV5::WithinProtocolStepLimit
            && attempt_100.completed_assistant_generations == 50
            && (attempt_100
                .terminal_reservation_ordinal
                .as_ref()
                .is_none_or(|ordinal| ordinal <= &ordinal_50)));

    if terminated_within_50 {
        if attempt_100.completed_assistant_generations <= 50
            && prefix_requests.len() == attempt_100.provider_requests.len()
        {
            return Ok(attempt_100.clone());
        }
        let resolved_tool_calls = options
            .prefix_tool_call_count
            .unwrap_or(attempt_100.tool_call_count);
        let resolved_term_ms = options
            .prefix_terminal_outcome_elapsed_ms
            .or(attempt_100.terminal_outcome_elapsed_ms);
        let mut resolved_wall_ms = options.prefix_elapsed_wall_time_ms.or(resolved_term_ms);
        let resolved_active_ms = options
            .prefix_active_elapsed_ms
            .or(attempt_100.active_elapsed_ms);
        ensure_wall_time(&mut resolved_wall_ms, resolved_active_ms);
        let result = EpisodeAttemptV5 {
            attempt_id: format!("{}:prefix50", attempt_100.attempt_id),
            episode_id: attempt_100.episode_id.clone(),
            repeat_index: attempt_100.repeat_index,
            run_config_sha256: attempt_100.run_config_sha256.clone(),
            attempt_status: attempt_100.attempt_status,
            completed_assistant_generations: prefix_completed_gens,
            terminal_reservation_ordinal: attempt_100.terminal_reservation_ordinal.clone(),
            terminal_cause: attempt_100.terminal_cause.clone(),
            context_limit_cause: attempt_100.context_limit_cause,
            tool_call_count: resolved_tool_calls,
            retry_request_count: prefix_retry_count,
            elapsed_wall_time_ms: resolved_wall_ms,
            terminal_outcome_elapsed_ms: resolved_term_ms,
            active_elapsed_ms: resolved_active_ms,
            provider_requests: prefix_requests,
            g_srr: attempt_100.g_srr,
            sem_srr_commit: attempt_100.sem_srr_commit,
            overgrant_count: attempt_100.overgrant_count,
            avail_breach_count: attempt_100.avail_breach_count,
        };
        result
            .validate()
            .map_err(|_| invalid_prefix("derived prefix attempt is invalid"))?;
        return Ok(result);
    }

    let (matched_trace, non_post_flags) = options.execution.map_or_else(
        || (Vec::new(), Vec::new()),
        |execution| prefix_completion_trace(execution, &prefix_requests),
    );
    let resolved_tool_calls = if let Some(count) = options.prefix_tool_call_count {
        count
    } else if options.execution.is_some() && !matched_trace.is_empty() {
        matched_trace
            .iter()
            .zip(non_post_flags.iter())
            .filter(|(_, is_non_post)| **is_non_post)
            .try_fold(0_i64, |total, (trace, _)| {
                total.checked_add(trace.tool_calls_returned)
            })
            .ok_or_else(|| invalid_prefix("prefix tool-call count is outside supported range"))?
    } else if attempt_100.completed_assistant_generations <= 50 {
        attempt_100.tool_call_count
    } else {
        0
    };

    let (resolved_term_ms, mut resolved_wall_ms, resolved_active_ms) =
        if options.execution.is_some() && !matched_trace.is_empty() {
            let trace_ms = rounded_millis(matched_trace.iter().map(|trace| trace.latency_s).sum())?;
            (
                options
                    .prefix_terminal_outcome_elapsed_ms
                    .or(Some(trace_ms)),
                options.prefix_elapsed_wall_time_ms.or(Some(trace_ms)),
                options.prefix_active_elapsed_ms.or(Some(trace_ms)),
            )
        } else if attempt_100.completed_assistant_generations <= 50 {
            (
                options
                    .prefix_terminal_outcome_elapsed_ms
                    .or(attempt_100.terminal_outcome_elapsed_ms),
                options
                    .prefix_elapsed_wall_time_ms
                    .or(attempt_100.terminal_outcome_elapsed_ms),
                options
                    .prefix_active_elapsed_ms
                    .or(attempt_100.active_elapsed_ms),
            )
        } else {
            (
                options.prefix_terminal_outcome_elapsed_ms,
                options.prefix_elapsed_wall_time_ms,
                options.prefix_active_elapsed_ms,
            )
        };
    ensure_wall_time(&mut resolved_wall_ms, resolved_active_ms);
    ensure_wall_time(&mut resolved_wall_ms, resolved_term_ms);

    let mut resolved_overgrant = options.prefix_overgrant_count;
    let mut resolved_avail = options.prefix_avail_breach_count;
    if resolved_overgrant.is_none() || resolved_avail.is_none() {
        let mut computed_overgrant = 0;
        let mut computed_avail = 0;
        if let Some(execution) = options.execution {
            let p1_commit_in_prefix = snapshot_within_prefix(
                execution.phase1.commit.as_ref(),
                1,
                execution,
                &prefix_requests,
                &ordinal_50,
            );
            let p2_commit_in_prefix = snapshot_within_prefix(
                execution.phase2.commit.as_ref(),
                2,
                execution,
                &prefix_requests,
                &ordinal_50,
            );
            if p1_commit_in_prefix || p2_commit_in_prefix {
                if let (Some(episode), Some(oracle)) = (options.episode, options.oracle) {
                    let mut prefix_execution = execution.clone();
                    prefix_execution.attempt_status = AttemptStatusV5::WithinProtocolStepLimit;
                    prefix_execution.phase1_committed = p1_commit_in_prefix;
                    prefix_execution.phase2_committed = p2_commit_in_prefix;
                    if !p1_commit_in_prefix {
                        prefix_execution.phase1.commit = None;
                    }
                    if !p2_commit_in_prefix {
                        prefix_execution.phase2.commit = None;
                    }
                    let (_, score) = score_execution_v5(
                        &prefix_execution,
                        episode,
                        oracle,
                        ScoreExecutionOptionsV5::default(),
                    )
                    .map_err(|_| invalid_prefix("prefix safety scoring failed"))?;
                    if let Some(count) = score.safety_commit_overgrant_count {
                        computed_overgrant = count;
                    }
                    if let Some(count) = score.safety_commit_availability_breach_count {
                        computed_avail = count;
                    }
                } else if attempt_100.overgrant_count.is_some()
                    && attempt_100.avail_breach_count.is_some()
                    && p1_commit_in_prefix == execution.phase1.commit.is_some()
                    && p2_commit_in_prefix == execution.phase2.commit.is_some()
                {
                    computed_overgrant = attempt_100.overgrant_count.unwrap_or_default();
                    computed_avail = attempt_100.avail_breach_count.unwrap_or_default();
                }
            }
        }
        if resolved_overgrant.is_none() {
            resolved_overgrant = Some(computed_overgrant);
        }
        if resolved_avail.is_none() {
            resolved_avail = Some(computed_avail);
        }
    }

    let result = EpisodeAttemptV5 {
        attempt_id: format!("{}:prefix50", attempt_100.attempt_id),
        episode_id: attempt_100.episode_id.clone(),
        repeat_index: attempt_100.repeat_index,
        run_config_sha256: attempt_100.run_config_sha256.clone(),
        attempt_status: AttemptStatusV5::WithinProtocolStepLimit,
        completed_assistant_generations: prefix_completed_gens,
        terminal_reservation_ordinal: Some(ordinal_50),
        terminal_cause: Some("GENERATION_CAP_EXHAUSTED".to_owned()),
        context_limit_cause: None,
        tool_call_count: resolved_tool_calls,
        retry_request_count: prefix_retry_count,
        elapsed_wall_time_ms: resolved_wall_ms,
        terminal_outcome_elapsed_ms: resolved_term_ms,
        active_elapsed_ms: resolved_active_ms,
        provider_requests: prefix_requests,
        g_srr: Some(false),
        sem_srr_commit: Some(false),
        overgrant_count: resolved_overgrant,
        avail_breach_count: resolved_avail,
    };
    result
        .validate()
        .map_err(|_| invalid_prefix("derived prefix attempt is invalid"))?;
    Ok(result)
}

/// Select the 50 or 100 generation cap from resolved `v5_dev` sensitivity slots.
pub fn select_v5_dev_generation_cap(
    slots: &[DevCapSensitivitySlotV5],
    planned_dev_slots: usize,
    cap_selection_tracks: &[String],
) -> Result<DevCapSelectionResultV5, RunnerError> {
    if cap_selection_tracks.is_empty() {
        return Err(invalid_prefix("cap_selection_tracks must be non-empty"));
    }
    if cap_selection_tracks
        .iter()
        .any(|track| !CAP_SELECTION_TRACKS_V5.contains(&track.as_str()))
    {
        return Err(invalid_prefix(
            "cap_selection_tracks contains an unsupported track",
        ));
    }
    if slots.len() != planned_dev_slots {
        return Err(invalid_prefix(&format!(
            "all planned dev slots ({planned_dev_slots}) must be present; got {}",
            slots.len()
        )));
    }

    let selected_tracks: BTreeSet<&str> = cap_selection_tracks.iter().map(String::as_str).collect();
    let mut triggering = Vec::new();
    for slot in slots {
        slot.attempt_100
            .validate()
            .map_err(|_| invalid_prefix("100-cap attempt is invalid"))?;
        if let Some(attempt_50) = &slot.attempt_50 {
            attempt_50
                .validate()
                .map_err(|_| invalid_prefix("50-cap attempt is invalid"))?;
        }
        if !CAP_SELECTION_TRACKS_V5.contains(&slot.effort_track.as_str()) {
            return Err(invalid_prefix("slot effort_track is unsupported"));
        }
        if slot.attempt_100.attempt_status.is_incomplete() {
            return Err(invalid_prefix(&format!(
                "all planned dev slots must be resolved; slot {:?} is {}",
                slot.attempt_100.attempt_id, slot.attempt_100.attempt_status
            )));
        }
        if !selected_tracks.contains(slot.effort_track.as_str()) {
            continue;
        }
        let prefix_50 = match &slot.attempt_50 {
            Some(attempt) => attempt.clone(),
            None => derive_50_cap_prefix_outcome(&slot.attempt_100)?,
        };
        if prefix_50.attempt_status == AttemptStatusV5::WithinProtocolStepLimit
            && slot.attempt_100.attempt_status == AttemptStatusV5::Completed
            && slot.attempt_100.g_srr == Some(true)
        {
            triggering.push(slot.attempt_100.attempt_id.clone());
        }
    }

    Ok(DevCapSelectionResultV5 {
        selected_cap: if triggering.is_empty() { 50 } else { 100 },
        cap_selection_tracks: cap_selection_tracks.to_vec(),
        planned_dev_slots,
        resolved_dev_slots: slots.len(),
        triggering_attempt_ids: triggering,
    })
}

/// Select the cap using the default primary-highest comparison track.
pub fn select_v5_dev_generation_cap_default(
    slots: &[DevCapSensitivitySlotV5],
    planned_dev_slots: usize,
) -> Result<DevCapSelectionResultV5, RunnerError> {
    let tracks = DEFAULT_CAP_SELECTION_TRACKS_V5
        .iter()
        .map(|track| (*track).to_owned())
        .collect::<Vec<_>>();
    select_v5_dev_generation_cap(slots, planned_dev_slots, &tracks)
}

fn prefix_completion_trace(
    execution: &EpisodeExecutionV5,
    prefix_requests: &[ProviderRequestUsageV5],
) -> (Vec<CompletionTraceV5>, Vec<bool>) {
    let mut target_keys: Vec<(i64, String, bool, bool)> = Vec::new();
    let mut seen_logical = BTreeSet::new();
    for request in sorted_requests(prefix_requests) {
        if is_terminal_completed(request) {
            if seen_logical.insert(request.logical_request_id.as_str()) {
                target_keys.push((
                    request.dispatch_round,
                    request.role.clone(),
                    false,
                    !request.post_terminal,
                ));
            }
        } else if matches!(
            request.request_status,
            ProviderRequestStatusV5::NontransientFailure
                | ProviderRequestStatusV5::QuotaInterruption
                | ProviderRequestStatusV5::AmbiguousFailure
                | ProviderRequestStatusV5::Cancelled
        ) && request.normalized_decode_status != NormalizedDecodeStatusV5::NoResponse
            && seen_logical.insert(request.logical_request_id.as_str())
        {
            target_keys.push((
                request.dispatch_round,
                request.role.clone(),
                true,
                !request.post_terminal,
            ));
        }
    }

    let mut matched = Vec::new();
    let mut non_post_flags = Vec::new();
    let mut next_trace_index = 0;
    for (round, role, response_error, non_post) in target_keys {
        while let Some(trace) = execution.completion_trace.get(next_trace_index) {
            next_trace_index += 1;
            if trace.turn_index == round
                && trace.role == role
                && trace.response_error == response_error
            {
                matched.push(trace.clone());
                non_post_flags.push(non_post);
                break;
            }
        }
    }
    (matched, non_post_flags)
}

fn snapshot_within_prefix(
    snapshot: Option<&TrajectorySnapshotV5>,
    phase_index: i64,
    execution: &EpisodeExecutionV5,
    prefix_requests: &[ProviderRequestUsageV5],
    ordinal_50: &(i64, i64, i64, String, i64),
) -> bool {
    let Some(snapshot) = snapshot else {
        return false;
    };
    for entry in &execution.tool_trace {
        let phase = entry
            .arguments
            .get("phase_index")
            .and_then(serde_json::Value::as_i64);
        let status = entry
            .result
            .get("status")
            .and_then(serde_json::Value::as_str);
        if entry.tool_name == "commit_phase_mutations"
            && phase == Some(phase_index)
            && matches!(
                status,
                Some("COMMITTED_AND_ADVANCED" | "EPISODE_COMPLETED" | "OK")
            )
        {
            if entry.turn_index < ordinal_50.0 {
                return true;
            }
            if entry.turn_index > ordinal_50.0 {
                return false;
            }
            return prefix_requests.iter().any(|request| {
                !request.post_terminal && request.dispatch_round == entry.turn_index
            });
        }
    }
    if prefix_requests
        .iter()
        .any(|request| request.input_tokens_total.is_some())
    {
        let mut seen_logical = BTreeSet::new();
        let mut prefix_input = 0_i64;
        let mut prefix_output = 0_i64;
        for request in sorted_requests(prefix_requests) {
            if is_terminal_completed(request)
                && seen_logical.insert(request.logical_request_id.as_str())
            {
                prefix_input =
                    prefix_input.saturating_add(request.input_tokens_total.unwrap_or_default());
                prefix_output =
                    prefix_output.saturating_add(request.output_tokens_total.unwrap_or_default());
            }
        }
        snapshot.token_totals.input_tokens_actual() <= prefix_input
            && snapshot.token_totals.output_tokens_actual() <= prefix_output
    } else {
        false
    }
}

fn sorted_requests(requests: &[ProviderRequestUsageV5]) -> Vec<&ProviderRequestUsageV5> {
    let mut ordered: Vec<_> = requests.iter().collect();
    ordered.sort_by(|left, right| left.reservation_ordinal.cmp(&right.reservation_ordinal));
    ordered
}

fn is_terminal_completed(request: &ProviderRequestUsageV5) -> bool {
    request.request_status == ProviderRequestStatusV5::Completed
        && matches!(
            request.normalized_decode_status,
            NormalizedDecodeStatusV5::TerminalNormal
                | NormalizedDecodeStatusV5::TerminalRefusal
                | NormalizedDecodeStatusV5::TerminalOutputLimit
        )
}

fn ensure_wall_time(wall: &mut Option<i64>, other: Option<i64>) {
    if let (Some(wall_value), Some(other_value)) = (*wall, other)
        && other_value > wall_value
    {
        *wall = Some(other_value);
    }
}

fn rounded_millis(seconds: f64) -> Result<i64, RunnerError> {
    let millis = seconds * 1_000.0;
    if !millis.is_finite() || millis < 0.0 || millis > i64::MAX as f64 {
        return Err(invalid_prefix(
            "prefix trace latency is outside supported range",
        ));
    }
    Ok(millis.round_ties_even() as i64)
}

fn invalid_prefix(message: &str) -> RunnerError {
    RunnerError::InvalidInput(message.to_owned())
}
