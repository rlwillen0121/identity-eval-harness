//! `SPEC-v5.2` §8.1 offline deterministic baselines (`G-BASELINE-V52`): the pinned constructors of
//! `oracle_gold`, `noop_starter_commit`, `schema_minimal`, `deny_everything`, `grant_everything`, and
//! the eight section-level `gold_minus_<category>` ablations.
//!
//! Rules the specification leaves open are resolved here deterministically and listed in
//! [`BASELINE_RESOLVED_AMBIGUITIES_V52`] so they are published with the validation report.

use std::collections::{BTreeMap, BTreeSet, VecDeque};

use ib_v52_schemas::{
    AccessReviewsConfigV52, AccountGroupEdgeV52, AffiliationPolicyV52, AgentDecisionV52,
    AgentGovernanceActionV52, AttributeSyncConfigV52, CategoryIdV52, CloudCiemConfigV52,
    CloudEscalationVectorV52, CloudPrincipalRemediationV52, CloudRemediationActionV52,
    ConnectorsConfigV52, ContingentExpiryWorkflowV52, DelegationDecisionV52,
    EmergencyLeaverWorkflowV52, EnterpriseBrownfieldWorldV52, EpisodeV52, GardenLeaveWorkflowV52,
    GoLivePlanConfigV52, GoLiveSafetyGatesV52, HeadlessAccessRequestWorkflowV52,
    IdentityProfileConfigV52, IgaDeploymentConfigV52, IgaEngineBackendV52,
    LifecycleStatesConfigV52, NhiAndAgentConfigV52, NhiDecisionV52, NhiDispositionV52,
    OboDelegationVerdictV52, RoleModelConfigV52, SealedEpisodeOracleV52, TransformsConfigV52,
    UarDecisionKindV52, UarItemDecisionV52, UarSubjectKindV52, UsernameBasePatternV52,
    UsernamePolicyConfigV52, WorkflowsConfigV52, apply_category_section_v52,
};
use indexmap::IndexMap;
use serde::{Deserialize, Serialize};

const SCHEMA_VERSION: &str = "v5.2";

/// A §8.1 baseline row.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BaselineKindV52 {
    OracleGold,
    NoopStarterCommit,
    SchemaMinimal,
    DenyEverything,
    GrantEverything,
    GoldMinus(CategoryIdV52),
}

impl BaselineKindV52 {
    /// All 13 rows in table order: 5 whole-bundle baselines, then the 8 ablations in category order.
    #[must_use]
    pub fn all() -> Vec<Self> {
        let mut rows = vec![
            Self::OracleGold,
            Self::NoopStarterCommit,
            Self::SchemaMinimal,
            Self::DenyEverything,
            Self::GrantEverything,
        ];
        rows.extend(CategoryIdV52::ALL.iter().map(|&c| Self::GoldMinus(c)));
        rows
    }

    /// Row identifier (`oracle_gold`, …, `gold_minus_<category>`).
    #[must_use]
    pub fn id(self) -> String {
        match self {
            Self::OracleGold => "oracle_gold".to_owned(),
            Self::NoopStarterCommit => "noop_starter_commit".to_owned(),
            Self::SchemaMinimal => "schema_minimal".to_owned(),
            Self::DenyEverything => "deny_everything".to_owned(),
            Self::GrantEverything => "grant_everything".to_owned(),
            Self::GoldMinus(c) => format!("gold_minus_{}", c.as_str()),
        }
    }

    /// The four non-oracle baselines that define the shortcut ceiling (ablations excluded).
    #[must_use]
    pub const fn is_shortcut(self) -> bool {
        matches!(
            self,
            Self::NoopStarterCommit
                | Self::SchemaMinimal
                | Self::DenyEverything
                | Self::GrantEverything
        )
    }
}

