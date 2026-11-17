//! Core Phase-1/Phase-2 dispatch, LIVE_VERSION, observable witness strategy, and oracle builder.
//!
//! Mirrors `identitybench.v5.oracle`.

use std::collections::{BTreeMap, BTreeSet};

use ib_v5_schemas::compact::{TableRowV5, decode_compact_table_v5};
use ib_v5_schemas::scope_algebra::{
    CapabilityFootprint, effective_grant_footprint, engagement_is_live, group_footprints,
    has_live_engagement,
};
use ib_v5_schemas::{
    AccountReconActionV5, AtomicMutationV5, AuthorityTierV5, CompensatingControlV5,
    DeliveredSliceV5, EngagementV5, EpisodeV5, GrantModeV5, HRPersonV5, IdPAccountV5,
    JMLTraceEventV5, PhaseManifestV5, PhaseWorkQueueV5, PolicyOverlayV5, RegimeIdV5,
    SealedEpisodeOracleV5, TransferBridgeV5, WorldStateV5,
};
use indexmap::IndexMap;

use crate::error::OracleError;
use crate::oracle_p2::evaluate_phase2;
use crate::phase1::{
    phase1_accounts, phase1_agents, phase1_ciem, phase1_connectors, phase1_hr, phase1_itsm,
    phase1_nhi, phase1_optimizations, phase1_workflows,
};
use crate::types::{
    AccountPlanV5, GoldTargetTraceV5, PhaseContext, PhaseGoldResultV5, TargetKey, TraceContextV5,
};

#[inline]
pub(crate) fn target_key(mutation_kind: &str, target_id: &str) -> TargetKey {
    (mutation_kind.to_owned(), target_id.to_owned())
}

pub(crate) fn sorted_unique<I>(values: I) -> Vec<String>
where
    I: IntoIterator<Item = String>,
{
    let set: BTreeSet<String> = values.into_iter().collect();
    set.into_iter().collect()
}

pub(crate) fn account_binding_people(world: &WorldStateV5, acct: &IdPAccountV5) -> Vec<String> {
    if let Some(ref immutable) = acct.immutable_person_ref {
        let mut out: Vec<String> = world
            .hr_persons
            .values()
            .filter(|p| {
                p.person_id == *immutable
                    || (p.primary_email == acct.login_email
                        && has_live_engagement(p, world.clock_epoch_s))
            })
            .map(|p| p.person_id.clone())
            .collect();
        out.sort();
        out
    } else {
        let mut out: Vec<String> = world
            .hr_persons
            .values()
            .filter(|p| {
                acct.login_email == p.primary_email
                    || p.historical_emails.iter().any(|h| h == &acct.login_email)
            })
            .map(|p| p.person_id.clone())
            .collect();
        out.sort();
        out
    }
}

/// Total pre-P1 identity helper required by CONN/ITSM/NHI.
#[must_use]
pub fn pre_person(world: &WorldStateV5, account_id: Option<&str>) -> Option<String> {
    let account_id = account_id?;
    let acct = world.idp_accounts.get(account_id)?;
    if let Some(ref ref_id) = acct.immutable_person_ref {
        if !world.hr_persons.contains_key(ref_id) {
            return None;
        }
        if world.hr_persons.values().any(|p| {
            p.person_id != *ref_id
                && has_live_engagement(p, world.clock_epoch_s)
                && p.primary_email == acct.login_email
        }) {
            return None;
        }
        return Some(ref_id.clone());
    }
    let candidates = account_binding_people(world, acct);
    if candidates.len() == 1 {
        Some(candidates.into_iter().next().expect("len == 1"))
    } else {
        None
    }
}

pub(crate) fn bind_person_p1(
    account_id: &str,
    outcomes: &BTreeMap<String, AccountPlanV5>,
) -> Option<String> {
    let outcome = outcomes.get(account_id)?;
    if outcome.account_action == AccountReconActionV5::KeepActive {
        outcome.bound_person_id.clone()
    } else {
        None
    }
}

pub(crate) fn p1_action(
    outcomes: &BTreeMap<String, AccountPlanV5>,
    account_id: &str,
) -> Option<AccountReconActionV5> {
    outcomes.get(account_id).map(|o| o.account_action)
}

