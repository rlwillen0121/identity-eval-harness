//! Pure observation strategy helpers (Q1 and Q2) from `identitybench.v5.baselines`.
//!
//! Kept independent of providers and runner so both `ib-v5-generator` and `ib-v5-runner`
//! can import these directly.

use std::collections::{BTreeMap, BTreeSet};

use ib_v5_schemas::{
    AgentDelegationRequestV5, AuthorityTierV5, CompensatingControlV5, ConnectorSchemaV5,
    DeliveredSliceV5, EngagementV5, HRPersonV5, IdPAccountV5, JITRequestV5, PhaseManifestV5,
    PhaseWorkQueueV5, PolicyCardV5, SchemaValidationError, TableRowV5, TransferBridgeV5,
    UARCampaignV5, decode_compact_table_v5, engagement_is_live,
};
use thiserror::Error;

pub const CORE_TABLES_P1_V5: [&str; 14] = [
    "hr_persons",
    "roles",
    "grants",
    "groups",
    "idp_accounts",
    "connector_schemas",
    "hr_source_mappings",
    "baseline_nhis",
    "target_nhis",
    "baseline_agents",
    "enterprise_agents",
    "cloud_ciem_tasks",
    "optimization_tasks",
    "jml_workflow_tasks",
];

pub const CORE_TABLES_P2_V5: [&str; 7] = [
    "hr_persons",
    "cae_incidents",
    "uar_campaigns",
    "historical_sessions",
    "jit_requests",
    "agent_delegation_requests",
    "optimization_tasks",
];

/// Return the core table set for `phase_index` (1 or 2).
#[must_use]
pub fn core_tables_v5(phase_index: i64) -> Option<&'static [&'static str]> {
    match phase_index {
        1 => Some(&CORE_TABLES_P1_V5),
        2 => Some(&CORE_TABLES_P2_V5),
        _ => None,
    }
}