/// Resolutions of points §8.1 leaves undefined, published in the validation report.
pub const BASELINE_RESOLVED_AMBIGUITIES_V52: [&str; 5] = [
    "deny_everything UAR edges, human subjects (human_primary, human_admin_secondary): the subject's accounts are every account of world.spoke_accounts[target_spoke_id] whose (spoke_id, account_id) the starter identity configuration correlates to person subject_id, primary and adm_* secondary alike.",
    "deny_everything UAR edges, non-human subjects (nhi_workload, ai_agent, cloud_principal): no identity configuration correlates a non-human subject, so the subject's only account is identified by exact ID with no transform or correlation rule: for nhi_workload, the record's (spoke_id, account_or_principal_id); for ai_agent and cloud_principal, account_id = subject_id. The account must also be in world.spoke_accounts[target_spoke_id] and on the target spoke.",
    "deny_everything UAR edges, starter correlation failure: if the starter configuration cannot be evaluated (the engine rejects it), human subjects resolve to no accounts and the failure is recorded in the baseline notes.",
    "Baseline scoring failure: if the engine rejects a constructed bundle outright, the slot scores as a committed failure (G-SRR = 0, State-Acc = 0, Macro-Cat-Acc = 0, every category failing, OG = AB = 0) and the engine error is recorded.",
    "gold_minus_<category> 'exercises it': a category is exercised by an episode iff the oracle_gold score passes that category there (category pass requires at least one assertion), so with G-GOLD-REPLAY-V52 passing every category is exercised in every episode.",
];

/// Published ablation field paths (§8.1 ownership table), one entry per category.
#[must_use]
pub fn ablation_field_paths_v52() -> Vec<(CategoryIdV52, Vec<&'static str>)> {
    vec![
        (
            CategoryIdV52::IdentityResolution,
            vec![
                "config/transforms.json (whole file)",
                "config/username_policy.json (whole file)",
                "config/identity_profile.json: schema_version, authoritative_sources, attribute_mappings, affiliation_policy, spoke_correlation, correlate_admin_secondary_prefix (every member except service_account_classifiers)",
            ],
        ),
        (
            CategoryIdV52::HumanLifecycle,
            vec![
                "config/lifecycle_states.json (whole file), then for every state s present in the starter: states[s].spoke_disposition_overrides[\"sap\"|\"epic\"] copied from gold where present (existing key overwritten in place, new key appended)",
                "config/workflows.json: emergency_leaver",
                "config/workflows.json: garden_leave",
                "config/workflows.json: contingent_expiry",
            ],
        ),
        (
            CategoryIdV52::NhiAgentGovernance,
            vec!["config/nhi_and_agents.json (whole file)"],
        ),
        (
            CategoryIdV52::IntegrationFulfillment,
            vec![
                "config/connectors.json (whole file)",
                "config/workflows.json: headless_access_request.post_provisioning_webhook",
                "config/lifecycle_states.json: gold kept, with keys \"sap\" and \"epic\" removed from every states[*].spoke_disposition_overrides",
            ],
        ),
        (
            CategoryIdV52::AccessPolicy,
            vec![
                "config/role_model.json (whole file)",
                "config/workflows.json: headless_access_request (every member except post_provisioning_webhook, which stays gold)",
            ],
        ),
        (
            CategoryIdV52::CloudEntitlements,
            vec!["config/cloud_ciem.json (whole file)"],
        ),
        (
            CategoryIdV52::CertificationRemediation,
            vec!["config/access_reviews.json (whole file)"],
        ),
        (
            CategoryIdV52::ChangeCutoverSafety,
            vec![
                "config/attribute_sync.json (whole file)",
                "config/golive_plan.json (whole file)",
                "config/identity_profile.json: service_account_classifiers",
            ],
        ),
    ]
}

/// A constructed baseline bundle plus construction notes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ConstructedBaselineV52 {
    pub kind: BaselineKindV52,
    pub config: IgaDeploymentConfigV52,
    pub notes: Vec<String>,
}

/// Scope a baseline config so out-of-scope categories keep starter/baseline fields untouched.
fn scope_baseline_config(
    episode: &EpisodeV52,
    raw: IgaDeploymentConfigV52,
) -> IgaDeploymentConfigV52 {
    let scored = episode.effective_scored_categories();
    if scored.len() >= CategoryIdV52::ALL.len() {
        return raw;
    }
    let mut scoped = episode.starter_configs.clone();
    for cat in scored {
        apply_category_section_v52(&mut scoped, &raw, cat);
    }
    scoped
}

