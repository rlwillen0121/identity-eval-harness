#![recursion_limit = "256"]

use ib_v5_schemas::{
    AccountReconMutationV5, AttemptStatusV5, CANONICAL_ATTEMPT_STATUSES_V5,
    COMPACT_TABLE_COLUMNS_V5, CoSignReceiptStatusV5, CoSignReceiptV5, CoSignScopeKindV5,
    CompactTableV5, ConnectorPageV5, DescriptiveUsageSummaryV5, EngagementStatusV5,
    EngagementV5, EpisodeAttemptV5, EpisodeV5, GrantModeV5, GrantV5, HRPersonV5,
    HistoricalComparatorEntryV5, HistoricalComparatorManifestV5, ProviderRequestUsageV5,
    RunConfigurationV5, ScopeCatalogV5, StageReceiptStatusV5, StageReceiptV5, StrictV5Model,
    TABLE_PRIMARY_KEY_V5, TableRowV5, TerminalWorldStateV5, UARCampaignMutationV5,
    V5_APPROVED_ADDENDUM_PATH, V5_APPROVED_ADDENDUM_SHA256, V5_APPROVED_CONFORMANCE_CONTRACT_ID,
    V5_APPROVED_DIRECTIVE_PATH, V5_APPROVED_DIRECTIVE_SHA256, V5_BASE_SPEC_PATH,
    V5_BASE_SPEC_SHA256, V5_DRAFT5_SPEC_PATH, V5_DRAFT5_SPEC_SHA256,
    V5_PROMOTED_DRAFT5_CONTRACT_ID, approved_v5_normative_contract, compact_table_columns,
    decode_compact_table_v5, encode_compact_table_v5, promoted_v5_normative_contract,
    AffiliationKindV5, ETAG_HEX_RE, STAGE_ID_RE,
};
use serde_json::{Value, json};

fn sample_person(person_id: &str) -> HRPersonV5 {
    HRPersonV5 {
        person_id: person_id.to_owned(),
        emp_immutable_id: "emp_001".to_owned(),
        primary_email: "person@example.com".to_owned(),
        historical_emails: vec![],
        engagements: vec![EngagementV5 {
            eng_id: "eng_001".to_owned(),
            affiliation: AffiliationKindV5::Fte,
            eng_status: EngagementStatusV5::Active,
            dept_path: "/Engineering".to_owned(),
            prior_dept_path: None,
            dept_transfer_effective_at_s: None,
            manager_person_id: None,
            start_s: 1,
            end_s: None,
            conversion_start_s: None,
            conversion_grace_deadline_s: None,
            retro_terminated_effective_s: None,
        }],
        lifecycle_gen: 1,
        etag: "0123456789abcdef".to_owned(),
        evidence_id: "ev_person_001".to_owned(),
    }
}

#[test]
fn schema_inventory_matches_v5_contract() {
    assert_eq!(COMPACT_TABLE_COLUMNS_V5.len(), 23);
    assert_eq!(TABLE_PRIMARY_KEY_V5.len(), 23);
    let col_names: Vec<&str> = COMPACT_TABLE_COLUMNS_V5.iter().map(|(k, _)| *k).collect();
    let pk_names: Vec<&str> = TABLE_PRIMARY_KEY_V5.iter().map(|(k, _)| *k).collect();
    assert_eq!(col_names, pk_names);
    assert_eq!(CANONICAL_ATTEMPT_STATUSES_V5.len(), 10);
}