#[derive(Debug, Error)]
pub enum ObsStrategyError {
    #[error("phase_index must be 1 or 2, got {0}")]
    InvalidPhaseIndex(i64),
    #[error("compact table decode failed: {0}")]
    Decode(#[from] SchemaValidationError),
}

fn canonical_dept_prefixes(path: &str) -> Vec<String> {
    if !path.starts_with('/') {
        return vec![path.to_owned()];
    }
    let parts: Vec<&str> = path.split('/').filter(|p| !p.is_empty()).collect();
    (1..=parts.len())
        .map(|i| format!("/{}", parts[..i].join("/")))
        .collect()
}

/// Derive `OBS_STRATEGY` Q2 scope references from only Q1 rows and the public work queue.
pub fn observable_q2_references(
    q1_slices: &[DeliveredSliceV5],
    manifest: &PhaseManifestV5,
    work_queue: &PhaseWorkQueueV5,
    phase_index: i64,
) -> Result<BTreeSet<(String, String)>, ObsStrategyError> {
    if !matches!(phase_index, 1 | 2) {
        return Err(ObsStrategyError::InvalidPhaseIndex(phase_index));
    }
    let clock = manifest.clock_epoch_s;

    let mut connector_schemas: Vec<ConnectorSchemaV5> = Vec::new();
    let mut people: BTreeMap<String, HRPersonV5> = BTreeMap::new();
    let mut accounts: BTreeMap<String, IdPAccountV5> = BTreeMap::new();
    let mut controls: Vec<CompensatingControlV5> = Vec::new();
    let mut uar_campaigns: Vec<UARCampaignV5> = Vec::new();
    let mut jit_requests: Vec<JITRequestV5> = Vec::new();
    let mut delegation_requests: Vec<AgentDelegationRequestV5> = Vec::new();
    let mut transfer_bridges: Vec<TransferBridgeV5> = Vec::new();

    for slice in q1_slices {
        for table in &slice.tables {
            for row in decode_compact_table_v5(table)? {
                match row {
                    TableRowV5::ConnectorSchema(c) => connector_schemas.push(c),
                    TableRowV5::HrPerson(p) => {
                        people.insert(p.person_id.clone(), p);
                    }
                    TableRowV5::IdpAccount(a) => {
                        accounts.insert(a.account_id.clone(), a);
                    }
                    TableRowV5::CompensatingControl(cc) => controls.push(cc),
                    TableRowV5::UarCampaign(u) => uar_campaigns.push(u),
                    TableRowV5::JitRequest(j) => jit_requests.push(j),
                    TableRowV5::AgentDelegationRequest(d) => delegation_requests.push(d),
                    TableRowV5::TransferBridge(b) => transfer_bridges.push(b),
                    _ => {}
                }
            }
        }
    }

    let mut refs = BTreeSet::new();
    let target_connectors: BTreeSet<&str> = work_queue
        .target_connector_ids
        .iter()
        .map(String::as_str)
        .collect();
    for conn in &connector_schemas {
        if target_connectors.contains(conn.connector_id.as_str()) {
            refs.insert(("connector_id".to_owned(), conn.connector_id.clone()));
        }
    }

    let live_engagements = |person_id: &str| -> Vec<&EngagementV5> {
        people
            .get(person_id)
            .map(|person| {
                person
                    .engagements
                    .iter()
                    .filter(|e| engagement_is_live(e, clock))
                    .collect()
            })
            .unwrap_or_default()
    };

    let bound_people = |account_id: &str| -> Vec<String> {
        let Some(account) = accounts.get(account_id) else {
            return Vec::new();
        };
        if let Some(ref_id) = &account.immutable_person_ref {
            if !people.contains_key(ref_id) {
                return Vec::new();
            }
            let has_collision = people.iter().any(|(pid, person)| {
                pid != ref_id
                    && !live_engagements(pid).is_empty()
                    && person.primary_email == account.login_email
            });
            if has_collision {
                return Vec::new();
            }
            return vec![ref_id.clone()];
        }
        let matches: Vec<String> = people
            .iter()
            .filter(|(_, person)| {
                account.login_email == person.primary_email
                    || person.historical_emails.contains(&account.login_email)
            })
            .map(|(pid, _)| pid.clone())
            .collect();
        if matches.len() == 1 {
            matches
        } else {
            Vec::new()
        }
    };

    let bound_engagement = |account_id: &str, person_id: &str| -> Option<&EngagementV5> {
        let account = accounts.get(account_id)?;
        let person = people.get(person_id)?;
        if let Some(eng_ref) = &account.bound_engagement_ref
            && let Some(candidate) = person.engagements.iter().find(|e| &e.eng_id == eng_ref)
        {
            return engagement_is_live(candidate, clock).then_some(candidate);
        }
        let live = live_engagements(person_id);
        if live.len() == 1 { Some(live[0]) } else { None }
    };

    let target_accounts: BTreeSet<&str> = work_queue
        .target_human_account_ids
        .iter()
        .map(String::as_str)
        .collect();
    let mut dept_people = BTreeSet::new();
    if phase_index == 1 {
        for &account_id in &target_accounts {
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
        dept_people.extend(bound_people(&control.monitor_account_id));
    }

    for person_id in &dept_people {
        let mut visible_engagements: Vec<&EngagementV5> = target_accounts
            .iter()
            .filter(|&&account_id| bound_people(account_id).iter().any(|p| p == person_id))
            .filter_map(|&account_id| bound_engagement(account_id, person_id))
            .collect();
        if visible_engagements.is_empty() {
            visible_engagements = live_engagements(person_id);
        }
        for engagement in visible_engagements {
            for prefix in canonical_dept_prefixes(&engagement.dept_path) {
                refs.insert(("dept_prefix".to_owned(), prefix));
            }
            if engagement.dept_transfer_effective_at_s.is_some()
                && let Some(prior) = &engagement.prior_dept_path
            {
                for prefix in canonical_dept_prefixes(prior) {
                    refs.insert(("dept_prefix".to_owned(), prefix));
                }
            }
        }
    }

    let campaign_ids: BTreeSet<&str> = work_queue
        .target_uar_campaign_ids
        .iter()
        .map(String::as_str)
        .collect();
    for camp in &uar_campaigns {
        if campaign_ids.contains(camp.campaign_id.as_str()) {
            refs.insert(("app_id".to_owned(), camp.app_id.clone()));
        }
    }

    let jit_ids: BTreeSet<&str> = work_queue
        .target_jit_request_ids
        .iter()
        .map(String::as_str)
        .collect();
    for req in &jit_requests {
        if jit_ids.contains(req.request_id.as_str()) {
            refs.insert(("app_id".to_owned(), req.target_resource.clone()));
            if let Some(ticket) = &req.change_ticket_id {
                refs.insert(("ticket_partition".to_owned(), ticket.clone()));
            }
        }
    }

    let delegation_ids: BTreeSet<&str> = work_queue
        .target_agent_delegation_ids
        .iter()
        .map(String::as_str)
        .collect();
    for dlreq in &delegation_requests {
        if delegation_ids.contains(dlreq.request_id.as_str()) {
            refs.insert(("app_id".to_owned(), dlreq.target_resource.clone()));
        }
    }

    if phase_index == 1 {
        for bridge in &transfer_bridges {
            if target_accounts.contains(bridge.account_id.as_str()) {
                refs.insert(("app_id".to_owned(), bridge.target_resource.clone()));
            }
        }
    }

    Ok(refs)
}

/// `OBS_STRATEGY` Step Q1, derived from public manifest metadata only.
pub fn obs_strategy_q1(
    manifest: &PhaseManifestV5,
    policy_card: &PolicyCardV5,
    phase_index: i64,
) -> Result<Vec<String>, ObsStrategyError> {
    let core_tables =
        core_tables_v5(phase_index).ok_or(ObsStrategyError::InvalidPhaseIndex(phase_index))?;
    let clock = manifest.clock_epoch_s;
    let freshness = policy_card.overlay.freshness_sla_s;
    let mut selected = BTreeSet::new();
    for desc in &manifest.source_directory {
        let age = clock - desc.watermark_s;
        if desc.authority_tier == AuthorityTierV5::AuthoritativePrimary
            && desc.is_complete_for_scope
            && 0 <= age
            && age <= freshness
            && desc
                .table_names
                .iter()
                .any(|t| core_tables.contains(&t.as_str()))
        {
            selected.insert(desc.slice_id.clone());
        }
    }
    Ok(selected.into_iter().collect())
}

/// `OBS_STRATEGY` Step Q2, matching fresh partial slices to references observed in Q1.
pub fn obs_strategy_q2(
    manifest: &PhaseManifestV5,
    q1_slices: &[DeliveredSliceV5],
    work_queue: &PhaseWorkQueueV5,
    policy_card: &PolicyCardV5,
    phase_index: i64,
) -> Result<Vec<String>, ObsStrategyError> {
    if !matches!(phase_index, 1 | 2) {
        return Err(ObsStrategyError::InvalidPhaseIndex(phase_index));
    }
    let refs = observable_q2_references(q1_slices, manifest, work_queue, phase_index)?;
    let clock = manifest.clock_epoch_s;
    let freshness = policy_card.overlay.freshness_sla_s;
    let mut selected = BTreeSet::new();
    for desc in &manifest.source_directory {
        if desc.is_complete_for_scope {
            continue;
        }
        if !matches!(
            desc.authority_tier,
            AuthorityTierV5::AuthoritativePrimary | AuthorityTierV5::SecondaryReplica
        ) {
            continue;
        }
        let age = clock - desc.watermark_s;
        if !(0 <= age && age <= freshness) {
            continue;
        }
        if let Some((key, value)) = desc.scope_filter.split_once('=')
            && refs.contains(&(key.to_owned(), value.to_owned()))
        {
            selected.insert(desc.slice_id.clone());
        }
    }
    Ok(selected.into_iter().collect())
}