/// Total `LIVE_VERSION(eid, S_k)` implementation, including inherited P1 ledger.
#[must_use]
pub fn live_version(entity_id: &str, world: &WorldStateV5) -> Option<String> {
    let prefix = entity_id.split('_').next()?;
    let is_p2_only = matches!(prefix, "inc" | "camp" | "req" | "dlreq" | "tkt");
    if is_p2_only && world.phase_index != 2 {
        return None;
    }
    let is_p1_ledger_kind = matches!(
        prefix,
        "conn" | "hrmap" | "acct" | "nhi" | "agt" | "ciem" | "wfl"
    );
    if is_p1_ledger_kind && world.phase_index == 2
        && let Some(ledger) = &world.prior_phase_ledger
    {
        match prefix {
            "conn" => {
                if let Some(outcome) = ledger.connector_by_id().get(entity_id) {
                    world.connector_schemas.get(entity_id)?;
                    return Some(format!(
                        "{entity_id}@{}:{}",
                        outcome.current_etag, outcome.current_sync_gen
                    ));
                }
            }
            "hrmap" => {
                if let Some(outcome) = ledger.hr_mapping_by_id().get(entity_id) {
                    world.hr_source_mappings.get(entity_id)?;
                    return Some(format!(
                        "{entity_id}@{}:{}",
                        outcome.current_etag, outcome.current_mapping_gen
                    ));
                }
            }
            "acct" => {
                if let Some(outcome) = ledger.account_by_id().get(entity_id) {
                    world.idp_accounts.get(entity_id)?;
                    return Some(format!(
                        "{entity_id}@{}:{}",
                        outcome.current_etag, outcome.current_account_gen
                    ));
                }
            }
            "nhi" => {
                if let Some(outcome) = ledger.nhi_by_id().get(entity_id) {
                    world.target_nhis.get(entity_id)?;
                    return Some(format!(
                        "{entity_id}@{}:{}",
                        outcome.current_etag, outcome.current_nhi_gen
                    ));
                }
            }
            "agt" => {
                if let Some(outcome) = ledger.agent_by_id().get(entity_id) {
                    world.enterprise_agents.get(entity_id)?;
                    return Some(format!(
                        "{entity_id}@{}:{}",
                        outcome.current_etag, outcome.current_agent_gen
                    ));
                }
            }
            "ciem" => {
                if let Some(outcome) = ledger.ciem_by_id().get(entity_id) {
                    world.cloud_ciem_tasks.get(entity_id)?;
                    return Some(format!(
                        "{entity_id}@{}:{}",
                        outcome.current_etag, outcome.current_ciem_gen
                    ));
                }
            }
            "wfl" => {
                if let Some(outcome) = ledger.workflow_by_id().get(entity_id) {
                    world.jml_workflow_tasks.get(entity_id)?;
                    return Some(format!(
                        "{entity_id}@{}:{}",
                        outcome.current_etag, outcome.current_workflow_gen
                    ));
                }
            }
            _ => {}
        }
    }

    match prefix {
        "conn" => world
            .connector_schemas
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:{}", r.etag, r.sync_gen)),
        "hrmap" => world
            .hr_source_mappings
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:{}", r.etag, r.mapping_gen)),
        "acct" => world
            .idp_accounts
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:{}", r.etag, r.account_gen)),
        "nhi" => world
            .target_nhis
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:{}", r.etag, r.nhi_gen)),
        "agt" => world
            .enterprise_agents
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:{}", r.etag, r.agent_gen)),
        "ciem" => world
            .cloud_ciem_tasks
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:{}", r.etag, r.ciem_gen)),
        "wfl" => world
            .jml_workflow_tasks
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:{}", r.etag, r.workflow_gen)),
        "bnhi" => world
            .baseline_nhis
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:{}", r.etag, r.baseline_gen)),
        "pers" => world
            .hr_persons
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:{}", r.etag, r.lifecycle_gen)),
        "cc" => world
            .compensating_controls
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:1", r.etag)),
        "brg" => world
            .transfer_bridges
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:1", r.etag)),
        "opt" => world
            .optimization_tasks
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:1", r.etag)),
        "inc" => world
            .cae_incidents
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:1", r.etag)),
        "camp" => world
            .uar_campaigns
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:1", r.etag)),
        "req" => world
            .jit_requests
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:1", r.etag)),
        "dlreq" => world
            .agent_delegation_requests
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:1", r.etag)),
        "tkt" => world
            .change_tickets
            .get(entity_id)
            .map(|r| format!("{entity_id}@{}:1", r.etag)),
        _ => None,
    }
}

