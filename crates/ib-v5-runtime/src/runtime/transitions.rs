//! Phase reducers, coupled world induction, and JML workflow DAG simulation.

use std::collections::{BTreeMap, BTreeSet};

use ib_v5_schemas::{
    AffiliationKindV5, AgentDelegationDecisionKindV5, AtomicMutationV5, AuthorityTierV5,
    ContractorSponsorCheckModeV5, DisconnectedTargetPolicyV5, EpisodeV5, GrantModeV5, GrantV5,
    HRPersonV5, JITDecisionKindV5, JMLTraceEventKindV5, JMLTraceEventV5, JMLWorkflowSpecV5,
    JMLWorkflowTaskV5, JoinerContractorSponsorPolicyV5, JoinerStepV5, LeaverStepV5,
    LegalHoldDispositionV5, LegalHoldScopeModeV5, MoverStepV5, NoShowDispositionV5,
    OrphanCustodyModeV5, OrphanHandoffPolicyV5, PhaseWorkQueueV5, PriorAccountP1ActionV5,
    SpokeErrorPolicyV5, TableRowV5, TerminalWorldStateV5, UARDecisionKindV5, WorldStateV5,
    decode_compact_table_v5, effective_department, engagement_is_live,
};

use super::ledger::build_internal_prior_ledger;
use super::view::{RuntimeError, mutation_key};

const JML_JOINER_DAG_V5: [JoinerStepV5; 5] = [
    JoinerStepV5::VerifyHrAuthAndEffectiveDate,
    JoinerStepV5::CorrelateImmutableIdOrCreateAccount,
    JoinerStepV5::ProvisionBirthrightAccess,
    JoinerStepV5::BindTelemetryAndMfaPolicy,
    JoinerStepV5::EmitJoinerAuditReceipt,
];

const JML_MOVER_DAG_V5: [MoverStepV5; 5] = [
    MoverStepV5::SnapshotPriorGrantsAndBridges,
    MoverStepV5::RevokeOutOfScopeNonBridgedGrants,
    MoverStepV5::ProvisionNewDeptBirthrights,
    MoverStepV5::EnforcePostMoverSodAndRebindOwnership,
    MoverStepV5::TriggerTargetedMoverCertification,
];

const JML_LEAVER_DAG_V5: [LeaverStepV5; 5] = [
    LeaverStepV5::DisableIdpAndSsoSessions,
    LeaverStepV5::RevokeRefreshAndOboTrees,
    LeaverStepV5::TransferOwnedNhiAndAgentCustody,
    LeaverStepV5::DeprovisionSpokeEntitlementsOrDispatchItsm,
    LeaverStepV5::ApplyMailboxArchiveOrLegalHoldRetention,
];

fn authority_rank(tier: AuthorityTierV5) -> u8 {
    match tier {
        AuthorityTierV5::StaleCache => 1,
        AuthorityTierV5::SecondaryReplica => 2,
        AuthorityTierV5::AuthoritativePrimary => 3,
    }
}

/// Return whether `mutations` is duplicate-free and contains only Phase-1 mutation kinds.
#[must_use]
pub fn completeness_ok_from_mutations_only(mutations: &[AtomicMutationV5]) -> bool {
    let keys: Vec<(String, String)> = mutations.iter().map(mutation_key).collect();
    let unique: BTreeSet<&(String, String)> = keys.iter().collect();
    if keys.len() != unique.len() {
        return false;
    }
    keys.iter().all(|(kind, _)| {
        matches!(
            kind.as_str(),
            "CONNECTOR_SYNC_MUTATION"
                | "HR_SOURCE_MAPPING_MUTATION"
                | "ACCOUNT_RECON_MUTATION"
                | "NHI_GOVERNANCE_MUTATION"
                | "AGENT_REGISTRY_MUTATION"
                | "CLOUD_CIEM_MUTATION"
                | "OPTIMIZATION_MUTATION"
                | "JML_WORKFLOW_MUTATION"
        )
    })
}