/// Construct baseline `kind` for one episode.
#[must_use]
pub fn construct_baseline_v52(
    kind: BaselineKindV52,
    episode: &EpisodeV52,
    oracle: &SealedEpisodeOracleV52,
    engine: &dyn IgaEngineBackendV52,
) -> ConstructedBaselineV52 {
    let mut notes = Vec::new();
    let config = match kind {
        BaselineKindV52::OracleGold => oracle.gold_config.clone(),
        BaselineKindV52::NoopStarterCommit => episode.starter_configs.clone(),
        BaselineKindV52::SchemaMinimal => {
            scope_baseline_config(episode, schema_minimal_config_v52())
        }
        BaselineKindV52::DenyEverything => {
            let raw = deny_everything_config_v52(episode, engine, &mut notes);
            scope_baseline_config(episode, raw)
        }
        BaselineKindV52::GrantEverything => {
            scope_baseline_config(episode, grant_everything_config_v52(episode))
        }
        BaselineKindV52::GoldMinus(category) => {
            gold_minus_config_v52(category, &episode.starter_configs, &oracle.gold_config)
        }
    };
    ConstructedBaselineV52 {
        kind,
        config,
        notes,
    }
}

/// `schema_minimal`: every file at `"schema_version": "v5.2"`, `Default` scalars (`0`, `false`, `""`,
/// `None`), empty collections, and the first-declared variant of every required enum.
#[must_use]
pub fn schema_minimal_config_v52() -> IgaDeploymentConfigV52 {
    let v = || SCHEMA_VERSION.to_owned();
    IgaDeploymentConfigV52 {
        transforms: TransformsConfigV52 {
            schema_version: v(),
            transforms: IndexMap::new(),
        },
        identity_profile: IdentityProfileConfigV52 {
            schema_version: v(),
            authoritative_sources: Vec::new(),
            attribute_mappings: IndexMap::new(),
            affiliation_policy: AffiliationPolicyV52 {
                precedence_order: Vec::new(),
                preserve_active_student_on_worker_end: false,
                allow_additive_affiliations: false,
                furlough_retained_affiliations: Vec::new(),
            },
            spoke_correlation: IndexMap::new(),
            correlate_admin_secondary_prefix: None,
            service_account_classifiers: Vec::new(),
        },
        username_policy: UsernamePolicyConfigV52 {
            schema_version: v(),
            use_preferred_first_name: false,
            base_pattern: UsernameBasePatternV52::FirstInitialLast,
            strip_diacritics: false,
            lowercase: false,
            allowed_sam_punctuation: Vec::new(),
            sam_account_name_max_len: 0,
            reserve_len_for_collision_suffix: false,
            collision_counter_start: 0,
            default_upn_domain: String::new(),
            upn_domain_by_affiliation: IndexMap::new(),
            collision_check_sources: Vec::new(),
            preserve_correlated_identifiers: false,
        },
        lifecycle_states: LifecycleStatesConfigV52 {
            schema_version: v(),
            revoke_oauth_and_refresh_trees_on_disable: false,
            evaluation_order: Vec::new(),
            states: IndexMap::new(),
        },
        connectors: ConnectorsConfigV52 {
            schema_version: v(),
            connectors: IndexMap::new(),
        },
        attribute_sync: AttributeSyncConfigV52 {
            schema_version: v(),
            exclude_service_accounts: false,
            exclude_admin_secondary_accounts: false,
            max_allowed_null_overwrites: 0,
            max_modify_blast_radius_bps: 0,
            mappings: Vec::new(),
        },
        role_model: RoleModelConfigV52 {
            schema_version: v(),
            exclude_service_accounts_from_peer_mining: false,
            forbid_prod_and_nonprod_mixing_in_role: false,
            group_classifications: IndexMap::new(),
            roles: Vec::new(),
            sod_policies: Vec::new(),
        },
        workflows: WorkflowsConfigV52 {
            schema_version: v(),
            emergency_leaver: EmergencyLeaverWorkflowV52 {
                revoke_sso_and_refresh_tokens_immediately: false,
                walk_live_manager_chain: false,
                eligible_custodian_states: Vec::new(),
                fallback_governance_mailbox: String::new(),
                set_exchange_ooo_auto_reply: false,
                include_resolved_custodian_email_in_ooo: false,
                transfer_mailbox_and_onedrive_to_custodian: false,
                open_itsm_tasks: Vec::new(),
            },
            garden_leave: GardenLeaveWorkflowV52 {
                silent_access_cut_no_termination_ooo: false,
                preserve_hr_payroll_feed_active: false,
                notify_security_and_legal_queue: None,
            },
            contingent_expiry: ContingentExpiryWorkflowV52 {
                grace_period_days: 0,
                disable_auth_but_retain_account_during_grace: false,
                walk_live_sponsor_then_manager_for_extension_notice: false,
                fallback_vms_pmo_queue: String::new(),
            },
            headless_access_request: HeadlessAccessRequestWorkflowV52 {
                eligible_requester_states: Vec::new(),
                deny_contractor_privileged_admin_roles: false,
                require_live_manager_approval: false,
                require_app_owner_approval_for_requestable: false,
                block_sod_violating_requests: false,
                block_delegate_self_approval_and_cycles: false,
                post_provisioning_webhook: None,
            },
        },
        golive_plan: GoLivePlanConfigV52 {
            schema_version: v(),
            ordered_phases: Vec::new(),
            safety_gates: GoLiveSafetyGatesV52 {
                abort_if_null_attribute_overwrites_gt: 0,
                abort_if_uncorrelated_human_disable_count_gt: 0,
                abort_if_service_account_targeted_for_disable: false,
                abort_if_schema_validation_errors_gt: 0,
                abort_if_cohort_disable_ratio_bps_gt: 0,
                require_pilot_ou_validation_before_global_enable: false,
            },
        },
        nhi_and_agents: NhiAndAgentConfigV52 {
            schema_version: v(),
            nhi_decisions: IndexMap::new(),
            agent_decisions: IndexMap::new(),
            delegation_decisions: IndexMap::new(),
        },
        cloud_ciem: CloudCiemConfigV52 {
            schema_version: v(),
            principals: IndexMap::new(),
        },
        access_reviews: AccessReviewsConfigV52 {
            schema_version: v(),
            items: IndexMap::new(),
        },
    }
}