pub(crate) fn version_tokens<'a>(
    world: &WorldStateV5,
    entity_ids: impl IntoIterator<Item = &'a str>,
) -> Result<Vec<String>, OracleError> {
    let mut tokens = Vec::new();
    for entity_id in entity_ids {
        let token = live_version(entity_id, world)
            .ok_or_else(|| OracleError::MissingLiveVersion(entity_id.to_owned()))?;
        tokens.push(token);
    }
    Ok(sorted_unique(tokens))
}

pub(crate) fn record_trace_mutation(
    ctx: &mut PhaseContext,
    mutation: AtomicMutationV5,
    context: TraceContextV5,
) {
    let key = target_key(mutation.mutation_kind(), mutation.target_entity_id());
    let trace = GoldTargetTraceV5 {
        preconditions: mutation.if_match().to_vec(),
        citations: mutation.cite().to_vec(),
        mutation,
        context,
    };
    ctx.trace.insert(key, trace);
}

pub(crate) fn group_fp(
    ctx: &PhaseContext,
    account: &IdPAccountV5,
) -> Result<(CapabilityFootprint, CapabilityFootprint), OracleError> {
    Ok(group_footprints(
        account,
        &ctx.state.groups,
        &ctx.state.roles,
        ctx.catalog(),
    )?)
}

pub(crate) fn grant_fp(
    ctx: &PhaseContext,
    account: &IdPAccountV5,
    grant_id: &str,
) -> Result<CapabilityFootprint, OracleError> {
    let (_, deny) = group_fp(ctx, account)?;
    let grant = ctx.state.grants.get(grant_id).ok_or_else(|| {
        OracleError::Invariant(format!("missing grant {grant_id:?} in state.grants"))
    })?;
    Ok(effective_grant_footprint(
        grant,
        &ctx.state.roles,
        &deny,
        ctx.catalog(),
    )?)
}

pub(crate) fn person_standing_from_p1(
    ctx: &PhaseContext,
    person_id: &str,
    account_plans: &BTreeMap<String, AccountPlanV5>,
) -> Result<CapabilityFootprint, OracleError> {
    let mut out = CapabilityFootprint::new();
    for (account_id, plan) in account_plans {
        if plan.account_action != AccountReconActionV5::KeepActive
            || plan.bound_person_id.as_deref() != Some(person_id)
        {
            continue;
        }
        let account = &ctx.state.idp_accounts[account_id.as_str()];
        let (grp, _) = group_fp(ctx, account)?;
        out.extend(grp);
        for grant_id in &plan.retained_grant_ids {
            if let Some(grant) = ctx.state.grants.get(grant_id.as_str())
                && grant.grant_mode == GrantModeV5::Standing
            {
                out.extend(grant_fp(ctx, account, grant_id)?);
            }
        }
    }
    Ok(out)
}

/// Derive gold mutations and a verifier trace from one policy state.
pub fn evaluate_phase_with_trace(
    state: &WorldStateV5,
    work_queue: &PhaseWorkQueueV5,
    sealed_jml_adversarial_events: &[JMLTraceEventV5],
) -> Result<PhaseGoldResultV5, OracleError> {
    let mut ctx = PhaseContext::new(state.clone(), work_queue.clone(), state.phase_index);
    let mutations = match ctx.phase {
        1 => evaluate_phase1(&mut ctx, sealed_jml_adversarial_events)?,
        2 => evaluate_phase2(&mut ctx)?,
        other => {
            return Err(OracleError::InvalidInput(format!(
                "Unsupported phase_index: {other}"
            )));
        }
    };
    let expected = required_targets(ctx.phase, work_queue);
    let actual: BTreeSet<TargetKey> = mutations
        .iter()
        .map(|m| target_key(m.mutation_kind(), m.target_entity_id()))
        .collect();
    if actual != expected {
        let missing: Vec<TargetKey> = expected.difference(&actual).cloned().collect();
        let extra: Vec<TargetKey> = actual.difference(&expected).cloned().collect();
        return Err(OracleError::TargetCoverageMismatch { missing, extra });
    }
    Ok(PhaseGoldResultV5 {
        phase_index: ctx.phase,
        work_queue: work_queue.clone(),
        mutations,
        trace_by_target: ctx.trace,
    })
}