fn work_queue_from_targets(mutations: &[AtomicMutationV5]) -> PhaseWorkQueueV5 {
    let mut by_kind: BTreeMap<&str, BTreeSet<String>> = BTreeMap::new();
    for m in mutations {
        by_kind
            .entry(m.mutation_kind())
            .or_default()
            .insert(m.target_entity_id().to_owned());
    }
    let get = |k: &str| -> Vec<String> {
        by_kind
            .get(k)
            .map(|s| s.iter().cloned().collect())
            .unwrap_or_default()
    };
    PhaseWorkQueueV5 {
        target_connector_ids: get("CONNECTOR_SYNC_MUTATION"),
        target_hr_mapping_ids: get("HR_SOURCE_MAPPING_MUTATION"),
        target_human_account_ids: get("ACCOUNT_RECON_MUTATION"),
        target_nhi_ids: get("NHI_GOVERNANCE_MUTATION"),
        target_agent_ids: get("AGENT_REGISTRY_MUTATION"),
        target_ciem_task_ids: get("CLOUD_CIEM_MUTATION"),
        target_jml_workflow_ids: get("JML_WORKFLOW_MUTATION"),
        target_opt_task_ids: get("OPTIMIZATION_MUTATION"),
        target_cae_incident_ids: get("CAE_CONTAINMENT_MUTATION"),
        target_uar_campaign_ids: get("UAR_CAMPAIGN_MUTATION"),
        target_jit_request_ids: get("JIT_DECISION_MUTATION"),
        target_agent_delegation_ids: get("AGENT_DELEGATION_MUTATION"),
    }
}

pub(crate) fn apply_phase1_with_queue(
    world: &WorldStateV5,
    mutations: &[AtomicMutationV5],
    work_queue: &PhaseWorkQueueV5,
) -> Result<WorldStateV5, RuntimeError> {
    let ledger = build_internal_prior_ledger(world, mutations, work_queue)?;
    let mut next = world.clone();
    next.phase_index = 2;
    next.world_rev = world.world_rev + 1;
    next.prior_phase_ledger = Some(ledger);
    Ok(next)
}

/// Apply committed Phase-1 input and attach its internal prior-phase ledger.
pub fn apply_phase1(
    world: &WorldStateV5,
    mutations: &[AtomicMutationV5],
    work_queue: Option<&PhaseWorkQueueV5>,
) -> Result<WorldStateV5, RuntimeError> {
    if !completeness_ok_from_mutations_only(mutations) {
        return Err(RuntimeError::InvalidArgument(
            "apply_phase1 requires a complete, duplicate-free P1 mutation set".to_owned(),
        ));
    }
    match work_queue {
        Some(q) => apply_phase1_with_queue(world, mutations, q),
        None => {
            let q = work_queue_from_targets(mutations);
            apply_phase1_with_queue(world, mutations, &q)
        }
    }
}

pub(crate) fn apply_phase2_environment_delta(
    world: &WorldStateV5,
    episode: &EpisodeV5,
    phase2_environment_state: Option<&WorldStateV5>,
) -> Result<WorldStateV5, RuntimeError> {
    let delta_ids: BTreeSet<&str> = episode
        .phase2_delta_person_ids
        .iter()
        .map(String::as_str)
        .collect();
    let mut changed_rows: BTreeMap<String, HRPersonV5> = BTreeMap::new();
    if let Some(env_state) = phase2_environment_state {
        for pid in &delta_ids {
            if let Some(person) = env_state.hr_persons.get(*pid) {
                changed_rows.insert((*pid).to_owned(), person.clone());
            }
        }
    } else {
        let mut selected: BTreeMap<String, ((u8, i64, String), HRPersonV5)> = BTreeMap::new();
        for delivered in episode.phase2_slices.values() {
            let rank = (
                authority_rank(delivered.authority_tier),
                delivered.watermark_s,
                delivered.slice_id.clone(),
            );
            for table in &delivered.tables {
                if table.table_name != "hr_persons" {
                    continue;
                }
                for row in decode_compact_table_v5(table)? {
                    let TableRowV5::HrPerson(person) = row else {
                        return Err(RuntimeError::InvalidArgument(
                            "hr_persons table decoded to a different schema".to_owned(),
                        ));
                    };
                    let pid = person.person_id.clone();
                    if !delta_ids.contains(pid.as_str()) {
                        continue;
                    }
                    match selected.get(&pid) {
                        Some((prev_rank, _)) if rank <= *prev_rank => {}
                        _ => {
                            selected.insert(pid, (rank.clone(), person));
                        }
                    }
                }
            }
        }
        for (pid, (_, person)) in selected {
            changed_rows.insert(pid, person);
        }
    }

    let mut next = world.clone();
    next.phase_index = 2;
    next.clock_epoch_s = episode.phase2_base_manifest.clock_epoch_s;
    for (pid, person) in changed_rows {
        next.hr_persons.insert(pid, person);
    }
    Ok(next)
}

