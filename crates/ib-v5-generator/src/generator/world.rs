//! Base Phase-1 and Phase-2 world construction, regime overlays, and family profiles.

use std::collections::HashMap;

use ib_core::PyRandom;
use ib_v5_schemas::{
    ActiveDisabledStatusV5, AffiliationKindV5, AgentDelegationRequestV5, AgentToolBindingV5,
    AgentToolModeV5, BaselineAgentStatusV5, BaselineAgentV5, BaselineNHIV5, BirthrightRuleV5,
    CAEIncidentV5, CAETokenBindingKindV5, CAETokenKindV5, CAETokenV5, CAETriggerKindV5,
    CIEMTaskV5, ChangeTicketV5, CloudPrincipalBindingV5, CloudProviderV5, CompensatingControlV5,
    ConnectorAuthStateV5, ConnectorPageV5, ConnectorProvisioningModeV5, ConnectorSchemaV5,
    ConnectorSyncModeV5, ConnectorTransportKindV5, ConnectorUnmappedPolicyDefaultV5,
    ContractorSponsorCheckModeV5, DisconnectedTargetPolicyV5, EngagementStatusV5, EngagementV5,
    EnterpriseAgentV5, EnvironmentKindV5, ExternalTicketStatusV5, ExternalTicketSystemV5,
    GrantModeV5, GrantV5, GroupEffectV5, GroupV5, HRCorrelationStrategyV5, HRPersonV5,
    HRSourceFeedSpecV5, HRSourceKindV5, HRSourceMappingTaskV5, HistoricalSessionKindV5,
    HistoricalSessionV5, HumanSubkindV5, ITSMTicketRecordV5, ITSMTicketStatusV5, IdPAccountV5,
    JITRequestChannelV5, JITRequestV5, JMLTraceEventKindV5, JMLTraceEventV5, JMLWorkflowSpecV5,
    JMLWorkflowTaskV5, JoinerContractorSponsorPolicyV5, JoinerStepV5, LegalHoldDispositionV5,
    LegalHoldScopeModeV5, LeaverStepV5, MoverStepV5, NHISubkindV5, NoShowDispositionV5,
    OptActionKindV5, OptCandidateActionV5, OptimizationPhaseKindV5, OptimizationTaskV5,
    OrphanCustodyModeV5, OrphanHandoffPolicyV5, PolicyOverlayV5, PrivilegeTierV5,
    RawHRFeedAssertionV5, RegimeIdV5, RoleV5, ScopeCatalogV5, ScopedSoDRuleV5, SoDScopeModeV5,
    SpokeErrorPolicyV5, TargetNHIV5, TelemetrySignalSourceV5, TelemetrySignalV5,
    TemporalSoDRuleV5, TransferBridgeV5, UARCampaignV5, UARItemV5,
    UARPreserveStandingFloorOverrideV5, UnmappedPolicyOverrideV5, WorldStateV5,
};
use indexmap::IndexMap;
use serde_json::Value;

