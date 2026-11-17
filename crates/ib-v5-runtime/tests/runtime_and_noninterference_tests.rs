use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use ib_v5_runtime::{
    DiagnosticServiceV5, LedgerProjection, RuntimeOptionsV5, RuntimeV5, VerifierContextError,
    ViewV5, induce_phase2_world, ledger_from_projection, live_version, sim_jml_workflow,
};
use ib_v5_schemas::{
    AccountReconActionV5, AccountReconMutationKind, AccountReconMutationV5,
    ActiveDisabledStatusV5, AffiliationKindV5, AgentDelegationDecisionKindV5,
    AgentDelegationMutationKind, AgentDelegationMutationV5, AgentDelegationRequestV5,
    AgentRegistryActionV5, AgentRegistryMutationKind, AgentRegistryMutationV5, AtomicMutationV5,
    AuthorityTierV5, BirthrightRuleV5, CAEContainmentMutationKind, CAEContainmentMutationV5,
    CAEIncidentV5, CAETokenBindingKindV5, CAETokenKindV5, CAETokenV5, CAETriggerKindV5,
    CIEMPrincipalFindingV5, CIEMEscalationVectorV5, CIEMRemediationActionV5,
    CIEMTaskV5, CloudCIEMMutationKind, CloudCIEMMutationV5, CoSignReceiptStatusV5,
    CoSignScopeKindV5, CommitReceiptStatusV5, ConnectorHaltReasonV5,
    ConnectorProvisioningModeV5, ConnectorSchemaV5, ConnectorSyncModeV5,
    ConnectorSyncMutationKind, ConnectorSyncMutationV5, ConnectorTransportKindV5,
    ConnectorUnmappedPolicyDefaultV5, ContractorSponsorCheckModeV5, DeliveredSliceV5,
    DisconnectedTargetPolicyV5, EngagementStatusV5, EngagementV5, EnterpriseAgentV5,
    EpisodeV5, GrantModeV5, GrantV5, HRPersonV5, HRSourceMappingMutationKind,
    HRSourceMappingMutationV5, HRSourceMappingTaskV5, HumanSubkindV5, ITSMTicketActionKindV5,
    ITSMTicketActionV5, IdPAccountV5, JITDecisionKindV5, JITDecisionMutationKind,
    JITDecisionMutationV5, JITExternalSyncStatusV5, JITRequestChannelV5, JITRequestV5,
    JMLTraceEventKindV5, JMLTraceEventV5, JMLWorkflowMutationKind, JMLWorkflowMutationV5,
    JMLWorkflowSpecV5, JMLWorkflowTaskV5, JoinerContractorSponsorPolicyV5, JoinerStepV5,
    LeaverStepV5, LegalHoldDispositionV5, LegalHoldScopeModeV5, MoverStepV5,
    NHIGovernanceActionV5, NHIGovernanceMutationKind, NHIGovernanceMutationV5,
    NoShowDispositionV5, OptActionKindV5, OptCandidateActionV5, OptimizationMutationKind,
    OptimizationMutationV5, OptimizationPhaseKindV5, OptimizationTaskV5, OrphanCustodyModeV5,
    OrphanHandoffPolicyV5, PhaseManifestV5, PhaseWorkQueueV5, PolicyCardV5, PolicyOverlayV5,
    PrivilegeTierV5, QuerySourcesStatusV5, RoleV5, ScopeCatalogV5, ScopedSoDRuleV5,
    SliceDescriptorV5, SoDScopeModeV5, SpokeErrorPolicyV5, StageReceiptStatusV5,
    SteerProcessDirectiveV5, SteerReceiptStatusV5, SteerTriggerReasonV5, StrictV5Model,
    SuiteIdV5, TableRowV5, TargetNHIV5, UARCampaignMutationKind, UARCampaignMutationV5,
    UARCampaignV5, UARDecisionKindV5, UARItemV5, ViolationCategoryV5, WorldStateV5,
    encode_compact_table_v5,
};
use indexmap::IndexMap;

fn overlay() -> PolicyOverlayV5 {
    PolicyOverlayV5 {
        freshness_sla_s: 600,
        rotation_sla_s: 600,
        cae_sla_s: 120,
        conversion_grace_s: 600,
        break_glass_cooldown_s: 600,
        allow_contractor_nhi_owners: false,
        allow_contractor_agent_sponsors: false,
        enforce_cross_nhi_owner_sod: true,
        enforce_sponsor_agent_sod: true,
        permit_hitl_step_up_for_autonomous: true,
        require_immutable_hr_correlation: false,
        connector_unmapped_policy_default: ConnectorUnmappedPolicyDefaultV5::QuarantineDrift,
        uar_preserve_standing_floor_default: true,
        owner_excluded_dept_prefixes: Vec::new(),
        max_prod_nhis_per_owner: 4,
        max_prod_agents_per_sponsor: 4,
        max_agent_delegation_hops: 4,
        critical_jit_tiers: vec![PrivilegeTierV5::Critical],
        jit_ttl_cap_by_tier: vec![
            (PrivilegeTierV5::Read, 600),
            (PrivilegeTierV5::Standard, 600),
            (PrivilegeTierV5::Elevated, 600),
            (PrivilegeTierV5::Admin, 600),
            (PrivilegeTierV5::Critical, 600),
        ],
        forbidden_sod_rules: vec![ScopedSoDRuleV5 {
            rule_code: "sod_example".to_owned(),
            cap_a: "approve".to_owned(),
            cap_b: "pay".to_owned(),
            scope_mode: SoDScopeModeV5::SameLeafScope,
        }],
        temporal_sod_rules: Vec::new(),
    }
}

fn catalog() -> ScopeCatalogV5 {
    ScopeCatalogV5 {
        leaf_scopes: vec!["/org/a".to_owned(), "/org/b".to_owned()],
    }
}

fn policy_card() -> PolicyCardV5 {
    PolicyCardV5 {
        spec_version: "5.0".to_owned(),
        scope_catalog: catalog(),
        overlay: overlay(),
        rendered_rules_markdown: "policy".to_owned(),
    }
}

fn opt_task(task_id: &str, phase_kind: OptimizationPhaseKindV5) -> OptimizationTaskV5 {
    let candidate = if phase_kind == OptimizationPhaseKindV5::P1ZspRefactor {
        OptCandidateActionV5 {
            candidate_id: format!("cand_{task_id}"),
            action_kind: OptActionKindV5::ConvertGrantToEligible,
            target_person_id: Some("pers_1".to_owned()),
            target_grant_id: Some("grant_1".to_owned()),
            role_id: None,
            scope_path: None,
            candidate_account_id: None,
            covered_scopes: Vec::new(),
            action_cost: 1,
        }
    } else {
        OptCandidateActionV5 {
            candidate_id: format!("cand_{task_id}"),
            action_kind: OptActionKindV5::SelectReviewerAccount,
            target_person_id: None,
            target_grant_id: None,
            role_id: Some("role_1".to_owned()),
            scope_path: None,
            candidate_account_id: Some("acct_1".to_owned()),
            covered_scopes: vec!["/org/a".to_owned()],
            action_cost: 1,
        }
    };
    OptimizationTaskV5 {
        task_id: task_id.to_owned(),
        phase_kind,
        required_person_caps: Vec::new(),
        required_review_scopes: Vec::new(),
        excluded_conflict_person_ids: Vec::new(),
        max_assignments_per_person: 2,
        candidates: vec![candidate],
        etag: (if task_id == "opt_1" { "a" } else { "b" }).repeat(16),
        evidence_id: format!("ev_{task_id}"),
    }
}