/// Derive the coupled S2 state from S1, committed M1, and declared deltas.
pub fn induce_phase2_world(
    initial_world: &WorldStateV5,
    committed_p1: &[AtomicMutationV5],
    work_queue: &PhaseWorkQueueV5,
    episode: &EpisodeV5,
    phase2_environment_state: Option<&WorldStateV5>,
) -> Result<WorldStateV5, RuntimeError> {
    let induced_p1 = apply_phase1(initial_world, committed_p1, Some(work_queue))?;
    apply_phase2_environment_delta(&induced_p1, episode, phase2_environment_state)
}

fn grant_in_department(
    grant: &GrantV5,
    department: &str,
    world: &WorldStateV5,
) -> Result<bool, RuntimeError> {
    if grant.scope_path == "/*" {
        return Ok(true);
    }
    let g_leaves = world.scope_catalog.leaves(&grant.scope_path)?;
    let d_leaves = world.scope_catalog.leaves(department)?;
    Ok(g_leaves.is_subset(&d_leaves))
}

fn request_eval_seq(mutation: &AtomicMutationV5, world: &WorldStateV5) -> i64 {
    match mutation {
        AtomicMutationV5::JitDecision(m) => world
            .jit_requests
            .get(&m.target_entity_id)
            .map_or(i64::MAX, |r| r.eval_seq),
        AtomicMutationV5::AgentDelegation(m) => world
            .agent_delegation_requests
            .get(&m.target_entity_id)
            .map_or(i64::MAX, |r| r.eval_seq),
        _ => i64::MAX,
    }
}