/// Starter files 1–9 (`transforms` … `golive_plan`), with files 10–12 still to be constructed.
fn starter_files_1_to_9(starter: &IgaDeploymentConfigV52) -> IgaDeploymentConfigV52 {
    let mut cfg = starter.clone();
    cfg.nhi_and_agents = NhiAndAgentConfigV52 {
        schema_version: SCHEMA_VERSION.to_owned(),
        nhi_decisions: IndexMap::new(),
        agent_decisions: IndexMap::new(),
        delegation_decisions: IndexMap::new(),
    };
    cfg.cloud_ciem = CloudCiemConfigV52 {
        schema_version: SCHEMA_VERSION.to_owned(),
        principals: IndexMap::new(),
    };
    cfg.access_reviews = AccessReviewsConfigV52 {
        schema_version: SCHEMA_VERSION.to_owned(),
        items: IndexMap::new(),
    };
    cfg
}

/// `deny_everything` (§8.1): starter files 1–9, maximal gates, and maximal restriction in files 10–12.
pub fn deny_everything_config_v52(
    episode: &EpisodeV52,
    engine: &dyn IgaEngineBackendV52,
    notes: &mut Vec<String>,
) -> IgaDeploymentConfigV52 {
    let world = &episode.world;
    let mut cfg = starter_files_1_to_9(&episode.starter_configs);
    cfg.golive_plan.safety_gates = GoLiveSafetyGatesV52 {
        abort_if_null_attribute_overwrites_gt: 0,
        abort_if_uncorrelated_human_disable_count_gt: 0,
        abort_if_service_account_targeted_for_disable: true,
        abort_if_schema_validation_errors_gt: 0,
        abort_if_cohort_disable_ratio_bps_gt: 0,
        require_pilot_ou_validation_before_global_enable: true,
    };
    for nhi in &world.nhi_workloads {
        cfg.nhi_and_agents.nhi_decisions.insert(
            nhi.nhi_id.clone(),
            NhiDecisionV52 {
                disposition: NhiDispositionV52::QuarantineOrphanOrExpiredSecret,
                resolved_custodian_id: None,
            },
        );
    }
    for agent in &world.ai_agents {
        cfg.nhi_and_agents.agent_decisions.insert(
            agent.agent_id.clone(),
            AgentDecisionV52 {
                action: AgentGovernanceActionV52::QuarantineUnattestedOrShadow,
                resolved_sponsor_person_key: None,
                approved_autonomous_scopes: Vec::new(),
                approved_hitl_scopes: Vec::new(),
            },
        );
    }
    for req in &world.agent_delegation_requests {
        cfg.nhi_and_agents.delegation_decisions.insert(
            req.request_id.clone(),
            DelegationDecisionV52 {
                decision: OboDelegationVerdictV52::DenyScopeOrHopExcess,
                verified_approver_person_key: None,
            },
        );
    }
    for p in &world.cloud_principals {
        cfg.cloud_ciem.principals.insert(
            p.principal_id.clone(),
            CloudPrincipalRemediationV52 {
                escalation_vector: CloudEscalationVectorV52::None,
                action: CloudRemediationActionV52::RevokeOrphanPrincipal,
                retained_permissions: Vec::new(),
            },
        );
    }

    let correlated: Vec<(String, String, String)> = match engine.correlated_spoke_accounts(
        world,
        &episode.starter_configs,
        &episode.cutover_date,
    ) {
        Ok(rows) => rows,
        Err(err) => {
            notes.push(format!(
                "starter correlation failed ({err}); human UAR subjects resolve to no accounts"
            ));
            Vec::new()
        }
    };
    for campaign in &world.uar_campaigns {
        for item in &campaign.items {
            cfg.access_reviews.items.insert(
                item.item_id.clone(),
                UarItemDecisionV52 {
                    effective_reviewer_person_key: item.assigned_reviewer_person_key.clone(),
                    decision: UarDecisionKindV52::RevokeAllSubjectPaths,
                    remove_account_group_edges: deny_everything_uar_edges(world, item, &correlated),
                },
            );
        }
    }
    cfg
}