/// Return gold mutations for one phase, independent of submitted mutations.
pub fn evaluate_phase(
    state: &WorldStateV5,
    work_queue: &PhaseWorkQueueV5,
    sealed_jml_adversarial_events: &[JMLTraceEventV5],
) -> Result<Vec<AtomicMutationV5>, OracleError> {
    Ok(evaluate_phase_with_trace(state, work_queue, sealed_jml_adversarial_events)?.mutations)
}

pub(crate) fn required_targets(phase: i64, q: &PhaseWorkQueueV5) -> BTreeSet<TargetKey> {
    let mut set = BTreeSet::new();
    if phase == 1 {
        for x in &q.target_connector_ids {
            set.insert(("CONNECTOR_SYNC_MUTATION".to_owned(), x.clone()));
        }
        for x in &q.target_hr_mapping_ids {
            set.insert(("HR_SOURCE_MAPPING_MUTATION".to_owned(), x.clone()));
        }
        for x in &q.target_human_account_ids {
            set.insert(("ACCOUNT_RECON_MUTATION".to_owned(), x.clone()));
        }
        for x in &q.target_nhi_ids {
            set.insert(("NHI_GOVERNANCE_MUTATION".to_owned(), x.clone()));
        }
        for x in &q.target_agent_ids {
            set.insert(("AGENT_REGISTRY_MUTATION".to_owned(), x.clone()));
        }
        for x in &q.target_ciem_task_ids {
            set.insert(("CLOUD_CIEM_MUTATION".to_owned(), x.clone()));
        }
        for x in &q.target_opt_task_ids {
            set.insert(("OPTIMIZATION_MUTATION".to_owned(), x.clone()));
        }
        for x in &q.target_jml_workflow_ids {
            set.insert(("JML_WORKFLOW_MUTATION".to_owned(), x.clone()));
        }
    } else {
        for x in &q.target_cae_incident_ids {
            set.insert(("CAE_CONTAINMENT_MUTATION".to_owned(), x.clone()));
        }
        for x in &q.target_uar_campaign_ids {
            set.insert(("UAR_CAMPAIGN_MUTATION".to_owned(), x.clone()));
        }
        for x in &q.target_jit_request_ids {
            set.insert(("JIT_DECISION_MUTATION".to_owned(), x.clone()));
        }
        for x in &q.target_agent_delegation_ids {
            set.insert(("AGENT_DELEGATION_MUTATION".to_owned(), x.clone()));
        }
        for x in &q.target_opt_task_ids {
            set.insert(("OPTIMIZATION_MUTATION".to_owned(), x.clone()));
        }
    }
    set
}

fn evaluate_phase1(
    ctx: &mut PhaseContext,
    sealed_events: &[JMLTraceEventV5],
) -> Result<Vec<AtomicMutationV5>, OracleError> {
    let conn = phase1_connectors(ctx)?;
    let hr = phase1_hr(ctx, &conn)?;
    let accounts = phase1_accounts(ctx, &conn)?;
    let conn_mut = phase1_itsm(ctx, &conn)?;
    let nhi = phase1_nhi(ctx)?;
    let agents = phase1_agents(ctx, &conn)?;
    let ciem = phase1_ciem(ctx, &accounts, &nhi, &agents)?;
    let workflows = phase1_workflows(ctx, sealed_events)?;
    let opts = phase1_optimizations(ctx)?;

    let mut ordered = Vec::with_capacity(
        conn_mut.len()
            + hr.len()
            + accounts.len()
            + nhi.len()
            + agents.len()
            + ciem.len()
            + workflows.len()
            + opts.len(),
    );
    ordered.extend(conn_mut);
    ordered.extend(hr);
    ordered.extend(accounts);
    ordered.extend(nhi);
    ordered.extend(agents);
    ordered.extend(ciem);
    ordered.extend(workflows);
    ordered.extend(opts);
    ordered.sort_by(|a, b| {
        (a.mutation_kind(), a.target_entity_id()).cmp(&(b.mutation_kind(), b.target_entity_id()))
    });
    Ok(ordered)
}