/// Reduce committed Phase-2 mutations into the terminal grant/session ledger.
pub fn apply_phase2(
    world: &WorldStateV5,
    mutations: &[AtomicMutationV5],
    stage_digest: &str,
    committed_p1_digest: &str,
) -> Result<TerminalWorldStateV5, RuntimeError> {
    let ledger = world.prior_phase_ledger.as_ref().ok_or_else(|| {
        RuntimeError::InvalidArgument(
            "apply_phase2 requires the internal phase-1 prior ledger".to_owned(),
        )
    })?;

    let mut grants: BTreeMap<String, GrantV5> = world
        .grants
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let mut standing: BTreeMap<String, BTreeSet<String>> = ledger
        .account_outcomes
        .iter()
        .map(|o| {
            (
                o.account_id.clone(),
                o.effective_standing_grant_ids.iter().cloned().collect(),
            )
        })
        .collect();
    let mut eligible: BTreeMap<String, BTreeSet<String>> = ledger
        .account_outcomes
        .iter()
        .map(|o| {
            (
                o.account_id.clone(),
                o.effective_eligible_grant_ids.iter().cloned().collect(),
            )
        })
        .collect();

    let mut active: BTreeSet<String> = BTreeSet::new();
    let account_outcomes_by_id: BTreeMap<&str, _> = ledger
        .account_outcomes
        .iter()
        .map(|o| (o.account_id.as_str(), o))
        .collect();
    for outcome in &ledger.account_outcomes {
        if outcome.p1_action == PriorAccountP1ActionV5::KeepActive
            && let Some(pid) = outcome.bound_person_id.as_deref()
            && let Some(eid) = outcome.bound_engagement_id.as_deref()
            && let Some(person) = world.hr_persons.get(pid)
            && person.engagements.iter().any(|e| e.eng_id == eid)
        {
            active.insert(outcome.account_id.clone());
        }
    }

    for (aid, set) in &mut standing {
        if !active.contains(aid) {
            set.clear();
        }
    }
    for (aid, set) in &mut eligible {
        if !active.contains(aid) {
            set.clear();
        }
    }

    let retro_tainted: BTreeSet<&str> = mutations
        .iter()
        .filter_map(|m| match m {
            AtomicMutationV5::CaeContainment(c) => Some(&c.retro_invalidated_ids),
            _ => None,
        })
        .flat_map(|ids| ids.iter().map(String::as_str))
        .collect();

    let active_snapshot: Vec<String> = active.iter().cloned().collect();
    for aid in active_snapshot {
        let outcome = account_outcomes_by_id[aid.as_str()];
        let pid = outcome
            .bound_person_id
            .as_deref()
            .expect("active bound_person_id");
        let eid = outcome
            .bound_engagement_id
            .as_deref()
            .expect("active bound_engagement_id");
        let person = &world.hr_persons[pid];
        let engagement = person
            .engagements
            .iter()
            .find(|e| e.eng_id == eid)
            .expect("active bound engagement");
        if !engagement_is_live(engagement, world.clock_epoch_s) {
            active.remove(&aid);
            if let Some(s) = standing.get_mut(&aid) {
                s.clear();
            }
            if let Some(e) = eligible.get_mut(&aid) {
                e.clear();
            }
            continue;
        }
        let department = effective_department(engagement, world.clock_epoch_s);
        let all_aid_grants: Vec<String> = standing
            .get(&aid)
            .into_iter()
            .flatten()
            .chain(eligible.get(&aid).into_iter().flatten())
            .cloned()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        for grant_id in all_aid_grants {
            if let Some(grant) = grants.get(&grant_id)
                && grant_in_department(grant, department, world)?
            {
                continue;
            }
            let grant_opt = grants.get(&grant_id);
            let mut bridge_live = false;
            if let Some(grant) = grant_opt {
                for bridge in world.transfer_bridges.values() {
                    if bridge.account_id == aid
                        && bridge.grant_id == grant_id
                        && !retro_tainted.contains(bridge.bridge_id.as_str())
                        && engagement.prior_dept_path.as_deref()
                            == Some(bridge.from_dept_path.as_str())
                        && bridge.to_dept_path == department
                        && world
                            .roles
                            .get(&grant.role_id)
                            .is_some_and(|r| r.app_id == bridge.target_resource)
                        && bridge.approved_at_s <= world.clock_epoch_s
                        && world.clock_epoch_s < bridge.expires_at_s
                        && world
                            .scope_catalog
                            .leaves(&grant.scope_path)?
                            .is_subset(&world.scope_catalog.leaves(&bridge.from_dept_path)?)
                    {
                        bridge_live = true;
                        break;
                    }
                }
            }
            if !bridge_live {
                if let Some(s) = standing.get_mut(&aid) {
                    s.remove(&grant_id);
                }
                if let Some(e) = eligible.get_mut(&aid) {
                    e.remove(&grant_id);
                }
            }
        }
    }

    let mut uar_mutations: Vec<_> = mutations
        .iter()
        .filter_map(|m| match m {
            AtomicMutationV5::UarCampaign(u) => Some(u),
            _ => None,
        })
        .collect();
    uar_mutations.sort_by(|a, b| a.target_entity_id.cmp(&b.target_entity_id));

    for mutation in uar_mutations {
        let Some(campaign) = world.uar_campaigns.get(&mutation.target_entity_id) else {
            continue;
        };
        let decisions: BTreeMap<&str, UARDecisionKindV5> = mutation
            .uar_decisions
            .iter()
            .map(|(i, d)| (i.as_str(), *d))
            .collect();
        let mut items: Vec<_> = campaign.items.iter().collect();
        items.sort_by(|a, b| a.item_id.cmp(&b.item_id));
        for item in items {
            let decision = decisions.get(item.item_id.as_str()).copied();
            let account_id = &item.account_id;
            match decision {
                Some(UARDecisionKindV5::Revoke | UARDecisionKindV5::Escalate) => {
                    if let Some(s) = standing.get_mut(account_id) {
                        s.remove(&item.grant_id);
                    }
                    if let Some(e) = eligible.get_mut(account_id) {
                        e.remove(&item.grant_id);
                    }
                }
                Some(UARDecisionKindV5::ConvertToJit)
                    if standing
                        .get(account_id)
                        .is_some_and(|s| s.contains(&item.grant_id)) =>
                {
                    if let Some(s) = standing.get_mut(account_id) {
                        s.remove(&item.grant_id);
                    }
                    eligible
                        .entry(account_id.clone())
                        .or_default()
                        .insert(item.grant_id.clone());
                    if let Some(grant) = grants.get_mut(&item.grant_id) {
                        grant.grant_mode = GrantModeV5::Eligible;
                    }
                }
                _ => {}
            }
        }
        if let Some(custodian) = &mutation.emergency_custodian_account_id {
            let grant_id = format!("cust__{}__{}", campaign.campaign_id, custodian);
            standing
                .entry(custodian.clone())
                .or_default()
                .insert(grant_id);
        }
    }

    let mut materialized_sessions: Vec<String> = Vec::new();
    let mut consumed_tickets: BTreeSet<String> = BTreeSet::new();
    let mut request_mutations: Vec<&AtomicMutationV5> = mutations
        .iter()
        .filter(|m| matches!(m, AtomicMutationV5::JitDecision(_)))
        .collect();
    request_mutations.extend(
        mutations
            .iter()
            .filter(|m| matches!(m, AtomicMutationV5::AgentDelegation(_))),
    );
    request_mutations.sort_by_key(|m| request_eval_seq(m, world));

    for req_mut in request_mutations {
        match req_mut {
            AtomicMutationV5::JitDecision(m) => {
                let approved = matches!(
                    m.jit_decision,
                    JITDecisionKindV5::ApproveJit | JITDecisionKindV5::ApproveJitCosign
                );
                let request = world.jit_requests.get(&m.target_entity_id);
                if approved {
                    materialized_sessions.push(format!("sess_prov_{}", m.target_entity_id));
                }
                if approved
                    && let Some(req) = request
                    && let Some(ticket_id) = &req.change_ticket_id
                    && let Some(ticket) = world.change_tickets.get(ticket_id)
                    && ticket.single_use
                {
                    consumed_tickets.insert(ticket_id.clone());
                }
            }
            AtomicMutationV5::AgentDelegation(m) => {
                let approved = matches!(
                    m.delegation_decision,
                    AgentDelegationDecisionKindV5::ApproveDelegation
                        | AgentDelegationDecisionKindV5::ApproveDelegationCosign
                );
                if approved {
                    materialized_sessions.push(format!("sess_prov_{}", m.target_entity_id));
                }
            }
            _ => {}
        }
    }

    let mut sorted_accounts: Vec<&str> = world.idp_accounts.keys().map(String::as_str).collect();
    sorted_accounts.sort_unstable();
    let terminal_standing_grants_by_account = sorted_accounts
        .iter()
        .map(|&aid| {
            let list: Vec<String> = standing
                .get(aid)
                .map(|s| s.iter().cloned().collect())
                .unwrap_or_default();
            (aid.to_owned(), list)
        })
        .collect();
    let terminal_eligible_grants_by_account = sorted_accounts
        .iter()
        .map(|&aid| {
            let list: Vec<String> = eligible
                .get(aid)
                .map(|s| s.iter().cloned().collect())
                .unwrap_or_default();
            (aid.to_owned(), list)
        })
        .collect();

    Ok(TerminalWorldStateV5 {
        world_rev: world.world_rev + 1,
        terminal_standing_grants_by_account,
        terminal_eligible_grants_by_account,
        materialized_session_ids: materialized_sessions,
        consumed_ticket_ids: consumed_tickets.into_iter().collect(),
        committed_p1_digest: committed_p1_digest.to_owned(),
        committed_p2_digest: stage_digest.to_owned(),
    })
}

