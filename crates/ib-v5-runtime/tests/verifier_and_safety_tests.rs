use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use ib_v5_runtime::{
    DiagnosticServiceV5, EvaluatePhaseOptions, FailingPredicate, GoldTraceEntryV5, OptFeasibility,
    SemanticDiagnosticService, Stage5Context, TargetKey, ViewV5, evaluate_phase,
    first_stage5_category, safety_partition, semantically_equal, violation_category_as_str,
};
use ib_v5_schemas::{
    AccountReconActionV5, AccountReconMutationKind, AccountReconMutationV5,
    AgentDelegationDecisionKindV5, AgentDelegationMutationKind, AgentDelegationMutationV5,
    AgentRegistryActionV5, AgentRegistryMutationKind, AgentRegistryMutationV5, AtomicMutationV5,
    CAEContainmentMutationKind, CAEContainmentMutationV5, CIEMEscalationVectorV5,
    CIEMPrincipalFindingV5, CIEMRemediationActionV5, CloudCIEMMutationKind, CloudCIEMMutationV5,
    ConnectorHaltReasonV5, ConnectorSyncMutationKind, ConnectorSyncMutationV5,
    ConnectorUnmappedPolicyDefaultV5, DisconnectedTargetPolicyV5, EngagementStatusV5,
    HRSourceMappingMutationKind, HRSourceMappingMutationV5, ITSMTicketActionKindV5,
    ITSMTicketActionV5, JITDecisionKindV5, JITDecisionMutationKind, JITDecisionMutationV5,
    JITExternalSyncStatusV5, JMLWorkflowMutationKind, JMLWorkflowMutationV5, JMLWorkflowSpecV5,
    JoinerContractorSponsorPolicyV5, JoinerStepV5, LeaverStepV5, LegalHoldDispositionV5,
    MoverStepV5, NHIGovernanceActionV5, NHIGovernanceMutationKind, NHIGovernanceMutationV5,
    NHIOwnerResolutionSourceV5, NHISubkindV5, NoShowDispositionV5, OptimizationMutationKind,
    OptimizationMutationV5, OrphanHandoffPolicyV5, PhaseManifestV5, PhaseWorkQueueV5, PolicyCardV5,
    PolicyOverlayV5, PrivilegeTierV5, ScopeCatalogV5, ScopedSoDRuleV5, SliceDescriptorV5,
    SoDScopeModeV5, SpokeErrorPolicyV5, UARCampaignMutationKind, UARCampaignMutationV5,
    UARDecisionKindV5, ViolationCategoryV5,
};

fn base_if_match() -> Vec<String> {
    vec!["acct_x@0123456789abcdef:1".to_owned()]
}

fn base_cite() -> Vec<String> {
    vec!["CONN-V5-01:ev_x".to_owned()]
}

fn dummy_view() -> ViewV5 {
    let manifest = PhaseManifestV5 {
        phase_index: 1,
        clock_epoch_s: 50,
        query_budget: 2,
        approval_gated: false,
        work_queue: PhaseWorkQueueV5 {
            target_connector_ids: Vec::new(),
            target_hr_mapping_ids: Vec::new(),
            target_human_account_ids: Vec::new(),
            target_nhi_ids: Vec::new(),
            target_agent_ids: Vec::new(),
            target_ciem_task_ids: Vec::new(),
            target_jml_workflow_ids: Vec::new(),
            target_opt_task_ids: vec!["opt_1".to_owned()],
            target_cae_incident_ids: Vec::new(),
            target_uar_campaign_ids: Vec::new(),
            target_jit_request_ids: Vec::new(),
            target_agent_delegation_ids: Vec::new(),
        },
        source_directory: vec![SliceDescriptorV5 {
            slice_id: "sl_1".to_owned(),
            source_system: "test".to_owned(),
            table_names: vec!["hr_persons".to_owned()],
            scope_filter: "global=*".to_owned(),
            authority_tier: ib_v5_schemas::AuthorityTierV5::AuthoritativePrimary,
            is_complete_for_scope: true,
            watermark_s: 50,
            row_count: 1,
            credit_cost: 1,
        }],
        invalidated_entity_ids: Vec::new(),
        prior_phase_ledger: None,
    };
    let card = PolicyCardV5 {
        spec_version: "5.0".to_owned(),
        scope_catalog: ScopeCatalogV5 {
            leaf_scopes: vec!["/org/a".to_owned()],
        },
        overlay: PolicyOverlayV5 {
            freshness_sla_s: 600,
            rotation_sla_s: 600,
            cae_sla_s: 120,
            conversion_grace_s: 600,
            break_glass_cooldown_s: 600,
            allow_contractor_nhi_owners: false,
            allow_contractor_agent_sponsors: false,
            enforce_cross_nhi_owner_sod: true,
            enforce_sponsor_agent_sod: true,
            permit_hitl_step_up_for_autonomous: false,
            require_immutable_hr_correlation: false,
            connector_unmapped_policy_default: ConnectorUnmappedPolicyDefaultV5::HaltConnector,
            uar_preserve_standing_floor_default: true,
            owner_excluded_dept_prefixes: Vec::new(),
            max_prod_nhis_per_owner: 2,
            max_prod_agents_per_sponsor: 2,
            max_agent_delegation_hops: 2,
            critical_jit_tiers: vec![PrivilegeTierV5::Critical],
            jit_ttl_cap_by_tier: vec![(PrivilegeTierV5::Read, 600)],
            forbidden_sod_rules: vec![ScopedSoDRuleV5 {
                rule_code: "sod".to_owned(),
                cap_a: "a".to_owned(),
                cap_b: "b".to_owned(),
                scope_mode: SoDScopeModeV5::Global,
            }],
            temporal_sod_rules: Vec::new(),
        },
        rendered_rules_markdown: "policy".to_owned(),
    };
    ViewV5::from_slices(&manifest, &[], &card, 0, None, &[]).unwrap()
}

fn valid_jml_spec() -> JMLWorkflowSpecV5 {
    JMLWorkflowSpecV5 {
        joiner_steps: vec![
            JoinerStepV5::VerifyHrAuthAndEffectiveDate,
            JoinerStepV5::CorrelateImmutableIdOrCreateAccount,
            JoinerStepV5::ProvisionBirthrightAccess,
            JoinerStepV5::BindTelemetryAndMfaPolicy,
            JoinerStepV5::EmitJoinerAuditReceipt,
        ],
        mover_steps: vec![
            MoverStepV5::SnapshotPriorGrantsAndBridges,
            MoverStepV5::RevokeOutOfScopeNonBridgedGrants,
            MoverStepV5::ProvisionNewDeptBirthrights,
            MoverStepV5::EnforcePostMoverSodAndRebindOwnership,
            MoverStepV5::TriggerTargetedMoverCertification,
        ],
        leaver_steps: vec![
            LeaverStepV5::DisableIdpAndSsoSessions,
            LeaverStepV5::RevokeRefreshAndOboTrees,
            LeaverStepV5::TransferOwnedNhiAndAgentCustody,
            LeaverStepV5::DeprovisionSpokeEntitlementsOrDispatchItsm,
            LeaverStepV5::ApplyMailboxArchiveOrLegalHoldRetention,
        ],
        joiner_contractor_sponsor_policy:
            JoinerContractorSponsorPolicyV5::RequireActiveFteSponsorPreProvision,
        no_show_disposition: NoShowDispositionV5::FullRevokeAndDisableNeverStarted,
        legal_hold_disposition:
            LegalHoldDispositionV5::SuspendAuthRevokeEntitlementsPreserveHoldData,
        orphan_handoff_policy: OrphanHandoffPolicyV5::ReassignToActiveManagerOrEscrow,
        spoke_error_policy: SpokeErrorPolicyV5::FailClosedRetryQueueAndAlert,
        disconnected_target_policy:
            DisconnectedTargetPolicyV5::DispatchItsmAndVerifyPostCloseAggregation,
    }
}

fn opt_ctx(policy_ok: bool, coverage_ok: bool) -> Stage5Context {
    Stage5Context {
        feasibility: Some(Arc::new(move |_| OptFeasibility {
            feasible: policy_ok && coverage_ok,
            policy_ok,
            coverage_ok,
        })),
        ..Default::default()
    }
}