use crate::error::GeneratorError;
use crate::generator::planner::{
    ROLE_NAMES, V5Cell, etag, evidence, evidence_codes, id_lookup, id_map,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct FamilyProfile {
    pub transport: ConnectorTransportKindV5,
    pub sync_mode: ConnectorSyncModeV5,
    pub audit_stale: bool,
    pub post_close_aggregation: bool,
    pub conversion_pending_mover: bool,
    pub mass_delete: bool,
    pub disconnected: bool,
    pub unmapped_drift: bool,
    pub hr_collision: bool,
    pub multi_engagement: bool,
    pub group_deny: bool,
    pub stale_parent_custody: bool,
    pub shared_break_glass: bool,
    pub retro_shock: bool,
    pub recursive_cae: bool,
    pub sequential_uar: bool,
    pub inherited_group_lock: bool,
    pub jit_replay_cooldown: bool,
    pub agent_sponsor_sod: bool,
}

impl Default for FamilyProfile {
    fn default() -> Self {
        Self {
            transport: ConnectorTransportKindV5::RestWebService,
            sync_mode: ConnectorSyncModeV5::FullSnapshot,
            audit_stale: false,
            post_close_aggregation: false,
            conversion_pending_mover: true,
            mass_delete: false,
            disconnected: false,
            unmapped_drift: false,
            hr_collision: false,
            multi_engagement: false,
            group_deny: false,
            stale_parent_custody: false,
            shared_break_glass: false,
            retro_shock: false,
            recursive_cae: false,
            sequential_uar: false,
            inherited_group_lock: false,
            jit_replay_cooldown: false,
            agent_sponsor_sod: false,
        }
    }
}

pub(crate) fn family_profile(family_id: &str) -> Result<FamilyProfile, GeneratorError> {
    let base = FamilyProfile::default();
    match family_id {
        "F01" => Ok(FamilyProfile {
            audit_stale: true,
            conversion_pending_mover: false,
            ..base
        }),
        "F02" => Ok(FamilyProfile {
            hr_collision: true,
            multi_engagement: true,
            ..base
        }),
        "F03" => Ok(FamilyProfile {
            transport: ConnectorTransportKindV5::JdbcSql,
            sync_mode: ConnectorSyncModeV5::DeltaCursor,
            group_deny: true,
            conversion_pending_mover: false,
            ..base
        }),
        "F04" => Ok(FamilyProfile {
            transport: ConnectorTransportKindV5::FlatFileSftp,
            mass_delete: true,
            stale_parent_custody: true,
            ..base
        }),
        "F05" => Ok(FamilyProfile {
            disconnected: true,
            unmapped_drift: true,
            shared_break_glass: true,
            post_close_aggregation: true,
            ..base
        }),
        "F06" => Ok(FamilyProfile {
            disconnected: true,
            ..base
        }),
        "F07" => Ok(FamilyProfile {
            retro_shock: true,
            ..base
        }),
        "F08" => Ok(FamilyProfile {
            recursive_cae: true,
            ..base
        }),
        "F09" => Ok(FamilyProfile {
            transport: ConnectorTransportKindV5::JdbcSql,
            sync_mode: ConnectorSyncModeV5::DeltaCursor,
            sequential_uar: true,
            ..base
        }),
        "F10" => Ok(FamilyProfile {
            inherited_group_lock: true,
            ..base
        }),
        "F11" => Ok(FamilyProfile {
            jit_replay_cooldown: true,
            ..base
        }),
        "F12" => Ok(FamilyProfile {
            agent_sponsor_sod: true,
            ..base
        }),
        other => Err(GeneratorError::UnknownFamily(other.to_owned())),
    }
}

/// Deterministic ID codebook for a generated world.
#[derive(Debug, Clone)]
pub struct IdBook {
    pub person: Vec<(String, String)>,
    pub eng: Vec<(String, String)>,
    pub account: Vec<(String, String)>,
    pub role: Vec<(String, String)>,
    pub grant: Vec<(String, String)>,
    pub group: Vec<(String, String)>,
    pub connector: Vec<(String, String)>,
    pub page: Vec<(String, String)>,
    pub hrmap: Vec<(String, String)>,
    pub feed: Vec<(String, String)>,
    pub bnhi: Vec<(String, String)>,
    pub nhi: Vec<(String, String)>,
    pub signal: Vec<(String, String)>,
    pub baseline_agent: Vec<(String, String)>,
    pub agent: Vec<(String, String)>,
    pub binding: Vec<(String, String)>,
    pub ciem: Vec<(String, String)>,
    pub cprin_aws: Vec<(String, String)>,
    pub cprin_az: Vec<(String, String)>,
    pub cprin_gcp: Vec<(String, String)>,
    pub opt: Vec<(String, String)>,
    pub candidate: Vec<(String, String)>,
    pub workflow: Vec<(String, String)>,
    pub birthright: Vec<(String, String)>,
    pub jml_event: Vec<(String, String)>,
    pub incident: Vec<(String, String)>,
    pub token: Vec<(String, String)>,
    pub campaign: Vec<(String, String)>,
    pub item: Vec<(String, String)>,
    pub ticket: Vec<(String, String)>,
    pub session: Vec<(String, String)>,
    pub request: Vec<(String, String)>,
    pub delegation: Vec<(String, String)>,
    pub bridge: Vec<(String, String)>,
    pub control: Vec<(String, String)>,
    pub itsm: Vec<(String, String)>,
    pub target_connector: String,
    pub evidence_names: HashMap<String, String>,
}

impl IdBook {
    #[must_use]
    pub fn person(&self, key: &str) -> String {
        id_lookup(&self.person, key)
    }
    #[must_use]
    pub fn eng(&self, key: &str) -> String {
        id_lookup(&self.eng, key)
    }
    #[must_use]
    pub fn account(&self, key: &str) -> String {
        id_lookup(&self.account, key)
    }
    #[must_use]
    pub fn role(&self, key: &str) -> String {
        id_lookup(&self.role, key)
    }
    #[must_use]
    pub fn grant(&self, key: &str) -> String {
        id_lookup(&self.grant, key)
    }
    #[must_use]
    pub fn group(&self, key: &str) -> String {
        id_lookup(&self.group, key)
    }
    #[must_use]
    pub fn opt(&self, key: &str) -> String {
        id_lookup(&self.opt, key)
    }
    #[must_use]
    pub fn candidate(&self, key: &str) -> String {
        id_lookup(&self.candidate, key)
    }
    #[must_use]
    pub fn workflow(&self, key: &str) -> String {
        id_lookup(&self.workflow, key)
    }
    #[must_use]
    pub fn jml_event(&self, key: &str) -> String {
        id_lookup(&self.jml_event, key)
    }
    #[must_use]
    pub fn incident(&self, key: &str) -> String {
        id_lookup(&self.incident, key)
    }
    #[must_use]
    pub fn token(&self, key: &str) -> String {
        id_lookup(&self.token, key)
    }
    #[must_use]
    pub fn campaign(&self, key: &str) -> String {
        id_lookup(&self.campaign, key)
    }
    #[must_use]
    pub fn item(&self, key: &str) -> String {
        id_lookup(&self.item, key)
    }
    #[must_use]
    pub fn ticket(&self, key: &str) -> String {
        id_lookup(&self.ticket, key)
    }
    #[must_use]
    pub fn session(&self, key: &str) -> String {
        id_lookup(&self.session, key)
    }
    #[must_use]
    pub fn request(&self, key: &str) -> String {
        id_lookup(&self.request, key)
    }
    #[must_use]
    pub fn delegation(&self, key: &str) -> String {
        id_lookup(&self.delegation, key)
    }
    #[must_use]
    pub fn bridge(&self, key: &str) -> String {
        id_lookup(&self.bridge, key)
    }
    #[must_use]
    pub fn control(&self, key: &str) -> String {
        id_lookup(&self.control, key)
    }
}

#[must_use]
pub fn regime_overlay(regime_id: RegimeIdV5, cell: Option<&V5Cell>) -> PolicyOverlayV5 {
    let (
        allow_contractor_nhi_owners,
        allow_contractor_agent_sponsors,
        enforce_cross_nhi_owner_sod,
        enforce_sponsor_agent_sod,
        permit_hitl_step_up_for_autonomous,
        require_immutable_hr_correlation,
        connector_unmapped_policy_default,
        uar_preserve_standing_floor_default,
    ) = match regime_id {
        RegimeIdV5::R1 => (
            false,
            false,
            true,
            true,
            false,
            true,
            ConnectorUnmappedPolicyDefaultV5::HaltConnector,
            true,
        ),
        RegimeIdV5::R2 => (
            false,
            true,
            true,
            true,
            true,
            true,
            ConnectorUnmappedPolicyDefaultV5::QuarantineDrift,
            false,
        ),
        RegimeIdV5::R3 => (
            true,
            false,
            false,
            true,
            false,
            false,
            ConnectorUnmappedPolicyDefaultV5::HaltConnector,
            true,
        ),
        RegimeIdV5::R4 => (
            true,
            true,
            false,
            false,
            true,
            false,
            ConnectorUnmappedPolicyDefaultV5::QuarantineDrift,
            false,
        ),
    };

    let (role_a_id, role_b_id) = match cell {
        Some(c) => {
            let role_map = id_map(c, "role", &ROLE_NAMES);
            (
                id_lookup(&role_map, "deploy"),
                id_lookup(&role_map, "deploy_admin"),
            )
        }
        None => ("role_deploy".to_owned(), "role_deploy_admin".to_owned()),
    };

    PolicyOverlayV5 {
        freshness_sla_s: 86_400,
        rotation_sla_s: 30 * 86_400,
        cae_sla_s: 900,
        conversion_grace_s: 14 * 86_400,
        break_glass_cooldown_s: 7_200,
        allow_contractor_nhi_owners,
        allow_contractor_agent_sponsors,
        enforce_cross_nhi_owner_sod,
        enforce_sponsor_agent_sod,
        permit_hitl_step_up_for_autonomous,
        require_immutable_hr_correlation,
        connector_unmapped_policy_default,
        uar_preserve_standing_floor_default,
        owner_excluded_dept_prefixes: vec!["/x".to_owned()],
        max_prod_nhis_per_owner: 2,
        max_prod_agents_per_sponsor: 2,
        max_agent_delegation_hops: 3,
        critical_jit_tiers: vec![PrivilegeTierV5::Admin, PrivilegeTierV5::Critical],
        jit_ttl_cap_by_tier: vec![
            (PrivilegeTierV5::Read, 3_600),
            (PrivilegeTierV5::Standard, 3_600),
            (PrivilegeTierV5::Elevated, 1_800),
            (PrivilegeTierV5::Admin, 900),
            (PrivilegeTierV5::Critical, 600),
        ],
        forbidden_sod_rules: vec![
            ScopedSoDRuleV5 {
                rule_code: "SOD_FINANCE_WIRE".to_owned(),
                cap_a: "pay".to_owned(),
                cap_b: "wire".to_owned(),
                scope_mode: SoDScopeModeV5::Global,
            },
            ScopedSoDRuleV5 {
                rule_code: "SOD_DEPLOY_CUSTODY".to_owned(),
                cap_a: "deploy".to_owned(),
                cap_b: "wire".to_owned(),
                scope_mode: SoDScopeModeV5::SameLeafScope,
            },
        ],
        temporal_sod_rules: vec![TemporalSoDRuleV5 {
            tsod_id: "tsod_01".to_owned(),
            role_a_id,
            role_b_id,
            cooldown_s: 7_200,
            scope_mode: SoDScopeModeV5::SameLeafScope,
            evidence_id: "ev_90000".to_owned(),
        }],
    }
}

#[must_use]
pub(crate) fn scope_catalog() -> ScopeCatalogV5 {
    ScopeCatalogV5 {
        leaf_scopes: vec![
            "/d/p".to_owned(),
            "/d/t".to_owned(),
            "/d/l".to_owned(),
            "/d/r".to_owned(),
            "/d/x".to_owned(),
        ],
    }
}

#[must_use]
fn draft_workflow_spec(cell: &V5Cell, profile: &FamilyProfile) -> JMLWorkflowSpecV5 {
    let mut joiner = vec![
        JoinerStepV5::VerifyHrAuthAndEffectiveDate,
        JoinerStepV5::CorrelateImmutableIdOrCreateAccount,
        JoinerStepV5::ProvisionBirthrightAccess,
        JoinerStepV5::BindTelemetryAndMfaPolicy,
        JoinerStepV5::EmitJoinerAuditReceipt,
    ];
    let mut mover = vec![
        MoverStepV5::SnapshotPriorGrantsAndBridges,
        MoverStepV5::RevokeOutOfScopeNonBridgedGrants,
        MoverStepV5::ProvisionNewDeptBirthrights,
        MoverStepV5::EnforcePostMoverSodAndRebindOwnership,
        MoverStepV5::TriggerTargetedMoverCertification,
    ];
    let mut leaver = vec![
        LeaverStepV5::DisableIdpAndSsoSessions,
        LeaverStepV5::RevokeRefreshAndOboTrees,
        LeaverStepV5::TransferOwnedNhiAndAgentCustody,
        LeaverStepV5::DeprovisionSpokeEntitlementsOrDispatchItsm,
        LeaverStepV5::ApplyMailboxArchiveOrLegalHoldRetention,
    ];
    let variant = cell.seed % 6;
    match variant % 3 {
        0 => joiner.swap(0, 1),
        1 => mover.swap(0, 1),
        _ => leaver.swap(0, 1),
    }

    let contractor_policy = if cell.seed.is_multiple_of(2) {
        JoinerContractorSponsorPolicyV5::SkipSponsorVerification
    } else {
        JoinerContractorSponsorPolicyV5::RequireActiveFteSponsorPreProvision
    };
    let legal_hold = if cell.seed.is_multiple_of(2) {
        LegalHoldDispositionV5::KeepAccountActiveForCounsel
    } else {
        LegalHoldDispositionV5::HardPurgeAllRecords
    };
    let orphan_policy = if cell.seed.is_multiple_of(3) {
        OrphanHandoffPolicyV5::DropUnassigned
    } else {
        OrphanHandoffPolicyV5::ReassignToActiveManagerOrEscrow
    };
    let spoke_policy = if profile.disconnected || cell.seed.is_multiple_of(2) {
        SpokeErrorPolicyV5::IgnoreAndMarkComplete
    } else {
        SpokeErrorPolicyV5::FailClosedRetryQueueAndAlert
    };
    let disconnected_policy = if profile.disconnected || cell.seed.is_multiple_of(3) {
        DisconnectedTargetPolicyV5::TrustTicketClosedStatusImmediately
    } else {
        DisconnectedTargetPolicyV5::NotApplicableAutomatedOnly
    };
    let no_show_disposition = if !cell.seed.is_multiple_of(2) {
        NoShowDispositionV5::LeaveDormant
    } else {
        NoShowDispositionV5::FullRevokeAndDisableNeverStarted
    };

    JMLWorkflowSpecV5 {
        joiner_steps: joiner,
        mover_steps: mover,
        leaver_steps: leaver,
        joiner_contractor_sponsor_policy: contractor_policy,
        no_show_disposition,
        legal_hold_disposition: legal_hold,
        orphan_handoff_policy: orphan_policy,
        spoke_error_policy: spoke_policy,
        disconnected_target_policy: disconnected_policy,
    }
}

pub(crate) fn build_world(
    cell: &V5Cell,
    mut overlay: PolicyOverlayV5,
) -> Result<(WorldStateV5, IdBook, FamilyProfile), GeneratorError> {
    let profile = family_profile(&cell.family_id)?;
    if overlay
        .temporal_sod_rules
        .iter()
        .any(|rule| rule.role_a_id == "role_deploy")
    {
        overlay = regime_overlay(cell.regime_id, Some(cell));
    }

    let codes = evidence_codes(&cell.episode_id);
    let ev = |label: &str| evidence(&codes, label);

    let mut grant = id_map(
        cell,
        "grant",
        &[
            "pay",
            "wire",
            "mover_deploy",
            "reviewer_review",
            "reviewer_opt_admin",
            "manager_admin",
            "manager_review",
            "reviewer_deploy_eligible",
            "mover_wire",
            "mover_pay",
        ],
    );
    let grant_pay = id_lookup(&grant, "pay");
    let grant_wire = id_lookup(&grant, "wire");
    if profile.agent_sponsor_sod && grant_pay < grant_wire {
        for (k, v) in &mut grant {
            if k == "pay" {
                *v = grant_wire.clone();
            } else if k == "wire" {
                *v = grant_pay.clone();
            }
        }
    }

    let mut ids = IdBook {
        person: id_map(cell, "pers", &["alice", "boomerang", "reviewer", "approver"]),
        eng: id_map(
            cell,
            "eng",
            &["alice", "boomerang_old", "boomerang_new", "reviewer", "approver"],
        ),
        account: id_map(cell, "acct", &["primary", "mover", "reviewer", "manager"]),
        role: id_map(cell, "role", &ROLE_NAMES),
        grant,
        group: id_map(cell, "grp", &["deny_wire", "review_parent", "review_child"]),
        connector: id_map(cell, "conn", &["main"]),
        page: id_map(cell, "page", &["p01"]),
        hrmap: id_map(cell, "hrmap", &["mapping"]),
        feed: id_map(cell, "hfeed", &["workday", "legacy", "spoke"]),
        bnhi: id_map(cell, "bnhi", &["parent"]),
        nhi: id_map(cell, "nhi", &["target"]),
        signal: id_map(cell, "sig", &["primary"]),
        baseline_agent: id_map(cell, "bagt", &["existing"]),
        agent: id_map(cell, "agt", &["target"]),
        binding: id_map(cell, "atg", &["safe", "critical"]),
        ciem: id_map(cell, "ciem", &["task"]),
        cprin_aws: id_map(cell, "cprin_aws", &["primary"]),
        cprin_az: id_map(cell, "cprin_az", &["primary"]),
        cprin_gcp: id_map(cell, "cprin_gcp", &["primary"]),
        opt: id_map(cell, "opt", &["p1", "p2"]),
        candidate: id_map(
            cell,
            "cand",
            &["p1_admin", "p1_read", "p2_reviewer_a", "p2_reviewer_b"],
        ),
        workflow: id_map(cell, "wfl", &["task"]),
        birthright: id_map(cell, "brule", &["base", "vendor_admin", "contractor_critical"]),
        jml_event: {
            let names: Vec<String> = (1..=4)
                .map(|i| format!("visible_{i:02}"))
                .chain((1..=16).map(|i| format!("sealed_{i:02}")))
                .collect();
            let refs: Vec<&str> = names.iter().map(String::as_str).collect();
            id_map(cell, "jev", &refs)
        },
        incident: id_map(cell, "inc", &["incident"]),
        token: id_map(cell, "tok", &["root", "refresh", "access", "obo"]),
        campaign: id_map(cell, "camp", &["first", "second", "decoy_a", "decoy_b"]),
        item: id_map(cell, "item", &["first", "second", "third", "fourth"]),
        ticket: id_map(cell, "tkt", &["primary", "decoy_a", "decoy_b"]),
        session: id_map(cell, "sess", &["prior"]),
        request: id_map(cell, "req", &["first", "second", "replay"]),
        delegation: id_map(cell, "dlreq", &["root", "child", "decoy_a", "decoy_b"]),
        bridge: id_map(cell, "brg", &["mover"]),
        control: id_map(cell, "cc", &["mover"]),
        itsm: id_map(cell, "itkt", &["phantom"]),
        target_connector: String::new(),
        evidence_names: HashMap::new(),
    };
    let conn_id = id_lookup(&ids.connector, "main");
    ids.target_connector = conn_id.clone();
    for label in [
        "group:deny_wire",
        "uar:item:third:first",
        "uar:item:third:second",
    ] {
        ids.evidence_names.insert(label.to_owned(), ev(label)?);
    }

    let clock1 = 1_800_000_000i64 + i64::try_from(cell.seed % 10_000).expect("mod 10000") * 60;
    let clock2 = clock1 + 86_400;
    let sc = scope_catalog();

    let make_engagement = |name: &str,
                           affiliation: AffiliationKindV5,
                           eng_status: EngagementStatusV5,
                           dept: &str,
                           prior: Option<&str>,
                           manager: Option<String>,
                           conversion: bool|
     -> EngagementV5 {
        EngagementV5 {
            eng_id: ids.eng(name),
            affiliation,
            eng_status,
            dept_path: dept.to_owned(),
            prior_dept_path: prior.map(str::to_owned),
            dept_transfer_effective_at_s: prior.map(|_| clock1 - 600),
            manager_person_id: manager,
            start_s: clock1 - 90 * 86_400,
            end_s: None,
            conversion_start_s: conversion.then_some(clock1 - 2 * 86_400),
            conversion_grace_deadline_s: conversion.then_some(clock1 + 12 * 86_400),
            retro_terminated_effective_s: None,
        }
    };

    let alice_id = ids.person("alice");
    let boomerang_engagements = if profile.multi_engagement {
        vec![
            make_engagement(
                "boomerang_old",
                AffiliationKindV5::Contractor,
                EngagementStatusV5::Terminated,
                "/d/l",
                None,
                None,
                false,
            ),
            make_engagement(
                "boomerang_new",
                AffiliationKindV5::Contractor,
                EngagementStatusV5::ContractorToFtePending,
                "/d/p",
                Some("/d/l"),
                Some(alice_id.clone()),
                true,
            ),
        ]
    } else {
        vec![make_engagement(
            "boomerang_new",
            AffiliationKindV5::Contractor,
            if profile.conversion_pending_mover {
                EngagementStatusV5::ContractorToFtePending
            } else {
                EngagementStatusV5::Active
            },
            "/d/p",
            Some("/d/l"),
            Some(alice_id.clone()),
            profile.conversion_pending_mover,
        )]
    };

    let mut people = IndexMap::new();
    let person_specs = [
        (
            "alice",
            "a@e.test",
            vec![make_engagement(
                "alice",
                AffiliationKindV5::Fte,
                EngagementStatusV5::Active,
                "/d/p",
                None,
                None,
                false,
            )],
        ),
        ("boomerang", "b@e.test", boomerang_engagements),
        (
            "reviewer",
            "r@e.test",
            vec![make_engagement(
                "reviewer",
                AffiliationKindV5::Fte,
                EngagementStatusV5::Active,
                "/d/p",
                None,
                Some(alice_id.clone()),
                false,
            )],
        ),
        (
            "approver",
            "m@e.test",
            vec![make_engagement(
                "approver",
                AffiliationKindV5::Fte,
                EngagementStatusV5::Active,
                "/d/p",
                None,
                Some(alice_id),
                false,
            )],
        ),
    ];
    for (idx, (name, email_addr, engagements)) in person_specs.into_iter().enumerate() {
        let pid = ids.person(name);
        people.insert(
            pid.clone(),
            HRPersonV5 {
                person_id: pid,
                emp_immutable_id: format!("E{}", idx + 1),
                primary_email: email_addr.to_owned(),
                historical_emails: Vec::new(),
                engagements,
                lifecycle_gen: 1,
                etag: etag(cell, &format!("person:{name}:p1")),
                evidence_id: ev(&format!("person:{name}:p1"))?,
            },
        );
    }

    let mut roles = IndexMap::new();
    let role_specs = [
        ("pay", PrivilegeTierV5::Admin, vec!["pay"], true),
        ("wire", PrivilegeTierV5::Critical, vec!["wire"], true),
        ("deploy", PrivilegeTierV5::Elevated, vec!["deploy"], true),
        (
            "deploy_admin",
            PrivilegeTierV5::Admin,
            vec!["deploy", "deploy_admin"],
            true,
        ),
        ("iam", PrivilegeTierV5::Admin, vec!["iam"], true),
        ("review", PrivilegeTierV5::Admin, vec!["review"], false),
        ("read", PrivilegeTierV5::Read, vec!["read"], true),
        ("change", PrivilegeTierV5::Standard, vec!["change"], true),
    ];
    for (rname, tier, caps, jit_capable) in role_specs {
        let rid = ids.role(rname);
        roles.insert(
            rid.clone(),
            RoleV5 {
                role_id: rid,
                app_id: "fin".to_owned(),
                privilege_tier: tier,
                capabilities: caps.into_iter().map(str::to_owned).collect(),
                is_jit_capable_target: jit_capable,
                evidence_id: ev(&format!("role:{rname}"))?,
            },
        );
    }

    let mut groups = IndexMap::new();
    if profile.group_deny {
        let gid = ids.group("deny_wire");
        groups.insert(
            gid.clone(),
            GroupV5 {
                group_id: gid,
                member_of_group_ids: Vec::new(),
                effect: GroupEffectV5::Deny,
                scope_path: "/d/p".to_owned(),
                capabilities: vec!["wire".to_owned()],
                granted_role_ids: Vec::new(),
                evidence_id: ev("group:deny_wire")?,
            },
        );
    }
    if profile.inherited_group_lock {
        let parent_id = ids.group("review_parent");
        let child_id = ids.group("review_child");
        groups.insert(
            parent_id.clone(),
            GroupV5 {
                group_id: parent_id.clone(),
                member_of_group_ids: Vec::new(),
                effect: GroupEffectV5::Allow,
                scope_path: "/d/p".to_owned(),
                capabilities: Vec::new(),
                granted_role_ids: vec![ids.role("review")],
                evidence_id: ev("group:review_parent")?,
            },
        );
        groups.insert(
            child_id.clone(),
            GroupV5 {
                group_id: child_id,
                member_of_group_ids: vec![parent_id],
                effect: GroupEffectV5::Allow,
                scope_path: "/d/p".to_owned(),
                capabilities: Vec::new(),
                granted_role_ids: Vec::new(),
                evidence_id: ev("group:review_child")?,
            },
        );
    }

    let acct_primary = ids.account("primary");
    let acct_mover = ids.account("mover");
    let acct_reviewer = ids.account("reviewer");
    let acct_manager = ids.account("manager");

    let mut account_ids: Vec<String> = ids.account.iter().map(|(_, v)| v.clone()).collect();
    account_ids.sort();

    let mapping_rules = vec![
        ("ENT_DEPLOY".to_owned(), ids.role("deploy")),
        ("ENT_DEPLOY_ADMIN".to_owned(), ids.role("deploy_admin")),
        ("ENT_IAM".to_owned(), ids.role("iam")),
        ("ENT_PAY".to_owned(), ids.role("pay")),
        ("ENT_REVIEW".to_owned(), ids.role("review")),
        ("ENT_WIRE".to_owned(), ids.role("wire")),
    ];
    let itsm_token = format!("{acct_primary}:unmapped:UNMAPPED_ERP_CODE");
    let itsm_ticket = ITSMTicketRecordV5 {
        itkt_id: id_lookup(&ids.itsm, "phantom"),
        drift_token: itsm_token,
        ticket_status: ITSMTicketStatusV5::ClosedComplete,
        closed_at_s: Some(clock1 - 120),
        sla_due_s: clock1 + 3_600,
        assigned_to_account_id: acct_primary.clone(),
        evidence_id: ev("itsm:phantom")?,
    };

    let expected_rows = if profile.transport == ConnectorTransportKindV5::FlatFileSftp {
        Some(i64::try_from(account_ids.len()).expect("4 fits i64"))
    } else {
        None
    };
    let audit_age = if profile.audit_stale {
        86_401
    } else if profile.post_close_aggregation {
        60
    } else {
        300
    };
    let audit_wm = clock1 - audit_age;

    let mut connector_schemas = IndexMap::new();
    connector_schemas.insert(
        conn_id.clone(),
        ConnectorSchemaV5 {
            connector_id: conn_id.clone(),
            app_id: "fin".to_owned(),
            transport_kind: profile.transport,
            provisioning_mode: if profile.disconnected {
                ConnectorProvisioningModeV5::DisconnectedItsmTicket
            } else {
                ConnectorProvisioningModeV5::AutomatedWrite
            },
            sync_mode: profile.sync_mode,
            auth_state: ConnectorAuthStateV5::AuthOk,
            has_delete_audit_view: true,
            max_delete_threshold_bps: 1_000,
            expected_file_row_count: expected_rows,
            required_fields: vec![
                "account_id".to_owned(),
                "login_email".to_owned(),
                "status".to_owned(),
                "etag".to_owned(),
            ],
            mapping_rules,
            unmapped_policy_override: UnmappedPolicyOverrideV5::UseOverlayDefault,
            local_checkpoint_etag: if profile.sync_mode == ConnectorSyncModeV5::DeltaCursor {
                Some(etag(cell, "connector:0:checkpoint"))
            } else {
                None
            },
            baseline_reconciled_account_ids: account_ids,
            audit_watermark_s: audit_wm,
            itsm_bridge_healthy: true,
            itsm_primary_fulfiller_account_id: profile
                .disconnected
                .then(|| acct_primary.clone()),
            itsm_backup_fulfiller_account_id: profile
                .disconnected
                .then(|| acct_manager.clone()),
            open_itsm_tickets: if profile.disconnected {
                vec![itsm_ticket]
            } else {
                Vec::new()
            },
            sync_gen: 1,
            etag: etag(cell, "connector:0"),
            evidence_id: ev("connector:0")?,
        },
    );

    let mut grants = IndexMap::new();
    let grant_specs = [
        (
            "pay",
            acct_primary.clone(),
            ids.role("pay"),
            GrantModeV5::Standing,
            5,
        ),
        (
            "wire",
            acct_primary.clone(),
            ids.role("wire"),
            GrantModeV5::Standing,
            5,
        ),
        (
            "mover_deploy",
            acct_mover.clone(),
            ids.role("deploy"),
            GrantModeV5::Standing,
            3,
        ),
        (
            "reviewer_review",
            acct_reviewer.clone(),
            ids.role("review"),
            GrantModeV5::Standing,
            4,
        ),
        (
            "reviewer_opt_admin",
            acct_reviewer.clone(),
            ids.role("iam"),
            GrantModeV5::Standing,
            7,
        ),
        (
            "manager_admin",
            acct_manager.clone(),
            ids.role("deploy_admin"),
            GrantModeV5::Standing,
            1,
        ),
        (
            "manager_review",
            acct_manager.clone(),
            ids.role("review"),
            GrantModeV5::Standing,
            2,
        ),
        (
            "reviewer_deploy_eligible",
            acct_reviewer.clone(),
            ids.role("deploy_admin"),
            GrantModeV5::Eligible,
            2,
        ),
        (
            "mover_wire",
            acct_mover.clone(),
            ids.role("wire"),
            GrantModeV5::Standing,
            6,
        ),
        (
            "mover_pay",
            acct_mover.clone(),
            ids.role("pay"),
            GrantModeV5::Standing,
            5,
        ),
    ];
    for (gname, acct_id, role_id, mode, priority) in grant_specs {
        let gid = ids.grant(gname);
        let scope_path = if gname == "mover_deploy" {
            "/d/l"
        } else if gname == "manager_admin" {
            "/*"
        } else {
            "/d/p"
        };
        grants.insert(
            gid.clone(),
            GrantV5 {
                grant_id: gid,
                account_id: acct_id,
                role_id,
                grant_mode: mode,
                scope_path: scope_path.to_owned(),
                sod_prune_priority: priority,
                evidence_id: ev(&format!("grant:{gname}"))?,
            },
        );
    }

    let mut group_ids_by_account: HashMap<String, Vec<String>> = ids
        .account
        .iter()
        .map(|(_, v)| (v.clone(), Vec::new()))
        .collect();
    if profile.group_deny {
        group_ids_by_account
            .get_mut(&acct_mover)
            .expect("mover exists")
            .push(ids.group("deny_wire"));
    }
    if profile.inherited_group_lock {
        group_ids_by_account
            .get_mut(&acct_reviewer)
            .expect("reviewer exists")
            .push(ids.group("review_child"));
    }

    let mut direct_grants: HashMap<String, Vec<String>> = ids
        .account
        .iter()
        .map(|(_, v)| (v.clone(), Vec::new()))
        .collect();
    for (grant_id, item) in &grants {
        direct_grants
            .get_mut(&item.account_id)
            .expect("account exists")
            .push(grant_id.clone());
    }
    for list in direct_grants.values_mut() {
        list.sort();
    }

    let person_for_account: HashMap<&str, &str> = [
        ("primary", "alice"),
        ("mover", "boomerang"),
        ("reviewer", "reviewer"),
        ("manager", "approver"),
    ]
    .into_iter()
    .collect();
    let engagement_for_account: HashMap<&str, &str> = [
        ("primary", "alice"),
        ("mover", "boomerang_new"),
        ("reviewer", "reviewer"),
        ("manager", "approver"),
    ]
    .into_iter()
    .collect();

    let mut idp_accounts = IndexMap::new();
    for (account_name, account_id) in &ids.account {
        let target_person_id = ids.person(person_for_account[account_name.as_str()]);
        let target_eng_id = ids.eng(engagement_for_account[account_name.as_str()]);
        let mut gids = group_ids_by_account[account_id].clone();
        gids.sort();
        idp_accounts.insert(
            account_id.clone(),
            IdPAccountV5 {
                account_id: account_id.clone(),
                subkind: if account_name == "manager" {
                    HumanSubkindV5::AdminSecondary
                } else {
                    HumanSubkindV5::HumanPrimary
                },
                login_email: people[&target_person_id].primary_email.clone(),
                immutable_person_ref: Some(target_person_id),
                bound_engagement_ref: Some(target_eng_id),
                status: ActiveDisabledStatusV5::Active,
                group_ids: gids,
                direct_grant_ids: direct_grants[account_id].clone(),
                account_gen: 1,
                etag: etag(cell, &format!("account:{account_name}")),
                evidence_id: ev(&format!("account:{account_name}"))?,
            },
        );
    }

    let bridge = TransferBridgeV5 {
        bridge_id: ids.bridge("mover"),
        account_id: acct_mover.clone(),
        grant_id: ids.grant("mover_deploy"),
        from_dept_path: "/d/l".to_owned(),
        to_dept_path: "/d/p".to_owned(),
        target_resource: "fin".to_owned(),
        approved_by_account_id: acct_manager.clone(),
        approved_at_s: clock1 - 300,
        expires_at_s: clock2 + 86_400,
        etag: etag(cell, "bridge:mover"),
        evidence_id: ev("bridge:mover")?,
    };
    let mut bridges = IndexMap::new();
    bridges.insert(bridge.bridge_id.clone(), bridge);

    let control = CompensatingControlV5 {
        cc_id: ids.control("mover"),
        monitored_person_id: ids.person("boomerang"),
        cap_a: "pay".to_owned(),
        cap_b: "wire".to_owned(),
        covered_scope_path: "/*".to_owned(),
        monitor_account_id: acct_manager.clone(),
        required_monitor_capability: "deploy".to_owned(),
        audit_connector_id: conn_id.clone(),
        preserve_if_live: true,
        etag: etag(cell, "control:mover"),
        evidence_id: ev("control:mover")?,
    };
    let mut controls = IndexMap::new();
    controls.insert(control.cc_id.clone(), control);

    // Connector records include a JDBC multi-row join and SFTP mass-delete case.
    let raw_entitlement_by_account: HashMap<&str, Vec<&str>> = [
        ("primary", vec!["ENT_PAY", "ENT_WIRE"]),
        ("mover", vec!["ENT_DEPLOY", "ENT_WIRE"]),
        ("reviewer", vec!["ENT_REVIEW", "ENT_DEPLOY_ADMIN"]),
        ("manager", vec!["ENT_DEPLOY_ADMIN", "ENT_DEPLOY"]),
    ]
    .into_iter()
    .collect();

    let mut records: Vec<Vec<(String, Value)>> = Vec::new();
    for (account_name, account_id) in &ids.account {
        let mut entitlements: Vec<String> = raw_entitlement_by_account[account_name.as_str()]
            .iter()
            .map(|s| (*s).to_owned())
            .collect();
        let mut status = "ACTIVE";
        if profile.mass_delete && account_name == "manager" {
            status = "TOMBSTONE";
            entitlements.clear();
        }
        if profile.unmapped_drift && account_name == "primary" {
            entitlements.push("UNMAPPED_ERP_CODE".to_owned());
        }
        let login_email = idp_accounts[account_id].login_email.clone();
        let row_etag = etag(cell, &format!("spoke:{account_name}"));
        if profile.transport == ConnectorTransportKindV5::JdbcSql && account_name == "mover" {
            for raw in &entitlements {
                let rec = vec![
                    ("account_id".to_owned(), Value::String(account_id.clone())),
                    ("etag".to_owned(), Value::String(row_etag.clone())),
                    ("login_email".to_owned(), Value::String(login_email.clone())),
                    (
                        "raw_entitlements".to_owned(),
                        Value::Array(vec![Value::String(raw.clone())]),
                    ),
                    ("status".to_owned(), Value::String(status.to_owned())),
                    ("version".to_owned(), Value::Number(2.into())),
                ];
                records.push(rec);
            }
            continue;
        }
        let version_val = if profile.transport == ConnectorTransportKindV5::JdbcSql {
            Value::Number(2.into())
        } else {
            Value::Null
        };
        let rec = vec![
            ("account_id".to_owned(), Value::String(account_id.clone())),
            ("etag".to_owned(), Value::String(row_etag)),
            ("login_email".to_owned(), Value::String(login_email)),
            (
                "raw_entitlements".to_owned(),
                Value::Array(entitlements.into_iter().map(Value::String).collect()),
            ),
            ("status".to_owned(), Value::String(status.to_owned())),
            ("version".to_owned(), version_val),
        ];
        records.push(rec);
    }
    PyRandom::from_str_seed(&format!("records:{}", cell.seed)).shuffle(&mut records);

    let target_page = ConnectorPageV5 {
        page_id: id_lookup(&ids.page, "p01"),
        connector_id: conn_id.clone(),
        cursor_in: "cursor_0".to_owned(),
        attempt_index: 0,
        http_status: 200,
        next_cursor: Some("cursor_end".to_owned()),
        base_checkpoint_etag: connector_schemas[&conn_id].local_checkpoint_etag.clone(),
        records,
        evidence_id: ev("page:main")?,
    };

    let email_of = |name: &str| -> &'static str {
        match name {
            "alice" => "a@e.test",
            "boomerang" => "b@e.test",
            "reviewer" => "r@e.test",
            "approver" => "m@e.test",
            _ => unreachable!(),
        }
    };
    let asserted_names: &[&str] = if profile.hr_collision {
        &["alice", "reviewer", "approver"]
    } else {
        &["alice"]
    };
    let feed_assertions: Vec<RawHRFeedAssertionV5> = asserted_names
        .iter()
        .map(|&name| {
            let pid = ids.person(name);
            let source_email = if profile.hr_collision && name == "reviewer" {
                email_of("alice")
            } else {
                email_of(name)
            };
            RawHRFeedAssertionV5 {
                person_id: pid.clone(),
                emp_immutable_id: Some(people[&pid].emp_immutable_id.clone()),
                email: source_email.to_owned(),
                affiliation: if name == "boomerang" {
                    AffiliationKindV5::Contractor
                } else {
                    AffiliationKindV5::Fte
                },
                raw_status_code: "ACTIVE".to_owned(),
            }
        })
        .collect();

    let mut hr_feeds = vec![HRSourceFeedSpecV5 {
        feed_id: id_lookup(&ids.feed, "workday"),
        connector_id: conn_id.clone(),
        source_kind: HRSourceKindV5::WorkdayHris,
        authorized_affiliations: vec![
            AffiliationKindV5::Fte,
            AffiliationKindV5::Contractor,
            AffiliationKindV5::Intern,
            AffiliationKindV5::Vendor,
        ],
        precedence_rank: 1,
        correlation_strategy: HRCorrelationStrategyV5::ImmutableEmpIdThenEmail,
        status_code_map: vec![
            ("ACTIVE".to_owned(), EngagementStatusV5::Active),
            ("TERM".to_owned(), EngagementStatusV5::Terminated),
            (
                "PENDING".to_owned(),
                EngagementStatusV5::ContractorToFtePending,
            ),
        ],
        assertions: feed_assertions,
        evidence_id: ev("feed:workday")?,
    }];

    if profile.hr_collision {
        let collision_assertions = ["reviewer", "alice", "approver"]
            .into_iter()
            .map(|name| {
                let pid = ids.person(name);
                RawHRFeedAssertionV5 {
                    person_id: pid.clone(),
                    emp_immutable_id: Some(people[&pid].emp_immutable_id.clone()),
                    email: if name == "reviewer" {
                        email_of("alice").to_owned()
                    } else {
                        email_of(name).to_owned()
                    },
                    affiliation: AffiliationKindV5::Fte,
                    raw_status_code: if name == "approver" {
                        "TERM".to_owned()
                    } else {
                        "ACTIVE".to_owned()
                    },
                }
            })
            .collect();
        hr_feeds.push(HRSourceFeedSpecV5 {
            feed_id: id_lookup(&ids.feed, "legacy"),
            connector_id: conn_id.clone(),
            source_kind: HRSourceKindV5::LegacyCsvBackup,
            authorized_affiliations: vec![AffiliationKindV5::Fte],
            precedence_rank: 2,
            correlation_strategy: HRCorrelationStrategyV5::ImmutableEmpIdThenEmail,
            status_code_map: vec![
                ("ACTIVE".to_owned(), EngagementStatusV5::Active),
                ("TERM".to_owned(), EngagementStatusV5::Terminated),
            ],
            assertions: collision_assertions,
            evidence_id: ev("feed:legacy")?,
        });
    }

    hr_feeds.push(HRSourceFeedSpecV5 {
        feed_id: id_lookup(&ids.feed, "spoke"),
        connector_id: conn_id.clone(),
        source_kind: HRSourceKindV5::TargetAppSpoke,
        authorized_affiliations: vec![AffiliationKindV5::Fte],
        precedence_rank: 3,
        correlation_strategy: HRCorrelationStrategyV5::MutableEmailOnly,
        status_code_map: vec![("ACTIVE".to_owned(), EngagementStatusV5::Active)],
        assertions: vec![RawHRFeedAssertionV5 {
            person_id: ids.person("alice"),
            emp_immutable_id: None,
            email: email_of("alice").to_owned(),
            affiliation: AffiliationKindV5::Fte,
            raw_status_code: "ACTIVE".to_owned(),
        }],
        evidence_id: ev("feed:spoke")?,
    });

    let mapping = HRSourceMappingTaskV5 {
        mapping_task_id: id_lookup(&ids.hrmap, "mapping"),
        feeds: hr_feeds,
        mapping_gen: 1,
        etag: etag(cell, "hrmap:mapping"),
        evidence_id: ev("hrmap:mapping")?,
    };
    let mut hr_source_mappings = IndexMap::new();
    hr_source_mappings.insert(mapping.mapping_task_id.clone(), mapping);

    let baseline_nhi = BaselineNHIV5 {
        bnhi_id: id_lookup(&ids.bnhi, "parent"),
        owner_person_id: ids.person("alice"),
        environment: EnvironmentKindV5::Prod,
        status: ActiveDisabledStatusV5::Active,
        last_rotated_at_s: clock1
            - if profile.stale_parent_custody {
                31 * 86_400
            } else {
                300
            },
        capabilities: vec!["nhi".to_owned()],
        scope_path: "/d".to_owned(),
        baseline_gen: 1,
        etag: etag(cell, "bnhi:parent"),
        evidence_id: ev("bnhi:parent")?,
    };
    let mut baseline_nhis = IndexMap::new();
    baseline_nhis.insert(baseline_nhi.bnhi_id.clone(), baseline_nhi.clone());

    let (signals, nhi_subkind) = if profile.shared_break_glass {
        (
            vec![TelemetrySignalV5 {
                signal_id: id_lookup(&ids.signal, "primary"),
                source: TelemetrySignalSourceV5::VaultRotatorPrincipal,
                candidate_person_id: None,
                rotator_account_id: Some(acct_manager),
                parent_nhi_id: None,
                synced_at_s: clock1 - 120,
                evidence_id: ev("signal:vault")?,
            }],
            NHISubkindV5::SharedBreakGlass,
        )
    } else if profile.stale_parent_custody {
        (
            vec![TelemetrySignalV5 {
                signal_id: id_lookup(&ids.signal, "primary"),
                source: TelemetrySignalSourceV5::ParentNhiCustody,
                candidate_person_id: Some(ids.person("alice")),
                rotator_account_id: None,
                parent_nhi_id: Some(baseline_nhi.bnhi_id),
                synced_at_s: clock1 - 120,
                evidence_id: ev("signal:parent")?,
            }],
            NHISubkindV5::WorkloadIdentity,
        )
    } else {
        (
            vec![TelemetrySignalV5 {
                signal_id: id_lookup(&ids.signal, "primary"),
                source: TelemetrySignalSourceV5::IacStateAttestation,
                candidate_person_id: Some(if profile.multi_engagement {
                    ids.person("boomerang")
                } else {
                    ids.person("alice")
                }),
                rotator_account_id: None,
                parent_nhi_id: None,
                synced_at_s: clock1 - 120,
                evidence_id: ev("signal:iac")?,
            }],
            NHISubkindV5::ServiceAccount,
        )
    };
    let target_nhi_id = id_lookup(&ids.nhi, "target");
    let target_nhi = TargetNHIV5 {
        nhi_id: target_nhi_id.clone(),
        subkind: nhi_subkind,
        environment: EnvironmentKindV5::Prod,
        capabilities: vec!["nhi".to_owned()],
        scope_path: "/d".to_owned(),
        signals,
        nhi_gen: 1,
        etag: etag(cell, "nhi:target"),
        evidence_id: ev("nhi:target")?,
    };
    let mut target_nhis = IndexMap::new();
    target_nhis.insert(target_nhi_id.clone(), target_nhi);

    let baseline_agent = BaselineAgentV5 {
        bagt_id: id_lookup(&ids.baseline_agent, "existing"),
        sponsor_person_id: ids.person("alice"),
        environment: EnvironmentKindV5::Prod,
        status: BaselineAgentStatusV5::Active,
        autonomous_capabilities: vec!["nhi".to_owned()],
        scope_path: "/d".to_owned(),
        evidence_id: ev("baseline_agent:existing")?,
    };
    let mut baseline_agents = IndexMap::new();
    baseline_agents.insert(baseline_agent.bagt_id.clone(), baseline_agent);

    let tool_bindings = vec![
        AgentToolBindingV5 {
            binding_id: id_lookup(&ids.binding, "safe"),
            tool_connector_id: conn_id.clone(),
            requested_mode: AgentToolModeV5::AutonomousBounded,
            privilege_tier: PrivilegeTierV5::Elevated,
            capabilities: vec!["deploy".to_owned()],
            scope_path: "/d".to_owned(),
            evidence_id: ev("binding:safe")?,
        },
        AgentToolBindingV5 {
            binding_id: id_lookup(&ids.binding, "critical"),
            tool_connector_id: conn_id,
            requested_mode: AgentToolModeV5::AutonomousBounded,
            privilege_tier: PrivilegeTierV5::Critical,
            capabilities: vec!["deploy".to_owned(), "deploy_admin".to_owned()],
            scope_path: "/d/p".to_owned(),
            evidence_id: ev("binding:critical")?,
        },
    ];
    let agent_id = id_lookup(&ids.agent, "target");
    let enterprise_agent = EnterpriseAgentV5 {
        agent_id: agent_id.clone(),
        environment: EnvironmentKindV5::Prod,
        manifest_digest_etag: etag(cell, "agent:manifest"),
        approved_registry_digest_etag: etag(cell, "agent:manifest"),
        attestation_synced_at_s: clock1 - 120,
        candidate_sponsor_person_id: if profile.agent_sponsor_sod {
            ids.person("alice")
        } else {
            ids.person("reviewer")
        },
        tool_bindings,
        agent_gen: 1,
        etag: etag(cell, "agent:target"),
        evidence_id: ev("agent:target")?,
    };
    let mut enterprise_agents = IndexMap::new();
    enterprise_agents.insert(agent_id.clone(), enterprise_agent);

    let aws = CloudPrincipalBindingV5 {
        cprin_id: id_lookup(&ids.cprin_aws, "primary"),
        cloud_provider: CloudProviderV5::Aws,
        linked_principal_id: acct_reviewer,
        aws_same_account_resource: true,
        aws_identity_allow: vec![
            "iam:PassRole".to_owned(),
            "sts:AssumeRole".to_owned(),
            "compute:RunWorkload".to_owned(),
        ],
        aws_resource_policy_allow: vec!["s3:DeleteBucket".to_owned(), "kms:Decrypt".to_owned()],
        aws_permission_boundary_allow: Some(vec![
            "iam:PassRole".to_owned(),
            "sts:AssumeRole".to_owned(),
            "compute:RunWorkload".to_owned(),
            "s3:DeleteBucket".to_owned(),
            "kms:Decrypt".to_owned(),
        ]),
        aws_scp_allow: vec![
            "iam:PassRole".to_owned(),
            "sts:AssumeRole".to_owned(),
            "compute:RunWorkload".to_owned(),
            "s3:DeleteBucket".to_owned(),
            "kms:Decrypt".to_owned(),
        ],
        aws_explicit_deny: Vec::new(),
        aws_passrole_target_actions: vec!["s3:DeleteBucket".to_owned()],
        aws_assumerole_chain_actions: vec!["kms:Decrypt".to_owned()],
        aws_trust_policy_valid: true,
        az_role_assignments: Vec::new(),
        az_deny_assignment_actions: Vec::new(),
        az_exempt_from_deny_assignment: false,
        az_unconstrained_role_assign_write_target_actions: Vec::new(),
        az_abac_delegation_constrained: false,
        az_entra_app_cred_target_actions: Vec::new(),
        gcp_resource_env_tag: "prod".to_owned(),
        gcp_allow_bindings: Vec::new(),
        gcp_v2_deny_actions: Vec::new(),
        gcp_sa_impersonation_target_actions: Vec::new(),
        gcp_wif_unscoped_repo_claim: false,
    };
    let azure = CloudPrincipalBindingV5 {
        cprin_id: id_lookup(&ids.cprin_az, "primary"),
        cloud_provider: CloudProviderV5::Azure,
        linked_principal_id: target_nhi_id,
        aws_same_account_resource: true,
        aws_identity_allow: Vec::new(),
        aws_resource_policy_allow: Vec::new(),
        aws_permission_boundary_allow: None,
        aws_scp_allow: Vec::new(),
        aws_explicit_deny: Vec::new(),
        aws_passrole_target_actions: Vec::new(),
        aws_assumerole_chain_actions: Vec::new(),
        aws_trust_policy_valid: false,
        az_role_assignments: vec![(
            vec![
                "Microsoft.Authorization/roleAssignments/write".to_owned(),
                "Microsoft.Storage/storageAccounts/delete".to_owned(),
            ],
            Vec::new(),
        )],
        az_deny_assignment_actions: Vec::new(),
        az_exempt_from_deny_assignment: false,
        az_unconstrained_role_assign_write_target_actions: vec![
            "Microsoft.Authorization/roleAssignments/write".to_owned(),
        ],
        az_abac_delegation_constrained: false,
        az_entra_app_cred_target_actions: vec![
            "Microsoft.Graph/appCredentials/write".to_owned(),
        ],
        gcp_resource_env_tag: "prod".to_owned(),
        gcp_allow_bindings: Vec::new(),
        gcp_v2_deny_actions: Vec::new(),
        gcp_sa_impersonation_target_actions: Vec::new(),
        gcp_wif_unscoped_repo_claim: false,
    };
    let gcp = CloudPrincipalBindingV5 {
        cprin_id: id_lookup(&ids.cprin_gcp, "primary"),
        cloud_provider: CloudProviderV5::Gcp,
        linked_principal_id: agent_id,
        aws_same_account_resource: true,
        aws_identity_allow: Vec::new(),
        aws_resource_policy_allow: Vec::new(),
        aws_permission_boundary_allow: None,
        aws_scp_allow: Vec::new(),
        aws_explicit_deny: Vec::new(),
        aws_passrole_target_actions: Vec::new(),
        aws_assumerole_chain_actions: Vec::new(),
        aws_trust_policy_valid: false,
        az_role_assignments: Vec::new(),
        az_deny_assignment_actions: Vec::new(),
        az_exempt_from_deny_assignment: false,
        az_unconstrained_role_assign_write_target_actions: Vec::new(),
        az_abac_delegation_constrained: false,
        az_entra_app_cred_target_actions: Vec::new(),
        gcp_resource_env_tag: "prod".to_owned(),
        gcp_allow_bindings: vec![(
            "prod".to_owned(),
            vec![
                "iam.serviceAccounts.actAs".to_owned(),
                "iam.serviceAccounts.getAccessToken".to_owned(),
            ],
        )],
        gcp_v2_deny_actions: Vec::new(),
        gcp_sa_impersonation_target_actions: vec!["storage.buckets.delete".to_owned()],
        gcp_wif_unscoped_repo_claim: true,
    };
    let ciem = CIEMTaskV5 {
        ciem_task_id: id_lookup(&ids.ciem, "task"),
        forbidden_standing_cloud_actions: vec![
            "Microsoft.Storage/storageAccounts/delete".to_owned(),
            "kms:Decrypt".to_owned(),
            "s3:DeleteBucket".to_owned(),
            "storage.buckets.delete".to_owned(),
        ],
        principals: vec![aws, azure, gcp],
        ciem_gen: 1,
        etag: etag(cell, "ciem:task"),
        evidence_id: ev("ciem:task")?,
    };
    let mut cloud_ciem_tasks = IndexMap::new();
    cloud_ciem_tasks.insert(ciem.ciem_task_id.clone(), ciem);

    let birthright_catalog = vec![
        BirthrightRuleV5 {
            rule_id: id_lookup(&ids.birthright, "base"),
            dept_prefix: "/d".to_owned(),
            allowed_affiliations: vec![AffiliationKindV5::Fte],
            role_id: ids.role("read"),
            grant_mode: GrantModeV5::Standing,
            scope_path: "/d".to_owned(),
            evidence_id: ev("birthright:base")?,
        },
        BirthrightRuleV5 {
            rule_id: id_lookup(&ids.birthright, "vendor_admin"),
            dept_prefix: "/d".to_owned(),
            allowed_affiliations: vec![AffiliationKindV5::Vendor],
            role_id: ids.role("review"),
            grant_mode: GrantModeV5::Standing,
            scope_path: "/d".to_owned(),
            evidence_id: ev("birthright:vendor_admin")?,
        },
        BirthrightRuleV5 {
            rule_id: id_lookup(&ids.birthright, "contractor_critical"),
            dept_prefix: "/d".to_owned(),
            allowed_affiliations: vec![AffiliationKindV5::Contractor],
            role_id: ids.role("wire"),
            grant_mode: GrantModeV5::Standing,
            scope_path: "/d".to_owned(),
            evidence_id: ev("birthright:contractor_critical")?,
        },
    ];
    let visible_kinds = [
        JMLTraceEventKindV5::Joiner,
        JMLTraceEventKindV5::NoShowCancel,
        JMLTraceEventKindV5::Mover,
        JMLTraceEventKindV5::Leaver,
    ];
    let mut visible_events = Vec::with_capacity(4);
    for (idx, kind) in visible_kinds.into_iter().enumerate() {
        let i = idx + 1;
        visible_events.push(JMLTraceEventV5 {
            event_id: ids.jml_event(&format!("visible_{i:02}")),
            event_kind: kind,
            affiliation: if i == 1 {
                AffiliationKindV5::Contractor
            } else {
                AffiliationKindV5::Fte
            },
            under_legal_hold: i == 4,
            owns_prod_nhi_or_agent: i == 4,
            has_active_tokens: i != 2,
            spoke_transient_error: matches!(i, 2 | 3),
            target_is_disconnected_itsm: profile.disconnected && matches!(i, 1 | 2),
            evidence_id: ev(&format!("jml:visible:{i}"))?,
        });
    }
    let workflow = JMLWorkflowTaskV5 {
        workflow_task_id: ids.workflow("task"),
        legal_hold_scope_mode: if matches!(cell.family_id.as_str(), "F08" | "F10") {
            LegalHoldScopeModeV5::IsolateForensicSnapshot
        } else {
            LegalHoldScopeModeV5::SuspendPreserveArchive
        },
        orphan_custody_mode: if matches!(cell.family_id.as_str(), "F04" | "F07") {
            OrphanCustodyModeV5::ImmediateSuspendAll
        } else {
            OrphanCustodyModeV5::HandoffToManagerElseSuspend
        },
        has_disconnected_spokes: profile.disconnected,
        contractor_sponsor_check_mode: if profile.multi_engagement {
            ContractorSponsorCheckModeV5::ExemptInternalConversion
        } else {
            ContractorSponsorCheckModeV5::StrictPreProvision
        },
        birthright_catalog,
        visible_trace_events: visible_events,
        draft_spec: draft_workflow_spec(cell, &profile),
        workflow_gen: 1,
        etag: etag(cell, "workflow:task"),
        evidence_id: ev("workflow:task")?,
    };
    let mut jml_workflow_tasks = IndexMap::new();
    jml_workflow_tasks.insert(workflow.workflow_task_id.clone(), workflow);

    let opt1_candidates = vec![
        OptCandidateActionV5 {
            candidate_id: ids.candidate("p1_admin"),
            action_kind: OptActionKindV5::ConvertGrantToEligible,
            target_person_id: Some(ids.person("reviewer")),
            target_grant_id: Some(ids.grant("reviewer_opt_admin")),
            role_id: None,
            scope_path: None,
            candidate_account_id: None,
            covered_scopes: Vec::new(),
            action_cost: 1,
        },
        OptCandidateActionV5 {
            candidate_id: ids.candidate("p1_read"),
            action_kind: OptActionKindV5::AssignRoleStanding,
            target_person_id: Some(ids.person("reviewer")),
            target_grant_id: None,
            role_id: Some(ids.role("change")),
            scope_path: Some("/d/p".to_owned()),
            candidate_account_id: None,
            covered_scopes: Vec::new(),
            action_cost: 2,
        },
    ];
    let p1_opt_id = ids.opt("p1");
    let mut optimization_tasks = IndexMap::new();
    optimization_tasks.insert(
        p1_opt_id.clone(),
        OptimizationTaskV5 {
            task_id: p1_opt_id,
            phase_kind: OptimizationPhaseKindV5::P1ZspRefactor,
            required_person_caps: vec![(
                ids.person("reviewer"),
                "change".to_owned(),
                "/d/p".to_owned(),
            )],
            required_review_scopes: Vec::new(),
            excluded_conflict_person_ids: Vec::new(),
            max_assignments_per_person: 2,
            candidates: opt1_candidates,
            etag: etag(cell, "opt:p1"),
            evidence_id: ev("opt:p1")?,
        },
    );

    let world = WorldStateV5 {
        phase_index: 1,
        world_rev: 0,
        clock_epoch_s: clock1,
        scope_catalog: sc,
        overlay,
        hr_persons: people,
        roles,
        grants,
        groups,
        idp_accounts,
        transfer_bridges: bridges,
        compensating_controls: controls,
        connector_schemas,
        connector_pages: vec![target_page],
        hr_source_mappings,
        baseline_nhis,
        target_nhis,
        baseline_agents,
        enterprise_agents,
        cloud_ciem_tasks,
        optimization_tasks,
        jml_workflow_tasks,
        cae_incidents: IndexMap::new(),
        uar_campaigns: IndexMap::new(),
        change_tickets: IndexMap::new(),
        historical_sessions: Vec::new(),
        jit_requests: IndexMap::new(),
        agent_delegation_requests: IndexMap::new(),
        prior_phase_ledger: None,
    };

    Ok((world, ids, profile))
}