/// Build the sealed oracle from explicit generator-owned seed and worlds.
pub fn build_v5_oracle(
    episode: &EpisodeV5,
    seed: u64,
    internal_regime_id: RegimeIdV5,
    omniscient_s1: WorldStateV5,
    omniscient_s2_gold: WorldStateV5,
    sealed_jml_adversarial_events: &[JMLTraceEventV5],
) -> Result<SealedEpisodeOracleV5, OracleError> {
    let q1 = &episode.phase1_manifest.work_queue;
    let q2 = &episode.phase2_base_manifest.work_queue;
    let p1 = evaluate_phase(&omniscient_s1, q1, sealed_jml_adversarial_events)?;
    let p2 = evaluate_phase(&omniscient_s2_gold, q2, &[])?;
    let (q1_1, q2_1) = obs_strategy(
        &episode.phase1_manifest,
        &episode.phase1_slices,
        &episode.policy_card.overlay,
    )?;
    let (q1_2, q2_2) = obs_strategy(
        &episode.phase2_base_manifest,
        &episode.phase2_slices,
        &episode.policy_card.overlay,
    )?;
    let req1 = sorted_unique(q1_1.iter().cloned().chain(q2_1.iter().cloned()));
    let req2 = sorted_unique(q1_2.iter().cloned().chain(q2_2.iter().cloned()));
    Ok(SealedEpisodeOracleV5 {
        episode_id: episode.episode_id.clone(),
        internal_regime_id,
        seed,
        sealed_jml_adversarial_events: sealed_jml_adversarial_events.to_vec(),
        req_slices_p1: req1,
        req_slices_p2: req2,
        obs_q1_slices_p1: q1_1,
        obs_q2_slices_p1: q2_1,
        obs_q1_slices_p2: q1_2,
        obs_q2_slices_p2: q2_2,
        omniscient_s1,
        omniscient_s2_gold,
        gold_p1_mutations: p1,
        gold_p2_mutations: p2,
    })
}

fn is_core_table_p1(name: &str) -> bool {
    matches!(
        name,
        "hr_persons"
            | "roles"
            | "grants"
            | "groups"
            | "idp_accounts"
            | "connector_schemas"
            | "hr_source_mappings"
            | "baseline_nhis"
            | "target_nhis"
            | "baseline_agents"
            | "enterprise_agents"
            | "cloud_ciem_tasks"
            | "optimization_tasks"
            | "jml_workflow_tasks"
    )
}

fn is_core_table_p2(name: &str) -> bool {
    matches!(
        name,
        "hr_persons"
            | "cae_incidents"
            | "uar_campaigns"
            | "historical_sessions"
            | "jit_requests"
            | "agent_delegation_requests"
            | "optimization_tasks"
    )
}

fn dept_ancestors(path: &str) -> Vec<String> {
    let pieces: Vec<&str> = path
        .trim_matches('/')
        .split('/')
        .filter(|p| !p.is_empty())
        .collect();
    (1..=pieces.len())
        .map(|i| format!("/{}", pieces[..i].join("/")))
        .collect()
}