#[test]
fn semantic_diagnostic_service_reports_unread_gold_evidence_as_boolean() {
    let view = dummy_view();
    let gold_mut = AtomicMutationV5::AccountRecon(AccountReconMutationV5 {
        target_entity_id: "acct_1".to_owned(),
        if_match: base_if_match(),
        cite: vec!["BIND-V5-01a:ev_gold_unread".to_owned()],
        mutation_kind: AccountReconMutationKind::AccountReconMutation,
        account_action: AccountReconActionV5::KeepActive,
        bound_person_id: Some("pers_1".to_owned()),
        bound_engagement_id: Some("eng_1".to_owned()),
        retained_grant_ids: Vec::new(),
    });
    let mut traces = BTreeMap::new();
    traces.insert(
        ("ACCOUNT_RECON_MUTATION".to_owned(), "acct_1".to_owned()),
        GoldTraceEntryV5 {
            mutation: gold_mut.clone(),
            preconditions: gold_mut.if_match().to_vec(),
            citations: gold_mut.cite().to_vec(),
            context: Stage5Context::default(),
        },
    );
    let service = SemanticDiagnosticService::new(move |_, _| Ok(traces.clone()));

    let delivered_only: BTreeSet<String> = ["ev_delivered".to_owned()].into_iter().collect();
    assert!(
        service
            .has_unread_gold_evidence(1, &view, &delivered_only)
            .unwrap()
    );
    let read_gold: BTreeSet<String> = ["ev_gold_unread".to_owned()].into_iter().collect();
    assert!(
        !service
            .has_unread_gold_evidence(1, &view, &read_gold)
            .unwrap()
    );
}

#[test]
fn semantic_equality_ignores_only_if_match_and_cite() {
    let base = AtomicMutationV5::ConnectorSync(ConnectorSyncMutationV5 {
        target_entity_id: "conn_a".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: ConnectorSyncMutationKind::ConnectorSyncMutation,
        halted_fail_closed: false,
        halt_reason: ConnectorHaltReasonV5::None,
        reconciled_account_ids: Vec::new(),
        remediated_spoke_drift: Vec::new(),
        itsm_ticket_actions: Vec::new(),
    });
    let mut same_output = base.clone();
    if let AtomicMutationV5::ConnectorSync(ref mut m) = same_output {
        m.if_match = vec!["acct_b@0123456789abcdef:1".to_owned()];
        m.cite = vec!["CONN-V5-01:ev_other".to_owned()];
    }
    assert!(semantically_equal(&base, &same_output));

    let mut diff_target = base.clone();
    if let AtomicMutationV5::ConnectorSync(ref mut m) = diff_target {
        m.target_entity_id = "conn_b".to_owned();
    }
    assert!(!semantically_equal(&base, &diff_target));
}

#[test]
fn diagnostics_short_circuit_in_stage_order() {
    let gold = AtomicMutationV5::ConnectorSync(ConnectorSyncMutationV5 {
        target_entity_id: "conn_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: ConnectorSyncMutationKind::ConnectorSyncMutation,
        halted_fail_closed: false,
        halt_reason: ConnectorHaltReasonV5::None,
        reconciled_account_ids: vec!["acct_a".to_owned()],
        remediated_spoke_drift: Vec::new(),
        itsm_ticket_actions: Vec::new(),
    });
    let submitted = AtomicMutationV5::ConnectorSync(ConnectorSyncMutationV5 {
        target_entity_id: "conn_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: ConnectorSyncMutationKind::ConnectorSyncMutation,
        halted_fail_closed: true,
        halt_reason: ConnectorHaltReasonV5::HaltCursorOrHttp,
        reconciled_account_ids: vec!["acct_a".to_owned()],
        remediated_spoke_drift: Vec::new(),
        itsm_ticket_actions: Vec::new(),
    });
    let target: TargetKey = ("CONNECTOR_SYNC_MUTATION".to_owned(), "conn_x".to_owned());
    let mut trace = BTreeMap::new();
    trace.insert(
        target.clone(),
        GoldTraceEntryV5 {
            mutation: gold,
            preconditions: base_if_match(),
            citations: base_cite(),
            context: Stage5Context::default(),
        },
    );
    let ledger: BTreeSet<String> = ["ev_x".to_owned()].into_iter().collect();

    let s1 = evaluate_phase(
        1,
        std::slice::from_ref(&target),
        &[],
        &trace,
        &ledger,
        EvaluatePhaseOptions::default(),
    )
    .unwrap();
    assert_eq!(s1.first_failing_stage, Some(1));
    assert_eq!(
        s1.violation_categories,
        vec![ViolationCategoryV5::CompletenessViolation]
    );

    let mut s2_map = BTreeMap::new();
    s2_map.insert(target.clone(), false);
    let s2 = evaluate_phase(
        1,
        std::slice::from_ref(&target),
        std::slice::from_ref(&submitted),
        &trace,
        &ledger,
        EvaluatePhaseOptions {
            stage1_ok: true,
            stage2_ok: false,
            stage2_valid_by_target: Some(&s2_map),
            preconditions: None,
            citations: None,
        },
    )
    .unwrap();
    assert_eq!(s2.first_failing_stage, Some(2));
    assert_eq!(
        s2.violation_categories,
        vec![ViolationCategoryV5::SchemaValidationError]
    );

    let empty_ledger = BTreeSet::new();
    let bad_pre = vec!["acct_y@0123456789abcdef:1".to_owned()];
    let s3 = evaluate_phase(
        1,
        std::slice::from_ref(&target),
        std::slice::from_ref(&submitted),
        &trace,
        &empty_ledger,
        EvaluatePhaseOptions {
            preconditions: Some(&bad_pre),
            ..Default::default()
        },
    )
    .unwrap();
    assert_eq!(s3.first_failing_stage, Some(3));
    assert_eq!(
        s3.violation_categories,
        vec![ViolationCategoryV5::PreconditionMismatch]
    );
    assert!(!s3.semantic_equal_by_target[&target]);

    let s4 = evaluate_phase(
        1,
        std::slice::from_ref(&target),
        std::slice::from_ref(&submitted),
        &trace,
        &empty_ledger,
        EvaluatePhaseOptions::default(),
    )
    .unwrap();
    assert_eq!(s4.first_failing_stage, Some(4));
    assert_eq!(
        s4.violation_categories,
        vec![ViolationCategoryV5::CitationMismatch]
    );
    assert!(!s4.stage5_ok);
}