pub(crate) fn sealed_jml_events(
    cell: &V5Cell,
    ids: &IdBook,
    profile: &FamilyProfile,
) -> Result<Vec<JMLTraceEventV5>, GeneratorError> {
    let codes = evidence_codes(&cell.episode_id);
    let kinds = [
        JMLTraceEventKindV5::Joiner,
        JMLTraceEventKindV5::NoShowCancel,
        JMLTraceEventKindV5::Mover,
        JMLTraceEventKindV5::Leaver,
    ];
    let affiliations = [
        AffiliationKindV5::Fte,
        AffiliationKindV5::Contractor,
        AffiliationKindV5::Intern,
        AffiliationKindV5::Vendor,
    ];
    let mut result = Vec::with_capacity(16);
    for index in 1usize..=16 {
        let aff_idx = ((index as u64).wrapping_add(cell.seed) % 4) as usize;
        result.push(JMLTraceEventV5 {
            event_id: ids.jml_event(&format!("sealed_{index:02}")),
            event_kind: kinds[(index - 1) % 4],
            affiliation: affiliations[aff_idx],
            under_legal_hold: matches!(index, 4 | 8 | 12 | 16),
            owns_prod_nhi_or_agent: matches!(index, 3 | 7 | 11 | 15),
            has_active_tokens: !matches!(index, 2 | 6 | 10 | 14),
            spoke_transient_error: matches!(index, 1 | 5 | 9 | 13),
            target_is_disconnected_itsm: profile.disconnected && matches!(index, 1 | 5 | 9 | 13),
            evidence_id: evidence(&codes, &format!("jml:sealed:{index}"))?,
        });
    }
    Ok(result)
}