/// Build observable Q1/Q2 witness using only public manifest and slices.
pub fn obs_strategy(
    manifest: &PhaseManifestV5,
    slices: &IndexMap<String, DeliveredSliceV5>,
    overlay: &PolicyOverlayV5,
) -> Result<(Vec<String>, Vec<String>), OracleError> {
    let clock = manifest.clock_epoch_s;
    let is_core = if manifest.phase_index == 1 {
        is_core_table_p1
    } else {
        is_core_table_p2
    };

    let mut q1_ids: Vec<String> = manifest
        .source_directory
        .iter()
        .filter(|desc| {
            desc.authority_tier == AuthorityTierV5::AuthoritativePrimary
                && desc.is_complete_for_scope
                && (0..=overlay.freshness_sla_s).contains(&(clock - desc.watermark_s))
                && desc.table_names.iter().any(|t| is_core(t.as_str()))
        })
        .map(|desc| desc.slice_id.clone())
        .collect();
    q1_ids.sort();

    let mut connector_ids_in_q1: Vec<String> = Vec::new();
    let mut people: BTreeMap<String, HRPersonV5> = BTreeMap::new();
    let mut accounts: BTreeMap<String, IdPAccountV5> = BTreeMap::new();
    let mut controls: Vec<CompensatingControlV5> = Vec::new();
    let mut uar_campaigns: Vec<(String, String)> = Vec::new();
    let mut jit_rows: Vec<(String, String, Option<String>)> = Vec::new();
    let mut del_rows: Vec<(String, String)> = Vec::new();
    let mut bridge_rows: Vec<TransferBridgeV5> = Vec::new();

    for sid in &q1_ids {
        let delivered = slices.get(sid.as_str()).ok_or_else(|| {
            OracleError::InvalidInput(format!("OBS_STRATEGY missing delivered Q1 slice {sid:?}"))
        })?;
        for table in &delivered.tables {
            let decoded = decode_compact_table_v5(table)?;
            for row in decoded {
                match row {
                    TableRowV5::ConnectorSchema(c) => connector_ids_in_q1.push(c.connector_id),
                    TableRowV5::HrPerson(p) => {
                        people.insert(p.person_id.clone(), p);
                    }
                    TableRowV5::IdpAccount(a) => {
                        accounts.insert(a.account_id.clone(), a);
                    }
                    TableRowV5::CompensatingControl(cc) => controls.push(cc),
                    TableRowV5::UarCampaign(c) => uar_campaigns.push((c.campaign_id, c.app_id)),
                    TableRowV5::JitRequest(j) => {
                        jit_rows.push((j.request_id, j.target_resource, j.change_ticket_id));
                    }
                    TableRowV5::AgentDelegationRequest(d) => {
                        del_rows.push((d.request_id, d.target_resource));
                    }
                    TableRowV5::TransferBridge(b) => bridge_rows.push(b),
                    _ => {}
                }
            }
        }
    }

    let mut refs: BTreeSet<(String, String)> = BTreeSet::new();
    let q = &manifest.work_queue;
    let target_connectors: BTreeSet<&str> = q
        .target_connector_ids
        .iter()
        .map(String::as_str)
        .collect();
    for cid in connector_ids_in_q1 {
        if target_connectors.contains(cid.as_str()) {
            refs.insert(("connector_id".to_owned(), cid));
        }
    }

    let live_engagements_for = |person_id: &str| -> Vec<EngagementV5> {
        let Some(person) = people.get(person_id) else {
            return Vec::new();
        };
        person
            .engagements
            .iter()
            .filter(|e| engagement_is_live(e, clock))
            .cloned()
            .collect()
    };

    let bound_people = |account_id: &str| -> Vec<String> {
        let Some(account) = accounts.get(account_id) else {
            return Vec::new();
        };
        if let Some(ref ref_id) = account.immutable_person_ref {
            if !people.contains_key(ref_id.as_str()) {
                return Vec::new();
            }
            if people.iter().any(|(pid, person)| {
                pid != ref_id
                    && !live_engagements_for(pid).is_empty()
                    && person.primary_email == account.login_email
            }) {
                return Vec::new();
            }
            return vec![ref_id.clone()];
        }
        let mut matches: Vec<String> = people
            .iter()
            .filter(|(_, person)| {
                account.login_email == person.primary_email
                    || person.historical_emails.contains(&account.login_email)
            })
            .map(|(pid, _)| pid.clone())
            .collect();
        matches.sort();
        if matches.len() == 1 {
            matches
        } else {
            Vec::new()
        }
    };

    let bound_engagement = |account_id: &str, person_id: &str| -> Option<EngagementV5> {
        let account = accounts.get(account_id)?;
        let person = people.get(person_id)?;
        let live = live_engagements_for(person_id);
        if let Some(ref eng_ref) = account.bound_engagement_ref
            && let Some(candidate) = person.engagements.iter().find(|e| &e.eng_id == eng_ref)
        {
            return if live.iter().any(|l| l == candidate) {
                Some(candidate.clone())
            } else {
                None
            };
        }
        if live.len() == 1 {
            live.into_iter().next()
        } else {
            None
        }
    };

    let mut dept_people: BTreeSet<String> = BTreeSet::new();
    if manifest.phase_index == 1 {
        for account_id in &q.target_human_account_ids {
            for person_id in bound_people(account_id) {
                if let Some(eng) = bound_engagement(account_id, &person_id)
                    && eng.dept_transfer_effective_at_s.is_some()
                {
                    dept_people.insert(person_id);
                }
            }
        }
    }
    for control in &controls {
        dept_people.insert(control.monitored_person_id.clone());
        for pid in bound_people(&control.monitor_account_id) {
            dept_people.insert(pid);
        }
    }
    for person_id in &dept_people {
        let mut engs: Vec<EngagementV5> = Vec::new();
        for account_id in &q.target_human_account_ids {
            if bound_people(account_id).iter().any(|p| p == person_id)
                && let Some(bound) = bound_engagement(account_id, person_id)
            {
                engs.push(bound);
            }
        }
        if engs.is_empty() {
            engs = live_engagements_for(person_id);
        }
        for eng in engs {
            for prefix in dept_ancestors(&eng.dept_path) {
                refs.insert(("dept_prefix".to_owned(), prefix));
            }
            if eng.dept_transfer_effective_at_s.is_some()
                && let Some(ref prior) = eng.prior_dept_path
            {
                for prefix in dept_ancestors(prior) {
                    refs.insert(("dept_prefix".to_owned(), prefix));
                }
            }
        }
    }

    let campaign_ids: BTreeSet<&str> = q
        .target_uar_campaign_ids
        .iter()
        .map(String::as_str)
        .collect();
    for (cid, app_id) in uar_campaigns {
        if campaign_ids.contains(cid.as_str()) {
            refs.insert(("app_id".to_owned(), app_id));
        }
    }
    let jit_ids: BTreeSet<&str> = q
        .target_jit_request_ids
        .iter()
        .map(String::as_str)
        .collect();
    for (rid, resource, ticket) in jit_rows {
        if jit_ids.contains(rid.as_str()) {
            refs.insert(("app_id".to_owned(), resource));
            if let Some(tkt) = ticket {
                refs.insert(("ticket_partition".to_owned(), tkt));
            }
        }
    }
    let del_ids: BTreeSet<&str> = q
        .target_agent_delegation_ids
        .iter()
        .map(String::as_str)
        .collect();
    for (rid, resource) in del_rows {
        if del_ids.contains(rid.as_str()) {
            refs.insert(("app_id".to_owned(), resource));
        }
    }
    let target_accounts: BTreeSet<&str> = q
        .target_human_account_ids
        .iter()
        .map(String::as_str)
        .collect();
    if manifest.phase_index == 1 {
        for b in bridge_rows {
            if target_accounts.contains(b.account_id.as_str()) {
                refs.insert(("app_id".to_owned(), b.target_resource));
            }
        }
    }

    let is_q2_tier = |tier: AuthorityTierV5| {
        matches!(
            tier,
            AuthorityTierV5::AuthoritativePrimary | AuthorityTierV5::SecondaryReplica
        )
    };
    let all_q2_ids: BTreeSet<String> = manifest
        .source_directory
        .iter()
        .filter(|desc| {
            !desc.is_complete_for_scope
                && is_q2_tier(desc.authority_tier)
                && (0..=overlay.freshness_sla_s).contains(&(clock - desc.watermark_s))
                && desc
                    .scope_filter
                    .split_once('=')
                    .is_some_and(|(k, _)| {
                        matches!(
                            k,
                            "connector_id" | "dept_prefix" | "app_id" | "ticket_partition"
                        )
                    })
        })
        .map(|desc| desc.slice_id.clone())
        .collect();

    let mut q2_ids: Vec<String> = manifest
        .source_directory
        .iter()
        .filter(|desc| {
            !desc.is_complete_for_scope
                && is_q2_tier(desc.authority_tier)
                && (0..=overlay.freshness_sla_s).contains(&(clock - desc.watermark_s))
                && desc
                    .scope_filter
                    .split_once('=')
                    .is_some_and(|(k, v)| refs.contains(&(k.to_owned(), v.to_owned())))
        })
        .map(|desc| desc.slice_id.clone())
        .collect();
    q2_ids.sort();

    let selected_set: BTreeSet<&str> = q1_ids
        .iter()
        .chain(q2_ids.iter())
        .map(String::as_str)
        .collect();
    let all_scoped_set: BTreeSet<&str> = q1_ids
        .iter()
        .map(String::as_str)
        .chain(all_q2_ids.iter().map(String::as_str))
        .collect();

    let cost_of = |sid: &str| -> i64 {
        manifest
            .source_directory
            .iter()
            .find(|d| d.slice_id == sid)
            .map_or(0, |d| d.credit_cost)
    };
    let selected_cost: i64 = selected_set.iter().map(|sid| cost_of(sid)).sum();
    let all_scoped_cost: i64 = all_scoped_set.iter().map(|sid| cost_of(sid)).sum();

    if selected_cost > manifest.query_budget {
        return Err(OracleError::InvalidInput(format!(
            "OBS_STRATEGY exceeds phase {} query budget",
            manifest.phase_index
        )));
    }
    if all_scoped_cost <= manifest.query_budget {
        return Err(OracleError::InvalidInput(format!(
            "OBS_STRATEGY dump-all does not exceed phase {} query budget",
            manifest.phase_index
        )));
    }
    Ok((q1_ids, q2_ids))
}