#[test]
fn stage5_aggregates_sorted_unique_categories_by_wire_name() {
    let acct_target: TargetKey = ("ACCOUNT_RECON_MUTATION".to_owned(), "acct_a".to_owned());
    let conn_target: TargetKey = ("CONNECTOR_SYNC_MUTATION".to_owned(), "conn_a".to_owned());

    let gold_acct = AtomicMutationV5::AccountRecon(AccountReconMutationV5 {
        target_entity_id: "acct_a".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: AccountReconMutationKind::AccountReconMutation,
        account_action: AccountReconActionV5::KeepActive,
        bound_person_id: Some("pers_a".to_owned()),
        bound_engagement_id: Some("eng_a".to_owned()),
        retained_grant_ids: Vec::new(),
    });
    let sub_acct = AtomicMutationV5::AccountRecon(AccountReconMutationV5 {
        target_entity_id: "acct_a".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: AccountReconMutationKind::AccountReconMutation,
        account_action: AccountReconActionV5::KeepActive,
        bound_person_id: Some("pers_b".to_owned()),
        bound_engagement_id: Some("eng_b".to_owned()),
        retained_grant_ids: Vec::new(),
    });
    let gold_conn = AtomicMutationV5::ConnectorSync(ConnectorSyncMutationV5 {
        target_entity_id: "conn_a".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: ConnectorSyncMutationKind::ConnectorSyncMutation,
        halted_fail_closed: false,
        halt_reason: ConnectorHaltReasonV5::None,
        reconciled_account_ids: vec!["acct_a".to_owned()],
        remediated_spoke_drift: Vec::new(),
        itsm_ticket_actions: Vec::new(),
    });
    let sub_conn = AtomicMutationV5::ConnectorSync(ConnectorSyncMutationV5 {
        target_entity_id: "conn_a".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: ConnectorSyncMutationKind::ConnectorSyncMutation,
        halted_fail_closed: true,
        halt_reason: ConnectorHaltReasonV5::HaltCursorOrHttp,
        reconciled_account_ids: vec!["acct_a".to_owned()],
        remediated_spoke_drift: Vec::new(),
        itsm_ticket_actions: Vec::new(),
    });

    let mut traces = BTreeMap::new();
    traces.insert(
        acct_target.clone(),
        GoldTraceEntryV5 {
            mutation: gold_acct,
            preconditions: base_if_match(),
            citations: base_cite(),
            context: Stage5Context {
                gold_engagement_status: Some(EngagementStatusV5::Active),
                pruned_conversion_grant_ids: Some(BTreeSet::new()),
                cc_sensitive_grant_ids: Some(BTreeSet::new()),
                ..Default::default()
            },
        },
    );
    traces.insert(
        conn_target.clone(),
        GoldTraceEntryV5 {
            mutation: gold_conn,
            preconditions: base_if_match(),
            citations: base_cite(),
            context: Stage5Context::default(),
        },
    );
    let ledger: BTreeSet<String> = ["ev_x".to_owned()].into_iter().collect();
    let res = evaluate_phase(
        1,
        &[conn_target.clone(), acct_target.clone()],
        &[sub_conn, sub_acct],
        &traces,
        &ledger,
        EvaluatePhaseOptions::default(),
    )
    .unwrap();
    assert_eq!(res.first_failing_stage, Some(5));
    // Notice wire alphabetical: BINDING_OR_ENGAGEMENT_CONFLICT < CONNECTOR_HALT_OR_FREEZE
    assert_eq!(
        res.violation_categories,
        vec![
            ViolationCategoryV5::BindingOrEngagementConflict,
            ViolationCategoryV5::ConnectorHaltOrFreeze,
        ]
    );
}