/// The edge set `E` of a `deny_everything` UAR item (§8.1, with the documented non-human rule).
fn deny_everything_uar_edges(
    world: &EnterpriseBrownfieldWorldV52,
    item: &ib_v52_schemas::UarReviewItemRecordV52,
    correlated: &[(String, String, String)],
) -> Vec<AccountGroupEdgeV52> {
    let target_spoke = item.target_spoke_id.as_str();
    let Some(spoke_accounts) = world.spoke_accounts.get(target_spoke) else {
        return Vec::new();
    };
    let subject_account_ids: BTreeSet<&str> = match item.subject_kind {
        UarSubjectKindV52::HumanPrimary | UarSubjectKindV52::HumanAdminSecondary => correlated
            .iter()
            .filter(|(spoke, _, person)| spoke == target_spoke && *person == item.subject_id)
            .map(|(_, account, _)| account.as_str())
            .collect(),
        UarSubjectKindV52::NhiWorkload => world
            .nhi_workloads
            .iter()
            .filter(|n| n.nhi_id == item.subject_id && n.spoke_id == target_spoke)
            .map(|n| n.account_or_principal_id.as_str())
            .collect(),
        UarSubjectKindV52::AiAgent | UarSubjectKindV52::CloudPrincipal => {
            std::iter::once(item.subject_id.as_str()).collect()
        }
    };

    let parents: BTreeMap<&str, &[String]> = world
        .group_catalog
        .iter()
        .map(|(gid, g)| (gid.as_str(), g.member_of_group_ids.as_slice()))
        .collect();
    let reaches_target = |start: &str| -> bool {
        let mut seen: BTreeSet<&str> = BTreeSet::new();
        let mut queue: VecDeque<&str> = VecDeque::from([start]);
        while let Some(g) = queue.pop_front() {
            if g == item.target_entitlement_id {
                return true;
            }
            if !seen.insert(g) {
                continue;
            }
            if let Some(ps) = parents.get(g) {
                queue.extend(ps.iter().map(|p| p.trim()).filter(|p| !p.is_empty()));
            }
        }
        false
    };

    let mut edges: BTreeSet<AccountGroupEdgeV52> = BTreeSet::new();
    for acct in spoke_accounts {
        if !subject_account_ids.contains(acct.account_id.as_str()) {
            continue;
        }
        for group_id in &acct.entitlement_ids {
            if reaches_target(group_id.trim()) {
                edges.insert(AccountGroupEdgeV52 {
                    spoke_id: target_spoke.to_owned(),
                    account_id: acct.account_id.clone(),
                    group_id: group_id.clone(),
                });
            }
        }
    }
    edges.into_iter().collect()
}