pub(crate) fn build_phase2_world(
    phase1_world: &WorldStateV5,
    ids: &IdBook,
    profile: &FamilyProfile,
    cell: &V5Cell,
) -> Result<(WorldStateV5, Vec<String>), GeneratorError> {
    let codes = evidence_codes(&cell.episode_id);
    let ev = |label: &str| evidence(&codes, label);

    let clock1 = phase1_world.clock_epoch_s;
    let clock2 = clock1 + 86_400;

    let incident_person_name = if profile.retro_shock {
        "boomerang"
    } else {
        "alice"
    };
    let incident_person = ids.person(incident_person_name);
    let delta_people = vec![incident_person.clone()];

    let mut people = phase1_world.hr_persons.clone();
    {
        let entry = people.get_mut(&incident_person).expect("incident person exists");
        entry.lifecycle_gen += 1;
        entry.etag = etag(cell, &format!("person:{incident_person}:p2-refresh"));
        entry.evidence_id = ev(&format!("person:{incident_person_name}:p2-refresh"))?;
    }
    if profile.retro_shock {
        let boomerang_id = ids.person("boomerang");
        let boomerang_new_eng = ids.eng("boomerang_new");
        let entry = people.get_mut(&boomerang_id).expect("boomerang exists");
        for eng in &mut entry.engagements {
            if eng.eng_id == boomerang_new_eng {
                eng.retro_terminated_effective_s = Some(clock1 - 600);
            }
        }
        entry.etag = etag(cell, "person:boomerang:p2-retro");
        entry.evidence_id = ev("person:boomerang:p2-retro")?;
    }

    let target_sponsor = if profile.agent_sponsor_sod {
        ids.person("alice")
    } else {
        ids.person("reviewer")
    };
    let mut agent = phase1_world
        .enterprise_agents
        .values()
        .next()
        .expect("at least one enterprise agent")
        .clone();
    agent.candidate_sponsor_person_id = target_sponsor;
    let mut agents = IndexMap::new();
    agents.insert(agent.agent_id.clone(), agent.clone());

    let trigger = if profile.retro_shock || profile.recursive_cae {
        CAETriggerKindV5::HrTerminationEvent
    } else if cell.family_id == "F06" {
        CAETriggerKindV5::BenignSessionRenewal
    } else {
        CAETriggerKindV5::CredentialCompromise
    };
    let event_at = clock2 - 600;
    let acct_primary = ids.account("primary");
    let acct_manager = ids.account("manager");
    let acct_reviewer = ids.account("reviewer");
    let acct_mover = ids.account("mover");

    let token_root = CAETokenV5 {
        token_id: ids.token("root"),
        account_id: acct_primary.clone(),
        token_kind: CAETokenKindV5::Refresh,
        parent_token_id: None,
        binding_kind: CAETokenBindingKindV5::BoundDpop,
        cae_capable_spoke: false,
        issued_at_s: clock2 - 8_000,
        token_exp_s: clock2 + 7_200,
        revoke_ack_at_s: profile.recursive_cae.then_some(clock2 - 100),
        next_spoke_poll_sync_at_s: clock2 + 1_800,
        agent_id: None,
        evidence_id: ev("token:root")?,
    };
    let token_refresh = CAETokenV5 {
        token_id: ids.token("refresh"),
        account_id: acct_primary.clone(),
        token_kind: CAETokenKindV5::Refresh,
        parent_token_id: Some(token_root.token_id.clone()),
        binding_kind: CAETokenBindingKindV5::BoundDpop,
        cae_capable_spoke: false,
        issued_at_s: clock2 - 4_000,
        token_exp_s: clock2 + 3_600,
        revoke_ack_at_s: None,
        next_spoke_poll_sync_at_s: clock2 + 1_200,
        agent_id: None,
        evidence_id: ev("token:refresh")?,
    };
    let token_access = CAETokenV5 {
        token_id: ids.token("access"),
        account_id: acct_primary.clone(),
        token_kind: CAETokenKindV5::Access,
        parent_token_id: Some(token_refresh.token_id.clone()),
        binding_kind: CAETokenBindingKindV5::BoundDpop,
        cae_capable_spoke: true,
        issued_at_s: clock2 - 1_800,
        token_exp_s: clock2 + 1_800,
        revoke_ack_at_s: None,
        next_spoke_poll_sync_at_s: clock2 + if profile.recursive_cae { 150 } else { 900 },
        agent_id: None,
        evidence_id: ev("token:access")?,
    };
    let token_obo = CAETokenV5 {
        token_id: ids.token("obo"),
        account_id: acct_manager.clone(),
        token_kind: CAETokenKindV5::OboAgent,
        parent_token_id: Some(token_root.token_id.clone()),
        binding_kind: CAETokenBindingKindV5::BearerUnbound,
        cae_capable_spoke: true,
        issued_at_s: clock2 - 900,
        token_exp_s: clock2 + 900,
        revoke_ack_at_s: None,
        next_spoke_poll_sync_at_s: clock2 + 300,
        agent_id: Some(agent.agent_id.clone()),
        evidence_id: ev("token:obo")?,
    };
    let incident = CAEIncidentV5 {
        incident_id: ids.incident("incident"),
        person_id: incident_person,
        trigger_kind: trigger,
        event_ts_s: event_at,
        tokens: vec![token_root, token_refresh, token_access, token_obo],
        etag: etag(cell, "incident"),
        evidence_id: ev("incident")?,
    };

    let primary_ticket_id = ids.ticket("primary");
    let mut tickets = IndexMap::new();
    tickets.insert(
        primary_ticket_id.clone(),
        ChangeTicketV5 {
            ticket_id: primary_ticket_id.clone(),
            external_system: ExternalTicketSystemV5::ServicenowRitm,
            external_ticket_status: ExternalTicketStatusV5::ApprovedExternal,
            bound_requester_account_id: acct_reviewer.clone(),
            approved_role_id: ids.role("deploy_admin"),
            approved_scope_path: "/d/p".to_owned(),
            target_resource: "fin".to_owned(),
            single_use: true,
            approved_by_account_id: acct_mover,
            approved_at_s: clock1 - 300,
            expires_at_s: clock2 + 7_200,
            etag: etag(cell, "ticket:primary"),
            evidence_id: ev("ticket:primary")?,
        },
    );

    let mut requests = IndexMap::new();
    let req_first = ids.request("first");
    requests.insert(
        req_first.clone(),
        JITRequestV5 {
            request_id: req_first,
            eval_seq: 1,
            requester_account_id: acct_reviewer.clone(),
            requested_role_id: ids.role("deploy_admin"),
            requested_scope_path: "/d/p".to_owned(),
            target_resource: "fin".to_owned(),
            request_channel: JITRequestChannelV5::StandardTicket,
            change_ticket_id: Some(primary_ticket_id.clone()),
            proposed_approver_account_id: acct_manager.clone(),
            requested_ttl_s: 1_200,
            etag: etag(cell, "request:first"),
            evidence_id: ev("request:first")?,
        },
    );
    let req_second = ids.request("second");
    requests.insert(
        req_second.clone(),
        JITRequestV5 {
            request_id: req_second,
            eval_seq: 2,
            requester_account_id: acct_reviewer.clone(),
            requested_role_id: ids.role("deploy_admin"),
            requested_scope_path: "/d/p".to_owned(),
            target_resource: "fin".to_owned(),
            request_channel: JITRequestChannelV5::StandardTicket,
            change_ticket_id: Some(primary_ticket_id),
            proposed_approver_account_id: acct_manager.clone(),
            requested_ttl_s: 900,
            etag: etag(cell, "request:second"),
            evidence_id: ev("request:second")?,
        },
    );
    let req_replay = ids.request("replay");
    requests.insert(
        req_replay.clone(),
        JITRequestV5 {
            request_id: req_replay,
            eval_seq: 3,
            requester_account_id: acct_primary.clone(),
            requested_role_id: ids.role("wire"),
            requested_scope_path: "/d/p".to_owned(),
            target_resource: "fin".to_owned(),
            request_channel: JITRequestChannelV5::EmergencyBreakGlass,
            change_ticket_id: None,
            proposed_approver_account_id: acct_manager.clone(),
            requested_ttl_s: 900,
            etag: etag(cell, "request:replay"),
            evidence_id: ev("request:replay")?,
        },
    );

    let mut delegation_requests = IndexMap::new();
    let dl_root = ids.delegation("root");
    delegation_requests.insert(
        dl_root.clone(),
        AgentDelegationRequestV5 {
            request_id: dl_root.clone(),
            eval_seq: 4,
            workflow_id: ids.workflow("task"),
            agent_id: agent.agent_id.clone(),
            delegator_account_id: acct_manager.clone(),
            parent_delegation_request_id: None,
            delegation_hop: 1,
            requested_role_id: ids.role("deploy_admin"),
            requested_scope_path: "/d/p".to_owned(),
            target_resource: "fin".to_owned(),
            proposed_hitl_approver_account_id: Some(acct_reviewer.clone()),
            requested_ttl_s: 600,
            etag: etag(cell, "delegation:root"),
            evidence_id: ev("delegation:root")?,
        },
    );
    let dl_child = ids.delegation("child");
    delegation_requests.insert(
        dl_child.clone(),
        AgentDelegationRequestV5 {
            request_id: dl_child,
            eval_seq: 5,
            workflow_id: ids.workflow("task"),
            agent_id: agent.agent_id,
            delegator_account_id: acct_manager.clone(),
            parent_delegation_request_id: Some(dl_root),
            delegation_hop: 2,
            requested_role_id: ids.role("deploy"),
            requested_scope_path: "/d/p".to_owned(),
            target_resource: "fin".to_owned(),
            proposed_hitl_approver_account_id: None,
            requested_ttl_s: 300,
            etag: etag(cell, "delegation:child"),
            evidence_id: ev("delegation:child")?,
        },
    );

    let sessions = vec![HistoricalSessionV5 {
        session_id: ids.session("prior"),
        account_id: acct_primary,
        role_id: if profile.jit_replay_cooldown {
            ids.role("wire")
        } else {
            ids.role("read")
        },
        scope_path: "/d/p".to_owned(),
        session_kind: if profile.jit_replay_cooldown {
            HistoricalSessionKindV5::SharedBreakGlass
        } else {
            HistoricalSessionKindV5::PersonalJit
        },
        start_s: clock2 - 900,
        scheduled_end_s: clock2 - 300,
        revoked_at_s: None,
        audit_events_count: 1,
        last_audit_event_s: Some(clock2 - 350),
        evidence_id: ev("session:prior")?,
    }];

    let reviewer_review_grant = ids.grant("reviewer_review");
    let manager_review_grant = ids.grant("manager_review");
    type ItemSpec<'a> = (&'static str, &'a String, &'a String, bool, &'static str);
    type CampaignSpec<'a> = (&'static str, Vec<ItemSpec<'a>>, bool);
    let campaign_specs: Vec<CampaignSpec<'_>> =
        if profile.sequential_uar {
            vec![
                (
                    "first",
                    vec![("first", &acct_reviewer, &reviewer_review_grant, true, "reviewer")],
                    false,
                ),
                (
                    "second",
                    vec![("second", &acct_manager, &manager_review_grant, true, "manager")],
                    false,
                ),
            ]
        } else if profile.inherited_group_lock {
            vec![(
                "first",
                vec![
                    ("first", &acct_reviewer, &reviewer_review_grant, false, "reviewer"),
                    ("second", &acct_manager, &manager_review_grant, false, "manager"),
                ],
                true,
            )]
        } else {
            vec![(
                "first",
                vec![("first", &acct_reviewer, &reviewer_review_grant, true, "manager")],
                false,
            )]
        };

    let mut campaigns = IndexMap::new();
    for (campaign_name, item_specs, inherited_lock_case) in campaign_specs {
        let campaign_id = ids.campaign(campaign_name);
        let mut items = Vec::with_capacity(item_specs.len());
        for (item_name, account_id, grant_id, used, reviewer_name) in item_specs {
            items.push(UARItemV5 {
                item_id: ids.item(item_name),
                account_id: account_id.clone(),
                grant_id: grant_id.clone(),
                assigned_reviewer_account_id: ids.account(reviewer_name),
                used_within_lookback: used,
                evidence_id: ev(&format!("uar:item:{item_name}:{campaign_name}"))?,
            });
        }
        campaigns.insert(
            campaign_id.clone(),
            UARCampaignV5 {
                campaign_id,
                app_id: "fin".to_owned(),
                scope_path: "/d/p".to_owned(),
                admin_role_id: ids.role("review"),
                m_standing: 1,
                m_eligible: 0,
                preserve_standing_floor_override:
                    UARPreserveStandingFloorOverrideV5::UseOverlayDefault,
                backup_reviewer_account_ids: vec![acct_manager.clone(), acct_reviewer.clone()],
                candidate_custodian_account_ids: if inherited_lock_case {
                    vec![acct_manager.clone()]
                } else {
                    Vec::new()
                },
                items,
                etag: etag(cell, &format!("campaign:{campaign_name}")),
                evidence_id: ev(&format!("campaign:{campaign_name}"))?,
            },
        );
    }

    let p2_opt_candidates = vec![
        OptCandidateActionV5 {
            candidate_id: ids.candidate("p2_reviewer_a"),
            action_kind: OptActionKindV5::SelectReviewerAccount,
            target_person_id: None,
            target_grant_id: None,
            role_id: Some(ids.role("review")),
            scope_path: None,
            candidate_account_id: Some(acct_reviewer),
            covered_scopes: vec!["/d/p".to_owned()],
            action_cost: 1,
        },
        OptCandidateActionV5 {
            candidate_id: ids.candidate("p2_reviewer_b"),
            action_kind: OptActionKindV5::SelectReviewerAccount,
            target_person_id: None,
            target_grant_id: None,
            role_id: Some(ids.role("review")),
            scope_path: None,
            candidate_account_id: Some(acct_manager),
            covered_scopes: vec!["/d/p".to_owned()],
            action_cost: 2,
        },
    ];
    let p2_opt_id = ids.opt("p2");
    let p2_opt = OptimizationTaskV5 {
        task_id: p2_opt_id.clone(),
        phase_kind: OptimizationPhaseKindV5::P2ReviewerAssignment,
        required_person_caps: Vec::new(),
        required_review_scopes: vec!["/d/p".to_owned()],
        excluded_conflict_person_ids: Vec::new(),
        max_assignments_per_person: 1,
        candidates: p2_opt_candidates,
        etag: etag(cell, "opt:p2"),
        evidence_id: ev("opt:p2")?,
    };
    let mut updated_optimizations = IndexMap::new();
    updated_optimizations.insert(p2_opt_id, p2_opt);

    let mut cae_incidents = IndexMap::new();
    cae_incidents.insert(incident.incident_id.clone(), incident);

    let mut next_world = phase1_world.clone();
    next_world.phase_index = 2;
    next_world.clock_epoch_s = clock2;
    next_world.hr_persons = people;
    next_world.enterprise_agents = agents;
    next_world.optimization_tasks = updated_optimizations;
    next_world.cae_incidents = cae_incidents;
    next_world.uar_campaigns = campaigns;
    next_world.change_tickets = tickets;
    next_world.historical_sessions = sessions;
    next_world.jit_requests = requests;
    next_world.agent_delegation_requests = delegation_requests;

    Ok((next_world, delta_people))
}