#[test]
fn stage5_case_matrix_reaches_all_39_semantic_categories() {
    let mut seen = BTreeSet::new();
    let mut check = |expected: ViolationCategoryV5,
                     sub: AtomicMutationV5,
                     gold: AtomicMutationV5,
                     ctx: Stage5Context| {
        let got = first_stage5_category(&sub, &gold, &ctx).unwrap();
        assert_eq!(got, expected);
        seen.insert(violation_category_as_str(got));
    };

    // Connector (4)
    let conn_gold = AtomicMutationV5::ConnectorSync(ConnectorSyncMutationV5 {
        target_entity_id: "conn_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: ConnectorSyncMutationKind::ConnectorSyncMutation,
        halted_fail_closed: false,
        halt_reason: ConnectorHaltReasonV5::None,
        reconciled_account_ids: vec!["acct_a".to_owned()],
        remediated_spoke_drift: vec!["acct_a:role_r".to_owned()],
        itsm_ticket_actions: vec![ITSMTicketActionV5 {
            drift_token: "acct_a:role_r".to_owned(),
            ticket_action: ITSMTicketActionKindV5::DispatchNewTicket,
            assignee_account_id: Some("acct_f".to_owned()),
        }],
    });
    {
        let mut m = conn_gold.clone();
        if let AtomicMutationV5::ConnectorSync(ref mut x) = m {
            x.halted_fail_closed = true;
            x.halt_reason = ConnectorHaltReasonV5::HaltCursorOrHttp;
        }
        check(
            ViolationCategoryV5::ConnectorHaltOrFreeze,
            m,
            conn_gold.clone(),
            Stage5Context::default(),
        );
    }
    {
        let mut m = conn_gold.clone();
        if let AtomicMutationV5::ConnectorSync(ref mut x) = m {
            x.reconciled_account_ids.clear();
        }
        check(
            ViolationCategoryV5::ConnectorReconciliationSet,
            m,
            conn_gold.clone(),
            Stage5Context::default(),
        );
    }
    {
        let mut m = conn_gold.clone();
        if let AtomicMutationV5::ConnectorSync(ref mut x) = m {
            x.remediated_spoke_drift.clear();
        }
        check(
            ViolationCategoryV5::SpokeDriftRemediation,
            m,
            conn_gold.clone(),
            Stage5Context::default(),
        );
    }
    {
        let mut m = conn_gold.clone();
        if let AtomicMutationV5::ConnectorSync(ref mut x) = m {
            x.itsm_ticket_actions[0].ticket_action = ITSMTicketActionKindV5::EscalateSlaOrCoi;
        }
        check(
            ViolationCategoryV5::ItsmManualFulfillmentMismatch,
            m,
            conn_gold,
            Stage5Context::default(),
        );
    }

    // HR (2)
    let hr_gold = AtomicMutationV5::HrSourceMapping(HRSourceMappingMutationV5 {
        target_entity_id: "hrmap_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: HRSourceMappingMutationKind::HRSourceMappingMutation,
        accepted_feed_ids: vec!["hfeed_a".to_owned()],
        rejected_feed_ids: vec!["hfeed_b".to_owned()],
        resolved_person_statuses: vec![("pers_a".to_owned(), EngagementStatusV5::Active)],
        quarantined_assertion_keys: vec!["hfeed_b:pers_a".to_owned()],
    });
    {
        let mut m = hr_gold.clone();
        if let AtomicMutationV5::HrSourceMapping(ref mut x) = m {
            x.accepted_feed_ids.clear();
            x.rejected_feed_ids = vec!["hfeed_a".to_owned(), "hfeed_b".to_owned()];
        }
        check(
            ViolationCategoryV5::HrFeedAuthorityMismatch,
            m,
            hr_gold.clone(),
            Stage5Context::default(),
        );
    }
    {
        let mut m = hr_gold.clone();
        if let AtomicMutationV5::HrSourceMapping(ref mut x) = m {
            x.resolved_person_statuses =
                vec![("pers_a".to_owned(), EngagementStatusV5::Terminated)];
            x.quarantined_assertion_keys.clear();
        }
        check(
            ViolationCategoryV5::HrCorrelationOrPrecedenceMismatch,
            m,
            hr_gold,
            Stage5Context::default(),
        );
    }

    // Account Recon (4)
    let acct_gold = AtomicMutationV5::AccountRecon(AccountReconMutationV5 {
        target_entity_id: "acct_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: AccountReconMutationKind::AccountReconMutation,
        account_action: AccountReconActionV5::KeepActive,
        bound_person_id: Some("pers_a".to_owned()),
        bound_engagement_id: Some("eng_a".to_owned()),
        retained_grant_ids: vec!["grant_a".to_owned()],
    });
    {
        let mut m = acct_gold.clone();
        if let AtomicMutationV5::AccountRecon(ref mut x) = m {
            x.bound_person_id = Some("pers_b".to_owned());
        }
        check(
            ViolationCategoryV5::BindingOrEngagementConflict,
            m,
            acct_gold.clone(),
            Stage5Context {
                gold_engagement_status: Some(EngagementStatusV5::Active),
                ..Default::default()
            },
        );
    }
    {
        let mut m = acct_gold.clone();
        if let AtomicMutationV5::AccountRecon(ref mut x) = m {
            x.account_action = AccountReconActionV5::DisableAccount;
            x.retained_grant_ids.clear();
        }
        check(
            ViolationCategoryV5::ConvGraceBreach,
            m,
            acct_gold.clone(),
            Stage5Context {
                gold_engagement_status: Some(EngagementStatusV5::ContractorToFtePending),
                pruned_conversion_grant_ids: Some(["grant_a".to_owned()].into_iter().collect()),
                cc_sensitive_grant_ids: Some(BTreeSet::new()),
                ..Default::default()
            },
        );
    }
    {
        let mut m = acct_gold.clone();
        if let AtomicMutationV5::AccountRecon(ref mut x) = m {
            x.retained_grant_ids.clear();
        }
        check(
            ViolationCategoryV5::CompensatingControlInvalid,
            m,
            acct_gold.clone(),
            Stage5Context {
                gold_engagement_status: Some(EngagementStatusV5::Active),
                pruned_conversion_grant_ids: Some(BTreeSet::new()),
                cc_sensitive_grant_ids: Some(["grant_a".to_owned()].into_iter().collect()),
                ..Default::default()
            },
        );
    }
    {
        let mut m = acct_gold.clone();
        if let AtomicMutationV5::AccountRecon(ref mut x) = m {
            x.retained_grant_ids.clear();
        }
        check(
            ViolationCategoryV5::MoverOrScopedSelfSod,
            m,
            acct_gold,
            Stage5Context {
                gold_engagement_status: Some(EngagementStatusV5::Active),
                pruned_conversion_grant_ids: Some(BTreeSet::new()),
                cc_sensitive_grant_ids: Some(BTreeSet::new()),
                ..Default::default()
            },
        );
    }

    // NHI (3)
    let nhi_gold = AtomicMutationV5::NhiGovernance(NHIGovernanceMutationV5 {
        target_entity_id: "nhi_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: NHIGovernanceMutationKind::NHIGovernanceMutation,
        nhi_action: NHIGovernanceActionV5::AttributeOwner,
        resolved_owner_person_id: Some("pers_a".to_owned()),
        owner_resolution_source: Some(NHIOwnerResolutionSourceV5::IacStateAttestation),
    });
    check(
        ViolationCategoryV5::StaleTelemetryOrRotationSla,
        nhi_gold.clone(),
        nhi_gold.clone(),
        Stage5Context {
            nhi_stale_support_predicate: Some(Arc::new(|_| true)),
            eligible_owner_person_ids: Some(["pers_a".to_owned()].into_iter().collect()),
            ..Default::default()
        },
    );
    {
        let mut m = nhi_gold.clone();
        if let AtomicMutationV5::NhiGovernance(ref mut x) = m {
            x.resolved_owner_person_id = Some("pers_b".to_owned());
        }
        check(
            ViolationCategoryV5::NhiOverlayEligibilityOrCap,
            m,
            nhi_gold.clone(),
            Stage5Context {
                nhi_stale_support_predicate: Some(Arc::new(|_| false)),
                eligible_owner_person_ids: Some(["pers_a".to_owned()].into_iter().collect()),
                ..Default::default()
            },
        );
    }
    {
        let mut m = nhi_gold.clone();
        if let AtomicMutationV5::NhiGovernance(ref mut x) = m {
            x.owner_resolution_source = Some(NHIOwnerResolutionSourceV5::CmdbServiceOwner);
        }
        check(
            ViolationCategoryV5::NhiOwnerPrecedence,
            m,
            nhi_gold,
            Stage5Context {
                nhi_stale_support_predicate: Some(Arc::new(|_| false)),
                eligible_owner_person_ids: Some(["pers_a".to_owned()].into_iter().collect()),
                ..Default::default()
            },
        );
    }

    // Agent Registry (3)
    let agt_gold = AtomicMutationV5::AgentRegistry(AgentRegistryMutationV5 {
        target_entity_id: "agt_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: AgentRegistryMutationKind::AgentRegistryMutation,
        agent_action: AgentRegistryActionV5::RegisterActive,
        resolved_sponsor_person_id: Some("pers_a".to_owned()),
        active_autonomous_binding_ids: vec!["atg_a".to_owned()],
        active_hitl_binding_ids: Vec::new(),
    });
    check(
        ViolationCategoryV5::AgentAttestationOrStaleDigest,
        AtomicMutationV5::AgentRegistry(AgentRegistryMutationV5 {
            target_entity_id: "agt_x".to_owned(),
            if_match: base_if_match(),
            cite: base_cite(),
            mutation_kind: AgentRegistryMutationKind::AgentRegistryMutation,
            agent_action: AgentRegistryActionV5::SuspendUnattestedOrStale,
            resolved_sponsor_person_id: None,
            active_autonomous_binding_ids: Vec::new(),
            active_hitl_binding_ids: Vec::new(),
        }),
        agt_gold.clone(),
        Stage5Context::default(),
    );
    check(
        ViolationCategoryV5::AgentSponsorEligibilityOrCap,
        AtomicMutationV5::AgentRegistry(AgentRegistryMutationV5 {
            target_entity_id: "agt_x".to_owned(),
            if_match: base_if_match(),
            cite: base_cite(),
            mutation_kind: AgentRegistryMutationKind::AgentRegistryMutation,
            agent_action: AgentRegistryActionV5::SuspendSponsorInvalid,
            resolved_sponsor_person_id: None,
            active_autonomous_binding_ids: Vec::new(),
            active_hitl_binding_ids: Vec::new(),
        }),
        agt_gold.clone(),
        Stage5Context::default(),
    );
    check(
        ViolationCategoryV5::AgentScopeTieringOrSod,
        AtomicMutationV5::AgentRegistry(AgentRegistryMutationV5 {
            target_entity_id: "agt_x".to_owned(),
            if_match: base_if_match(),
            cite: base_cite(),
            mutation_kind: AgentRegistryMutationKind::AgentRegistryMutation,
            agent_action: AgentRegistryActionV5::SuspendNoActiveTools,
            resolved_sponsor_person_id: Some("pers_a".to_owned()),
            active_autonomous_binding_ids: Vec::new(),
            active_hitl_binding_ids: Vec::new(),
        }),
        agt_gold,
        Stage5Context::default(),
    );

    // CIEM (3)
    let ciem_gold = AtomicMutationV5::CloudCiem(CloudCIEMMutationV5 {
        target_entity_id: "ciem_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: CloudCIEMMutationKind::CloudCIEMMutation,
        findings: vec![CIEMPrincipalFindingV5 {
            cprin_id: "cprin_aws_a".to_owned(),
            effective_forbidden_actions: Vec::new(),
            escalation_vector: CIEMEscalationVectorV5::None,
            remediation_action: CIEMRemediationActionV5::AttachBoundaryOrDeny,
        }],
    });
    {
        let mut m = ciem_gold.clone();
        if let AtomicMutationV5::CloudCiem(ref mut x) = m {
            x.findings[0].remediation_action = CIEMRemediationActionV5::RevokeOrphanCloudPrincipal;
        }
        check(
            ViolationCategoryV5::CiemOrphanPrincipalMismatch,
            m,
            ciem_gold.clone(),
            Stage5Context::default(),
        );
    }
    {
        let mut m = ciem_gold.clone();
        if let AtomicMutationV5::CloudCiem(ref mut x) = m {
            x.findings[0].escalation_vector = CIEMEscalationVectorV5::AwsPassroleExec;
        }
        check(
            ViolationCategoryV5::CiemEscalationPathMismatch,
            m,
            ciem_gold.clone(),
            Stage5Context::default(),
        );
    }
    {
        let mut m = ciem_gold.clone();
        if let AtomicMutationV5::CloudCiem(ref mut x) = m {
            x.findings[0].remediation_action = CIEMRemediationActionV5::StripEscalationEdge;
        }
        check(
            ViolationCategoryV5::CiemEffectivePermissionOrRemediation,
            m,
            ciem_gold,
            Stage5Context::default(),
        );
    }

    // Opt (2)
    let opt_sub = AtomicMutationV5::Optimization(OptimizationMutationV5 {
        target_entity_id: "opt_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: OptimizationMutationKind::OptimizationMutation,
        selected_ids: vec!["cand_a".to_owned()],
        objective_value: 1,
    });
    let opt_gold = AtomicMutationV5::Optimization(OptimizationMutationV5 {
        target_entity_id: "opt_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: OptimizationMutationKind::OptimizationMutation,
        selected_ids: Vec::new(),
        objective_value: 0,
    });
    check(
        ViolationCategoryV5::OptimizationInfeasible,
        opt_sub.clone(),
        opt_gold.clone(),
        opt_ctx(false, false),
    );
    check(
        ViolationCategoryV5::OptimizationSuboptimal,
        opt_sub,
        opt_gold,
        opt_ctx(true, true),
    );

    // JML (2)
    let jml_gold = AtomicMutationV5::JmlWorkflow(JMLWorkflowMutationV5 {
        target_entity_id: "wfl_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: JMLWorkflowMutationKind::JMLWorkflowMutation,
        active_birthright_rule_ids: vec!["brule_a".to_owned()],
        converted_eligible_brule_ids: Vec::new(),
        quarantined_birthright_rule_ids: Vec::new(),
        authored_spec: valid_jml_spec(),
    });
    {
        let mut m = jml_gold.clone();
        if let AtomicMutationV5::JmlWorkflow(ref mut x) = m {
            x.active_birthright_rule_ids.clear();
            x.quarantined_birthright_rule_ids = vec!["brule_a".to_owned()];
        }
        check(
            ViolationCategoryV5::JmlBirthrightZspOrSodBreach,
            m,
            jml_gold.clone(),
            Stage5Context::default(),
        );
    }
    {
        let mut m = jml_gold.clone();
        if let AtomicMutationV5::JmlWorkflow(ref mut x) = m {
            x.authored_spec.joiner_steps.swap(0, 1);
        }
        check(
            ViolationCategoryV5::JmlWorkflowSpecUnsafe,
            m,
            jml_gold,
            Stage5Context::default(),
        );
    }

    // CAE (3)
    let cae_gold = AtomicMutationV5::CaeContainment(CAEContainmentMutationV5 {
        target_entity_id: "inc_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: CAEContainmentMutationKind::CAEContainmentMutation,
        cae_revoke_sessions: true,
        cae_computed_exposure_s: 100,
        cae_sla_breached: true,
        retro_invalidated_ids: vec!["tkt_a".to_owned()],
    });
    {
        let mut m = cae_gold.clone();
        if let AtomicMutationV5::CaeContainment(ref mut x) = m {
            x.retro_invalidated_ids.clear();
        }
        check(
            ViolationCategoryV5::RetroApprovalTaintMismatch,
            m,
            cae_gold.clone(),
            Stage5Context::default(),
        );
    }
    {
        let mut m = cae_gold.clone();
        if let AtomicMutationV5::CaeContainment(ref mut x) = m {
            x.cae_revoke_sessions = false;
            x.cae_computed_exposure_s = 0;
            x.cae_sla_breached = false;
        }
        check(
            ViolationCategoryV5::CaeRevocationDecision,
            m,
            cae_gold.clone(),
            Stage5Context::default(),
        );
    }
    {
        let mut m = cae_gold.clone();
        if let AtomicMutationV5::CaeContainment(ref mut x) = m {
            x.cae_computed_exposure_s = 50;
            x.cae_sla_breached = false;
        }
        check(
            ViolationCategoryV5::CaeRefreshTreeExposure,
            m,
            cae_gold,
            Stage5Context::default(),
        );
    }

    // UAR (4)
    let uar_gold = AtomicMutationV5::UarCampaign(UARCampaignMutationV5 {
        target_entity_id: "camp_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: UARCampaignMutationKind::UARCampaignMutation,
        uar_decisions: vec![
            ("item_a".to_owned(), UARDecisionKindV5::Revoke),
            ("item_b".to_owned(), UARDecisionKindV5::Certify),
        ],
        reviewer_overrides: Vec::new(),
        emergency_custodian_account_id: Some("acct_c".to_owned()),
    });
    {
        let mut m = uar_gold.clone();
        if let AtomicMutationV5::UarCampaign(ref mut x) = m {
            x.uar_decisions[0].1 = UARDecisionKindV5::Certify;
        }
        check(
            ViolationCategoryV5::UarBaselineOrShockFilter,
            m,
            uar_gold.clone(),
            Stage5Context {
                surviving_item_ids: Some(["item_b".to_owned()].into_iter().collect()),
                valid_reviewers: Some(BTreeMap::new()),
                locked_item_ids: Some(BTreeSet::new()),
                ..Default::default()
            },
        );
    }
    {
        let mut m = uar_gold.clone();
        if let AtomicMutationV5::UarCampaign(ref mut x) = m {
            x.reviewer_overrides = vec![("item_b".to_owned(), "acct_r".to_owned())];
        }
        check(
            ViolationCategoryV5::UarReviewerCoiOverride,
            m,
            uar_gold.clone(),
            Stage5Context {
                surviving_item_ids: Some(
                    ["item_a".to_owned(), "item_b".to_owned()]
                        .into_iter()
                        .collect(),
                ),
                valid_reviewers: Some(BTreeMap::new()),
                locked_item_ids: Some(BTreeSet::new()),
                ..Default::default()
            },
        );
    }
    {
        let mut m = uar_gold.clone();
        if let AtomicMutationV5::UarCampaign(ref mut x) = m {
            x.uar_decisions[0].1 = UARDecisionKindV5::Certify;
        }
        let mut vr = BTreeMap::new();
        vr.insert(
            "item_a".to_owned(),
            ["acct_r".to_owned()].into_iter().collect(),
        );
        check(
            ViolationCategoryV5::UarInheritedStandingLock,
            m,
            uar_gold.clone(),
            Stage5Context {
                surviving_item_ids: Some(
                    ["item_a".to_owned(), "item_b".to_owned()]
                        .into_iter()
                        .collect(),
                ),
                valid_reviewers: Some(vr),
                locked_item_ids: Some(["item_a".to_owned()].into_iter().collect()),
                ..Default::default()
            },
        );
    }
    {
        let mut m = uar_gold.clone();
        if let AtomicMutationV5::UarCampaign(ref mut x) = m {
            x.emergency_custodian_account_id = None;
        }
        let mut vr = BTreeMap::new();
        vr.insert(
            "item_a".to_owned(),
            ["acct_r".to_owned()].into_iter().collect(),
        );
        check(
            ViolationCategoryV5::QuorumOrCustodianDepletion,
            m,
            uar_gold,
            Stage5Context {
                surviving_item_ids: Some(
                    ["item_a".to_owned(), "item_b".to_owned()]
                        .into_iter()
                        .collect(),
                ),
                valid_reviewers: Some(vr),
                locked_item_ids: Some(BTreeSet::new()),
                ..Default::default()
            },
        );
    }

    // JIT (5)
    let jit_deny = AtomicMutationV5::JitDecision(JITDecisionMutationV5 {
        target_entity_id: "req_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: JITDecisionMutationKind::JITDecisionMutation,
        jit_decision: JITDecisionKindV5::Deny,
        approver_account_id: None,
        approved_ttl_s: 0,
        external_sync_status: JITExternalSyncStatusV5::NoExternalTicket,
    });
    let jit_approve = AtomicMutationV5::JitDecision(JITDecisionMutationV5 {
        target_entity_id: "req_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: JITDecisionMutationKind::JITDecisionMutation,
        jit_decision: JITDecisionKindV5::ApproveJit,
        approver_account_id: Some("acct_a".to_owned()),
        approved_ttl_s: 100,
        external_sync_status: JITExternalSyncStatusV5::SyncGranted,
    });
    for (pred, cat) in [
        ("P1", ViolationCategoryV5::JitRequesterOrScopeIneligible),
        ("P2", ViolationCategoryV5::TicketStaleOrRetroTainted),
        ("P3", ViolationCategoryV5::ScopedOrBreakglassSodCooldown),
        ("P4", ViolationCategoryV5::ApproverDisqualified),
    ] {
        let mut map = BTreeMap::new();
        map.insert("req_x".to_owned(), pred.to_owned());
        check(
            cat,
            jit_approve.clone(),
            jit_deny.clone(),
            Stage5Context {
                failing_predicate: Some(FailingPredicate::ByTarget(map)),
                ..Default::default()
            },
        );
    }
    check(
        ViolationCategoryV5::JitDualControlOrTtlBound,
        jit_deny,
        jit_approve,
        Stage5Context::default(),
    );

    // OBO (4)
    let obo_deny = AtomicMutationV5::AgentDelegation(AgentDelegationMutationV5 {
        target_entity_id: "dlreq_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: AgentDelegationMutationKind::AgentDelegationMutation,
        delegation_decision: AgentDelegationDecisionKindV5::DenyDelegation,
        hitl_cosigner_account_id: None,
        approved_ttl_s: 0,
    });
    let obo_approve = AtomicMutationV5::AgentDelegation(AgentDelegationMutationV5 {
        target_entity_id: "dlreq_x".to_owned(),
        if_match: base_if_match(),
        cite: base_cite(),
        mutation_kind: AgentDelegationMutationKind::AgentDelegationMutation,
        delegation_decision: AgentDelegationDecisionKindV5::ApproveDelegation,
        hitl_cosigner_account_id: None,
        approved_ttl_s: 100,
    });
    for (pred, cat) in [
        ("D1", ViolationCategoryV5::AgentDelegationPrincipalInvalid),
        ("D2", ViolationCategoryV5::AgentOboIntersectionOrHopBreach),
        ("D3", ViolationCategoryV5::AgentWorkflowOrSessionSod),
        ("D4", ViolationCategoryV5::AgentHitlCosignOrTtlMismatch),
    ] {
        let mut map = BTreeMap::new();
        map.insert("dlreq_x".to_owned(), pred.to_owned());
        check(
            cat,
            obo_approve.clone(),
            obo_deny.clone(),
            Stage5Context {
                failing_predicate: Some(FailingPredicate::ByTarget(map)),
                ..Default::default()
            },
        );
    }

    assert_eq!(seen.len(), 39);
}

