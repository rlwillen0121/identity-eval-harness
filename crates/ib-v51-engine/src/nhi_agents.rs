//! Deterministic Non-Human Identity (NHI) custody, AI Agent governance, and RFC 8693 OBO
//! delegation evaluation & audit helpers for `SPEC-v5.1`.

use std::collections::{BTreeSet, HashMap, HashSet};

use ib_v51_schemas::{
    AffiliationTypeV51, AgentDecisionV51, AgentDelegationRequestV51, AgentGovernanceActionV51,
    DelegationDecisionV51, EnterpriseAiAgentRecordV51, EnterpriseBrownfieldWorldV51,
    IgaDeploymentConfigV51, LifecycleStateIdV51, NhiDecisionV51, NhiWorkloadRecordV51,
    SodPolicyV51,
};
use indexmap::IndexMap;

use crate::coalescence::CoalescedIdentity;
use crate::lifecycle::EvaluatedLifecycle;
use crate::roles::{expand_entitlements_transitively, find_sod_conflicts_transitive};

/// Combined normalized output of NHI custody, AI agent governance, and RFC 8693 OBO delegation decisions.
#[derive(Debug, Clone, Default)]
pub struct NhiAndAgentEvalOutput {
    pub nhi_outcomes: IndexMap<String, NhiDecisionV51>,
    pub agent_outcomes: IndexMap<String, AgentDecisionV51>,
    pub delegation_outcomes: IndexMap<String, DelegationDecisionV51>,
}

/// Evaluate `config.nhi_and_agents` deterministically against `world.nhi_workloads`,
/// `world.ai_agents`, and `world.agent_delegation_requests`.
#[must_use]
pub fn evaluate_nhi_and_agents(
    world: &EnterpriseBrownfieldWorldV51,
    config: &IgaDeploymentConfigV51,
) -> NhiAndAgentEvalOutput {
    let mut nhi_outcomes = IndexMap::new();
    let mut sorted_nhis: Vec<&NhiWorkloadRecordV51> = world.nhi_workloads.iter().collect();
    sorted_nhis.sort_by(|a, b| a.nhi_id.cmp(&b.nhi_id));

    for nhi in sorted_nhis {
        if let Some(dec) = config.nhi_and_agents.nhi_decisions.get(&nhi.nhi_id) {
            nhi_outcomes.insert(
                nhi.nhi_id.clone(),
                NhiDecisionV51 {
                    disposition: dec.disposition,
                    resolved_custodian_id: normalize_opt_str(dec.resolved_custodian_id.as_deref()),
                },
            );
        }
    }

    let mut agent_outcomes = IndexMap::new();
    let mut sorted_agents: Vec<&EnterpriseAiAgentRecordV51> = world.ai_agents.iter().collect();
    sorted_agents.sort_by(|a, b| a.agent_id.cmp(&b.agent_id));

    for agent in sorted_agents {
        if let Some(dec) = config.nhi_and_agents.agent_decisions.get(&agent.agent_id) {
            agent_outcomes.insert(
                agent.agent_id.clone(),
                AgentDecisionV51 {
                    action: dec.action,
                    resolved_sponsor_person_key: normalize_opt_str(
                        dec.resolved_sponsor_person_key.as_deref(),
                    ),
                    approved_autonomous_scopes: normalize_sorted_strings(
                        &dec.approved_autonomous_scopes,
                    ),
                    approved_hitl_scopes: normalize_sorted_strings(&dec.approved_hitl_scopes),
                },
            );
        }
    }

    let mut delegation_outcomes = IndexMap::new();
    let mut sorted_reqs: Vec<&AgentDelegationRequestV51> =
        world.agent_delegation_requests.iter().collect();
    sorted_reqs.sort_by(|a, b| a.request_id.cmp(&b.request_id));

    for req in sorted_reqs {
        if let Some(dec) = config
            .nhi_and_agents
            .delegation_decisions
            .get(&req.request_id)
        {
            delegation_outcomes.insert(
                req.request_id.clone(),
                DelegationDecisionV51 {
                    decision: dec.decision,
                    verified_approver_person_key: normalize_opt_str(
                        dec.verified_approver_person_key.as_deref(),
                    ),
                },
            );
        }
    }

    NhiAndAgentEvalOutput {
        nhi_outcomes,
        agent_outcomes,
        delegation_outcomes,
    }
}

/// Walk up `manager_person_key` (cycle-safe, up to `max_hops`) starting from `start_person_key`
/// to find the first human person in `eligible_states`.
#[must_use]
pub fn resolve_live_human_custodian(
    start_person_key: Option<&str>,
    cubes: &IndexMap<String, CoalescedIdentity<'_>>,
    lifecycles: &IndexMap<String, EvaluatedLifecycle>,
    eligible_states: &[LifecycleStateIdV51],
    max_hops: usize,
) -> Option<String> {
    let mut visited = HashSet::new();
    let mut cursor = start_person_key.map(str::trim).filter(|s| !s.is_empty());
    let mut hops = 0usize;

    while let Some(pk) = cursor {
        if hops > max_hops || !visited.insert(pk.to_owned()) {
            break;
        }
        let state = lifecycles
            .get(pk)
            .map(|lc| lc.lifecycle_state)
            .unwrap_or(LifecycleStateIdV51::InactiveOrphan);
        if eligible_states.contains(&state) {
            return Some(pk.to_owned());
        }
        cursor = cubes
            .get(pk)
            .and_then(|c| c.attributes.get("manager_person_key"))
            .map(|s| s.trim())
            .filter(|s| !s.is_empty());
        hops += 1;
    }

    None
}