/// Output of [`simulate_workflow_dag`].
#[derive(Debug, Clone, PartialEq)]
pub struct WorkflowDAGResultV5 {
    pub phase2_world: WorldStateV5,
    pub terminal_state: TerminalWorldStateV5,
}

/// Run the deterministic phase reducers for a coupled or reset DAG path.
pub fn simulate_workflow_dag(
    world: &WorldStateV5,
    phase1_mutations: &[AtomicMutationV5],
    phase2_mutations: &[AtomicMutationV5],
    phase1_work_queue: &PhaseWorkQueueV5,
    phase1_digest: &str,
    phase2_digest: &str,
    phase2_reset_state: Option<&WorldStateV5>,
) -> Result<WorkflowDAGResultV5, RuntimeError> {
    let mut phase2_world = apply_phase1_with_queue(world, phase1_mutations, phase1_work_queue)?;
    if let Some(reset) = phase2_reset_state {
        phase2_world = reset.clone();
    }
    let terminal_state = apply_phase2(
        &phase2_world,
        phase2_mutations,
        phase2_digest,
        phase1_digest,
    )?;
    Ok(WorkflowDAGResultV5 {
        phase2_world,
        terminal_state,
    })
}

/// Check a workflow's causal step order and task-specific event policies.
#[must_use]
pub fn simulate_jml_workflow_dag(
    spec: &JMLWorkflowSpecV5,
    task: &JMLWorkflowTaskV5,
    events: &[JMLTraceEventV5],
) -> bool {
    if spec.joiner_steps.as_slice() != JML_JOINER_DAG_V5
        || spec.mover_steps.as_slice() != JML_MOVER_DAG_V5
        || spec.leaver_steps.as_slice() != JML_LEAVER_DAG_V5
    {
        return false;
    }

    for event in events {
        if event.event_kind == JMLTraceEventKindV5::Joiner
            && event.affiliation == AffiliationKindV5::Contractor
        {
            let expected = match task.contractor_sponsor_check_mode {
                ContractorSponsorCheckModeV5::StrictPreProvision => {
                    JoinerContractorSponsorPolicyV5::RequireActiveFteSponsorPreProvision
                }
                ContractorSponsorCheckModeV5::ExemptInternalConversion => {
                    JoinerContractorSponsorPolicyV5::AllowInternalConversionCarryoverSponsor
                }
            };
            if spec.joiner_contractor_sponsor_policy != expected {
                return false;
            }
        }
        if event.event_kind == JMLTraceEventKindV5::NoShowCancel
            && spec.no_show_disposition != NoShowDispositionV5::FullRevokeAndDisableNeverStarted
        {
            return false;
        }
        if event.under_legal_hold {
            let expected = match task.legal_hold_scope_mode {
                LegalHoldScopeModeV5::SuspendPreserveArchive => {
                    LegalHoldDispositionV5::SuspendAuthRevokeEntitlementsPreserveHoldData
                }
                LegalHoldScopeModeV5::IsolateForensicSnapshot => {
                    LegalHoldDispositionV5::SuspendAuthIsolateForensicSnapshot
                }
            };
            if spec.legal_hold_disposition != expected {
                return false;
            }
        }
        if event.owns_prod_nhi_or_agent {
            let expected = match task.orphan_custody_mode {
                OrphanCustodyModeV5::HandoffToManagerElseSuspend => {
                    OrphanHandoffPolicyV5::ReassignToActiveManagerOrEscrow
                }
                OrphanCustodyModeV5::ImmediateSuspendAll => {
                    OrphanHandoffPolicyV5::ImmediateSuspendAllOwnedEntities
                }
            };
            if spec.orphan_handoff_policy != expected {
                return false;
            }
        }
        if event.spoke_transient_error
            && spec.spoke_error_policy != SpokeErrorPolicyV5::FailClosedRetryQueueAndAlert
        {
            return false;
        }
        if event.target_is_disconnected_itsm {
            let expected = if task.has_disconnected_spokes {
                DisconnectedTargetPolicyV5::DispatchItsmAndVerifyPostCloseAggregation
            } else {
                DisconnectedTargetPolicyV5::NotApplicableAutomatedOnly
            };
            if spec.disconnected_target_policy != expected {
                return false;
            }
        }
    }

    true
}

/// Spec-spelled alias for the v5 task-parameterized JML simulator.
#[must_use]
pub fn sim_jml_workflow(
    spec: &JMLWorkflowSpecV5,
    events: &[JMLTraceEventV5],
    task: &JMLWorkflowTaskV5,
    _world: Option<&WorldStateV5>,
) -> bool {
    simulate_jml_workflow_dag(spec, task, events)
}