fn hr_person(etag: &str, evidence_id: &str, status: EngagementStatusV5) -> HRPersonV5 {
    HRPersonV5 {
        person_id: "pers_1".to_owned(),
        emp_immutable_id: "emp_1".to_owned(),
        primary_email: "person@example.test".to_owned(),
        historical_emails: Vec::new(),
        engagements: vec![EngagementV5 {
            eng_id: "eng_1".to_owned(),
            affiliation: AffiliationKindV5::Fte,
            eng_status: status,
            dept_path: "/org/a".to_owned(),
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
        etag: etag.to_owned(),
        evidence_id: evidence_id.to_owned(),
    }
}

fn phase1_opt_mutation(task: &OptimizationTaskV5) -> AtomicMutationV5 {
    AtomicMutationV5::Optimization(OptimizationMutationV5 {
        target_entity_id: task.task_id.clone(),
        if_match: vec![format!("{}@{}:1", task.task_id, task.etag)],
        cite: vec![format!("OPT-V5-01:{}", task.evidence_id)],
        mutation_kind: OptimizationMutationKind::OptimizationMutation,
        selected_ids: Vec::new(),
        objective_value: 0,
    })
}

fn descriptor(slice_id: &str, table_names: Vec<String>, watermark_s: i64) -> SliceDescriptorV5 {
    SliceDescriptorV5 {
        slice_id: slice_id.to_owned(),
        source_system: "fixture".to_owned(),
        table_names,
        scope_filter: "global=*".to_owned(),
        authority_tier: AuthorityTierV5::AuthoritativePrimary,
        is_complete_for_scope: true,
        watermark_s,
        row_count: 1,
        credit_cost: 1,
    }
}

fn runtime_fixture() -> (RuntimeV5, OptimizationTaskV5, JITRequestV5) {
    let opt1 = opt_task("opt_1", OptimizationPhaseKindV5::P1ZspRefactor);
    let opt2 = opt_task("opt_2", OptimizationPhaseKindV5::P2ReviewerAssignment);
    let person = hr_person(&"1".repeat(16), "ev_person_old", EngagementStatusV5::Active);
    let account = IdPAccountV5 {
        account_id: "acct_1".to_owned(),
        subkind: HumanSubkindV5::HumanPrimary,
        login_email: "person@example.test".to_owned(),
        immutable_person_ref: Some("pers_1".to_owned()),
        bound_engagement_ref: Some("eng_1".to_owned()),
        status: ActiveDisabledStatusV5::Active,
        group_ids: Vec::new(),
        direct_grant_ids: vec!["grant_1".to_owned()],
        account_gen: 1,
        etag: "2".repeat(16),
        evidence_id: "ev_account".to_owned(),
    };
    let role = RoleV5 {
        role_id: "role_1".to_owned(),
        app_id: "app_1".to_owned(),
        privilege_tier: PrivilegeTierV5::Standard,
        capabilities: vec!["read".to_owned()],
        is_jit_capable_target: true,
        evidence_id: "ev_role".to_owned(),
    };
    let grant = GrantV5 {
        grant_id: "grant_1".to_owned(),
        account_id: "acct_1".to_owned(),
        role_id: "role_1".to_owned(),
        grant_mode: GrantModeV5::Standing,
        scope_path: "/org/a".to_owned(),
        sod_prune_priority: 1,
        evidence_id: "ev_grant".to_owned(),
    };
    let incident = CAEIncidentV5 {
        incident_id: "inc_1".to_owned(),
        person_id: "pers_1".to_owned(),
        trigger_kind: CAETriggerKindV5::BenignSessionRenewal,
        event_ts_s: 40,
        tokens: vec![CAETokenV5 {
            token_id: "tok_1".to_owned(),
            account_id: "acct_1".to_owned(),
            token_kind: CAETokenKindV5::Access,
            parent_token_id: None,
            binding_kind: CAETokenBindingKindV5::BoundDpop,
            cae_capable_spoke: true,
            issued_at_s: 1,
            token_exp_s: 90,
            revoke_ack_at_s: None,
            next_spoke_poll_sync_at_s: 60,
            agent_id: None,
            evidence_id: "ev_token".to_owned(),
        }],
        etag: "3".repeat(16),
        evidence_id: "ev_incident".to_owned(),
    };
    let campaign = UARCampaignV5 {
        campaign_id: "camp_1".to_owned(),
        app_id: "app_1".to_owned(),
        scope_path: "/org/a".to_owned(),
        admin_role_id: "role_1".to_owned(),
        m_standing: 0,
        m_eligible: 0,
        preserve_standing_floor_override: ib_v5_schemas::UARPreserveStandingFloorOverrideV5::UseOverlayDefault,
        backup_reviewer_account_ids: Vec::new(),
        candidate_custodian_account_ids: vec!["acct_1".to_owned()],
        items: vec![UARItemV5 {
            item_id: "item_1".to_owned(),
            account_id: "acct_1".to_owned(),
            grant_id: "grant_1".to_owned(),
            assigned_reviewer_account_id: "acct_1".to_owned(),
            used_within_lookback: true,
            evidence_id: "ev_item".to_owned(),
        }],
        etag: "4".repeat(16),
        evidence_id: "ev_campaign".to_owned(),
    };
    let jit_request = JITRequestV5 {
        request_id: "req_1".to_owned(),
        eval_seq: 1,
        requester_account_id: "acct_1".to_owned(),
        requested_role_id: "role_1".to_owned(),
        requested_scope_path: "/org/a".to_owned(),
        target_resource: "app_1".to_owned(),
        request_channel: JITRequestChannelV5::StandardTicket,
        change_ticket_id: None,
        proposed_approver_account_id: "acct_1".to_owned(),
        requested_ttl_s: 600,
        etag: "5".repeat(16),
        evidence_id: "ev_request_old".to_owned(),
    };
    let dl_request = AgentDelegationRequestV5 {
        request_id: "dlreq_1".to_owned(),
        eval_seq: 2,
        workflow_id: "workflow_1".to_owned(),
        agent_id: "agt_1".to_owned(),
        delegator_account_id: "acct_1".to_owned(),
        parent_delegation_request_id: None,
        delegation_hop: 1,
        requested_role_id: "role_1".to_owned(),
        requested_scope_path: "/org/a".to_owned(),
        target_resource: "app_1".to_owned(),
        proposed_hitl_approver_account_id: None,
        requested_ttl_s: 600,
        etag: "6".repeat(16),
        evidence_id: "ev_dlreq".to_owned(),
    };

    let q1 = PhaseWorkQueueV5 {
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
    };
    let q2 = PhaseWorkQueueV5 {
        target_connector_ids: Vec::new(),
        target_hr_mapping_ids: Vec::new(),
        target_human_account_ids: Vec::new(),
        target_nhi_ids: Vec::new(),
        target_agent_ids: Vec::new(),
        target_ciem_task_ids: Vec::new(),
        target_jml_workflow_ids: Vec::new(),
        target_opt_task_ids: vec!["opt_2".to_owned()],
        target_cae_incident_ids: vec!["inc_1".to_owned()],
        target_uar_campaign_ids: vec!["camp_1".to_owned()],
        target_jit_request_ids: vec!["req_1".to_owned()],
        target_agent_delegation_ids: vec!["dlreq_1".to_owned()],
    };

    let tables = vec![
        encode_compact_table_v5("optimization_tasks", &[TableRowV5::OptimizationTask(opt1.clone())]).unwrap(),
        encode_compact_table_v5("optimization_tasks", &[TableRowV5::OptimizationTask(opt2.clone())]).unwrap(),
        encode_compact_table_v5("hr_persons", &[TableRowV5::HrPerson(person.clone())]).unwrap(),
        encode_compact_table_v5("idp_accounts", &[TableRowV5::IdpAccount(account.clone())]).unwrap(),
        encode_compact_table_v5("roles", &[TableRowV5::Role(role.clone())]).unwrap(),
        encode_compact_table_v5("grants", &[TableRowV5::Grant(grant.clone())]).unwrap(),
        encode_compact_table_v5("cae_incidents", &[TableRowV5::CaeIncident(incident.clone())]).unwrap(),
        encode_compact_table_v5("uar_campaigns", &[TableRowV5::UarCampaign(campaign.clone())]).unwrap(),
        encode_compact_table_v5("jit_requests", &[TableRowV5::JitRequest(jit_request.clone())]).unwrap(),
        encode_compact_table_v5("agent_delegation_requests", &[TableRowV5::AgentDelegationRequest(dl_request.clone())]).unwrap(),
    ];
    let p1_table_names: Vec<String> = tables.iter().map(|t| t.table_name.clone()).collect();
    let phase1_payload = DeliveredSliceV5 {
        slice_id: "sl_p1".to_owned(),
        authority_tier: AuthorityTierV5::SecondaryReplica,
        watermark_s: 50,
        tables,
    };
    let fresh_person = hr_person(&"7".repeat(16), "ev_person_new", EngagementStatusV5::Terminated);
    let mut fresh_request = jit_request.clone();
    fresh_request.etag = "8".repeat(16);
    fresh_request.evidence_id = "ev_request_new".to_owned();
    let phase2_payload = DeliveredSliceV5 {
        slice_id: "sl_p2_delta".to_owned(),
        authority_tier: AuthorityTierV5::AuthoritativePrimary,
        watermark_s: 100,
        tables: vec![
            encode_compact_table_v5("hr_persons", &[TableRowV5::HrPerson(fresh_person)]).unwrap(),
            encode_compact_table_v5("jit_requests", &[TableRowV5::JitRequest(fresh_request)]).unwrap(),
        ],
    };

    let mut p1_slices = IndexMap::new();
    p1_slices.insert("sl_p1".to_owned(), phase1_payload);
    let mut p2_slices = IndexMap::new();
    p2_slices.insert("sl_p2_delta".to_owned(), phase2_payload);

    let episode = EpisodeV5 {
        episode_id: "v5_dev_f01_r1_s01".to_owned(),
        suite_id: SuiteIdV5::V5Dev,
        family_id: "F01".to_owned(),
        policy_card: policy_card(),
        phase1_manifest: PhaseManifestV5 {
            phase_index: 1,
            clock_epoch_s: 50,
            query_budget: 2,
            approval_gated: false,
            work_queue: q1,
            source_directory: vec![descriptor("sl_p1", p1_table_names, 50)],
            invalidated_entity_ids: Vec::new(),
            prior_phase_ledger: None,
        },
        phase1_slices: p1_slices,
        phase2_base_manifest: PhaseManifestV5 {
            phase_index: 2,
            clock_epoch_s: 100,
            query_budget: 2,
            approval_gated: false,
            work_queue: q2,
            source_directory: vec![descriptor(
                "sl_p2_delta",
                vec!["hr_persons".to_owned(), "jit_requests".to_owned()],
                100,
            )],
            invalidated_entity_ids: Vec::new(),
            prior_phase_ledger: None,
        },
        phase2_slices: p2_slices,
        phase2_delta_person_ids: vec!["pers_1".to_owned()],
    };

    let mut hr_persons = IndexMap::new();
    hr_persons.insert("pers_1".to_owned(), person);
    let mut roles = IndexMap::new();
    roles.insert("role_1".to_owned(), role);
    let mut grants = IndexMap::new();
    grants.insert("grant_1".to_owned(), grant);
    let mut idp_accounts = IndexMap::new();
    idp_accounts.insert("acct_1".to_owned(), account);
    let mut opt_tasks = IndexMap::new();
    opt_tasks.insert("opt_1".to_owned(), opt1.clone());
    opt_tasks.insert("opt_2".to_owned(), opt2);
    let mut cae_incidents = IndexMap::new();
    cae_incidents.insert("inc_1".to_owned(), incident);
    let mut uar_campaigns = IndexMap::new();
    uar_campaigns.insert("camp_1".to_owned(), campaign);
    let mut jit_requests = IndexMap::new();
    jit_requests.insert("req_1".to_owned(), jit_request.clone());
    let mut agent_delegation_requests = IndexMap::new();
    agent_delegation_requests.insert("dlreq_1".to_owned(), dl_request);

    let world = WorldStateV5 {
        phase_index: 1,
        world_rev: 1,
        clock_epoch_s: 50,
        scope_catalog: catalog(),
        overlay: overlay(),
        hr_persons,
        roles,
        grants,
        groups: IndexMap::new(),
        idp_accounts,
        transfer_bridges: IndexMap::new(),
        compensating_controls: IndexMap::new(),
        connector_schemas: IndexMap::new(),
        connector_pages: Vec::new(),
        hr_source_mappings: IndexMap::new(),
        baseline_nhis: IndexMap::new(),
        target_nhis: IndexMap::new(),
        baseline_agents: IndexMap::new(),
        enterprise_agents: IndexMap::new(),
        cloud_ciem_tasks: IndexMap::new(),
        optimization_tasks: opt_tasks,
        jml_workflow_tasks: IndexMap::new(),
        cae_incidents,
        uar_campaigns,
        change_tickets: IndexMap::new(),
        historical_sessions: Vec::new(),
        jit_requests,
        agent_delegation_requests,
        prior_phase_ledger: None,
    };

    (RuntimeV5::new(episode, world).unwrap(), opt1, jit_request)
}

fn phase2_mutations(runtime: &RuntimeV5) -> Vec<AtomicMutationV5> {
    let ver = |id: &str| vec![live_version(id, runtime.view()).unwrap()];
    vec![
        AtomicMutationV5::CaeContainment(CAEContainmentMutationV5 {
            target_entity_id: "inc_1".to_owned(),
            if_match: ver("inc_1"),
            cite: vec!["CAE-V5-01:ev_incident".to_owned()],
            mutation_kind: CAEContainmentMutationKind::CAEContainmentMutation,
            cae_revoke_sessions: false,
            cae_computed_exposure_s: 0,
            cae_sla_breached: false,
            retro_invalidated_ids: Vec::new(),
        }),
        AtomicMutationV5::UarCampaign(UARCampaignMutationV5 {
            target_entity_id: "camp_1".to_owned(),
            if_match: ver("camp_1"),
            cite: vec!["UAR-V5-01:ev_campaign".to_owned()],
            mutation_kind: UARCampaignMutationKind::UARCampaignMutation,
            uar_decisions: vec![("item_1".to_owned(), UARDecisionKindV5::Certify)],
            reviewer_overrides: Vec::new(),
            emergency_custodian_account_id: Some("acct_1".to_owned()),
        }),
        AtomicMutationV5::JitDecision(JITDecisionMutationV5 {
            target_entity_id: "req_1".to_owned(),
            if_match: ver("req_1"),
            cite: vec!["JIT-V5-01.P1:ev_request_old".to_owned()],
            mutation_kind: JITDecisionMutationKind::JITDecisionMutation,
            jit_decision: JITDecisionKindV5::ApproveJitCosign,
            approver_account_id: Some("acct_1".to_owned()),
            approved_ttl_s: 60,
            external_sync_status: JITExternalSyncStatusV5::SyncGrantedCosign,
        }),
        AtomicMutationV5::AgentDelegation(AgentDelegationMutationV5 {
            target_entity_id: "dlreq_1".to_owned(),
            if_match: ver("dlreq_1"),
            cite: vec!["AGENT-DEL-V5-01.D1:ev_dlreq".to_owned()],
            mutation_kind: AgentDelegationMutationKind::AgentDelegationMutation,
            delegation_decision: AgentDelegationDecisionKindV5::DenyDelegation,
            hitl_cosigner_account_id: None,
            approved_ttl_s: 0,
        }),
        AtomicMutationV5::Optimization(OptimizationMutationV5 {
            target_entity_id: "opt_2".to_owned(),
            if_match: ver("opt_2"),
            cite: vec!["OPT-V5-02:ev_opt_2".to_owned()],
            mutation_kind: OptimizationMutationKind::OptimizationMutation,
            selected_ids: Vec::new(),
            objective_value: 0,
        }),
    ]
}

fn cosign_both(runtime: &mut RuntimeV5, stage_id: &str) {
    assert_eq!(
        runtime
            .request_cosign(2, stage_id, CoSignScopeKindV5::UarCustodian, "camp_1", "acct_1")
            .status,
        CoSignReceiptStatusV5::CoSigned
    );
    assert_eq!(
        runtime
            .request_cosign(2, stage_id, CoSignScopeKindV5::CriticalJit, "req_1", "acct_1")
            .status,
        CoSignReceiptStatusV5::CoSigned
    );
}

#[test]
fn non_account_outcome_etags_match_independent_phase_one_preimages() {
    let base_if = |id: &str| vec![format!("{id}@{}:1", "a".repeat(16))];
    let base_c = || vec!["OPT-V5-01:ev_etag_fixture".to_owned()];

    let nhi = AtomicMutationV5::NhiGovernance(NHIGovernanceMutationV5 {
        target_entity_id: "nhi_fixture".to_owned(),
        if_match: base_if("nhi_fixture"),
        cite: base_c(),
        mutation_kind: NHIGovernanceMutationKind::NHIGovernanceMutation,
        nhi_action: NHIGovernanceActionV5::DisableOrphan,
        resolved_owner_person_id: None,
        owner_resolution_source: None,
    });
    let agent = AtomicMutationV5::AgentRegistry(AgentRegistryMutationV5 {
        target_entity_id: "agt_fixture".to_owned(),
        if_match: base_if("agt_fixture"),
        cite: base_c(),
        mutation_kind: AgentRegistryMutationKind::AgentRegistryMutation,
        agent_action: AgentRegistryActionV5::RegisterActive,
        resolved_sponsor_person_id: Some("pers_sponsor".to_owned()),
        active_autonomous_binding_ids: vec!["atg_a".to_owned(), "atg_b".to_owned()],
        active_hitl_binding_ids: vec!["atg_review".to_owned()],
    });
    let connector = AtomicMutationV5::ConnectorSync(ConnectorSyncMutationV5 {
        target_entity_id: "conn_active".to_owned(),
        if_match: base_if("conn_active"),
        cite: base_c(),
        mutation_kind: ConnectorSyncMutationKind::ConnectorSyncMutation,
        halted_fail_closed: false,
        halt_reason: ConnectorHaltReasonV5::None,
        reconciled_account_ids: vec!["acct_01".to_owned(), "acct_02".to_owned()],
        remediated_spoke_drift: vec!["acct_01:role_a".to_owned(), "acct_02:role_b".to_owned()],
        itsm_ticket_actions: vec![ITSMTicketActionV5 {
            drift_token: "acct_01:role_a".to_owned(),
            ticket_action: ITSMTicketActionKindV5::AwaitNextAggregation,
            assignee_account_id: None,
        }],
    });
    let halted_connector = AtomicMutationV5::ConnectorSync(ConnectorSyncMutationV5 {
        target_entity_id: "conn_halted".to_owned(),
        if_match: base_if("conn_halted"),
        cite: base_c(),
        mutation_kind: ConnectorSyncMutationKind::ConnectorSyncMutation,
        halted_fail_closed: true,
        halt_reason: ConnectorHaltReasonV5::HaltAuthOrTls,
        reconciled_account_ids: vec!["acct_03".to_owned()],
        remediated_spoke_drift: Vec::new(),
        itsm_ticket_actions: Vec::new(),
    });
    let hr_mapping = AtomicMutationV5::HrSourceMapping(HRSourceMappingMutationV5 {
        target_entity_id: "hrmap_fixture".to_owned(),
        if_match: base_if("hrmap_fixture"),
        cite: base_c(),
        mutation_kind: HRSourceMappingMutationKind::HRSourceMappingMutation,
        accepted_feed_ids: vec!["hfeed_a".to_owned(), "hfeed_b".to_owned()],
        rejected_feed_ids: Vec::new(),
        resolved_person_statuses: vec![
            ("pers_a".to_owned(), EngagementStatusV5::Active),
            ("pers_b".to_owned(), EngagementStatusV5::Terminated),
        ],
        quarantined_assertion_keys: Vec::new(),
    });
    let ciem = AtomicMutationV5::CloudCiem(CloudCIEMMutationV5 {
        target_entity_id: "ciem_fixture".to_owned(),
        if_match: base_if("ciem_fixture"),
        cite: base_c(),
        mutation_kind: CloudCIEMMutationKind::CloudCIEMMutation,
        findings: vec![CIEMPrincipalFindingV5 {
            cprin_id: "cprin_aws_root".to_owned(),
            effective_forbidden_actions: vec![
                "iam:CreateRole".to_owned(),
                "iam:PassRole".to_owned(),
            ],
            escalation_vector: CIEMEscalationVectorV5::AwsPassroleExec,
            remediation_action: CIEMRemediationActionV5::AttachBoundaryOrDeny,
        }],
    });
    let wfl_spec = JMLWorkflowSpecV5 {
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
    };
    let workflow = AtomicMutationV5::JmlWorkflow(JMLWorkflowMutationV5 {
        target_entity_id: "wfl_fixture".to_owned(),
        if_match: base_if("wfl_fixture"),
        cite: base_c(),
        mutation_kind: JMLWorkflowMutationKind::JMLWorkflowMutation,
        active_birthright_rule_ids: vec!["brule_01".to_owned(), "brule_02".to_owned()],
        converted_eligible_brule_ids: vec!["brule_03".to_owned()],
        quarantined_birthright_rule_ids: vec!["brule_04".to_owned(), "brule_05".to_owned()],
        authored_spec: wfl_spec.clone(),
    });

    let nhi_rec = TargetNHIV5 {
        nhi_id: "nhi_fixture".to_owned(),
        subkind: ib_v5_schemas::NHISubkindV5::ServiceAccount,
        environment: ib_v5_schemas::EnvironmentKindV5::Prod,
        capabilities: vec!["read".to_owned()],
        scope_path: "/*".to_owned(),
        signals: Vec::new(),
        nhi_gen: 2,
        etag: "d".repeat(16),
        evidence_id: "ev_nhi".to_owned(),
    };
    let agt_rec = EnterpriseAgentV5 {
        agent_id: "agt_fixture".to_owned(),
        environment: ib_v5_schemas::EnvironmentKindV5::Prod,
        manifest_digest_etag: "e".repeat(16),
        approved_registry_digest_etag: "e".repeat(16),
        attestation_synced_at_s: 1,
        candidate_sponsor_person_id: "pers_sponsor".to_owned(),
        tool_bindings: Vec::new(),
        agent_gen: 5,
        etag: "e".repeat(16),
        evidence_id: "ev_agt".to_owned(),
    };
    let conn_active_rec = ConnectorSchemaV5 {
        connector_id: "conn_active".to_owned(),
        app_id: "app_1".to_owned(),
        transport_kind: ConnectorTransportKindV5::RestWebService,
        provisioning_mode: ConnectorProvisioningModeV5::AutomatedWrite,
        sync_mode: ConnectorSyncModeV5::FullSnapshot,
        auth_state: ib_v5_schemas::ConnectorAuthStateV5::AuthOk,
        has_delete_audit_view: true,
        max_delete_threshold_bps: 1000,
        expected_file_row_count: None,
        required_fields: vec!["account_id".to_owned()],
        mapping_rules: Vec::new(),
        unmapped_policy_override: ib_v5_schemas::UnmappedPolicyOverrideV5::UseOverlayDefault,
        local_checkpoint_etag: None,
        baseline_reconciled_account_ids: Vec::new(),
        audit_watermark_s: 1,
        itsm_bridge_healthy: true,
        itsm_primary_fulfiller_account_id: None,
        itsm_backup_fulfiller_account_id: None,
        open_itsm_tickets: Vec::new(),
        sync_gen: 4,
        etag: "b".repeat(16),
        evidence_id: "ev_ca".to_owned(),
    };
    let mut conn_halted_rec = conn_active_rec.clone();
    conn_halted_rec.connector_id = "conn_halted".to_owned();
    conn_halted_rec.sync_gen = 8;
    conn_halted_rec.etag = "c".repeat(16);

    let hrmap_rec = HRSourceMappingTaskV5 {
        mapping_task_id: "hrmap_fixture".to_owned(),
        feeds: Vec::new(),
        mapping_gen: 3,
        etag: "f".repeat(16),
        evidence_id: "ev_hm".to_owned(),
    };
    let ciem_rec = CIEMTaskV5 {
        ciem_task_id: "ciem_fixture".to_owned(),
        forbidden_standing_cloud_actions: vec!["iam:PassRole".to_owned()],
        principals: Vec::new(),
        ciem_gen: 6,
        etag: "1".repeat(16),
        evidence_id: "ev_cm".to_owned(),
    };
    let wfl_rec = JMLWorkflowTaskV5 {
        workflow_task_id: "wfl_fixture".to_owned(),
        legal_hold_scope_mode: LegalHoldScopeModeV5::SuspendPreserveArchive,
        orphan_custody_mode: OrphanCustodyModeV5::HandoffToManagerElseSuspend,
        has_disconnected_spokes: true,
        contractor_sponsor_check_mode: ContractorSponsorCheckModeV5::StrictPreProvision,
        birthright_catalog: Vec::new(),
        visible_trace_events: Vec::new(),
        draft_spec: wfl_spec,
        workflow_gen: 7,
        etag: "2".repeat(16),
        evidence_id: "ev_wf".to_owned(),
    };

    let projection = LedgerProjection {
        idp_accounts: BTreeMap::new(),
        grants: BTreeMap::new(),
        target_nhis: [("nhi_fixture", &nhi_rec)].into_iter().collect(),
        enterprise_agents: [("agt_fixture", &agt_rec)].into_iter().collect(),
        connector_schemas: [
            ("conn_active", &conn_active_rec),
            ("conn_halted", &conn_halted_rec),
        ]
        .into_iter()
        .collect(),
        hr_source_mappings: [("hrmap_fixture", &hrmap_rec)].into_iter().collect(),
        cloud_ciem_tasks: [("ciem_fixture", &ciem_rec)].into_iter().collect(),
        jml_workflow_tasks: [("wfl_fixture", &wfl_rec)].into_iter().collect(),
    };
    let work_queue = PhaseWorkQueueV5 {
        target_connector_ids: vec!["conn_active".to_owned(), "conn_halted".to_owned()],
        target_hr_mapping_ids: vec!["hrmap_fixture".to_owned()],
        target_human_account_ids: Vec::new(),
        target_nhi_ids: vec!["nhi_fixture".to_owned()],
        target_agent_ids: vec!["agt_fixture".to_owned()],
        target_ciem_task_ids: vec!["ciem_fixture".to_owned()],
        target_jml_workflow_ids: vec!["wfl_fixture".to_owned()],
        target_opt_task_ids: vec!["opt_fixture".to_owned()],
        target_cae_incident_ids: Vec::new(),
        target_uar_campaign_ids: Vec::new(),
        target_jit_request_ids: Vec::new(),
        target_agent_delegation_ids: Vec::new(),
    };

    let ledger = ledger_from_projection(
        &projection,
        &[nhi, agent, connector, halted_connector, hr_mapping, ciem, workflow],
        &work_queue,
        false,
    )
    .unwrap();
    let halted_outcome = ledger
        .connector_outcomes
        .iter()
        .find(|o| o.connector_id == "conn_halted")
        .unwrap();
    assert_eq!(halted_outcome.current_sync_gen, 8);
    assert_eq!(halted_outcome.current_etag, "c".repeat(16));
    assert_eq!(ledger.nhi_outcomes[0].current_nhi_gen, 3);
    assert_eq!(ledger.agent_outcomes[0].current_agent_gen, 6);
    assert_eq!(ledger.hr_mapping_outcomes[0].current_mapping_gen, 4);
    assert_eq!(ledger.ciem_outcomes[0].current_ciem_gen, 7);
    assert_eq!(ledger.workflow_outcomes[0].current_workflow_gen, 8);
}

#[test]
fn delivered_view_is_order_independent_and_unknown_versions_are_total() {
    let (mut runtime, mut opt1, _req) = runtime_fixture();
    runtime.query_sources(1, &["sl_p1".to_owned()]).unwrap();
    let low = runtime.episode().phase1_slices["sl_p1"].clone();
    opt1.etag = "9".repeat(16);
    let high_table = encode_compact_table_v5(
        "optimization_tasks",
        &[TableRowV5::OptimizationTask(opt1)],
    )
    .unwrap();
    let high = DeliveredSliceV5 {
        slice_id: "sl_z_primary".to_owned(),
        authority_tier: AuthorityTierV5::AuthoritativePrimary,
        watermark_s: 99,
        tables: vec![high_table],
    };
    let first = ViewV5::from_slices(
        runtime.manifest(),
        &[low.clone(), high.clone()],
        &runtime.episode().policy_card,
        0,
        None,
        &[],
    )
    .unwrap();
    let second = ViewV5::from_slices(
        runtime.manifest(),
        &[high, low],
        &runtime.episode().policy_card,
        0,
        None,
        &[],
    )
    .unwrap();
    assert_eq!(
        first.resolve_optimization_task("opt_1").unwrap().etag,
        "9".repeat(16)
    );
    assert_eq!(
        first.resolve_optimization_task("opt_1"),
        second.resolve_optimization_task("opt_1")
    );
    assert_eq!(live_version("role_missing", &first), None);
    assert_eq!(live_version("unknown_prefix_1", &first), None);
}

#[test]
fn query_deduplicates_and_rejected_queries_do_not_spend_budget() {
    let (mut runtime, _opt, _req) = runtime_fixture();
    let start = runtime.remaining_query_budget();
    let bad = runtime.query_sources(1, &["missing".to_owned()]).unwrap();
    assert_eq!(bad.status, QuerySourcesStatusV5::RejectedUnknownSlice);
    assert_eq!(runtime.remaining_query_budget(), start);

    let empty = runtime.query_sources(1, &[]).unwrap();
    assert_eq!(empty.status, QuerySourcesStatusV5::RejectedUnknownSlice);
    assert_eq!(runtime.remaining_query_budget(), start);

    let ok = runtime
        .query_sources(1, &["sl_p1".to_owned(), "sl_p1".to_owned()])
        .unwrap();
    assert_eq!(ok.status, QuerySourcesStatusV5::Ok);
    assert_eq!(ok.slices.len(), 1);
    assert_eq!(ok.slices[0].slice_id, "sl_p1");
    assert_eq!(runtime.remaining_query_budget(), start - 1);

    let repeated = runtime.query_sources(1, &["sl_p1".to_owned()]).unwrap();
    assert_eq!(repeated.status, QuerySourcesStatusV5::Ok);
    assert!(repeated.slices.is_empty());
    assert_eq!(repeated.already_delivered_ids, vec!["sl_p1".to_owned()]);
}

#[test]
fn runtime_rejects_unknown_topology_and_steering_configuration() {
    let (runtime, _opt, _req) = runtime_fixture();
    assert!(
        RuntimeV5::with_options(
            runtime.episode().clone(),
            runtime.world().clone(),
            RuntimeOptionsV5 {
                topology: "T9_UNKNOWN".to_owned(),
                ..Default::default()
            }
        )
        .is_err()
    );
    assert!(
        RuntimeV5::with_options(
            runtime.episode().clone(),
            runtime.world().clone(),
            RuntimeOptionsV5 {
                steering_mode: "ALWAYS_ON".to_owned(),
                ..Default::default()
            }
        )
        .is_err()
    );
}

#[test]
fn stage_handle_fullmatch_and_rejection_preserve_prior_staged_buffer() {
    let (mut runtime, opt1, _req) = runtime_fixture();
    runtime.query_sources(1, &["sl_p1".to_owned()]).unwrap();
    let mutation = phase1_opt_mutation(&opt1);
    let accepted = runtime
        .stage_phase_mutations(1, "stg_one", std::slice::from_ref(&mutation))
        .unwrap();
    assert_eq!(accepted.status, StageReceiptStatusV5::Staged);
    let before = runtime.active_stage_digest().to_owned();

    let rejected = runtime
        .stage_phase_mutations(1, "stg_one\n", std::slice::from_ref(&mutation))
        .unwrap();
    assert_eq!(rejected.status, StageReceiptStatusV5::RejectedStructural);
    assert_eq!(rejected.stage_id, "stg_one\n");
    assert!(rejected.stage_digest.is_empty());
    assert!(rejected.dependency_digest.is_empty());
    assert_eq!(runtime.active_stage_digest(), before);

    let snaps = runtime.phase_snapshots(1).unwrap();
    assert!(snaps.first_stage.is_some() && snaps.last_stage.is_some());
    assert_eq!(snaps.stage_attempts.len(), 2);
    assert_eq!(snaps.stage_attempts[0].receipt_status, "STAGED");
    assert_eq!(snaps.stage_attempts[1].receipt_status, "REJECTED_STRUCTURAL");
    assert_eq!(snaps.first_stage.as_ref().unwrap(), &snaps.stage_attempts[0]);
    assert_eq!(snaps.last_stage.as_ref().unwrap(), &snaps.stage_attempts[0]);
}

#[test]
fn induce_phase2_world_matches_runtime_coupled_transition() {
    let (mut runtime, opt1, _req) = runtime_fixture();
    let initial_world = runtime.world().clone();
    let p1_muts = vec![phase1_opt_mutation(&opt1)];
    let induced = induce_phase2_world(
        &initial_world,
        &p1_muts,
        &runtime.episode().phase1_manifest.work_queue,
        runtime.episode(),
        None,
    )
    .unwrap();

    runtime.query_sources(1, &["sl_p1".to_owned()]).unwrap();
    assert_eq!(
        runtime
            .stage_phase_mutations(1, "stg_induce", &p1_muts)
            .unwrap()
            .status,
        StageReceiptStatusV5::Staged
    );
    assert_eq!(
        runtime.commit_phase_mutations(1, "stg_induce").unwrap().status,
        CommitReceiptStatusV5::CommittedAndAdvanced
    );
    assert_eq!(*runtime.world(), induced);
    assert_eq!(
        induced.clock_epoch_s,
        runtime.episode().phase2_base_manifest.clock_epoch_s
    );
    assert_eq!(induced.hr_persons["pers_1"].evidence_id, "ev_person_new");
}

#[test]
fn steer_queries_missing_gold_evidence_when_submitted_cites_were_read() {
    struct GoldUnreadDiagnostics;
    impl DiagnosticServiceV5 for GoldUnreadDiagnostics {
        fn diagnose(
            &self,
            _phase_index: i64,
            _stage_id: &str,
            _view: &ViewV5,
            _mutations: &[AtomicMutationV5],
            _preconditions: &[String],
            _citations: &[String],
            _read_ledger: &BTreeSet<String>,
            _stage1_ok: bool,
            _stage2_ok: bool,
        ) -> Result<(Option<usize>, Vec<ViolationCategoryV5>), VerifierContextError> {
            Ok((Some(4), vec![ViolationCategoryV5::CitationMismatch]))
        }

        fn has_unread_gold_evidence(
            &self,
            _phase_index: i64,
            _view: &ViewV5,
            read_ledger: &BTreeSet<String>,
        ) -> Result<bool, VerifierContextError> {
            assert!(read_ledger.contains("ev_opt_1"));
            Ok(true)
        }
    }

    let (base_runtime, opt1, _req) = runtime_fixture();
    let mut runtime = RuntimeV5::with_options(
        base_runtime.episode().clone(),
        base_runtime.world().clone(),
        RuntimeOptionsV5 {
            steering_mode: "CHECKPOINT_STEERED".to_owned(),
            diagnostic_service: Some(Arc::new(GoldUnreadDiagnostics)),
            ..Default::default()
        },
    )
    .unwrap();
    runtime.query_sources(1, &["sl_p1".to_owned()]).unwrap();
    let mut1 = phase1_opt_mutation(&opt1);
    assert_eq!(
        runtime
            .stage_phase_mutations(1, "stg_unread_gold", &[mut1])
            .unwrap()
            .status,
        StageReceiptStatusV5::Staged
    );
    let receipt = runtime
        .request_team_steer(1, "stg_unread_gold", SteerTriggerReasonV5::InitialCheckpoint)
        .unwrap();
    assert_eq!(receipt.status, SteerReceiptStatusV5::CorrectiveRedirect);
    assert_eq!(
        receipt.process_directive,
        Some(SteerProcessDirectiveV5::QueryMissingSlices)
    );
}

#[test]
fn gate3_burns_extra_mints_and_occ_failure_requires_fresh_cosign_and_stage() {
    let (mut runtime, opt1, _req) = runtime_fixture();
    runtime.query_sources(1, &["sl_p1".to_owned()]).unwrap();
    let p1_stage = runtime
        .stage_phase_mutations(1, "stg_p1", &[phase1_opt_mutation(&opt1)])
        .unwrap();
    assert_eq!(p1_stage.status, StageReceiptStatusV5::Staged);
    assert_eq!(
        runtime.commit_phase_mutations(1, "stg_p1").unwrap().status,
        CommitReceiptStatusV5::CommittedAndAdvanced
    );
    assert!(runtime.phase2_opened());
    assert_eq!(runtime.world().clock_epoch_s, 100);
    assert_eq!(runtime.world().hr_persons["pers_1"].etag, "7".repeat(16));

    let muts = phase2_mutations(&runtime);
    assert_eq!(
        runtime
            .stage_phase_mutations(2, "stg_p2", &muts)
            .unwrap()
            .status,
        StageReceiptStatusV5::Staged
    );
    assert_eq!(
        runtime.commit_phase_mutations(2, "stg_p2").unwrap().status,
        CommitReceiptStatusV5::CommitBlockedToken
    );

    cosign_both(&mut runtime, "stg_p2");
    runtime.inject_minted_cosign_for_test((
        runtime.episode().episode_id.clone(),
        2,
        "stg_p2".to_owned(),
        runtime.active_stage_digest().to_owned(),
        (
            CoSignScopeKindV5::CriticalJit,
            "req_extra".to_owned(),
            "acct_1".to_owned(),
        ),
    ));
    let extra_block = runtime.commit_phase_mutations(2, "stg_p2").unwrap();
    assert_eq!(extra_block.status, CommitReceiptStatusV5::CommitBlockedToken);
    assert!(!runtime.minted_cosigns().iter().any(|e| e.2 == "stg_p2"));

    assert_eq!(
        runtime
            .request_cosign(2, "stg_p2", CoSignScopeKindV5::UarCustodian, "camp_1", "acct_1")
            .status,
        CoSignReceiptStatusV5::RejectedStructural
    );
    assert_eq!(
        runtime
            .request_cosign(2, "stg_p2", CoSignScopeKindV5::CriticalJit, "req_1", "acct_1")
            .status,
        CoSignReceiptStatusV5::RejectedStructural
    );

    assert_eq!(
        runtime
            .stage_phase_mutations(2, "stg_p2_fresh", &muts)
            .unwrap()
            .status,
        StageReceiptStatusV5::Staged
    );
    cosign_both(&mut runtime, "stg_p2_fresh");
    let occ_block = runtime.query_sources(2, &["sl_p2_delta".to_owned()]).unwrap();
    assert_eq!(occ_block.status, QuerySourcesStatusV5::Ok);
    assert_eq!(
        runtime.view().resolve_hr_person("pers_1").unwrap().evidence_id,
        "ev_person_new"
    );
    assert!(!runtime.read_ledger().contains("ev_person_old"));
    assert!(runtime.read_ledger().contains("ev_person_new"));
    assert_eq!(
        runtime
            .commit_phase_mutations(2, "stg_p2_fresh")
            .unwrap()
            .status,
        CommitReceiptStatusV5::CommitBlockedPrecondition
    );
    assert!(!runtime.minted_cosigns().iter().any(|e| e.2 == "stg_p2_fresh"));
    assert_eq!(
        runtime
            .request_cosign(2, "stg_p2_fresh", CoSignScopeKindV5::UarCustodian, "camp_1", "acct_1")
            .status,
        CoSignReceiptStatusV5::RejectedStructural
    );
    assert_eq!(
        runtime
            .request_cosign(2, "stg_p2_fresh", CoSignScopeKindV5::CriticalJit, "req_1", "acct_1")
            .status,
        CoSignReceiptStatusV5::RejectedStructural
    );
    assert_eq!(
        runtime
            .commit_phase_mutations(2, "stg_p2_fresh")
            .unwrap()
            .status,
        CommitReceiptStatusV5::CommitBlockedToken
    );

    let new_muts: Vec<AtomicMutationV5> = muts
        .into_iter()
        .map(|mut m| {
            let new_ver = vec![live_version(m.target_entity_id(), runtime.view()).unwrap()];
            match &mut m {
                AtomicMutationV5::CaeContainment(x) => x.if_match = new_ver,
                AtomicMutationV5::UarCampaign(x) => x.if_match = new_ver,
                AtomicMutationV5::JitDecision(x) => x.if_match = new_ver,
                AtomicMutationV5::AgentDelegation(x) => x.if_match = new_ver,
                AtomicMutationV5::Optimization(x) => x.if_match = new_ver,
                _ => {}
            }
            m
        })
        .collect();
    assert_eq!(
        runtime
            .stage_phase_mutations(2, "stg_p2_retry", &new_muts)
            .unwrap()
            .status,
        StageReceiptStatusV5::Staged
    );
    cosign_both(&mut runtime, "stg_p2_retry");
    let committed = runtime.commit_phase_mutations(2, "stg_p2_retry").unwrap();
    assert_eq!(committed.status, CommitReceiptStatusV5::EpisodeCompleted);
    assert!(runtime.episode_completed());
    assert!(runtime.phase_snapshots(2).unwrap().commit.is_some());
}

#[test]
fn jml_workflow_dag_checks_order_and_task_parameterized_policies() {
    let task = JMLWorkflowTaskV5 {
        workflow_task_id: "wfl_1".to_owned(),
        legal_hold_scope_mode: LegalHoldScopeModeV5::SuspendPreserveArchive,
        orphan_custody_mode: OrphanCustodyModeV5::HandoffToManagerElseSuspend,
        has_disconnected_spokes: true,
        contractor_sponsor_check_mode: ContractorSponsorCheckModeV5::StrictPreProvision,
        birthright_catalog: vec![BirthrightRuleV5 {
            rule_id: "brule_0001".to_owned(),
            dept_prefix: "/org".to_owned(),
            allowed_affiliations: vec![AffiliationKindV5::Fte, AffiliationKindV5::Contractor],
            role_id: "role_1".to_owned(),
            grant_mode: GrantModeV5::Standing,
            scope_path: "/org/a".to_owned(),
            evidence_id: "ev_br1".to_owned(),
        }],
        visible_trace_events: vec![
            JMLTraceEventV5 {
                event_id: "jev_1".to_owned(),
                event_kind: JMLTraceEventKindV5::Joiner,
                affiliation: AffiliationKindV5::Contractor,
                under_legal_hold: false,
                owns_prod_nhi_or_agent: false,
                has_active_tokens: true,
                spoke_transient_error: false,
                target_is_disconnected_itsm: false,
                evidence_id: "ev_jev1".to_owned(),
            },
            JMLTraceEventV5 {
                event_id: "jev_2".to_owned(),
                event_kind: JMLTraceEventKindV5::NoShowCancel,
                affiliation: AffiliationKindV5::Fte,
                under_legal_hold: false,
                owns_prod_nhi_or_agent: false,
                has_active_tokens: true,
                spoke_transient_error: false,
                target_is_disconnected_itsm: false,
                evidence_id: "ev_jev2".to_owned(),
            },
            JMLTraceEventV5 {
                event_id: "jev_3".to_owned(),
                event_kind: JMLTraceEventKindV5::Leaver,
                affiliation: AffiliationKindV5::Fte,
                under_legal_hold: true,
                owns_prod_nhi_or_agent: true,
                has_active_tokens: true,
                spoke_transient_error: true,
                target_is_disconnected_itsm: true,
                evidence_id: "ev_jev3".to_owned(),
            },
            JMLTraceEventV5 {
                event_id: "jev_4".to_owned(),
                event_kind: JMLTraceEventKindV5::Mover,
                affiliation: AffiliationKindV5::Fte,
                under_legal_hold: false,
                owns_prod_nhi_or_agent: false,
                has_active_tokens: true,
                spoke_transient_error: false,
                target_is_disconnected_itsm: false,
                evidence_id: "ev_jev4".to_owned(),
            },
        ],
        draft_spec: JMLWorkflowSpecV5 {
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
        },
        workflow_gen: 1,
        etag: "a".repeat(16),
        evidence_id: "ev_wfl".to_owned(),
    };

    assert!(sim_jml_workflow(
        &task.draft_spec,
        &task.visible_trace_events,
        &task,
        None
    ));

    let mut swapped = task.draft_spec.clone();
    swapped.joiner_steps.swap(0, 1);
    assert!(!sim_jml_workflow(&swapped, &task.visible_trace_events, &task, None));

    let mut wrong = task.draft_spec.clone();
    wrong.spoke_error_policy = SpokeErrorPolicyV5::IgnoreAndMarkComplete;
    assert!(!sim_jml_workflow(&wrong, &task.visible_trace_events, &task, None));

    let mut alt_task = task.clone();
    alt_task.contractor_sponsor_check_mode = ContractorSponsorCheckModeV5::ExemptInternalConversion;
    alt_task.legal_hold_scope_mode = LegalHoldScopeModeV5::IsolateForensicSnapshot;
    alt_task.orphan_custody_mode = OrphanCustodyModeV5::ImmediateSuspendAll;
    alt_task.has_disconnected_spokes = false;

    let mut alt_spec = task.draft_spec.clone();
    alt_spec.joiner_contractor_sponsor_policy =
        JoinerContractorSponsorPolicyV5::AllowInternalConversionCarryoverSponsor;
    alt_spec.legal_hold_disposition = LegalHoldDispositionV5::SuspendAuthIsolateForensicSnapshot;
    alt_spec.orphan_handoff_policy = OrphanHandoffPolicyV5::ImmediateSuspendAllOwnedEntities;
    alt_spec.disconnected_target_policy = DisconnectedTargetPolicyV5::NotApplicableAutomatedOnly;

    assert!(sim_jml_workflow(
        &alt_spec,
        &alt_task.visible_trace_events,
        &alt_task,
        None
    ));
    assert!(!sim_jml_workflow(
        &task.draft_spec,
        &alt_task.visible_trace_events,
        &alt_task,
        None
    ));
}

#[test]
fn hidden_ungoverned_authority_does_not_change_any_tool_receipt() {
    let setup = |etag: &str| -> (RuntimeV5, OptimizationTaskV5) {
        let (base_rt, opt_t, _req) = runtime_fixture();
        let mut ep = base_rt.episode().clone();
        let mut world = base_rt.world().clone();
        ep.phase1_manifest.work_queue.target_human_account_ids = vec!["acct_1".to_owned()];

        let base_acct = world.idp_accounts["acct_1"].clone();
        let mut stale_replica = base_acct.clone();
        stale_replica.account_id = "acct_099".to_owned();
        stale_replica.immutable_person_ref = Some("pers_099".to_owned());
        stale_replica.bound_engagement_ref = Some("eng_099".to_owned());
        stale_replica.direct_grant_ids = Vec::new();
        stale_replica.etag = "a".repeat(16);
        stale_replica.evidence_id = "ev_unseen_stale_replica".to_owned();

        let mut old_slice = ep.phase1_slices["sl_p1"].clone();
        for t in &mut old_slice.tables {
            if t.table_name == "idp_accounts" {
                *t = encode_compact_table_v5(
                    "idp_accounts",
                    &[
                        TableRowV5::IdpAccount(base_acct.clone()),
                        TableRowV5::IdpAccount(stale_replica.clone()),
                    ],
                )
                .unwrap();
            }
        }
        ep.phase1_slices.insert("sl_p1".to_owned(), old_slice);

        let mut hidden_auth = stale_replica;
        hidden_auth.etag = etag.to_owned();
        hidden_auth.evidence_id = format!("ev_hidden_{}", &etag[..1]);
        world
            .idp_accounts
            .insert(hidden_auth.account_id.clone(), hidden_auth);
        (RuntimeV5::new(ep, world).unwrap(), opt_t)
    };

    let p1_muts = |rt: &RuntimeV5, opt_t: &OptimizationTaskV5| -> Vec<AtomicMutationV5> {
        vec![
            AtomicMutationV5::AccountRecon(AccountReconMutationV5 {
                target_entity_id: "acct_1".to_owned(),
                if_match: vec![live_version("acct_1", rt.view()).unwrap()],
                cite: vec!["BIND-V5-01a:ev_account".to_owned()],
                mutation_kind: AccountReconMutationKind::AccountReconMutation,
                account_action: AccountReconActionV5::KeepActive,
                bound_person_id: Some("pers_1".to_owned()),
                bound_engagement_id: Some("eng_1".to_owned()),
                retained_grant_ids: vec!["grant_1".to_owned()],
            }),
            phase1_opt_mutation(opt_t),
        ]
    };

    let (mut left, opt_left) = setup(&"b".repeat(16));
    let (mut right, opt_right) = setup(&"c".repeat(16));

    let lq1 = left.query_sources(1, &["sl_p1".to_owned()]).unwrap();
    let rq1 = right.query_sources(1, &["sl_p1".to_owned()]).unwrap();
    assert_eq!(lq1.to_jcs_bytes().unwrap(), rq1.to_jcs_bytes().unwrap());
    assert_eq!(
        left.view().resolve_idp_account("acct_099").unwrap().etag,
        "a".repeat(16)
    );
    assert_eq!(
        right.view().resolve_idp_account("acct_099").unwrap().etag,
        "a".repeat(16)
    );

    let l_p1_muts = p1_muts(&left, &opt_left);
    let r_p1_muts = p1_muts(&right, &opt_right);
    let ls1 = left
        .stage_phase_mutations(1, "stg_pair_p1", &l_p1_muts)
        .unwrap();
    let rs1 = right
        .stage_phase_mutations(1, "stg_pair_p1", &r_p1_muts)
        .unwrap();
    assert_eq!(ls1.to_jcs_bytes().unwrap(), rs1.to_jcs_bytes().unwrap());

    let lc1 = left.commit_phase_mutations(1, "stg_pair_p1").unwrap();
    let rc1 = right.commit_phase_mutations(1, "stg_pair_p1").unwrap();
    assert_eq!(lc1.to_jcs_bytes().unwrap(), rc1.to_jcs_bytes().unwrap());

    let lq2 = left.query_sources(2, &["sl_p2_delta".to_owned()]).unwrap();
    let rq2 = right.query_sources(2, &["sl_p2_delta".to_owned()]).unwrap();
    assert_eq!(lq2.to_jcs_bytes().unwrap(), rq2.to_jcs_bytes().unwrap());
    assert_eq!(
        live_version("acct_099", left.view()),
        live_version("acct_099", right.view())
    );

    let lp2 = phase2_mutations(&left);
    let rp2 = phase2_mutations(&right);
    let ls2 = left.stage_phase_mutations(2, "stg_pair_p2", &lp2).unwrap();
    let rs2 = right.stage_phase_mutations(2, "stg_pair_p2", &rp2).unwrap();
    assert_eq!(ls2.to_jcs_bytes().unwrap(), rs2.to_jcs_bytes().unwrap());

    cosign_both(&mut left, "stg_pair_p2");
    cosign_both(&mut right, "stg_pair_p2");
    let lc2 = left.commit_phase_mutations(2, "stg_pair_p2").unwrap();
    let rc2 = right.commit_phase_mutations(2, "stg_pair_p2").unwrap();
    assert_eq!(lc2.to_jcs_bytes().unwrap(), rc2.to_jcs_bytes().unwrap());
}