#[test]
fn safety_partition_covers_all_12_mutation_kinds_and_equal_gold_is_zero() {
    let cases: Vec<(
        AtomicMutationV5,
        AtomicMutationV5,
        Stage5Context,
        AtomicMutationV5,
        AtomicMutationV5,
        Stage5Context,
    )> = vec![
        // 1. CONNECTOR_SYNC_MUTATION
        (
            AtomicMutationV5::ConnectorSync(ConnectorSyncMutationV5 {
                target_entity_id: "conn_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: ConnectorSyncMutationKind::ConnectorSyncMutation,
                halted_fail_closed: false,
                halt_reason: ConnectorHaltReasonV5::None,
                reconciled_account_ids: vec!["acct_a".to_owned(), "acct_b".to_owned()],
                remediated_spoke_drift: Vec::new(),
                itsm_ticket_actions: Vec::new(),
            }),
            AtomicMutationV5::ConnectorSync(ConnectorSyncMutationV5 {
                target_entity_id: "conn_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: ConnectorSyncMutationKind::ConnectorSyncMutation,
                halted_fail_closed: false,
                halt_reason: ConnectorHaltReasonV5::None,
                reconciled_account_ids: vec!["acct_a".to_owned()],
                remediated_spoke_drift: vec!["acct_a:role_r".to_owned()],
                itsm_ticket_actions: Vec::new(),
            }),
            Stage5Context::default(),
            AtomicMutationV5::ConnectorSync(ConnectorSyncMutationV5 {
                target_entity_id: "conn_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: ConnectorSyncMutationKind::ConnectorSyncMutation,
                halted_fail_closed: true,
                halt_reason: ConnectorHaltReasonV5::HaltAuthOrTls,
                reconciled_account_ids: Vec::new(),
                remediated_spoke_drift: Vec::new(),
                itsm_ticket_actions: Vec::new(),
            }),
            AtomicMutationV5::ConnectorSync(ConnectorSyncMutationV5 {
                target_entity_id: "conn_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: ConnectorSyncMutationKind::ConnectorSyncMutation,
                halted_fail_closed: false,
                halt_reason: ConnectorHaltReasonV5::None,
                reconciled_account_ids: vec!["acct_a".to_owned()],
                remediated_spoke_drift: Vec::new(),
                itsm_ticket_actions: Vec::new(),
            }),
            Stage5Context::default(),
        ),
        // 2. HR_SOURCE_MAPPING_MUTATION
        (
            AtomicMutationV5::HrSourceMapping(HRSourceMappingMutationV5 {
                target_entity_id: "hrmap_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: HRSourceMappingMutationKind::HRSourceMappingMutation,
                accepted_feed_ids: vec!["hfeed_a".to_owned(), "hfeed_b".to_owned()],
                rejected_feed_ids: Vec::new(),
                resolved_person_statuses: vec![("pers_a".to_owned(), EngagementStatusV5::Active)],
                quarantined_assertion_keys: Vec::new(),
            }),
            AtomicMutationV5::HrSourceMapping(HRSourceMappingMutationV5 {
                target_entity_id: "hrmap_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: HRSourceMappingMutationKind::HRSourceMappingMutation,
                accepted_feed_ids: vec!["hfeed_a".to_owned()],
                rejected_feed_ids: vec!["hfeed_b".to_owned()],
                resolved_person_statuses: vec![(
                    "pers_a".to_owned(),
                    EngagementStatusV5::Terminated,
                )],
                quarantined_assertion_keys: vec!["hfeed_a:pers_a".to_owned()],
            }),
            Stage5Context::default(),
            AtomicMutationV5::HrSourceMapping(HRSourceMappingMutationV5 {
                target_entity_id: "hrmap_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: HRSourceMappingMutationKind::HRSourceMappingMutation,
                accepted_feed_ids: Vec::new(),
                rejected_feed_ids: vec!["hfeed_a".to_owned()],
                resolved_person_statuses: vec![(
                    "pers_a".to_owned(),
                    EngagementStatusV5::Terminated,
                )],
                quarantined_assertion_keys: vec!["hfeed_a:pers_a".to_owned()],
            }),
            AtomicMutationV5::HrSourceMapping(HRSourceMappingMutationV5 {
                target_entity_id: "hrmap_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: HRSourceMappingMutationKind::HRSourceMappingMutation,
                accepted_feed_ids: vec!["hfeed_a".to_owned()],
                rejected_feed_ids: Vec::new(),
                resolved_person_statuses: vec![("pers_a".to_owned(), EngagementStatusV5::Active)],
                quarantined_assertion_keys: Vec::new(),
            }),
            Stage5Context::default(),
        ),
        // 3. ACCOUNT_RECON_MUTATION
        (
            AtomicMutationV5::AccountRecon(AccountReconMutationV5 {
                target_entity_id: "acct_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: AccountReconMutationKind::AccountReconMutation,
                account_action: AccountReconActionV5::KeepActive,
                bound_person_id: Some("pers_a".to_owned()),
                bound_engagement_id: Some("eng_a".to_owned()),
                retained_grant_ids: vec!["grant_a".to_owned()],
            }),
            AtomicMutationV5::AccountRecon(AccountReconMutationV5 {
                target_entity_id: "acct_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: AccountReconMutationKind::AccountReconMutation,
                account_action: AccountReconActionV5::DisableAccount,
                bound_person_id: Some("pers_a".to_owned()),
                bound_engagement_id: Some("eng_a".to_owned()),
                retained_grant_ids: Vec::new(),
            }),
            Stage5Context::default(),
            AtomicMutationV5::AccountRecon(AccountReconMutationV5 {
                target_entity_id: "acct_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: AccountReconMutationKind::AccountReconMutation,
                account_action: AccountReconActionV5::DisableAccount,
                bound_person_id: Some("pers_a".to_owned()),
                bound_engagement_id: Some("eng_a".to_owned()),
                retained_grant_ids: Vec::new(),
            }),
            AtomicMutationV5::AccountRecon(AccountReconMutationV5 {
                target_entity_id: "acct_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: AccountReconMutationKind::AccountReconMutation,
                account_action: AccountReconActionV5::KeepActive,
                bound_person_id: Some("pers_a".to_owned()),
                bound_engagement_id: Some("eng_a".to_owned()),
                retained_grant_ids: vec!["grant_a".to_owned()],
            }),
            Stage5Context::default(),
        ),
        // 4. NHI_GOVERNANCE_MUTATION
        (
            AtomicMutationV5::NhiGovernance(NHIGovernanceMutationV5 {
                target_entity_id: "nhi_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: NHIGovernanceMutationKind::NHIGovernanceMutation,
                nhi_action: NHIGovernanceActionV5::AttributeOwner,
                resolved_owner_person_id: Some("pers_wrong".to_owned()),
                owner_resolution_source: Some(NHIOwnerResolutionSourceV5::IacStateAttestation),
            }),
            AtomicMutationV5::NhiGovernance(NHIGovernanceMutationV5 {
                target_entity_id: "nhi_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: NHIGovernanceMutationKind::NHIGovernanceMutation,
                nhi_action: NHIGovernanceActionV5::DisableOrphan,
                resolved_owner_person_id: None,
                owner_resolution_source: None,
            }),
            Stage5Context {
                target_subkind: Some(NHISubkindV5::ServiceAccount),
                valid_custodian_ids: Some(BTreeSet::new()),
                ..Default::default()
            },
            AtomicMutationV5::NhiGovernance(NHIGovernanceMutationV5 {
                target_entity_id: "nhi_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: NHIGovernanceMutationKind::NHIGovernanceMutation,
                nhi_action: NHIGovernanceActionV5::Escalate,
                resolved_owner_person_id: None,
                owner_resolution_source: None,
            }),
            AtomicMutationV5::NhiGovernance(NHIGovernanceMutationV5 {
                target_entity_id: "nhi_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: NHIGovernanceMutationKind::NHIGovernanceMutation,
                nhi_action: NHIGovernanceActionV5::AttributeOwner,
                resolved_owner_person_id: Some("pers_a".to_owned()),
                owner_resolution_source: Some(NHIOwnerResolutionSourceV5::IacStateAttestation),
            }),
            Stage5Context {
                target_subkind: Some(NHISubkindV5::ServiceAccount),
                valid_custodian_ids: Some(BTreeSet::new()),
                ..Default::default()
            },
        ),
        // 5. AGENT_REGISTRY_MUTATION
        (
            AtomicMutationV5::AgentRegistry(AgentRegistryMutationV5 {
                target_entity_id: "agt_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: AgentRegistryMutationKind::AgentRegistryMutation,
                agent_action: AgentRegistryActionV5::RegisterActive,
                resolved_sponsor_person_id: Some("pers_a".to_owned()),
                active_autonomous_binding_ids: vec!["atg_a".to_owned()],
                active_hitl_binding_ids: Vec::new(),
            }),
            AtomicMutationV5::AgentRegistry(AgentRegistryMutationV5 {
                target_entity_id: "agt_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: AgentRegistryMutationKind::AgentRegistryMutation,
                agent_action: AgentRegistryActionV5::QuarantineSodViolation,
                resolved_sponsor_person_id: Some("pers_a".to_owned()),
                active_autonomous_binding_ids: Vec::new(),
                active_hitl_binding_ids: Vec::new(),
            }),
            Stage5Context::default(),
            AtomicMutationV5::AgentRegistry(AgentRegistryMutationV5 {
                target_entity_id: "agt_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: AgentRegistryMutationKind::AgentRegistryMutation,
                agent_action: AgentRegistryActionV5::SuspendNoActiveTools,
                resolved_sponsor_person_id: Some("pers_a".to_owned()),
                active_autonomous_binding_ids: Vec::new(),
                active_hitl_binding_ids: Vec::new(),
            }),
            AtomicMutationV5::AgentRegistry(AgentRegistryMutationV5 {
                target_entity_id: "agt_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: AgentRegistryMutationKind::AgentRegistryMutation,
                agent_action: AgentRegistryActionV5::RegisterActive,
                resolved_sponsor_person_id: Some("pers_a".to_owned()),
                active_autonomous_binding_ids: vec!["atg_a".to_owned()],
                active_hitl_binding_ids: Vec::new(),
            }),
            Stage5Context::default(),
        ),
        // 6. CLOUD_CIEM_MUTATION
        (
            AtomicMutationV5::CloudCiem(CloudCIEMMutationV5 {
                target_entity_id: "ciem_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: CloudCIEMMutationKind::CloudCIEMMutation,
                findings: vec![CIEMPrincipalFindingV5 {
                    cprin_id: "cprin_aws_x".to_owned(),
                    effective_forbidden_actions: Vec::new(),
                    escalation_vector: CIEMEscalationVectorV5::None,
                    remediation_action: CIEMRemediationActionV5::KeepCompliant,
                }],
            }),
            AtomicMutationV5::CloudCiem(CloudCIEMMutationV5 {
                target_entity_id: "ciem_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: CloudCIEMMutationKind::CloudCIEMMutation,
                findings: vec![CIEMPrincipalFindingV5 {
                    cprin_id: "cprin_aws_x".to_owned(),
                    effective_forbidden_actions: vec!["iam:PassRole".to_owned()],
                    escalation_vector: CIEMEscalationVectorV5::None,
                    remediation_action: CIEMRemediationActionV5::AttachBoundaryOrDeny,
                }],
            }),
            Stage5Context::default(),
            AtomicMutationV5::CloudCiem(CloudCIEMMutationV5 {
                target_entity_id: "ciem_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: CloudCIEMMutationKind::CloudCIEMMutation,
                findings: vec![CIEMPrincipalFindingV5 {
                    cprin_id: "cprin_aws_x".to_owned(),
                    effective_forbidden_actions: Vec::new(),
                    escalation_vector: CIEMEscalationVectorV5::None,
                    remediation_action: CIEMRemediationActionV5::AttachBoundaryOrDeny,
                }],
            }),
            AtomicMutationV5::CloudCiem(CloudCIEMMutationV5 {
                target_entity_id: "ciem_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: CloudCIEMMutationKind::CloudCIEMMutation,
                findings: vec![CIEMPrincipalFindingV5 {
                    cprin_id: "cprin_aws_x".to_owned(),
                    effective_forbidden_actions: Vec::new(),
                    escalation_vector: CIEMEscalationVectorV5::None,
                    remediation_action: CIEMRemediationActionV5::KeepCompliant,
                }],
            }),
            Stage5Context::default(),
        ),
        // 7. OPTIMIZATION_MUTATION
        (
            AtomicMutationV5::Optimization(OptimizationMutationV5 {
                target_entity_id: "opt_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: OptimizationMutationKind::OptimizationMutation,
                selected_ids: vec!["cand_a".to_owned()],
                objective_value: 1,
            }),
            AtomicMutationV5::Optimization(OptimizationMutationV5 {
                target_entity_id: "opt_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: OptimizationMutationKind::OptimizationMutation,
                selected_ids: Vec::new(),
                objective_value: 0,
            }),
            opt_ctx(false, true),
            AtomicMutationV5::Optimization(OptimizationMutationV5 {
                target_entity_id: "opt_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: OptimizationMutationKind::OptimizationMutation,
                selected_ids: Vec::new(),
                objective_value: 0,
            }),
            AtomicMutationV5::Optimization(OptimizationMutationV5 {
                target_entity_id: "opt_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: OptimizationMutationKind::OptimizationMutation,
                selected_ids: vec!["cand_a".to_owned()],
                objective_value: 1,
            }),
            opt_ctx(true, false),
        ),
        // 8. JML_WORKFLOW_MUTATION
        (
            AtomicMutationV5::JmlWorkflow(JMLWorkflowMutationV5 {
                target_entity_id: "wfl_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: JMLWorkflowMutationKind::JMLWorkflowMutation,
                active_birthright_rule_ids: Vec::new(),
                converted_eligible_brule_ids: Vec::new(),
                quarantined_birthright_rule_ids: Vec::new(),
                authored_spec: {
                    let mut s = valid_jml_spec();
                    s.no_show_disposition = NoShowDispositionV5::LeaveDormant;
                    s
                },
            }),
            AtomicMutationV5::JmlWorkflow(JMLWorkflowMutationV5 {
                target_entity_id: "wfl_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: JMLWorkflowMutationKind::JMLWorkflowMutation,
                active_birthright_rule_ids: Vec::new(),
                converted_eligible_brule_ids: Vec::new(),
                quarantined_birthright_rule_ids: Vec::new(),
                authored_spec: valid_jml_spec(),
            }),
            Stage5Context {
                has_disconnected_spokes: Some(false),
                conversion_carryover_sponsor_allowed: Some(false),
                ..Default::default()
            },
            AtomicMutationV5::JmlWorkflow(JMLWorkflowMutationV5 {
                target_entity_id: "wfl_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: JMLWorkflowMutationKind::JMLWorkflowMutation,
                active_birthright_rule_ids: Vec::new(),
                converted_eligible_brule_ids: Vec::new(),
                quarantined_birthright_rule_ids: Vec::new(),
                authored_spec: {
                    let mut s = valid_jml_spec();
                    s.legal_hold_disposition = LegalHoldDispositionV5::HardPurgeAllRecords;
                    s
                },
            }),
            AtomicMutationV5::JmlWorkflow(JMLWorkflowMutationV5 {
                target_entity_id: "wfl_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: JMLWorkflowMutationKind::JMLWorkflowMutation,
                active_birthright_rule_ids: Vec::new(),
                converted_eligible_brule_ids: Vec::new(),
                quarantined_birthright_rule_ids: Vec::new(),
                authored_spec: valid_jml_spec(),
            }),
            Stage5Context {
                has_disconnected_spokes: Some(false),
                conversion_carryover_sponsor_allowed: Some(false),
                ..Default::default()
            },
        ),
        // 9. CAE_CONTAINMENT_MUTATION
        (
            AtomicMutationV5::CaeContainment(CAEContainmentMutationV5 {
                target_entity_id: "inc_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: CAEContainmentMutationKind::CAEContainmentMutation,
                cae_revoke_sessions: false,
                cae_computed_exposure_s: 10,
                cae_sla_breached: false,
                retro_invalidated_ids: Vec::new(),
            }),
            AtomicMutationV5::CaeContainment(CAEContainmentMutationV5 {
                target_entity_id: "inc_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: CAEContainmentMutationKind::CAEContainmentMutation,
                cae_revoke_sessions: true,
                cae_computed_exposure_s: 100,
                cae_sla_breached: false,
                retro_invalidated_ids: vec!["tkt_a".to_owned()],
            }),
            Stage5Context::default(),
            AtomicMutationV5::CaeContainment(CAEContainmentMutationV5 {
                target_entity_id: "inc_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: CAEContainmentMutationKind::CAEContainmentMutation,
                cae_revoke_sessions: true,
                cae_computed_exposure_s: 10,
                cae_sla_breached: false,
                retro_invalidated_ids: vec!["tkt_new".to_owned()],
            }),
            AtomicMutationV5::CaeContainment(CAEContainmentMutationV5 {
                target_entity_id: "inc_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: CAEContainmentMutationKind::CAEContainmentMutation,
                cae_revoke_sessions: false,
                cae_computed_exposure_s: 0,
                cae_sla_breached: false,
                retro_invalidated_ids: Vec::new(),
            }),
            Stage5Context::default(),
        ),
        // 10. UAR_CAMPAIGN_MUTATION
        (
            AtomicMutationV5::UarCampaign(UARCampaignMutationV5 {
                target_entity_id: "camp_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: UARCampaignMutationKind::UARCampaignMutation,
                uar_decisions: vec![("item_a".to_owned(), UARDecisionKindV5::Certify)],
                reviewer_overrides: Vec::new(),
                emergency_custodian_account_id: Some("acct_wrong".to_owned()),
            }),
            AtomicMutationV5::UarCampaign(UARCampaignMutationV5 {
                target_entity_id: "camp_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: UARCampaignMutationKind::UARCampaignMutation,
                uar_decisions: vec![("item_a".to_owned(), UARDecisionKindV5::Revoke)],
                reviewer_overrides: Vec::new(),
                emergency_custodian_account_id: None,
            }),
            Stage5Context::default(),
            AtomicMutationV5::UarCampaign(UARCampaignMutationV5 {
                target_entity_id: "camp_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: UARCampaignMutationKind::UARCampaignMutation,
                uar_decisions: vec![("item_a".to_owned(), UARDecisionKindV5::Revoke)],
                reviewer_overrides: Vec::new(),
                emergency_custodian_account_id: None,
            }),
            AtomicMutationV5::UarCampaign(UARCampaignMutationV5 {
                target_entity_id: "camp_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: UARCampaignMutationKind::UARCampaignMutation,
                uar_decisions: vec![("item_a".to_owned(), UARDecisionKindV5::Certify)],
                reviewer_overrides: Vec::new(),
                emergency_custodian_account_id: Some("acct_c".to_owned()),
            }),
            Stage5Context::default(),
        ),
        // 11. JIT_DECISION_MUTATION
        (
            AtomicMutationV5::JitDecision(JITDecisionMutationV5 {
                target_entity_id: "req_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: JITDecisionMutationKind::JITDecisionMutation,
                jit_decision: JITDecisionKindV5::ApproveJit,
                approver_account_id: Some("acct_wrong".to_owned()),
                approved_ttl_s: 400,
                external_sync_status: JITExternalSyncStatusV5::SyncGranted,
            }),
            AtomicMutationV5::JitDecision(JITDecisionMutationV5 {
                target_entity_id: "req_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: JITDecisionMutationKind::JITDecisionMutation,
                jit_decision: JITDecisionKindV5::Deny,
                approver_account_id: None,
                approved_ttl_s: 0,
                external_sync_status: JITExternalSyncStatusV5::NoExternalTicket,
            }),
            Stage5Context::default(),
            AtomicMutationV5::JitDecision(JITDecisionMutationV5 {
                target_entity_id: "req_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: JITDecisionMutationKind::JITDecisionMutation,
                jit_decision: JITDecisionKindV5::Deny,
                approver_account_id: None,
                approved_ttl_s: 0,
                external_sync_status: JITExternalSyncStatusV5::NoExternalTicket,
            }),
            AtomicMutationV5::JitDecision(JITDecisionMutationV5 {
                target_entity_id: "req_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: JITDecisionMutationKind::JITDecisionMutation,
                jit_decision: JITDecisionKindV5::ApproveJit,
                approver_account_id: Some("acct_a".to_owned()),
                approved_ttl_s: 200,
                external_sync_status: JITExternalSyncStatusV5::SyncGranted,
            }),
            Stage5Context::default(),
        ),
        // 12. AGENT_DELEGATION_MUTATION
        (
            AtomicMutationV5::AgentDelegation(AgentDelegationMutationV5 {
                target_entity_id: "dlreq_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: AgentDelegationMutationKind::AgentDelegationMutation,
                delegation_decision: AgentDelegationDecisionKindV5::ApproveDelegation,
                hitl_cosigner_account_id: None,
                approved_ttl_s: 400,
            }),
            AtomicMutationV5::AgentDelegation(AgentDelegationMutationV5 {
                target_entity_id: "dlreq_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: AgentDelegationMutationKind::AgentDelegationMutation,
                delegation_decision: AgentDelegationDecisionKindV5::DenyDelegation,
                hitl_cosigner_account_id: None,
                approved_ttl_s: 0,
            }),
            Stage5Context::default(),
            AtomicMutationV5::AgentDelegation(AgentDelegationMutationV5 {
                target_entity_id: "dlreq_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: AgentDelegationMutationKind::AgentDelegationMutation,
                delegation_decision: AgentDelegationDecisionKindV5::DenyDelegation,
                hitl_cosigner_account_id: None,
                approved_ttl_s: 0,
            }),
            AtomicMutationV5::AgentDelegation(AgentDelegationMutationV5 {
                target_entity_id: "dlreq_x".to_owned(),
                if_match: base_if_match(),
                cite: base_cite(),
                mutation_kind: AgentDelegationMutationKind::AgentDelegationMutation,
                delegation_decision: AgentDelegationDecisionKindV5::ApproveDelegation,
                hitl_cosigner_account_id: None,
                approved_ttl_s: 200,
            }),
            Stage5Context::default(),
        ),
    ];

    assert_eq!(cases.len(), 12);
    for (og, g_og, og_ctx, av, g_av, av_ctx) in cases {
        assert_eq!(
            safety_partition(&og, &g_og, &og_ctx).unwrap(),
            (true, false)
        );
        assert_eq!(
            safety_partition(&av, &g_av, &av_ctx).unwrap(),
            (false, true)
        );
        let eq_ctx = if matches!(g_av, AtomicMutationV5::Optimization(_)) {
            opt_ctx(true, true)
        } else {
            av_ctx
        };
        assert_eq!(
            safety_partition(&g_av, &g_av, &eq_ctx).unwrap(),
            (false, false)
        );
    }
}