#[test]
fn strict_models_reject_extra_fields_and_wrong_json_types() {
    assert!(
        ScopeCatalogV5::from_strict_json_str(
            r#"{"leaf_scopes":["/engineering"],"unexpected":"value"}"#
        )
        .is_err()
    );
    assert!(ScopeCatalogV5::from_strict_json_str(r#"{"leaf_scopes":"/engineering"}"#).is_err());
    assert!(ScopeCatalogV5::from_strict_json_str(r#"{"leaf_scopes":[]}"#).is_err());

    // Strict int/bool rejection: 1.0 is not an int, true is not an int, 1 is not a bool
    assert!(
        EngagementV5::from_strict_json_value(&json!({
            "eng_id": "eng_001",
            "affiliation": "FTE",
            "eng_status": "ACTIVE",
            "dept_path": "/Engineering",
            "start_s": 1.0
        }))
        .is_err()
    );
    assert!(
        EngagementV5::from_strict_json_value(&json!({
            "eng_id": "eng_001",
            "affiliation": "FTE",
            "eng_status": "ACTIVE",
            "dept_path": "/Engineering",
            "start_s": true
        }))
        .is_err()
    );
    assert!(
        EngagementV5::from_strict_json_value(&json!({
            "eng_id": "eng_001",
            "affiliation": "FTE",
            "eng_status": "ACTIVE",
            "dept_path": "/Engineering",
            "start_s": "1"
        }))
        .is_err()
    );
    assert!(
        EpisodeV5::from_strict_json_value(&json!({"gold_p1_mutations": []})).is_err()
    );
}

#[test]
fn identifier_and_stage_patterns_use_fullmatch() {
    assert!(STAGE_ID_RE.is_match("stg_repair_1"));
    assert!(!STAGE_ID_RE.is_match("stg_repair_1\n"));
    assert!(ETAG_HEX_RE.is_match("0123456789abcdef"));
    assert!(!ETAG_HEX_RE.is_match("0123456789abcdef\n"));

    assert!(sample_person("pers_001\n").validate().is_err());

    let receipt = StageReceiptV5 {
        status: StageReceiptStatusV5::Staged,
        phase_index: 1,
        stage_id: "stg_repair_1\n".to_owned(),
        mutation_count: 1,
        stage_digest: "a".repeat(64),
        dependency_digest: "b".repeat(64),
        error_detail: None,
    };
    receipt.validate().expect("receipt echoes strict strings");
    assert_eq!(receipt.stage_id, "stg_repair_1\n");

    let rejected_cosign = CoSignReceiptV5 {
        status: CoSignReceiptStatusV5::RejectedStructural,
        phase_index: 1,
        stage_id: "bad handle".to_owned(),
        scope_kind: CoSignScopeKindV5::UarCustodian,
        target_entity_id: "invalid-target".to_owned(),
        actor_account_id: "invalid-actor".to_owned(),
    };
    rejected_cosign.validate().expect("cosign preserves invalid handles");
}

#[test]
fn normative_synthetic_custodian_grant_id_is_narrow_and_fullmatched() {
    let custodian_id = "cust__camp_campaign_01__acct_002";
    let custodian_grant = GrantV5 {
        grant_id: custodian_id.to_owned(),
        account_id: "acct_002".to_owned(),
        role_id: "role_admin".to_owned(),
        grant_mode: GrantModeV5::Standing,
        scope_path: "/finance".to_owned(),
        sod_prune_priority: 1,
        evidence_id: "ev_campaign_01".to_owned(),
    };
    custodian_grant.validate().expect("valid custodian grant");

    let mut bad1 = custodian_grant.clone();
    bad1.grant_id = "cust__not_a_campaign__acct_002".to_owned();
    assert!(bad1.validate().is_err());

    let mut bad2 = custodian_grant.clone();
    bad2.grant_id = format!("{custodian_id}\n");
    assert!(bad2.validate().is_err());

    let terminal = TerminalWorldStateV5 {
        world_rev: 2,
        terminal_standing_grants_by_account: vec![(
            "acct_002".to_owned(),
            vec![custodian_id.to_owned()],
        )],
        terminal_eligible_grants_by_account: vec![(
            "acct_002".to_owned(),
            vec!["grant_003".to_owned()],
        )],
        materialized_session_ids: vec![],
        consumed_ticket_ids: vec![],
        committed_p1_digest: "a".repeat(64),
        committed_p2_digest: "b".repeat(64),
    };
    terminal.validate().expect("valid terminal world state");

    let mut bad_term1 = terminal.clone();
    bad_term1.terminal_standing_grants_by_account =
        vec![("acct_002".to_owned(), vec!["cust__bad".to_owned()])];
    assert!(bad_term1.validate().is_err());

    let mut bad_term2 = terminal;
    bad_term2.terminal_eligible_grants_by_account =
        vec![("acct_002".to_owned(), vec![custodian_id.to_owned()])];
    assert!(bad_term2.validate().is_err());
}

#[test]
fn mutation_tokens_and_nested_tuple_ids_validated() {
    let valid = AccountReconMutationV5::from_strict_json_value(&json!({
        "mutation_kind": "ACCOUNT_RECON_MUTATION",
        "target_entity_id": "acct_001",
        "if_match": ["acct_001@0123456789abcdef:1"],
        "cite": ["BIND-V5-01a:ev_person_001"],
        "account_action": "KEEP_ACTIVE",
        "bound_person_id": "pers_001",
        "bound_engagement_id": "eng_001",
        "retained_grant_ids": []
    }))
    .expect("valid account recon mutation");
    assert_eq!(valid.target_entity_id, "acct_001");

    assert!(
        AccountReconMutationV5::from_strict_json_value(&json!({
            "mutation_kind": "ACCOUNT_RECON_MUTATION",
            "target_entity_id": "acct_001",
            "if_match": ["acct_001@0123456789abcdef:1\n"],
            "cite": ["BIND-V5-01a:ev_person_001"],
            "account_action": "KEEP_ACTIVE",
            "bound_person_id": "pers_001",
            "bound_engagement_id": "eng_001"
        }))
        .is_err()
    );

    assert!(
        UARCampaignMutationV5::from_strict_json_value(&json!({
            "mutation_kind": "UAR_CAMPAIGN_MUTATION",
            "target_entity_id": "camp_001",
            "if_match": ["camp_001@0123456789abcdef:1"],
            "cite": ["UAR-V5-01:ev_campaign_001"],
            "uar_decisions": [["item_001\n", "CERTIFY"]]
        }))
        .is_err()
    );
}

#[test]
fn wire_cells_reject_non_finite_numbers() {
    let overflow_num: Value = serde_json::from_str("1e400").expect("overflow float parses as Number");
    let page = ConnectorPageV5 {
        page_id: "page_001".to_owned(),
        connector_id: "conn_erp".to_owned(),
        cursor_in: "cursor_0".to_owned(),
        attempt_index: 0,
        http_status: 200,
        next_cursor: None,
        base_checkpoint_etag: None,
        records: vec![vec![("raw_entitlements".to_owned(), overflow_num.clone())]],
        evidence_id: "ev_page_001".to_owned(),
    };
    assert!(page.validate().is_err());

    let table = CompactTableV5 {
        table_name: "connector_pages".to_owned(),
        columns: compact_table_columns("connector_pages")
            .unwrap()
            .iter()
            .map(|s| (*s).to_owned())
            .collect(),
        rows: vec![vec![
            json!("page_001"),
            json!("conn_erp"),
            json!("cursor_0"),
            json!(0),
            json!(200),
            Value::Null,
            Value::Null,
            json!([[["raw_entitlements", overflow_num]]]),
            json!("ev_page_001"),
        ]],
    };
    assert!(table.validate().is_err());
}

#[test]
fn compact_table_roundtrips_and_checks_primary_keys() {
    let person = sample_person("pers_001");
    let table = encode_compact_table_v5("hr_persons", &[TableRowV5::HrPerson(person.clone())])
        .expect("encode hr_persons");
    let decoded = decode_compact_table_v5(&table).expect("decode hr_persons");
    assert_eq!(decoded, vec![TableRowV5::HrPerson(person)]);

    let wire = table.to_jcs_string().expect("jcs");
    let restored = CompactTableV5::from_strict_json_str(&wire).expect("from_strict_json_str");
    assert_eq!(restored, table);

    let mut dup_table = table.clone();
    dup_table.rows.push(table.rows[0].clone());
    assert!(dup_table.validate().is_err());

    let mut bad_pk_table = table;
    bad_pk_table.rows[0][0] = json!("pers_001\n");
    assert!(bad_pk_table.validate().is_err());
}

fn valid_run_config_json() -> Value {
    let hex64 = "a".repeat(64);
    json!({
        "model_id": "model-primary",
        "provider_id": "provider-a",
        "provider_model_version": "2026-11",
        "topology": "T0_SINGLE_MONOLITH",
        "steering_mode": "UNSTEERED",
        "coupling_mode": "COUPLED",
        "effort_track": "PRIMARY_HIGHEST",
        "native_effort_policy": "FIXED_NATIVE_SETTING",
        "reasoning_effort_label": "high",
        "native_effort_config_json": r#"{"effort":"high"}"#,
        "native_numeric_budget_cap_tokens": null,
        "native_numeric_budget_min_tokens": null,
        "native_visible_output_reserve_tokens": null,
        "supported_reasoning_effort_labels_json": r#"["low","medium","high"]"#,
        "max_assistant_generations_per_episode": 50,
        "effort_order_source": "provider-docs-v1",
        "effort_minimum_output_tokens": 4096,
        "reasoning_retention_policy_json": r#"{"mode":"opaque"}"#,
        "usage_accounting_policy_json": r#"{"inclusive_output":true}"#,
        "transport_api_id": "responses",
        "transport_api_version": "v1",
        "transport_adapter_version": "1.0.0",
        "transport_streaming": true,
        "transport_max_logical_output_tokens": 32768,
        "provider_segment_output_limit_tokens": 16384,
        "segment_continuation_capability": "SUPPORTED",
        "decode_segmentation_policy_json": r#"{"mode":"cursor"}"#,
        "tokenizer_id": "tok-frozen",
        "tokenizer_version": "1.0",
        "context_window_tokens": 200000,
        "provider_schema_overhead_tokens": 512,
        "provider_max_output_tokens": 32768,
        "requested_output_ceiling_tokens": 32768,
        "base_effective_output_ceiling_tokens": 32768,
        "minimum_context_headroom_tokens": 1024,
        "headroom_algorithm_id": "det-v1",
        "sampling_config_sha256": hex64,
        "toolset_sha256": hex64,
        "retry_policy_id": "retry-2-backoff",
        "request_client_timeout_s": 300,
        "recovery_policy_json": r#"{"exact_state":true}"#,
        "cap_disclosed": false,
        "prompt_sha256": hex64,
        "primary_prompt_bundle_sha256": hex64,
        "adapter_sha256": hex64,
        "tool_decoding_policy_json": r#"{"strict":true}"#,
        "harness_sha256": hex64,
        "dataset_seal_sha256": hex64,
        "topology_profile_sha256": hex64,
        "topology_role_priority_json": r#"{"primary":0}"#,
        "repeat_seed_schedule_json": "[101,102,103]",
        "repeat_execution_order_json": "[1,2,3]",
        "run_config_sha256": hex64
    })
}

#[test]
fn run_configuration_v5_validators_and_segment_helpers() {
    let cfg = RunConfigurationV5::from_strict_json_value(&valid_run_config_json()).expect("valid");
    assert!(cfg.requires_supported_segment_continuation());
    assert!(cfg.is_segment_continuation_eligible());

    let mut unsupported = valid_run_config_json();
    unsupported["segment_continuation_capability"] = json!("UNSUPPORTED");
    let unsupported = RunConfigurationV5::from_strict_json_value(&unsupported).unwrap();
    assert!(unsupported.requires_supported_segment_continuation());
    assert!(!unsupported.is_segment_continuation_eligible());

    let mut bad_ceiling = valid_run_config_json();
    bad_ceiling["requested_output_ceiling_tokens"] = json!(16384);
    assert!(RunConfigurationV5::from_strict_json_value(&bad_ceiling).is_err());

    let mut bad_primary = valid_run_config_json();
    bad_primary["topology"] = json!("T5_HIERARCHICAL_PIPELINE");
    assert!(RunConfigurationV5::from_strict_json_value(&bad_primary).is_err());

    let mut bad_sha = valid_run_config_json();
    bad_sha["run_config_sha256"] = json!("not-a-sha256");
    assert!(RunConfigurationV5::from_strict_json_value(&bad_sha).is_err());
}

fn valid_request_usage_json() -> Value {
    json!({
        "request_id": "req_phys_001",
        "logical_request_id": "lreq_001",
        "request_kind": "INITIAL",
        "retry_index": 0,
        "quota_continuation_index": null,
        "recovery_continuation_index": null,
        "segment_continuation_index": null,
        "decode_segment_index": 0,
        "attempt_id": "att_001",
        "agent_id": "primary",
        "role": "primary",
        "dispatch_round": 0,
        "reservation_ordinal": [0, 0, 0, "primary", 0],
        "request_status": "COMPLETED",
        "post_terminal": false,
        "provider_stop_reason": "stop",
        "provider_http_status": 200,
        "provider_error_code": null,
        "provider_decode_status_raw": "completed",
        "normalized_decode_status": "TERMINAL_NORMAL",
        "provider_refusal_signal": false,
        "actual_native_numeric_budget_tokens": null,
        "native_generation_parameters_json": r#"{"effort":"high"}"#,
        "rendered_input_context_tokens": 1200,
        "effective_output_allowance_tokens": 32768,
        "input_tokens_total": 1200,
        "cached_input_tokens": 300,
        "output_tokens_total": 500,
        "reasoning_tokens_subset": 200,
        "tool_serialization_tokens_subset": 100,
        "provider_reported_cost_micros": 1234,
        "estimated_cost_micros": 1200,
        "cost_basis": "PROVIDER_BILLED",
        "pricing_manifest_sha256": "b".repeat(64),
        "raw_usage_fields_json": r#"{"input_tokens":1200}"#,
        "usage_scope": "PHYSICAL_REQUEST",
        "currency_code": "USD"
    })
}

#[test]
fn provider_request_usage_and_episode_attempt_and_summary_validators() {
    let req = ProviderRequestUsageV5::from_strict_json_value(&valid_request_usage_json())
        .expect("valid request usage");
    assert_eq!(req.agent_id, "primary");

    let mut bad_cached = valid_request_usage_json();
    bad_cached["input_tokens_total"] = json!(100);
    bad_cached["cached_input_tokens"] = json!(101);
    assert!(ProviderRequestUsageV5::from_strict_json_value(&bad_cached).is_err());

    let mut bad_agent = valid_request_usage_json();
    bad_agent["agent_id"] = json!("bad agent\n");
    assert!(ProviderRequestUsageV5::from_strict_json_value(&bad_agent).is_err());

    let attempt = EpisodeAttemptV5::from_strict_json_value(&json!({
        "attempt_id": "att_001",
        "episode_id": "v5_dev_ep_001",
        "repeat_index": 1,
        "run_config_sha256": "c".repeat(64),
        "attempt_status": "COMPLETED",
        "completed_assistant_generations": 5,
        "terminal_reservation_ordinal": [4, 0, 0, "primary", 0],
        "terminal_cause": "EPISODE_COMPLETED",
        "context_limit_cause": null,
        "tool_call_count": 6,
        "retry_request_count": 0,
        "elapsed_wall_time_ms": 1500,
        "terminal_outcome_elapsed_ms": 1400,
        "active_elapsed_ms": 1200,
        "provider_requests": [valid_request_usage_json()],
        "g_srr": true,
        "sem_srr_commit": true,
        "overgrant_count": 0,
        "avail_breach_count": 0
    }))
    .expect("valid completed attempt");
    assert_eq!(attempt.attempt_status, AttemptStatusV5::Completed);

    let mut bad_incomplete = attempt.clone();
    bad_incomplete.attempt_status = AttemptStatusV5::IncompleteProviderFailure;
    assert!(bad_incomplete.validate().is_err());

    let counts = json!({
        "COMPLETED": 10,
        "WITHIN_PROTOCOL_REFUSAL": 1,
        "WITHIN_PROTOCOL_STEP_LIMIT": 1,
        "INCOMPLETE_PROVIDER_QUOTA": 2,
        "INCOMPLETE_PROVIDER_FAILURE": 1,
        "INCOMPLETE_CONTEXT_INCOMPATIBLE": 0,
        "INCOMPLETE_HARNESS_FAILURE": 1
    });
    let summary = DescriptiveUsageSummaryV5::from_strict_json_value(&json!({
        "run_config_sha256": "d".repeat(64),
        "planned_attempt_slots": 16,
        "completed_attempts": 10,
        "incomplete_provider_quota_attempts": 2,
        "incomplete_provider_failure_attempts": 1,
        "incomplete_context_attempts": 0,
        "incomplete_harness_attempts": 1,
        "attempt_status_counts_json": counts.to_string(),
        "elapsed_wall_time_ms": 50000,
        "active_elapsed_ms": 42000,
        "total_input_tokens": 10000,
        "cached_input_tokens": 2000,
        "total_output_tokens": 4000,
        "reasoning_tokens_subset": 1500,
        "tool_serialization_tokens_subset": 500,
        "provider_billed_cost_micros": 9000,
        "estimated_cost_micros": 8800,
        "currency_code": "USD",
        "accounting_coverage_status": "COMPLETE"
    }))
    .expect("valid summary");
    assert_eq!(summary.completed_attempts, 10);

    let mut bool_counts = summary.clone();
    bool_counts.attempt_status_counts_json = r#"{"COMPLETED":true}"#.to_owned();
    assert!(bool_counts.validate().is_err());

    let mut float_counts = summary;
    float_counts.attempt_status_counts_json = r#"{"COMPLETED":10.0}"#.to_owned();
    assert!(float_counts.validate().is_err());
}

#[test]
fn historical_comparator_and_normative_contract_bindings() {
    let entry: HistoricalComparatorEntryV5 = HistoricalComparatorEntryV5::from_strict_json_value(&json!({
        "model_id": "model-a",
        "provider_id": "provider-a",
        "reasoning_effort_label": "high",
        "topology": "T0_SINGLE_MONOLITH",
        "v41_completed_scenarios": 180,
        "execution_valid_rate_bps": 10000,
        "v41_run_manifest_sha256": "e".repeat(64),
        "input_tokens_total": 1000,
        "cached_input_tokens": 200,
        "output_tokens_total": 500,
        "reasoning_tokens_subset": 150,
        "historical_cost_micros": 4500,
        "historical_cost_basis": "FROZEN_RATE_ESTIMATE"
    }))
    .unwrap();
    let manifest = HistoricalComparatorManifestV5 {
        source_spec_sha256: "f".repeat(64),
        pricing_manifest_sha256: None,
        entries: vec![entry.clone()],
    };
    manifest.validate().unwrap();
    let dup = HistoricalComparatorManifestV5 {
        source_spec_sha256: "f".repeat(64),
        pricing_manifest_sha256: Some("0".repeat(64)),
        entries: vec![entry.clone(), entry],
    };
    assert!(dup.validate().is_err());

    assert_eq!(
        approved_v5_normative_contract(),
        json!({
            "contract_id": V5_APPROVED_CONFORMANCE_CONTRACT_ID,
            "base_spec": {"path": V5_BASE_SPEC_PATH, "sha256": V5_BASE_SPEC_SHA256},
            "approved_addendum": {"path": V5_APPROVED_ADDENDUM_PATH, "sha256": V5_APPROVED_ADDENDUM_SHA256},
            "approved_directive": {"path": V5_APPROVED_DIRECTIVE_PATH, "sha256": V5_APPROVED_DIRECTIVE_SHA256},
        })
    );
    assert_eq!(
        promoted_v5_normative_contract(),
        json!({
            "contract_id": V5_PROMOTED_DRAFT5_CONTRACT_ID,
            "base_spec": {"path": V5_BASE_SPEC_PATH, "sha256": V5_BASE_SPEC_SHA256},
            "protocol_revision_spec": {"path": V5_DRAFT5_SPEC_PATH, "sha256": V5_DRAFT5_SPEC_SHA256},
            "approved_addendum": {"path": V5_APPROVED_ADDENDUM_PATH, "sha256": V5_APPROVED_ADDENDUM_SHA256},
            "approved_directive": {"path": V5_APPROVED_DIRECTIVE_PATH, "sha256": V5_APPROVED_DIRECTIVE_SHA256},
        })
    );
}