/// Check whether a human person is active and eligible to serve as an AI agent sponsor
/// (active lifecycle state and non-external/non-contingent affiliation when required).
#[must_use]
pub fn is_human_active(
    person_key: &str,
    lifecycles: &IndexMap<String, EvaluatedLifecycle>,
) -> bool {
    lifecycles
        .get(person_key)
        .is_some_and(|lc| lc.lifecycle_state == LifecycleStateIdV51::Active)
}

/// Check whether a human person holds an external/contingent affiliation (`ContingentWorker`,
/// `Vendor`, or `B2bGuest`).
#[must_use]
pub fn is_external_or_contingent_person(
    person_key: &str,
    cubes: &IndexMap<String, CoalescedIdentity<'_>>,
) -> bool {
    cubes.get(person_key).is_some_and(|c| {
        matches!(
            c.primary_affiliation,
            AffiliationTypeV51::ContingentWorker
                | AffiliationTypeV51::Vendor
                | AffiliationTypeV51::B2bGuest
        ) || c
            .active_affiliations
            .contains(&AffiliationTypeV51::ContingentWorker)
            || c.active_affiliations.contains(&AffiliationTypeV51::Vendor)
            || c.active_affiliations
                .contains(&AffiliationTypeV51::B2bGuest)
    })
}

/// Check whether an `NhiWorkloadRecordV51` has an expired non-vault-managed secret.
#[must_use]
pub fn is_nhi_secret_expired(nhi: &NhiWorkloadRecordV51) -> bool {
    !nhi.vault_managed_rotation && nhi.secret_age_days > nhi.max_secret_age_days
}

/// Check whether an RFC 8693 OBO delegation request violates hop limits, upstream scope attenuation,
/// or allowed MCP target audience rules.
#[must_use]
pub fn is_obo_scope_or_hop_excess(
    req: &AgentDelegationRequestV51,
    agents_by_id: &HashMap<&str, &EnterpriseAiAgentRecordV51>,
) -> bool {
    if req.actor_chain.len() > req.max_allowed_hops as usize {
        return true;
    }
    if !req.granted_upstream_scopes.is_empty()
        && !req
            .granted_upstream_scopes
            .iter()
            .any(|s| s.trim() == req.requested_tool_scope.trim())
    {
        return true;
    }
    for actor_id in &req.actor_chain {
        if let Some(agent) = agents_by_id.get(actor_id.as_str()) {
            if req.actor_chain.len() > agent.max_obo_hops as usize {
                return true;
            }
            if !agent.allowed_mcp_servers.is_empty()
                && !agent
                    .allowed_mcp_servers
                    .iter()
                    .any(|srv| srv.trim() == req.target_audience.trim())
            {
                return true;
            }
        }
    }
    false
}

/// Check whether an RFC 8693 OBO delegation request involves an inactive initiating human,
/// an unattested/unregistered/non-authorized agent in `actor_chain`, or an inactive sponsor.
#[must_use]
pub fn is_obo_actor_or_sponsor_inactive(
    req: &AgentDelegationRequestV51,
    agents_by_id: &HashMap<&str, &EnterpriseAiAgentRecordV51>,
    agent_outcomes: &IndexMap<String, AgentDecisionV51>,
    lifecycles: &IndexMap<String, EvaluatedLifecycle>,
) -> bool {
    if !is_human_active(&req.initiating_human_person_key, lifecycles) {
        return true;
    }
    for actor_id in &req.actor_chain {
        let Some(agent) = agents_by_id.get(actor_id.as_str()) else {
            return true;
        };
        if !agent.registered_in_catalog || !agent.attested_blueprint {
            return true;
        }
        if let Some(dec) = agent_outcomes.get(actor_id.as_str())
            && dec.action != AgentGovernanceActionV51::AuthorizeActive
        {
            return true;
        }
        if let Some(sponsor) = agent.sponsor_person_key.as_deref()
            && !is_human_active(sponsor, lifecycles)
        {
            return true;
        }
    }
    false
}

/// Check whether an RFC 8693 OBO delegation request triggers a human-agent split-SoD conflict
/// across the initiating human's effective entitlements (primary + `adm_*` + birthright, transitively
/// expanded), prior human/agent actions, and the requested tool scope.
#[must_use]
pub fn detect_obo_split_sod_conflicts(
    req: &AgentDelegationRequestV51,
    person_effective_entitlements: &IndexMap<String, BTreeSet<String>>,
    world: &EnterpriseBrownfieldWorldV51,
    sod_policies: &[SodPolicyV51],
) -> Vec<String> {
    let mut combined: BTreeSet<String> = person_effective_entitlements
        .get(&req.initiating_human_person_key)
        .cloned()
        .unwrap_or_default();

    for prior in &req.prior_human_or_agent_actions {
        let p = prior.trim();
        if !p.is_empty() {
            combined.insert(p.to_owned());
        }
    }

    let requested = req.requested_tool_scope.trim();
    if !requested.is_empty() {
        combined.insert(requested.to_owned());
    }

    let expanded = expand_entitlements_transitively(combined.iter(), world);
    find_sod_conflicts_transitive(&expanded, world, sod_policies)
}

fn normalize_opt_str(val: Option<&str>) -> Option<String> {
    val.map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
}

fn normalize_sorted_strings(values: &[String]) -> Vec<String> {
    values
        .iter()
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}
