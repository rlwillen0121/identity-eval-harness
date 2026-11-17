use ib_v5_accounting::EpisodeAttemptV5;
use ib_v5_runner::{
    DevCapSensitivitySlotV5, derive_50_cap_prefix_outcome, select_v5_dev_generation_cap_default,
};
use ib_v5_schemas::{
    AttemptStatusV5, CostBasisV5, NormalizedDecodeStatusV5, ProviderRequestKindV5,
    ProviderRequestStatusV5, ProviderRequestUsageV5, StrictV5Model, UsageScopeV5,
};

const RUN_SHA: &str = "1111111111111111111111111111111111111111111111111111111111111111";

fn request(index: i64) -> ProviderRequestUsageV5 {
    ProviderRequestUsageV5 {
        request_id: format!("req_{index}"),
        logical_request_id: format!("lreq_{index}"),
        request_kind: if index % 10 == 0 {
            ProviderRequestKindV5::TransientRetry
        } else {
            ProviderRequestKindV5::Initial
        },
        retry_index: if index % 10 == 0 { Some(1) } else { Some(0) },
        quota_continuation_index: None,
        recovery_continuation_index: None,
        segment_continuation_index: None,
        decode_segment_index: 0,
        attempt_id: "att_ep_1".to_owned(),
        agent_id: "unified".to_owned(),
        role: "unified".to_owned(),
        dispatch_round: index,
        reservation_ordinal: (1, index, 0, "unified".to_owned(), 0),
        request_status: ProviderRequestStatusV5::Completed,
        post_terminal: false,
        provider_stop_reason: None,
        provider_http_status: None,
        provider_error_code: None,
        provider_decode_status_raw: None,
        normalized_decode_status: NormalizedDecodeStatusV5::TerminalNormal,
        provider_refusal_signal: None,
        actual_native_numeric_budget_tokens: None,
        native_generation_parameters_json: None,
        rendered_input_context_tokens: None,
        effective_output_allowance_tokens: None,
        input_tokens_total: None,
        cached_input_tokens: None,
        output_tokens_total: None,
        reasoning_tokens_subset: None,
        tool_serialization_tokens_subset: None,
        provider_reported_cost_micros: None,
        estimated_cost_micros: None,
        cost_basis: CostBasisV5::Unavailable,
        pricing_manifest_sha256: None,
        raw_usage_fields_json: None,
        usage_scope: UsageScopeV5::Unavailable,
        currency_code: None,
    }
}

fn completed_attempt(id: &str, generations: i64, g_srr: bool) -> EpisodeAttemptV5 {
    let provider_requests: Vec<_> = (1..=generations).map(request).collect();
    EpisodeAttemptV5 {
        attempt_id: id.to_owned(),
        episode_id: "ep_1".to_owned(),
        repeat_index: 1,
        run_config_sha256: RUN_SHA.to_owned(),
        attempt_status: AttemptStatusV5::Completed,
        completed_assistant_generations: generations,
        terminal_reservation_ordinal: provider_requests
            .last()
            .map(|request| request.reservation_ordinal.clone()),
        terminal_cause: Some("EPISODE_COMPLETED".to_owned()),
        context_limit_cause: None,
        tool_call_count: 75,
        retry_request_count: 0,
        elapsed_wall_time_ms: Some(1_000),
        terminal_outcome_elapsed_ms: Some(900),
        active_elapsed_ms: Some(700),
        provider_requests,
        g_srr: Some(g_srr),
        sem_srr_commit: Some(g_srr),
        overgrant_count: Some(0),
        avail_breach_count: Some(0),
    }
}

#[test]
fn derives_exact_first_50_completed_ordinals_and_step_limit_metrics() {
    let attempt = completed_attempt("att_100", 100, true);
    attempt.validate().unwrap();
    let prefix = derive_50_cap_prefix_outcome(&attempt).unwrap();
    assert_eq!(prefix.attempt_id, "att_100:prefix50");
    assert_eq!(
        prefix.attempt_status,
        AttemptStatusV5::WithinProtocolStepLimit
    );
    assert_eq!(prefix.completed_assistant_generations, 50);
    assert_eq!(
        prefix.terminal_reservation_ordinal,
        attempt
            .provider_requests
            .get(49)
            .map(|r| r.reservation_ordinal.clone())
    );
    assert_eq!(
        prefix.terminal_cause.as_deref(),
        Some("GENERATION_CAP_EXHAUSTED")
    );
    assert_eq!(prefix.provider_requests.len(), 50);
    assert_eq!(prefix.retry_request_count, 5);
    assert_eq!(prefix.tool_call_count, 0);
    assert_eq!(prefix.g_srr, Some(false));
    assert_eq!(prefix.sem_srr_commit, Some(false));
    prefix.validate().unwrap();
}

#[test]
fn preserves_attempt_that_already_terminated_within_the_prefix() {
    let mut attempt = completed_attempt("att_50", 50, false);
    attempt.attempt_status = AttemptStatusV5::WithinProtocolStepLimit;
    attempt.g_srr = Some(false);
    attempt.sem_srr_commit = Some(false);
    attempt.terminal_cause = Some("GENERATION_CAP_EXHAUSTED".to_owned());
    attempt.validate().unwrap();
    assert_eq!(derive_50_cap_prefix_outcome(&attempt).unwrap(), attempt);
}

#[test]
fn cap_selection_uses_a_resolved_successful_100_cap_prefix_trigger() {
    let trigger = DevCapSensitivitySlotV5 {
        attempt_100: completed_attempt("att_trigger", 100, true),
        effort_track: "PRIMARY_HIGHEST".to_owned(),
        attempt_50: None,
    };
    let result = select_v5_dev_generation_cap_default(&[trigger], 1).unwrap();
    assert_eq!(result.selected_cap, 100);
    assert_eq!(result.triggering_attempt_ids, ["att_trigger"]);

    let no_trigger = DevCapSensitivitySlotV5 {
        attempt_100: completed_attempt("att_no_trigger", 100, false),
        effort_track: "PRIMARY_HIGHEST".to_owned(),
        attempt_50: None,
    };
    let result = select_v5_dev_generation_cap_default(&[no_trigger], 1).unwrap();
    assert_eq!(result.selected_cap, 50);
    assert!(result.triggering_attempt_ids.is_empty());
}

#[test]
fn cap_selection_rejects_missing_or_unresolved_dev_slots() {
    let missing = select_v5_dev_generation_cap_default(&[], 1).unwrap_err();
    assert!(missing.to_string().contains("all planned dev slots"));

    let mut incomplete = completed_attempt("att_incomplete", 60, false);
    incomplete.attempt_status = AttemptStatusV5::IncompleteProviderFailure;
    incomplete.g_srr = None;
    incomplete.sem_srr_commit = None;
    incomplete.overgrant_count = None;
    incomplete.avail_breach_count = None;
    incomplete.validate().unwrap();
    let slot = DevCapSensitivitySlotV5 {
        attempt_100: incomplete,
        effort_track: "PRIMARY_HIGHEST".to_owned(),
        attempt_50: None,
    };
    let unresolved = select_v5_dev_generation_cap_default(&[slot], 1).unwrap_err();
    assert!(
        unresolved
            .to_string()
            .contains("all planned dev slots must be resolved")
    );
}