/// `grant_everything` (§8.1): starter files 1–9, disabled gates, and maximal permission in files 10–12.
#[must_use]
pub fn grant_everything_config_v52(episode: &EpisodeV52) -> IgaDeploymentConfigV52 {
    let world = &episode.world;
    let mut cfg = starter_files_1_to_9(&episode.starter_configs);
    cfg.golive_plan.safety_gates = GoLiveSafetyGatesV52 {
        abort_if_null_attribute_overwrites_gt: usize::MAX,
        abort_if_uncorrelated_human_disable_count_gt: usize::MAX,
        abort_if_service_account_targeted_for_disable: false,
        abort_if_schema_validation_errors_gt: usize::MAX,
        abort_if_cohort_disable_ratio_bps_gt: u32::MAX,
        require_pilot_ou_validation_before_global_enable: false,
    };
    for nhi in &world.nhi_workloads {
        cfg.nhi_and_agents.nhi_decisions.insert(
            nhi.nhi_id.clone(),
            NhiDecisionV52 {
                disposition: NhiDispositionV52::KeepActiveOwnerVerified,
                resolved_custodian_id: nhi.owner_person_key.clone(),
            },
        );
    }
    for agent in &world.ai_agents {
        cfg.nhi_and_agents.agent_decisions.insert(
            agent.agent_id.clone(),
            AgentDecisionV52 {
                action: AgentGovernanceActionV52::AuthorizeActive,
                resolved_sponsor_person_key: agent.sponsor_person_key.clone(),
                approved_autonomous_scopes: agent.requested_autonomous_scopes.clone(),
                approved_hitl_scopes: agent.requested_hitl_scopes.clone(),
            },
        );
    }
    for req in &world.agent_delegation_requests {
        cfg.nhi_and_agents.delegation_decisions.insert(
            req.request_id.clone(),
            DelegationDecisionV52 {
                decision: OboDelegationVerdictV52::Approve,
                verified_approver_person_key: req.proposed_approver_person_key.clone(),
            },
        );
    }
    for p in &world.cloud_principals {
        cfg.cloud_ciem.principals.insert(
            p.principal_id.clone(),
            CloudPrincipalRemediationV52 {
                escalation_vector: CloudEscalationVectorV52::None,
                action: CloudRemediationActionV52::KeepLeastPrivilege,
                retained_permissions: p.direct_permissions.clone(),
            },
        );
    }
    for campaign in &world.uar_campaigns {
        for item in &campaign.items {
            cfg.access_reviews.items.insert(
                item.item_id.clone(),
                UarItemDecisionV52 {
                    effective_reviewer_person_key: item.assigned_reviewer_person_key.clone(),
                    decision: UarDecisionKindV52::Certify,
                    remove_account_group_edges: Vec::new(),
                },
            );
        }
    }
    cfg
}

/// `gold_minus_<category>` (§8.1 ownership table): gold with the category's sections taken from starter.
#[must_use]
pub fn gold_minus_config_v52(
    category: CategoryIdV52,
    starter: &IgaDeploymentConfigV52,
    gold: &IgaDeploymentConfigV52,
) -> IgaDeploymentConfigV52 {
    let mut cfg = gold.clone();
    apply_category_section_v52(&mut cfg, starter, category);
    cfg
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn thirteen_rows_with_four_shortcuts() {
        let rows = BaselineKindV52::all();
        assert_eq!(rows.len(), 13);
        assert_eq!(rows.iter().filter(|r| r.is_shortcut()).count(), 4);
        assert_eq!(
            BaselineKindV52::GoldMinus(CategoryIdV52::AccessPolicy).id(),
            "gold_minus_access_policy"
        );
        assert_eq!(ablation_field_paths_v52().len(), 8);
    }

    #[test]
    fn schema_minimal_round_trips_through_the_strict_wire_contract() {
        let cfg = schema_minimal_config_v52();
        let text = serde_json::to_string(&cfg).unwrap();
        let back: IgaDeploymentConfigV52 = serde_json::from_str(&text).unwrap();
        assert_eq!(back, cfg);
        let value: serde_json::Value = serde_json::to_value(&cfg).unwrap();
        for (_, file) in value.as_object().unwrap() {
            assert_eq!(file["schema_version"], "v5.2");
        }
        assert_eq!(
            value["username_policy"]["base_pattern"],
            serde_json::json!("first_initial_last")
        );
    }
}
