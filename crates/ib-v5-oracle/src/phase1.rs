//! Phase-1 deterministic gold evaluators (P1.1a through P1.9).
//!
//! Mirrors the Phase-1 functions of `identitybench.v5.oracle`.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::sync::{Arc, LazyLock};

use ib_core::pyjson::{PyNumber, classify_number};
use ib_v5_schemas::scope_algebra::{
    CapabilityFootprint, capability_footprint, effective_department, effective_grant_footprint,
    engagement_is_live, group_closure, group_footprints, has_live_engagement, is_subordinate,
    resolve_scope, sod_witnesses, syntactically_mitigates, unmitigated_witnesses,
};
use ib_v5_schemas::{
    AccountReconActionV5, AccountReconMutationKind, AccountReconMutationV5, ActiveDisabledStatusV5,
    AffiliationKindV5, AgentRegistryActionV5, AgentRegistryMutationKind, AgentRegistryMutationV5,
    AgentToolModeV5, AtomicMutationV5, BaselineAgentStatusV5, CIEMEscalationVectorV5,
    CIEMPrincipalFindingV5, CIEMRemediationActionV5, CloudCIEMMutationKind, CloudCIEMMutationV5,
    CloudProviderV5, CompensatingControlV5, ConnectorAuthStateV5, ConnectorHaltReasonV5,
    ConnectorProvisioningModeV5, ConnectorSyncModeV5, ConnectorSyncMutationKind,
    ConnectorSyncMutationV5, ConnectorTransportKindV5, ConnectorUnmappedPolicyDefaultV5,
    ContractorSponsorCheckModeV5, DisconnectedTargetPolicyV5, EngagementStatusV5,
    EnterpriseAgentV5, EnvironmentKindV5, GrantModeV5, HRCorrelationStrategyV5, HRSourceFeedSpecV5,
    HRSourceKindV5, HRSourceMappingMutationKind, HRSourceMappingMutationV5, ITSMTicketActionKindV5,
    ITSMTicketActionV5, ITSMTicketRecordV5, ITSMTicketStatusV5, JMLTraceEventKindV5,
    JMLTraceEventV5, JMLWorkflowMutationKind, JMLWorkflowMutationV5, JMLWorkflowSpecV5,
    JoinerContractorSponsorPolicyV5, JoinerStepV5, LegalHoldDispositionV5, LegalHoldScopeModeV5,
    LeaverStepV5, MoverStepV5, NHIGovernanceActionV5, NHIGovernanceMutationKind,
    NHIGovernanceMutationV5, NHIOwnerResolutionSourceV5, NHISubkindV5, NoShowDispositionV5,
    OptActionKindV5, OptCandidateActionV5, OptimizationMutationKind, OptimizationMutationV5,
    OptimizationPhaseKindV5, OrphanCustodyModeV5, OrphanHandoffPolicyV5, PrivilegeTierV5,
    RawHRFeedAssertionV5, SpokeErrorPolicyV5, TelemetrySignalSourceV5, TelemetrySignalV5,
    UnmappedPolicyOverrideV5,
};
use regex::Regex;
use serde_json::Value;

use crate::error::OracleError;
use crate::oracle::{
    account_binding_people, bind_person_p1, grant_fp, group_fp, p1_action, person_standing_from_p1,
    pre_person, record_trace_mutation, sorted_unique, version_tokens,
};
use crate::types::{
    AccountPlanV5, AgentOutcomeV5, ConnectorOutcomeV5, NhiOutcomeV5, NhiSignalRowTraceV5,
    OptimizationDetailsV5, OptimizationFeasibilityFn, OptimizationFeasibilityV5, PhaseContext,
    TraceContextV5,
};

static ACCT_ROW_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\Aacct_[a-z0-9_]+\z").expect("valid regex"));
static RAW_ENT_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[a-zA-Z0-9_./:-]{1,64}\z").expect("valid regex"));

#[derive(Debug, Clone)]
struct ConnectorRowState {
    v: Option<i64>,
    status: String,
    ents: BTreeSet<String>,
}

/// P1.1a transport reconciliation, drift, and audit-sink health.
pub fn phase1_connectors(
    ctx: &mut PhaseContext,
) -> Result<BTreeMap<String, ConnectorOutcomeV5>, OracleError> {
    let world = ctx.state.clone();
    let clock = ctx.clock();
    let mut target_cids = ctx.queue.target_connector_ids.clone();
    target_cids.sort();
    let mut outcomes = BTreeMap::new();

    for cid in target_cids {
        let conn = world
            .connector_schemas
            .get(&cid)
            .ok_or_else(|| OracleError::InvalidInput(format!("missing connector {cid:?}")))?
            .clone();
        let safe: Vec<String> = if conn.sync_mode == ConnectorSyncModeV5::DeltaCursor
            || conn.transport_kind == ConnectorTransportKindV5::FlatFileSftp
        {
            let mut v = conn.baseline_reconciled_account_ids.clone();
            v.sort();
            v
        } else {
            Vec::new()
        };

        let mut halt = ConnectorHaltReasonV5::None;
        let mut inspected = Vec::new();
        let mut row_state: BTreeMap<String, ConnectorRowState> = BTreeMap::new();
        let mut total_rows: i64 = 0;

        if conn.auth_state != ConnectorAuthStateV5::AuthOk {
            halt = ConnectorHaltReasonV5::HaltAuthOrTls;
        } else if conn.transport_kind == ConnectorTransportKindV5::JdbcSql
            && conn.sync_mode == ConnectorSyncModeV5::DeltaCursor
            && !conn.has_delete_audit_view
        {
            halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
        } else {
            let mut cur = "cursor_0".to_owned();
            let mut visited = BTreeSet::new();
            while halt == ConnectorHaltReasonV5::None {
                if !visited.insert(cur.clone()) {
                    halt = ConnectorHaltReasonV5::HaltCursorOrHttp;
                    break;
                }
                let mut attempts: Vec<_> = world
                    .connector_pages
                    .iter()
                    .filter(|p| p.connector_id == cid && p.cursor_in == cur)
                    .cloned()
                    .collect();
                attempts.sort_by_key(|p| p.attempt_index);

                let mut page_opt = None;
                for attempt in attempts {
                    inspected.push(attempt.clone());
                    if attempt.http_status == 429 {
                        continue;
                    }
                    if attempt.http_status == 500 {
                        halt = ConnectorHaltReasonV5::HaltCursorOrHttp;
                        break;
                    }
                    if attempt.http_status == 200 {
                        page_opt = Some(attempt);
                        break;
                    }
                }
                if halt != ConnectorHaltReasonV5::None {
                    break;
                }
                let Some(page) = page_opt else {
                    halt = ConnectorHaltReasonV5::HaltCursorOrHttp;
                    break;
                };
                if cur == "cursor_0"
                    && conn.sync_mode == ConnectorSyncModeV5::DeltaCursor
                    && page.base_checkpoint_etag != conn.local_checkpoint_etag
                {
                    halt = ConnectorHaltReasonV5::HaltCheckpointDrift;
                    break;
                }

                for raw_row in &page.records {
                    total_rows += 1;
                    let mut rec: HashMap<&str, &Value> = HashMap::with_capacity(raw_row.len());
                    let mut dup_key = false;
                    for (k, v) in raw_row {
                        if rec.insert(k.as_str(), v).is_some() {
                            dup_key = true;
                            break;
                        }
                    }
                    if dup_key {
                        halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
                        break;
                    }
                    if conn.required_fields.iter().any(|f| {
                        !matches!(rec.get(f.as_str()), Some(Value::String(s)) if !s.trim().is_empty())
                    }) {
                        halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
                        break;
                    }
                    let aid = match rec.get("account_id") {
                        Some(Value::String(s)) if ACCT_ROW_RE.is_match(s) => s.clone(),
                        _ => {
                            halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
                            break;
                        }
                    };
                    let status = match rec.get("status") {
                        Some(Value::String(s))
                            if matches!(s.as_str(), "ACTIVE" | "DISABLED" | "TOMBSTONE") =>
                        {
                            s.clone()
                        }
                        _ => {
                            halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
                            break;
                        }
                    };
                    let mut ents = BTreeSet::new();
                    match rec.get("raw_entitlements") {
                        None => {}
                        Some(Value::Array(arr)) => {
                            let mut bad = false;
                            for item in arr {
                                match item {
                                    Value::String(s) if RAW_ENT_RE.is_match(s) => {
                                        ents.insert(s.clone());
                                    }
                                    _ => {
                                        bad = true;
                                        break;
                                    }
                                }
                            }
                            if bad {
                                halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
                                break;
                            }
                        }
                        Some(_) => {
                            halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
                            break;
                        }
                    }
                    let version: Option<i64> = match rec.get("version") {
                        None | Some(Value::Null) => None,
                        Some(Value::Number(n)) => match classify_number(n) {
                            PyNumber::Int(s) => match s.parse::<i64>() {
                                Ok(v) => Some(v),
                                Err(_) => {
                                    halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
                                    break;
                                }
                            },
                            PyNumber::Float(_) => {
                                halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
                                break;
                            }
                        },
                        Some(_) => {
                            halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
                            break;
                        }
                    };

                    let value = ConnectorRowState {
                        v: version,
                        status: status.clone(),
                        ents: if status == "TOMBSTONE" {
                            BTreeSet::new()
                        } else {
                            ents.clone()
                        },
                    };

                    match conn.transport_kind {
                        ConnectorTransportKindV5::FlatFileSftp => {
                            if row_state.contains_key(&aid) {
                                halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
                                break;
                            }
                            row_state.insert(aid, value);
                        }
                        ConnectorTransportKindV5::RestWebService => {
                            if let Some(prior) = row_state.get(&aid) {
                                if version.is_some() != prior.v.is_some() {
                                    halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
                                    break;
                                }
                                if version.is_none() || version >= prior.v {
                                    row_state.insert(aid, value);
                                }
                            } else {
                                row_state.insert(aid, value);
                            }
                        }
                        ConnectorTransportKindV5::JdbcSql => {
                            if let Some(prior) = row_state.get_mut(&aid) {
                                if version.is_some() != prior.v.is_some()
                                    || (version.is_some() && version < prior.v)
                                {
                                    halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
                                    break;
                                }
                                if version.is_some() && version > prior.v {
                                    *prior = value;
                                } else if status != prior.status {
                                    halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
                                    break;
                                } else if status != "TOMBSTONE" {
                                    prior.ents.extend(ents);
                                }
                            } else {
                                row_state.insert(aid, value);
                            }
                        }
                    }
                }
                if halt != ConnectorHaltReasonV5::None {
                    break;
                }
                let Some(next_cursor) = page.next_cursor else {
                    halt = ConnectorHaltReasonV5::HaltCursorOrHttp;
                    break;
                };
                if next_cursor == "cursor_end" {
                    break;
                }
                cur = next_cursor;
            }
        }

        if halt == ConnectorHaltReasonV5::None
            && conn.transport_kind == ConnectorTransportKindV5::FlatFileSftp
            && Some(total_rows) != conn.expected_file_row_count
        {
            halt = ConnectorHaltReasonV5::HaltSchemaOrTrailer;
        }

        let baseline: BTreeSet<String> = conn.baseline_reconciled_account_ids.iter().cloned().collect();
        let mut candidate = BTreeSet::new();
        if halt == ConnectorHaltReasonV5::None {
            if conn.sync_mode == ConnectorSyncModeV5::FullSnapshot {
                for (aid, row) in &row_state {
                    if row.status != "TOMBSTONE" {
                        candidate.insert(aid.clone());
                    }
                }
            } else {
                let mut tombstones = BTreeSet::new();
                let mut present = BTreeSet::new();
                for (aid, row) in &row_state {
                    if row.status == "TOMBSTONE" {
                        tombstones.insert(aid.clone());
                    } else {
                        present.insert(aid.clone());
                    }
                }
                for aid in &baseline {
                    if !tombstones.contains(aid) {
                        candidate.insert(aid.clone());
                    }
                }
                candidate.extend(present);
            }

            if conn.transport_kind == ConnectorTransportKindV5::FlatFileSftp
                && conn.sync_mode == ConnectorSyncModeV5::FullSnapshot
                && !baseline.is_empty()
            {
                let deleted = baseline.difference(&candidate).count() as u128;
                let max_bps = conn.max_delete_threshold_bps.max(0) as u128;
                if deleted * 10_000 > max_bps * (baseline.len() as u128) {
                    halt = ConnectorHaltReasonV5::HaltMassDeleteGuard;
                }
            }
        }

        let mut drift = BTreeSet::new();
        if halt == ConnectorHaltReasonV5::None {
            let halt_on_unmapped = match conn.unmapped_policy_override {
                UnmappedPolicyOverrideV5::UseOverlayDefault => {
                    ctx.overlay().connector_unmapped_policy_default
                        == ConnectorUnmappedPolicyDefaultV5::HaltConnector
                }
                UnmappedPolicyOverrideV5::HaltConnector => true,
                UnmappedPolicyOverrideV5::QuarantineDrift => false,
            };
            let cmap: HashMap<&str, &str> = conn
                .mapping_rules
                .iter()
                .map(|(k, v)| (k.as_str(), v.as_str()))
                .collect();

            for aid in &candidate {
                let Some(row) = row_state.get(aid.as_str()) else {
                    continue;
                };
                if row.status == "TOMBSTONE" {
                    continue;
                }
                for raw in &row.ents {
                    let Some(&role) = cmap.get(raw.as_str()) else {
                        if halt_on_unmapped {
                            halt = ConnectorHaltReasonV5::HaltUnmappedStrict;
                            break;
                        }
                        drift.insert(format!("{aid}:unmapped:{raw}"));
                        continue;
                    };
                    let mut authorized = false;
                    if let Some(account) = world.idp_accounts.get(aid.as_str())
                        && account.status == ActiveDisabledStatusV5::Active
                        && world.roles.contains_key(role)
                    {
                        for gid in &account.direct_grant_ids {
                            if let Some(grant) = world.grants.get(gid.as_str())
                                && grant.role_id == role
                                && grant.grant_mode == GrantModeV5::Standing
                                && !grant_fp(ctx, account, gid)?.is_empty()
                            {
                                authorized = true;
                                break;
                            }
                        }
                        if !authorized {
                            let (_, deny_fp) = group_fp(ctx, account)?;
                            for gid in group_closure(account, &world.groups) {
                                let group = &world.groups[gid.as_str()];
                                if group.effect != ib_v5_schemas::GroupEffectV5::Allow
                                    || !group.granted_role_ids.iter().any(|r| r == role)
                                {
                                    continue;
                                }
                                let raw_fp = capability_footprint(
                                    ctx.catalog(),
                                    world.roles[role].capabilities.iter().map(String::as_str),
                                    [group.scope_path.as_str()],
                                )?;
                                if raw_fp.difference(&deny_fp).next().is_some() {
                                    authorized = true;
                                    break;
                                }
                            }
                        }
                    }
                    if !authorized {
                        drift.insert(format!("{aid}:{role}"));
                    }
                }
                if halt != ConnectorHaltReasonV5::None {
                    break;
                }
            }
        }

        let halted = halt != ConnectorHaltReasonV5::None;
        let reconciled: Vec<String> = if halted {
            safe.clone()
        } else {
            candidate.iter().cloned().collect()
        };
        let drift_vec: Vec<String> = if halted {
            Vec::new()
        } else {
            drift.into_iter().collect()
        };
        let audit_sink_healthy = !halted
            && (0..=ctx.overlay().freshness_sla_s).contains(&(clock - conn.audit_watermark_s));

        outcomes.insert(
            cid,
            ConnectorOutcomeV5 {
                connector: conn,
                halted_fail_closed: halted,
                halt_reason: halt,
                reconciled_account_ids: reconciled.clone(),
                remediated_spoke_drift: drift_vec.clone(),
                itsm_ticket_actions: Vec::new(),
                inspected_pages: inspected,
                candidate_ids: reconciled,
                drift_ids: drift_vec,
                audit_sink_healthy,
            },
        );
    }
    ctx.semantic.p1_connectors = outcomes.clone();
    Ok(outcomes)
}

/// P1.1b authoritative HR feed gates, collision quarantine, precedence.
pub fn phase1_hr(
    ctx: &mut PhaseContext,
    conn_results: &BTreeMap<String, ConnectorOutcomeV5>,
) -> Result<Vec<AtomicMutationV5>, OracleError> {
    let world = ctx.state.clone();
    let mut target_tids = ctx.queue.target_hr_mapping_ids.clone();
    target_tids.sort();
    let mut mutations = Vec::new();

    for tid in target_tids {
        let task = world
            .hr_source_mappings
            .get(&tid)
            .ok_or_else(|| OracleError::InvalidInput(format!("missing hr mapping task {tid:?}")))?
            .clone();

        let mut accepted: Vec<&HRSourceFeedSpecV5> = Vec::new();
        let mut rejected: Vec<&HRSourceFeedSpecV5> = Vec::new();
        for feed in &task.feeds {
            let healthy = conn_results
                .get(&feed.connector_id)
                .is_some_and(|c| c.audit_sink_healthy);
            let blocked_mutable = ctx.overlay().require_immutable_hr_correlation
                && feed.correlation_strategy == HRCorrelationStrategyV5::MutableEmailOnly;
            if feed.source_kind != HRSourceKindV5::TargetAppSpoke && healthy && !blocked_mutable {
                accepted.push(feed);
            } else {
                rejected.push(feed);
            }
        }

        let emp_index: HashMap<&str, &str> = world
            .hr_persons
            .values()
            .map(|p| (p.emp_immutable_id.as_str(), p.person_id.as_str()))
            .collect();

        let mut valid_rows: Vec<(&HRSourceFeedSpecV5, &RawHRFeedAssertionV5)> = Vec::new();
        let mut quarantined = BTreeSet::new();
        let mut consulted = BTreeSet::new();

        for feed in &accepted {
            let status_map: HashMap<&str, EngagementStatusV5> = feed
                .status_code_map
                .iter()
                .map(|(k, v)| (k.as_str(), *v))
                .collect();
            for assertion in &feed.assertions {
                let key = format!("{}:{}", feed.feed_id, assertion.person_id);
                let p = world.hr_persons.get(&assertion.person_id);
                if let Some(person) = p {
                    consulted.insert(person.person_id.clone());
                }
                for candidate in world.hr_persons.values() {
                    if assertion
                        .emp_immutable_id
                        .as_ref()
                        .is_some_and(|eid| eid == &candidate.emp_immutable_id)
                        || assertion.email == candidate.primary_email
                        || candidate.historical_emails.contains(&assertion.email)
                    {
                        consulted.insert(candidate.person_id.clone());
                    }
                }
                let mut ok = p.is_some()
                    && feed.authorized_affiliations.contains(&assertion.affiliation)
                    && status_map.contains_key(assertion.raw_status_code.as_str());
                if ok {
                    if let Some(ref emp_id) = assertion.emp_immutable_id {
                        ok = !emp_id.trim().is_empty()
                            && feed.correlation_strategy
                                == HRCorrelationStrategyV5::ImmutableEmpIdThenEmail
                            && emp_index.get(emp_id.as_str()).copied()
                                == Some(assertion.person_id.as_str());
                    } else if let Some(person) = p {
                        ok = !ctx.overlay().require_immutable_hr_correlation
                            && assertion.email == person.primary_email
                            && !world.hr_persons.values().any(|other| {
                                other.person_id != assertion.person_id
                                    && (assertion.email == other.primary_email
                                        || other.historical_emails.contains(&assertion.email))
                            });
                    }
                }
                if ok {
                    valid_rows.push((feed, assertion));
                } else {
                    quarantined.insert(key);
                }
            }
        }

        for i in 0..valid_rows.len() {
            for j in (i + 1)..valid_rows.len() {
                let (f1, a1) = valid_rows[i];
                let (f2, a2) = valid_rows[j];
                if a1.person_id != a2.person_id
                    && (a1.email == a2.email
                        || (a1.emp_immutable_id.is_some()
                            && a1.emp_immutable_id == a2.emp_immutable_id))
                {
                    quarantined.insert(format!("{}:{}", f1.feed_id, a1.person_id));
                    quarantined.insert(format!("{}:{}", f2.feed_id, a2.person_id));
                }
            }
        }

        let mut winners: BTreeMap<&str, (&HRSourceFeedSpecV5, &RawHRFeedAssertionV5)> =
            BTreeMap::new();
        for &(feed, assertion) in &valid_rows {
            let key = format!("{}:{}", feed.feed_id, assertion.person_id);
            if quarantined.contains(&key) {
                continue;
            }
            let replace = match winners.get(assertion.person_id.as_str()) {
                None => true,
                Some((prior_feed, _)) => {
                    (feed.precedence_rank, &feed.feed_id)
                        < (prior_feed.precedence_rank, &prior_feed.feed_id)
                }
            };
            if replace {
                winners.insert(assertion.person_id.as_str(), (feed, assertion));
            }
        }

        let resolved: Vec<(String, EngagementStatusV5)> = winners
            .into_iter()
            .map(|(pid, (feed, assertion))| {
                let status = feed
                    .status_code_map
                    .iter()
                    .find(|(k, _)| k == &assertion.raw_status_code)
                    .map(|(_, s)| *s)
                    .expect("validated status code");
                (pid.to_owned(), status)
            })
            .collect();

        let accepted_ids = sorted_unique(accepted.iter().map(|f| f.feed_id.clone()));
        let rejected_ids = sorted_unique(rejected.iter().map(|f| f.feed_id.clone()));
        let quarantined_vec: Vec<String> = quarantined.into_iter().collect();
        let consulted_vec: Vec<String> = consulted.into_iter().collect();

        let mut pre_ids = vec![tid.clone()];
        for f in &task.feeds {
            pre_ids.push(f.connector_id.clone());
        }
        pre_ids.extend(consulted_vec.iter().cloned());

        let mut cites = vec![format!("HR-AUTH-V5-01:{}", task.evidence_id)];
        for feed in &task.feeds {
            cites.push(format!("HR-AUTH-V5-01:{}", feed.evidence_id));
            cites.push(format!(
                "HR-AUTH-V5-01:{}",
                world.connector_schemas[feed.connector_id.as_str()].evidence_id
            ));
        }
        for pid in &consulted_vec {
            cites.push(format!("HR-AUTH-V5-01:{}", world.hr_persons[pid.as_str()].evidence_id));
        }

        let pre = version_tokens(&world, pre_ids.iter().map(String::as_str))?;
        let cite = sorted_unique(cites);
        let mutation = AtomicMutationV5::HrSourceMapping(HRSourceMappingMutationV5 {
            target_entity_id: tid,
            if_match: pre,
            cite,
            mutation_kind: HRSourceMappingMutationKind::HRSourceMappingMutation,
            accepted_feed_ids: accepted_ids.clone(),
            rejected_feed_ids: rejected_ids,
            resolved_person_statuses: resolved,
            quarantined_assertion_keys: quarantined_vec.clone(),
        });
        let trace_ctx = TraceContextV5 {
            accepted_feed_ids: accepted_ids,
            quarantined_assertion_keys: quarantined_vec,
            consulted_person_ids: consulted_vec,
            ..Default::default()
        };
        record_trace_mutation(ctx, mutation.clone(), trace_ctx);
        mutations.push(mutation);
    }
    Ok(mutations)
}

/// P1.2-P1.4a binding, conversion, mover bridges, monitor DAG, scoped SoD.
pub fn phase1_accounts(
    ctx: &mut PhaseContext,
    conn_results: &BTreeMap<String, ConnectorOutcomeV5>,
) -> Result<Vec<AtomicMutationV5>, OracleError> {
    let world = ctx.state.clone();
    let clock = ctx.clock();
    let mut target_aids = ctx.queue.target_human_account_ids.clone();
    target_aids.sort();

    let mut plans: BTreeMap<String, AccountPlanV5> = BTreeMap::new();
    for aid in target_aids {
        let acct = world
            .idp_accounts
            .get(&aid)
            .ok_or_else(|| OracleError::InvalidInput(format!("missing account {aid:?}")))?;
        let bind_people = account_binding_people(&world, acct);
        let mut action = AccountReconActionV5::KeepActive;
        let mut person_id: Option<String> = None;
        let mut engagement = None;
        let mut bound_engagement_id: Option<String> = None;

        if let Some(ref immutable) = acct.immutable_person_ref {
            let conflict = !world.hr_persons.contains_key(immutable.as_str())
                || world.hr_persons.values().any(|p| {
                    p.person_id != *immutable
                        && has_live_engagement(p, clock)
                        && p.primary_email == acct.login_email
                });
            if conflict {
                action = AccountReconActionV5::EscalateBindingConflict;
            } else {
                person_id = Some(immutable.clone());
            }
        } else if bind_people.len() != 1 {
            action = AccountReconActionV5::EscalateBindingConflict;
        } else {
            person_id = Some(bind_people[0].clone());
        }

        if action == AccountReconActionV5::KeepActive
            && let Some(ref pid) = person_id
        {
            let person = &world.hr_persons[pid.as_str()];
            let by_id = person.engagements_by_id();
            if let Some(eng_ref) = acct.bound_engagement_ref.as_deref()
                && let Some(&found) = by_id.get(eng_ref)
            {
                engagement = Some(found.clone());
            } else {
                let live: Vec<_> = person
                    .engagements
                    .iter()
                    .filter(|e| engagement_is_live(e, clock))
                    .collect();
                if live.len() > 1 {
                    action = AccountReconActionV5::EscalateBindingConflict;
                    person_id = None;
                } else if live.len() == 1 {
                    engagement = Some(live[0].clone());
                } else {
                    engagement = person
                        .engagements
                        .iter()
                        .max_by(|a, b| (a.start_s, &a.eng_id).cmp(&(b.start_s, &b.eng_id)))
                        .cloned();
                }
            }
            if action == AccountReconActionV5::KeepActive
                && let Some(ref eng) = engagement
            {
                bound_engagement_id = Some(eng.eng_id.clone());
                if !engagement_is_live(eng, clock) {
                    action = AccountReconActionV5::DisableAccount;
                }
            }
        }

        let mut conversion_pruned = BTreeSet::new();
        let mut after_conversion: BTreeSet<String> = acct.direct_grant_ids.iter().cloned().collect();
        if action == AccountReconActionV5::KeepActive
            && let Some(ref eng) = engagement
            && eng.eng_status == EngagementStatusV5::ContractorToFtePending
            && let (Some(conv_start), Some(conv_deadline)) =
                (eng.conversion_start_s, eng.conversion_grace_deadline_s)
            && conv_start <= clock
            && clock < conv_deadline
        {
            for gid in &acct.direct_grant_ids {
                if let Some(grant) = world.grants.get(gid.as_str())
                    && grant.grant_mode == GrantModeV5::Standing
                    && matches!(
                        world.roles[grant.role_id.as_str()].privilege_tier,
                        PrivilegeTierV5::Admin | PrivilegeTierV5::Critical
                    )
                {
                    conversion_pruned.insert(gid.clone());
                }
            }
            for gid in &conversion_pruned {
                after_conversion.remove(gid);
            }
        }

        let mut mover_candidates = BTreeSet::new();
        let mut mover_pruned = BTreeSet::new();
        let mut x_mover = BTreeSet::new();
        if action == AccountReconActionV5::KeepActive
            && let Some(ref eng) = engagement
        {
            let dept = effective_department(eng, clock);
            let dept_leaves = resolve_scope(ctx.catalog(), dept)?;
            for gid in &after_conversion {
                let Some(grant) = world.grants.get(gid.as_str()) else {
                    continue;
                };
                if grant.scope_path == "/*"
                    || resolve_scope(ctx.catalog(), &grant.scope_path)?.is_subset(&dept_leaves)
                {
                    mover_candidates.insert(gid.clone());
                    continue;
                }
                x_mover.insert(gid.clone());
                let grant_leaves = resolve_scope(ctx.catalog(), &grant.scope_path)?;
                let bridge_ok = world.transfer_bridges.values().any(|b| {
                    b.account_id == aid
                        && b.grant_id == *gid
                        && eng.prior_dept_path.as_deref() == Some(b.from_dept_path.as_str())
                        && b.to_dept_path == dept
                        && b.target_resource == world.roles[grant.role_id.as_str()].app_id
                        && b.approved_at_s <= clock
                        && clock < b.expires_at_s
                        && resolve_scope(ctx.catalog(), &b.from_dept_path)
                            .is_ok_and(|from_leaves| grant_leaves.is_subset(&from_leaves))
                });
                if bridge_ok {
                    mover_candidates.insert(gid.clone());
                } else {
                    mover_pruned.insert(gid.clone());
                }
            }
        }

        let initial_retained = if action == AccountReconActionV5::EscalateBindingConflict {
            acct.direct_grant_ids.iter().cloned().collect()
        } else {
            BTreeSet::new()
        };
        let gold_eng_status = engagement.as_ref().map(|e| e.eng_status);

        plans.insert(
            aid,
            AccountPlanV5 {
                account_action: action,
                bound_person_id: person_id,
                bound_engagement_id,
                engagement,
                binding_people: bind_people,
                conversion_pruned_grant_ids: conversion_pruned,
                after_conversion_grant_ids: after_conversion,
                mover_candidate_grant_ids: mover_candidates,
                mover_pruned_grant_ids: mover_pruned,
                x_mover_grant_ids: x_mover,
                retained_grant_ids: initial_retained,
                sod_pruned_grant_ids: BTreeSet::new(),
                sod_pruned_by_group_ids: BTreeMap::new(),
                sod_pruned_by_prior_grant_ids: BTreeMap::new(),
                sod_witnesses_by_grant: BTreeMap::new(),
                k_before_by_grant: BTreeMap::new(),
                cc_sensitive_grant_ids: BTreeSet::new(),
                gold_engagement_status: gold_eng_status,
            },
        );
    }

    // Topological sort of monitor DAG
    let mut indegree: BTreeMap<String, usize> =
        world.hr_persons.keys().map(|k| (k.clone(), 0)).collect();
    let mut edges: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let mut control_monitor_people: BTreeMap<String, Option<String>> = BTreeMap::new();
    for cc in world.compensating_controls.values() {
        let qpid = plans.get(&cc.monitor_account_id).and_then(|m| {
            if m.account_action == AccountReconActionV5::KeepActive {
                m.bound_person_id.clone()
            } else {
                None
            }
        });
        control_monitor_people.insert(cc.cc_id.clone(), qpid.clone());
        if let Some(ref mon_pid) = qpid
            && indegree.contains_key(&cc.monitored_person_id)
            && edges
                .entry(mon_pid.clone())
                .or_default()
                .insert(cc.monitored_person_id.clone())
            && let Some(deg) = indegree.get_mut(&cc.monitored_person_id)
        {
            *deg += 1;
        }
    }

    let mut ready: BTreeSet<String> = indegree
        .iter()
        .filter_map(|(pid, &deg)| if deg == 0 { Some(pid.clone()) } else { None })
        .collect();
    let mut topo = Vec::with_capacity(indegree.len());
    while let Some(pid) = ready.pop_first() {
        topo.push(pid.clone());
        if let Some(children) = edges.get(&pid) {
            for child in children {
                let deg = indegree.get_mut(child).expect("child in indegree");
                *deg -= 1;
                if *deg == 0 {
                    ready.insert(child.clone());
                }
            }
        }
    }
    if topo.len() != indegree.len() {
        return Err(OracleError::Invariant(
            "P1 compensating-control monitor graph contains a cycle".to_owned(),
        ));
    }

    let mut live_controls = BTreeSet::new();
    let mut k_kept_person: BTreeMap<String, CapabilityFootprint> = BTreeMap::new();
    let account_person: BTreeMap<String, String> = plans
        .iter()
        .filter_map(|(aid, p)| {
            if p.account_action == AccountReconActionV5::KeepActive {
                p.bound_person_id.as_ref().map(|pid| (aid.clone(), pid.clone()))
            } else {
                None
            }
        })
        .collect();

    let mut controls_sorted: Vec<&CompensatingControlV5> =
        world.compensating_controls.values().collect();
    controls_sorted.sort_by(|a, b| a.cc_id.cmp(&b.cc_id));

    for pid in &topo {
        for cc in controls_sorted
            .iter()
            .copied()
            .filter(|c| &c.monitored_person_id == pid)
        {
            let monitor_acct = plans.get(&cc.monitor_account_id);
            let monitor_pid = control_monitor_people.get(&cc.cc_id).and_then(|o| o.as_ref());
            let audit_healthy = conn_results
                .get(&cc.audit_connector_id)
                .is_some_and(|c| c.audit_sink_healthy);
            let Some(mon_pid) = monitor_pid else {
                continue;
            };
            if !(audit_healthy
                && monitor_acct.is_some_and(|a| a.account_action == AccountReconActionV5::KeepActive)
                && mon_pid != pid
                && !is_subordinate(mon_pid, pid, clock, &world.hr_persons))
            {
                continue;
            }
            let acct = &world.idp_accounts[cc.monitor_account_id.as_str()];
            let plan = &plans[cc.monitor_account_id.as_str()];
            let (mut monitor_fp, _) = group_fp(ctx, acct)?;
            for gid in &plan.retained_grant_ids {
                if let Some(g) = world.grants.get(gid.as_str())
                    && g.grant_mode == GrantModeV5::Standing
                {
                    monitor_fp.extend(grant_fp(ctx, acct, gid)?);
                }
            }
            let needed = capability_footprint(
                ctx.catalog(),
                [cc.required_monitor_capability.as_str()],
                [cc.covered_scope_path.as_str()],
            )?;
            if needed.is_subset(&monitor_fp) {
                live_controls.insert(cc.cc_id.clone());
            }
        }

        let active_accounts: Vec<String> = plans
            .keys()
            .filter(|aid| account_person.get(aid.as_str()) == Some(pid))
            .cloned()
            .collect();

        let mut effective_allow_groups_by_id: BTreeMap<String, CapabilityFootprint> = BTreeMap::new();
        let mut group_fp_union = CapabilityFootprint::new();
        for group_aid in &active_accounts {
            let group_acct = &world.idp_accounts[group_aid.as_str()];
            let (acct_grp_fp, group_deny) = group_fp(ctx, group_acct)?;
            group_fp_union.extend(acct_grp_fp);
            for group_id in group_closure(group_acct, &world.groups) {
                let group = &world.groups[group_id.as_str()];
                if group.effect != ib_v5_schemas::GroupEffectV5::Allow {
                    continue;
                }
                let mut group_caps: BTreeSet<&str> =
                    group.capabilities.iter().map(String::as_str).collect();
                for role_id in &group.granted_role_ids {
                    group_caps.extend(
                        world.roles[role_id.as_str()]
                            .capabilities
                            .iter()
                            .map(String::as_str),
                    );
                }
                let mut gfp = capability_footprint(
                    ctx.catalog(),
                    group_caps,
                    [group.scope_path.as_str()],
                )?;
                gfp.retain(|item| !group_deny.contains(item));
                if !gfp.is_empty() {
                    effective_allow_groups_by_id
                        .entry(group_id)
                        .or_default()
                        .extend(gfp);
                }
            }
        }

        let mut candidates: Vec<(String, String)> = Vec::new();
        for aid in &active_accounts {
            for gid in &plans[aid.as_str()].mover_candidate_grant_ids {
                candidates.push((aid.clone(), gid.clone()));
            }
        }

        let live_cc_refs: Vec<&CompensatingControlV5> = live_controls
            .iter()
            .map(|id| &world.compensating_controls[id.as_str()])
            .collect();

        let mut d1: Vec<(String, String)> = Vec::new();
        for (aid, gid) in &candidates {
            let acct = &world.idp_accounts[aid.as_str()];
            let fp = grant_fp(ctx, acct, gid)?;
            let witnesses = sod_witnesses(&fp, &group_fp_union, ctx.sod_rules());
            let unmit = unmitigated_witnesses(
                &fp,
                &group_fp_union,
                pid,
                live_cc_refs.iter().copied(),
                ctx.sod_rules(),
                ctx.catalog(),
            )?;
            plans
                .get_mut(aid.as_str())
                .expect("plan")
                .sod_witnesses_by_grant
                .insert(gid.clone(), witnesses);
            if unmit.is_empty() {
                d1.push((aid.clone(), gid.clone()));
            } else {
                let p = plans.get_mut(aid.as_str()).expect("plan");
                p.sod_pruned_grant_ids.insert(gid.clone());
                let conflicting_groups: Vec<String> = effective_allow_groups_by_id
                    .iter()
                    .filter_map(|(group_id, gfp)| {
                        if !sod_witnesses(&fp, gfp, ctx.sod_rules()).is_empty() {
                            Some(group_id.clone())
                        } else {
                            None
                        }
                    })
                    .collect();
                p.sod_pruned_by_group_ids
                    .insert(gid.clone(), conflicting_groups);
            }
        }

        d1.sort_by(|a, b| {
            (world.grants[a.1.as_str()].sod_prune_priority, &a.1, &a.0)
                .cmp(&(world.grants[b.1.as_str()].sod_prune_priority, &b.1, &b.0))
        });

        let mut k_kept = group_fp_union.clone();
        let mut earlier_kept: Vec<(String, String, CapabilityFootprint)> = Vec::new();
        for (aid, gid) in d1 {
            let acct = &world.idp_accounts[aid.as_str()];
            plans
                .get_mut(aid.as_str())
                .expect("plan")
                .k_before_by_grant
                .insert(gid.clone(), k_kept.clone());
            let fp = grant_fp(ctx, acct, &gid)?;
            let witnesses = sod_witnesses(&fp, &k_kept, ctx.sod_rules());
            let unmit = unmitigated_witnesses(
                &fp,
                &k_kept,
                pid,
                live_cc_refs.iter().copied(),
                ctx.sod_rules(),
                ctx.catalog(),
            )?;
            plans
                .get_mut(aid.as_str())
                .expect("plan")
                .sod_witnesses_by_grant
                .insert(gid.clone(), witnesses);
            if unmit.is_empty() {
                plans
                    .get_mut(aid.as_str())
                    .expect("plan")
                    .retained_grant_ids
                    .insert(gid.clone());
                earlier_kept.push((aid, gid, fp.clone()));
                k_kept.extend(fp);
            } else {
                let conflicting_priors: Vec<String> = earlier_kept
                    .iter()
                    .filter_map(|(_, prior_gid, prior_fp)| {
                        if !sod_witnesses(&fp, prior_fp, ctx.sod_rules()).is_empty() {
                            Some(prior_gid.clone())
                        } else {
                            None
                        }
                    })
                    .collect();
                let p = plans.get_mut(aid.as_str()).expect("plan");
                p.sod_pruned_grant_ids.insert(gid.clone());
                p.sod_pruned_by_prior_grant_ids
                    .insert(gid, conflicting_priors);
            }
        }

        let candidate_controls: Vec<&CompensatingControlV5> = world
            .compensating_controls
            .values()
            .filter(|cc| &cc.monitored_person_id == pid)
            .collect();
        let mut sorted_candidates = candidates.clone();
        sorted_candidates.sort_by(|a, b| {
            (world.grants[a.1.as_str()].sod_prune_priority, &a.1, &a.0)
                .cmp(&(world.grants[b.1.as_str()].sod_prune_priority, &b.1, &b.0))
        });
        let mut all_kept = BTreeSet::new();
        let mut k_all = group_fp_union;
        for (aid, gid) in sorted_candidates {
            let acct = &world.idp_accounts[aid.as_str()];
            let fp = grant_fp(ctx, acct, &gid)?;
            if unmitigated_witnesses(
                &fp,
                &k_all,
                pid,
                candidate_controls.iter().copied(),
                ctx.sod_rules(),
                ctx.catalog(),
            )?
            .is_empty()
            {
                all_kept.insert((aid, gid));
                k_all.extend(fp);
            }
        }
        for (aid, gid) in candidates {
            let retained_now = plans[aid.as_str()].retained_grant_ids.contains(&gid);
            let retained_all = all_kept.contains(&(aid.clone(), gid.clone()));
            if retained_now != retained_all {
                plans
                    .get_mut(aid.as_str())
                    .expect("plan")
                    .cc_sensitive_grant_ids
                    .insert(gid);
            }
        }
        k_kept_person.insert(pid.clone(), k_kept);
    }

    let mut mutations = Vec::new();
    let mut all_pbind = BTreeMap::new();
    let aid_keys: Vec<String> = plans.keys().cloned().collect();
    for aid in aid_keys {
        let mut plan = plans.get(&aid).expect("plan").clone();
        all_pbind.insert(aid.clone(), plan.binding_people.clone());
        if plan.account_action == AccountReconActionV5::DisableAccount {
            plan.retained_grant_ids.clear();
        } else if plan.account_action == AccountReconActionV5::EscalateBindingConflict {
            plan.retained_grant_ids = world.idp_accounts[aid.as_str()]
                .direct_grant_ids
                .iter()
                .cloned()
                .collect();
        }
        plans.insert(aid.clone(), plan.clone());

        let retained: Vec<String> = plan.retained_grant_ids.iter().cloned().collect();
        let acct = &world.idp_accounts[aid.as_str()];
        let mut pre_ids = vec![aid.clone()];
        pre_ids.extend(plan.binding_people.iter().cloned());
        let mut cites = Vec::new();
        let bind_rule = if acct.immutable_person_ref.is_some() {
            "BIND-V5-01a"
        } else {
            "BIND-V5-01b"
        };
        cites.push(format!("{bind_rule}:{}", acct.evidence_id));
        for pid in &plan.binding_people {
            cites.push(format!(
                "{bind_rule}:{}",
                world.hr_persons[pid.as_str()].evidence_id
            ));
        }
        if let (Some(pid), Some(eng)) = (&plan.bound_person_id, &plan.engagement)
            && eng.eng_status == EngagementStatusV5::ContractorToFtePending
            && engagement_is_live(eng, clock)
        {
            cites.push(format!(
                "CONV-V5-02:{}",
                world.hr_persons[pid.as_str()].evidence_id
            ));
            for gid in &plan.conversion_pruned_grant_ids {
                cites.push(format!("CONV-V5-02:{}", world.grants[gid.as_str()].evidence_id));
            }
        }
        if plan.account_action == AccountReconActionV5::KeepActive {
            let person_id_str = plan.bound_person_id.as_deref().unwrap_or("");
            for gid in &plan.x_mover_grant_ids {
                cites.push(format!(
                    "MOVER-V5-03:{}",
                    world.grants[gid.as_str()].evidence_id
                ));
                for bridge in world.transfer_bridges.values() {
                    if bridge.account_id == aid && &bridge.grant_id == gid {
                        cites.push(format!("MOVER-V5-03:{}", bridge.evidence_id));
                    }
                }
            }
            for cc_id in &live_controls {
                let cc = &world.compensating_controls[cc_id.as_str()];
                let mut any_mit = false;
                for gid in &retained {
                    if let Some(witnesses) = plan.sod_witnesses_by_grant.get(gid) {
                        for witness in witnesses {
                            if let Some(rule) =
                                ctx.sod_rules().iter().find(|z| z.rule_code == witness.0)
                                && syntactically_mitigates(
                                    cc,
                                    person_id_str,
                                    witness,
                                    rule,
                                    ctx.catalog(),
                                )?
                            {
                                any_mit = true;
                                break;
                            }
                        }
                    }
                    if any_mit {
                        break;
                    }
                }
                if any_mit {
                    pre_ids.push(cc_id.clone());
                }
            }
            for gid in &retained {
                let empty_fp = CapabilityFootprint::new();
                let before = plan.k_before_by_grant.get(gid).unwrap_or(&empty_fp);
                let gfp = grant_fp(ctx, acct, gid)?;
                let witnesses = sod_witnesses(&gfp, before, ctx.sod_rules());
                for cc_id in &live_controls {
                    let cc = &world.compensating_controls[cc_id.as_str()];
                    for witness in &witnesses {
                        if let Some(rule) =
                            ctx.sod_rules().iter().find(|z| z.rule_code == witness.0)
                            && syntactically_mitigates(
                                cc,
                                person_id_str,
                                witness,
                                rule,
                                ctx.catalog(),
                            )?
                        {
                            cites.push(format!("CC-V5-01:{}", cc.evidence_id));
                            cites.push(format!(
                                "CC-V5-01:{}",
                                world.connector_schemas[cc.audit_connector_id.as_str()].evidence_id
                            ));
                            cites.push(format!(
                                "CC-V5-01:{}",
                                world.idp_accounts[cc.monitor_account_id.as_str()].evidence_id
                            ));
                        }
                    }
                }

                let grant = &world.grants[gid.as_str()];
                let raw_fp = capability_footprint(
                    ctx.catalog(),
                    world.roles[grant.role_id.as_str()]
                        .capabilities
                        .iter()
                        .map(String::as_str),
                    [grant.scope_path.as_str()],
                )?;
                let raw_witnesses = sod_witnesses(&raw_fp, before, ctx.sod_rules());
                if !raw_witnesses.is_empty() {
                    for group_id in group_closure(acct, &world.groups) {
                        let group = &world.groups[group_id.as_str()];
                        if group.effect != ib_v5_schemas::GroupEffectV5::Deny {
                            continue;
                        }
                        let mut group_caps: BTreeSet<&str> =
                            group.capabilities.iter().map(String::as_str).collect();
                        for role_id in &group.granted_role_ids {
                            group_caps.extend(
                                world.roles[role_id.as_str()]
                                    .capabilities
                                    .iter()
                                    .map(String::as_str),
                            );
                        }
                        let denied_fp = capability_footprint(
                            ctx.catalog(),
                            group_caps,
                            [group.scope_path.as_str()],
                        )?;
                        let intersection: CapabilityFootprint =
                            raw_fp.intersection(&denied_fp).cloned().collect();
                        let masked_witnesses = sod_witnesses(&intersection, before, ctx.sod_rules());
                        if !raw_witnesses.is_disjoint(&masked_witnesses) {
                            cites.push(format!("SOD-V5-01:{}", group.evidence_id));
                        }
                    }
                }
            }
            for cc in world.compensating_controls.values() {
                if live_controls.contains(&cc.cc_id) {
                    continue;
                }
                for gid in &plan.sod_pruned_grant_ids {
                    if let Some(witnesses) = plan.sod_witnesses_by_grant.get(gid) {
                        for witness in witnesses {
                            if let Some(rule) =
                                ctx.sod_rules().iter().find(|z| z.rule_code == witness.0)
                                && syntactically_mitigates(
                                    cc,
                                    person_id_str,
                                    witness,
                                    rule,
                                    ctx.catalog(),
                                )?
                            {
                                cites.push(format!("CC-V5-01:{}", cc.evidence_id));
                            }
                        }
                    }
                }
            }
            for gid in &plan.sod_pruned_grant_ids {
                cites.push(format!(
                    "SOD-V5-01:{}",
                    world.grants[gid.as_str()].evidence_id
                ));
                if let Some(group_ids) = plan.sod_pruned_by_group_ids.get(gid) {
                    for group_id in group_ids {
                        cites.push(format!(
                            "SOD-V5-01:{}",
                            world.groups[group_id.as_str()].evidence_id
                        ));
                    }
                } else if let Some(prior_gids) = plan.sod_pruned_by_prior_grant_ids.get(gid) {
                    for prior_gid in prior_gids {
                        cites.push(format!(
                            "SOD-V5-01:{}",
                            world.grants[prior_gid.as_str()].evidence_id
                        ));
                    }
                }
            }
        }

        let trace_ctx = TraceContextV5 {
            pruned_conversion_grant_ids: plan
                .conversion_pruned_grant_ids
                .iter()
                .cloned()
                .collect(),
            cc_sensitive_grant_ids: plan.cc_sensitive_grant_ids.iter().cloned().collect(),
            gold_engagement_status: plan.gold_engagement_status,
            binding_person_ids: plan.binding_people.clone(),
            mover_pruned_grant_ids: plan.mover_pruned_grant_ids.iter().cloned().collect(),
            x_mover_grant_ids: plan.x_mover_grant_ids.iter().cloned().collect(),
            ..Default::default()
        };

        let pre = version_tokens(&world, pre_ids.iter().map(String::as_str))?;
        let cite = sorted_unique(cites);
        let mutation = AtomicMutationV5::AccountRecon(AccountReconMutationV5 {
            target_entity_id: aid,
            if_match: pre,
            cite,
            mutation_kind: AccountReconMutationKind::AccountReconMutation,
            account_action: plan.account_action,
            bound_person_id: plan.bound_person_id,
            bound_engagement_id: plan.bound_engagement_id,
            retained_grant_ids: retained,
        });
        record_trace_mutation(ctx, mutation.clone(), trace_ctx);
        mutations.push(mutation);
    }

    let mut standing_by_person = BTreeMap::new();
    for pid in world.hr_persons.keys() {
        standing_by_person.insert(pid.clone(), person_standing_from_p1(ctx, pid, &plans)?);
    }

    ctx.semantic.p1_account_outcomes = plans;
    ctx.semantic.p1_account_pbind = all_pbind;
    ctx.semantic.p1_live_controls = live_controls.into_iter().collect();
    ctx.semantic.p1_k_kept_person = k_kept_person;
    ctx.semantic.p1_k_standing_person = standing_by_person;
    Ok(mutations)
}

/// P1.4b disconnected ITSM fulfillment after final P1 account decisions.
pub fn phase1_itsm(
    ctx: &mut PhaseContext,
    conn_results: &BTreeMap<String, ConnectorOutcomeV5>,
) -> Result<Vec<AtomicMutationV5>, OracleError> {
    let world = ctx.state.clone();
    let clock = ctx.clock();
    let outcomes = ctx.semantic.p1_account_outcomes.clone();
    let target_accounts: BTreeSet<String> = ctx
        .queue
        .target_human_account_ids
        .iter()
        .cloned()
        .collect();

    let mut mutations = Vec::new();
    for (cid, outcome) in conn_results {
        let conn = &outcome.connector;
        let mut actions: Vec<ITSMTicketActionV5> = Vec::new();
        let mut consulted_fulfillers = BTreeSet::new();
        let mut matched_tickets: BTreeMap<String, ITSMTicketRecordV5> = BTreeMap::new();

        if !outcome.halted_fail_closed
            && conn.provisioning_mode == ConnectorProvisioningModeV5::DisconnectedItsmTicket
        {
            let mut candidates = BTreeSet::new();
            if let Some(ref p) = conn.itsm_primary_fulfiller_account_id {
                candidates.insert(p.clone());
            }
            if let Some(ref b) = conn.itsm_backup_fulfiller_account_id {
                candidates.insert(b.clone());
            }
            for t in &conn.open_itsm_tickets {
                candidates.insert(t.assigned_to_account_id.clone());
            }

            let qual_fulfiller = |aid: Option<&str>, subject: Option<&str>| -> bool {
                let Some(aid) = aid else {
                    return false;
                };
                if !world.idp_accounts.contains_key(aid) {
                    return false;
                }
                let Some(plan) = outcomes.get(aid) else {
                    return false;
                };
                if plan.account_action != AccountReconActionV5::KeepActive {
                    return false;
                }
                let Some(ref pid) = plan.bound_person_id else {
                    return false;
                };
                let Some(ref eid) = plan.bound_engagement_id else {
                    return false;
                };
                let Some(eng) = world.hr_persons[pid.as_str()]
                    .engagements_by_id()
                    .get(eid.as_str())
                    .copied()
                else {
                    return false;
                };
                engagement_is_live(eng, clock) && subject.is_none_or(|s| pid.as_str() != s)
            };

            let mut drift_sorted = outcome.remediated_spoke_drift.clone();
            drift_sorted.sort();
            for token in drift_sorted {
                let aid_subj = token.split(':').next().unwrap_or("");
                let subject = if target_accounts.contains(aid_subj)
                    && bind_person_p1(aid_subj, &outcomes).is_some()
                {
                    bind_person_p1(aid_subj, &outcomes)
                } else {
                    pre_person(&world, Some(aid_subj))
                };
                for candidate in &candidates {
                    if world.idp_accounts.contains_key(candidate.as_str()) {
                        consulted_fulfillers.insert(candidate.clone());
                    }
                }
                let primary = conn.itsm_primary_fulfiller_account_id.as_deref();
                let backup = conn.itsm_backup_fulfiller_account_id.as_deref();
                let best = if qual_fulfiller(primary, subject.as_deref()) {
                    primary.map(str::to_owned)
                } else if qual_fulfiller(backup, subject.as_deref()) {
                    backup.map(str::to_owned)
                } else {
                    None
                };

                let ticket = conn
                    .open_itsm_tickets
                    .iter()
                    .find(|t| t.drift_token == token);
                if let Some(t) = ticket {
                    matched_tickets.insert(token.clone(), t.clone());
                }

                let (action, assignee) = if let Some(t) = ticket
                    && t.ticket_status == ITSMTicketStatusV5::ClosedComplete
                    && t.closed_at_s
                        .is_some_and(|closed| conn.audit_watermark_s <= closed)
                {
                    (ITSMTicketActionKindV5::AwaitNextAggregation, None)
                } else if !conn.itsm_bridge_healthy || best.is_none() {
                    (ITSMTicketActionKindV5::EscalateItsmUnavailable, None)
                } else if let Some(t) = ticket {
                    if t.ticket_status == ITSMTicketStatusV5::ClosedComplete {
                        (ITSMTicketActionKindV5::ReopenPhantomClosure, best)
                    } else if clock >= t.sla_due_s
                        || !qual_fulfiller(
                            Some(t.assigned_to_account_id.as_str()),
                            subject.as_deref(),
                        )
                    {
                        (ITSMTicketActionKindV5::EscalateSlaOrCoi, best)
                    } else {
                        (
                            ITSMTicketActionKindV5::KeepPendingInSla,
                            Some(t.assigned_to_account_id.clone()),
                        )
                    }
                } else {
                    (ITSMTicketActionKindV5::DispatchNewTicket, best)
                };
                actions.push(ITSMTicketActionV5 {
                    drift_token: token,
                    ticket_action: action,
                    assignee_account_id: assignee,
                });
            }
        }

        let mut pre_ids = vec![cid.clone()];
        if !outcome.halted_fail_closed
            && conn.provisioning_mode == ConnectorProvisioningModeV5::DisconnectedItsmTicket
        {
            for aid in &consulted_fulfillers {
                if world.idp_accounts.contains_key(aid.as_str())
                    && let Some(pid) = bind_person_p1(aid, &outcomes)
                {
                    pre_ids.push(aid.clone());
                    pre_ids.push(pid);
                }
            }
        }

        let mut cites = vec![format!("CONN-V5-01:{}", conn.evidence_id)];
        for page in &outcome.inspected_pages {
            cites.push(format!("CONN-V5-01:{}", page.evidence_id));
        }
        if !outcome.halted_fail_closed {
            for token in &outcome.remediated_spoke_drift {
                let aid = token.split(':').next().unwrap_or("");
                if let Some(acct) = world.idp_accounts.get(aid) {
                    cites.push(format!("CONN-V5-01:{}", acct.evidence_id));
                }
            }
        }
        if !outcome.halted_fail_closed && !actions.is_empty() {
            cites.push(format!("ITSM-V5-01:{}", conn.evidence_id));
            for t in matched_tickets.values() {
                cites.push(format!("ITSM-V5-01:{}", t.evidence_id));
            }
            for aid in &consulted_fulfillers {
                let Some(acct) = world.idp_accounts.get(aid.as_str()) else {
                    continue;
                };
                cites.push(format!("ITSM-V5-01:{}", acct.evidence_id));
                if let Some(pid) = bind_person_p1(aid, &outcomes)
                    && let Some(person) = world.hr_persons.get(pid.as_str())
                {
                    cites.push(format!("ITSM-V5-01:{}", person.evidence_id));
                }
            }
        }

        let inspected_page_ids: Vec<String> = outcome
            .inspected_pages
            .iter()
            .map(|p| p.page_id.clone())
            .collect();
        let consulted_vec: Vec<String> = consulted_fulfillers.into_iter().collect();
        let mut matched_ticket_ids: Vec<String> = matched_tickets
            .values()
            .map(|t| t.itkt_id.clone())
            .collect();
        matched_ticket_ids.sort();

        let pre = version_tokens(&world, pre_ids.iter().map(String::as_str))?;
        let cite = sorted_unique(cites);
        let mutation = AtomicMutationV5::ConnectorSync(ConnectorSyncMutationV5 {
            target_entity_id: cid.clone(),
            if_match: pre,
            cite,
            mutation_kind: ConnectorSyncMutationKind::ConnectorSyncMutation,
            halted_fail_closed: outcome.halted_fail_closed,
            halt_reason: outcome.halt_reason,
            reconciled_account_ids: outcome.reconciled_account_ids.clone(),
            remediated_spoke_drift: outcome.remediated_spoke_drift.clone(),
            itsm_ticket_actions: actions,
        });
        let trace_ctx = TraceContextV5 {
            inspected_page_ids,
            consulted_fulfiller_account_ids: consulted_vec,
            matched_ticket_ids,
            ..Default::default()
        };
        record_trace_mutation(ctx, mutation.clone(), trace_ctx);
        mutations.push(mutation);
    }
    Ok(mutations)
}

fn nhi_source_rank(source: TelemetrySignalSourceV5) -> Option<(i64, NHIOwnerResolutionSourceV5, &'static str)> {
    match source {
        TelemetrySignalSourceV5::IacStateAttestation => Some((
            1,
            NHIOwnerResolutionSourceV5::IacStateAttestation,
            "iac_state_attestation",
        )),
        TelemetrySignalSourceV5::VaultRotatorPrincipal => Some((
            2,
            NHIOwnerResolutionSourceV5::VaultRotatorPrincipal,
            "vault_rotator_principal",
        )),
        TelemetrySignalSourceV5::ParentNhiCustody => Some((
            3,
            NHIOwnerResolutionSourceV5::ParentNhiCustody,
            "parent_nhi_custody",
        )),
        TelemetrySignalSourceV5::CicdDeployProvenance => Some((
            4,
            NHIOwnerResolutionSourceV5::CicdDeployProvenance,
            "cicd_deploy_provenance",
        )),
        TelemetrySignalSourceV5::CmdbServiceOwner => Some((
            5,
            NHIOwnerResolutionSourceV5::CmdbServiceOwner,
            "cmdb_service_owner",
        )),
        TelemetrySignalSourceV5::StaleGroupTag => None,
    }
}

fn telemetry_source_str(source: TelemetrySignalSourceV5) -> &'static str {
    match source {
        TelemetrySignalSourceV5::IacStateAttestation => "iac_state_attestation",
        TelemetrySignalSourceV5::VaultRotatorPrincipal => "vault_rotator_principal",
        TelemetrySignalSourceV5::ParentNhiCustody => "parent_nhi_custody",
        TelemetrySignalSourceV5::CicdDeployProvenance => "cicd_deploy_provenance",
        TelemetrySignalSourceV5::CmdbServiceOwner => "cmdb_service_owner",
        TelemetrySignalSourceV5::StaleGroupTag => "stale_group_tag",
    }
}

fn nhi_resolution_source_str(source: NHIOwnerResolutionSourceV5) -> &'static str {
    match source {
        NHIOwnerResolutionSourceV5::IacStateAttestation => "iac_state_attestation",
        NHIOwnerResolutionSourceV5::VaultRotatorPrincipal => "vault_rotator_principal",
        NHIOwnerResolutionSourceV5::ParentNhiCustody => "parent_nhi_custody",
        NHIOwnerResolutionSourceV5::CicdDeployProvenance => "cicd_deploy_provenance",
        NHIOwnerResolutionSourceV5::CmdbServiceOwner => "cmdb_service_owner",
    }
}

/// P1.5 strict, source-specific NHI custody, SoD, and dynamic prod caps.
pub fn phase1_nhi(ctx: &mut PhaseContext) -> Result<Vec<AtomicMutationV5>, OracleError> {
    let world = ctx.state.clone();
    let clock = ctx.clock();
    let account_plans = ctx.semantic.p1_account_outcomes.clone();
    let standing = ctx.semantic.p1_k_standing_person.clone();

    let mut nprod: BTreeMap<String, i64> = BTreeMap::new();
    let mut fnhi: BTreeMap<String, CapabilityFootprint> = BTreeMap::new();
    for baseline in world.baseline_nhis.values() {
        if baseline.status == ActiveDisabledStatusV5::Active {
            let fp = capability_footprint(
                ctx.catalog(),
                baseline.capabilities.iter().map(String::as_str),
                [baseline.scope_path.as_str()],
            )?;
            fnhi.entry(baseline.owner_person_id.clone())
                .or_default()
                .extend(fp);
            if baseline.environment == EnvironmentKindV5::Prod {
                *nprod.entry(baseline.owner_person_id.clone()).or_insert(0) += 1;
            }
        }
    }

    let mut target_nhis = ctx.queue.target_nhi_ids.clone();
    target_nhis.sort();
    let mut mutations = Vec::new();
    let mut outcomes = BTreeMap::new();
    let mut prod_prior_attribution: BTreeMap<String, Vec<String>> = BTreeMap::new();

    for nhi_id in target_nhis {
        let target = world
            .target_nhis
            .get(&nhi_id)
            .ok_or_else(|| OracleError::InvalidInput(format!("missing target nhi {nhi_id:?}")))?
            .clone();
        let f_target = capability_footprint(
            ctx.catalog(),
            target.capabilities.iter().map(String::as_str),
            [target.scope_path.as_str()],
        )?;

        let is_fresh = |sig: &TelemetrySignalV5| -> bool {
            (0..=ctx.overlay().freshness_sla_s).contains(&(clock - sig.synced_at_s))
        };

        let mut signal_rows = Vec::with_capacity(target.signals.len());
        let mut valid_custody: Vec<&TelemetrySignalV5> = Vec::new();
        for sig in &target.signals {
            let (raw_person, custody_live) = match sig.source {
                TelemetrySignalSourceV5::IacStateAttestation
                | TelemetrySignalSourceV5::CicdDeployProvenance
                | TelemetrySignalSourceV5::CmdbServiceOwner => {
                    let rp = sig
                        .candidate_person_id
                        .as_ref()
                        .filter(|pid| world.hr_persons.contains_key(pid.as_str()))
                        .cloned();
                    let live = rp.is_some();
                    (rp, live)
                }
                TelemetrySignalSourceV5::VaultRotatorPrincipal => {
                    let raid = sig.rotator_account_id.as_deref();
                    let rp = match raid {
                        Some(id) if world.idp_accounts.contains_key(id) => {
                            bind_person_p1(id, &account_plans)
                                .or_else(|| pre_person(&world, Some(id)))
                        }
                        _ => pre_person(&world, raid),
                    };
                    let live = raid.is_some_and(|id| {
                        world.idp_accounts.contains_key(id)
                            && p1_action(&account_plans, id)
                                == Some(AccountReconActionV5::KeepActive)
                            && bind_person_p1(id, &account_plans).is_some()
                    });
                    (rp, live)
                }
                TelemetrySignalSourceV5::ParentNhiCustody => {
                    let parent_baseline = sig
                        .parent_nhi_id
                        .as_deref()
                        .and_then(|id| world.baseline_nhis.get(id));
                    let rp = parent_baseline.and_then(|b| {
                        if world.hr_persons.contains_key(b.owner_person_id.as_str()) {
                            Some(b.owner_person_id.clone())
                        } else {
                            None
                        }
                    });
                    let live = rp.is_some()
                        && parent_baseline.is_some_and(|b| {
                            b.status == ActiveDisabledStatusV5::Active
                                && (0..=ctx.overlay().rotation_sla_s)
                                    .contains(&(clock - b.last_rotated_at_s))
                        });
                    (rp, live)
                }
                TelemetrySignalSourceV5::StaleGroupTag => (None, false),
            };
            signal_rows.push(NhiSignalRowTraceV5 {
                signal_id: sig.signal_id.clone(),
                source: telemetry_source_str(sig.source).to_owned(),
                raw_person: raw_person.clone(),
                fresh: is_fresh(sig),
                custody_live,
                person: if custody_live { raw_person } else { None },
                evidence_id: sig.evidence_id.clone(),
            });
            if sig.source == TelemetrySignalSourceV5::VaultRotatorPrincipal
                && is_fresh(sig)
                && sig.rotator_account_id.as_deref().is_some_and(|id| {
                    world.idp_accounts.contains_key(id)
                        && p1_action(&account_plans, id) == Some(AccountReconActionV5::KeepActive)
                })
            {
                valid_custody.push(sig);
            }
        }

        let mut eligible_owner_people = BTreeSet::new();
        for (person_id, person) in &world.hr_persons {
            let eligible_eng = person.engagements.iter().any(|e| {
                engagement_is_live(e, clock)
                    && (e.affiliation == AffiliationKindV5::Fte
                        || (ctx.overlay().allow_contractor_nhi_owners
                            && e.affiliation == AffiliationKindV5::Contractor))
                    && !ctx
                        .overlay()
                        .owner_excluded_dept_prefixes
                        .iter()
                        .any(|prefix| effective_department(e, clock).starts_with(prefix.as_str()))
            });
            if !eligible_eng {
                continue;
            }
            let empty_fp = CapabilityFootprint::new();
            let person_standing = standing.get(person_id.as_str()).unwrap_or(&empty_fp);
            if !sod_witnesses(&f_target, person_standing, ctx.sod_rules()).is_empty() {
                continue;
            }
            if ctx.overlay().enforce_cross_nhi_owner_sod {
                let person_fnhi = fnhi.get(person_id.as_str()).unwrap_or(&empty_fp);
                if !sod_witnesses(&f_target, person_fnhi, ctx.sod_rules()).is_empty() {
                    continue;
                }
            }
            if target.environment == EnvironmentKindV5::Prod
                && nprod.get(person_id.as_str()).copied().unwrap_or(0)
                    >= ctx.overlay().max_prod_nhis_per_owner
            {
                continue;
            }
            eligible_owner_people.insert(person_id.clone());
        }

        let action: NHIGovernanceActionV5;
        let mut owner: Option<String> = None;
        let mut owner_source: Option<NHIOwnerResolutionSourceV5> = None;
        let mut rank_candidates: BTreeMap<i64, Vec<String>> = BTreeMap::new();

        if target.subkind == NHISubkindV5::SharedBreakGlass {
            action = if !valid_custody.is_empty() {
                NHIGovernanceActionV5::FlagSharedException
            } else {
                NHIGovernanceActionV5::DisableOrphan
            };
        } else {
            let mut eligible_by_rank: BTreeMap<
                i64,
                Vec<(&str, &TelemetrySignalV5, NHIOwnerResolutionSourceV5)>,
            > = BTreeMap::new();
            for (sig, row) in target.signals.iter().zip(signal_rows.iter()) {
                let Some(ref person_id) = row.person else {
                    continue;
                };
                let Some((rank, res_src, _)) = nhi_source_rank(sig.source) else {
                    continue;
                };
                if !eligible_owner_people.contains(person_id.as_str()) || !row.fresh {
                    continue;
                }
                eligible_by_rank
                    .entry(rank)
                    .or_default()
                    .push((person_id.as_str(), sig, res_src));
            }
            for (&rank, rows) in &eligible_by_rank {
                let pids: BTreeSet<String> =
                    rows.iter().map(|(pid, _, _)| (*pid).to_owned()).collect();
                rank_candidates.insert(rank, pids.into_iter().collect());
            }
            let winner_rank = (1..=5).find(|r| eligible_by_rank.contains_key(r));
            if let Some(wr) = winner_rank {
                let ranked = &eligible_by_rank[&wr];
                let rank_people: BTreeSet<&str> =
                    ranked.iter().map(|(pid, _, _)| *pid).collect();
                if rank_people.len() == 1 {
                    action = NHIGovernanceActionV5::AttributeOwner;
                    let win_owner = rank_people.into_iter().next().expect("len == 1").to_owned();
                    let win_src = ranked
                        .iter()
                        .filter(|(pid, _, _)| *pid == win_owner.as_str())
                        .map(|(_, _, src)| *src)
                        .min_by_key(|src| nhi_resolution_source_str(*src))
                        .expect("at least one row for winner");
                    owner = Some(win_owner.clone());
                    owner_source = Some(win_src);
                    fnhi.entry(win_owner.clone())
                        .or_default()
                        .extend(f_target.iter().cloned());
                    if target.environment == EnvironmentKindV5::Prod {
                        *nprod.entry(win_owner.clone()).or_insert(0) += 1;
                        prod_prior_attribution
                            .entry(win_owner)
                            .or_default()
                            .push(nhi_id.clone());
                    }
                } else {
                    action = NHIGovernanceActionV5::Escalate;
                }
            } else {
                action = NHIGovernanceActionV5::DisableOrphan;
            }
        }

        let mut pre_ids = vec![nhi_id.clone()];
        let mut cites = vec![format!("NHI-V5-01:{}", target.evidence_id)];
        match action {
            NHIGovernanceActionV5::FlagSharedException => {
                for sig in &valid_custody {
                    if let Some(ref raid) = sig.rotator_account_id {
                        pre_ids.push(raid.clone());
                        cites.push(format!("NHI-V5-01:{}", sig.evidence_id));
                        cites.push(format!(
                            "NHI-V5-01:{}",
                            world.idp_accounts[raid.as_str()].evidence_id
                        ));
                    }
                }
            }
            NHIGovernanceActionV5::AttributeOwner => {
                if let (Some(win_owner), Some(win_src)) = (&owner, owner_source) {
                    let (rank, _, _) = nhi_source_rank(match win_src {
                        NHIOwnerResolutionSourceV5::IacStateAttestation => {
                            TelemetrySignalSourceV5::IacStateAttestation
                        }
                        NHIOwnerResolutionSourceV5::VaultRotatorPrincipal => {
                            TelemetrySignalSourceV5::VaultRotatorPrincipal
                        }
                        NHIOwnerResolutionSourceV5::ParentNhiCustody => {
                            TelemetrySignalSourceV5::ParentNhiCustody
                        }
                        NHIOwnerResolutionSourceV5::CicdDeployProvenance => {
                            TelemetrySignalSourceV5::CicdDeployProvenance
                        }
                        NHIOwnerResolutionSourceV5::CmdbServiceOwner => {
                            TelemetrySignalSourceV5::CmdbServiceOwner
                        }
                    })
                    .expect("valid rank");
                    pre_ids.push(win_owner.clone());
                    for (sig, row) in target.signals.iter().zip(signal_rows.iter()) {
                        let sig_rank = nhi_source_rank(sig.source);
                        if row.person.as_deref() == Some(win_owner.as_str())
                            && sig_rank.is_some_and(|(r, s, _)| s == win_src && r == rank)
                            && row.fresh
                        {
                            if let Some(ref raid) = sig.rotator_account_id
                                && world.idp_accounts.contains_key(raid.as_str())
                            {
                                pre_ids.push(raid.clone());
                            }
                            if let Some(ref parent_id) = sig.parent_nhi_id
                                && world.baseline_nhis.contains_key(parent_id.as_str())
                            {
                                pre_ids.push(parent_id.clone());
                            }
                            cites.push(format!("NHI-V5-01:{}", sig.evidence_id));
                            if let Some(ref raid) = sig.rotator_account_id
                                && let Some(acct) = world.idp_accounts.get(raid.as_str())
                            {
                                cites.push(format!("NHI-V5-01:{}", acct.evidence_id));
                            }
                            if let Some(ref parent_id) = sig.parent_nhi_id
                                && let Some(b) = world.baseline_nhis.get(parent_id.as_str())
                            {
                                cites.push(format!("NHI-V5-01:{}", b.evidence_id));
                            }
                        }
                    }
                    cites.push(format!(
                        "NHI-V5-01:{}",
                        world.hr_persons[win_owner.as_str()].evidence_id
                    ));
                    if target.environment == EnvironmentKindV5::Prod {
                        for b in world.baseline_nhis.values() {
                            if b.owner_person_id == *win_owner
                                && b.environment == EnvironmentKindV5::Prod
                                && b.status == ActiveDisabledStatusV5::Active
                            {
                                cites.push(format!("NHI-V5-01:{}", b.evidence_id));
                            }
                        }
                        if let Some(priors) = prod_prior_attribution.get(win_owner.as_str()) {
                            for prior_id in priors {
                                if prior_id != &nhi_id {
                                    cites.push(format!(
                                        "NHI-V5-01:{}",
                                        world.target_nhis[prior_id.as_str()].evidence_id
                                    ));
                                }
                            }
                        }
                    }
                }
            }
            NHIGovernanceActionV5::Escalate => {
                let source_rank = (1..=5).find(|r| {
                    rank_candidates
                        .get(r)
                        .is_some_and(|cands| cands.len() > 1)
                });
                if let Some(sr) = source_rank {
                    let allowed_pids: BTreeSet<&str> = rank_candidates[&sr]
                        .iter()
                        .map(String::as_str)
                        .collect();
                    for (sig, row) in target.signals.iter().zip(signal_rows.iter()) {
                        let Some(ref pid) = row.person else {
                            continue;
                        };
                        if !allowed_pids.contains(pid.as_str())
                            || nhi_source_rank(sig.source).is_none_or(|(r, _, _)| r != sr)
                            || !row.fresh
                        {
                            continue;
                        }
                        cites.push(format!("NHI-V5-01:{}", sig.evidence_id));
                        if let Some(ref raid) = sig.rotator_account_id
                            && let Some(acct) = world.idp_accounts.get(raid.as_str())
                        {
                            pre_ids.push(raid.clone());
                            cites.push(format!("NHI-V5-01:{}", acct.evidence_id));
                        }
                        if let Some(ref parent_id) = sig.parent_nhi_id
                            && let Some(b) = world.baseline_nhis.get(parent_id.as_str())
                        {
                            pre_ids.push(parent_id.clone());
                            cites.push(format!("NHI-V5-01:{}", b.evidence_id));
                        }
                    }
                }
            }
            NHIGovernanceActionV5::DisableOrphan => {
                for sig in &target.signals {
                    cites.push(format!("NHI-V5-01:{}", sig.evidence_id));
                }
            }
        }

        let valid_custodian_ids = sorted_unique(valid_custody.iter().map(|s| s.signal_id.clone()));
        let has_valid_custody = !valid_custody.is_empty();
        let rows_for_pred = signal_rows.clone();
        let stale_pred = Arc::new(move |submitted: &NHIGovernanceMutationV5| -> bool {
            if submitted.nhi_action == NHIGovernanceActionV5::AttributeOwner {
                let src_str = submitted
                    .owner_resolution_source
                    .map(nhi_resolution_source_str);
                let submitted_rows: Vec<&NhiSignalRowTraceV5> = rows_for_pred
                    .iter()
                    .filter(|row| {
                        Some(row.source.as_str()) == src_str
                            && row.raw_person.as_deref()
                                == submitted.resolved_owner_person_id.as_deref()
                    })
                    .collect();
                return !submitted_rows.is_empty()
                    && submitted_rows
                        .iter()
                        .all(|row| !row.fresh || !row.custody_live);
            }
            let vault_rows: Vec<&NhiSignalRowTraceV5> = rows_for_pred
                .iter()
                .filter(|row| row.source == "vault_rotator_principal")
                .collect();
            submitted.nhi_action == NHIGovernanceActionV5::FlagSharedException
                && !has_valid_custody
                && !vault_rows.is_empty()
                && vault_rows
                    .iter()
                    .all(|row| !row.fresh || !row.custody_live)
        });

        let pre = version_tokens(&world, pre_ids.iter().map(String::as_str))?;
        let cite = sorted_unique(cites);
        let mutation = AtomicMutationV5::NhiGovernance(NHIGovernanceMutationV5 {
            target_entity_id: nhi_id.clone(),
            if_match: pre,
            cite,
            mutation_kind: NHIGovernanceMutationKind::NHIGovernanceMutation,
            nhi_action: action,
            resolved_owner_person_id: owner.clone(),
            owner_resolution_source: owner_source,
        });
        let trace_ctx = TraceContextV5 {
            target_subkind: Some(target.subkind),
            valid_custodian_ids,
            signal_rows,
            eligible_owner_person_ids: eligible_owner_people.into_iter().collect(),
            eligible_sources_by_rank: rank_candidates,
            nhi_stale_support_predicate: Some(stale_pred),
            ..Default::default()
        };
        record_trace_mutation(ctx, mutation.clone(), trace_ctx);
        outcomes.insert(
            nhi_id,
            NhiOutcomeV5 {
                nhi_action: action,
                resolved_owner_person_id: owner,
                owner_resolution_source: owner_source,
            },
        );
        mutations.push(mutation);
    }

    ctx.semantic.p1_nhi_outcomes = outcomes;
    ctx.semantic.p1_f_nhi = fnhi;
    Ok(mutations)
}

fn agent_sponsor_eligible(
    ctx: &PhaseContext,
    person_id: &str,
    account_plans: &BTreeMap<String, AccountPlanV5>,
    contractors: bool,
) -> bool {
    let Some(person) = ctx.state.hr_persons.get(person_id) else {
        return false;
    };
    let has_active_acct = account_plans.values().any(|plan| {
        plan.account_action == AccountReconActionV5::KeepActive
            && plan.bound_person_id.as_deref() == Some(person_id)
    });
    if !has_active_acct {
        return false;
    }
    person.engagements.iter().any(|e| {
        engagement_is_live(e, ctx.clock())
            && (e.affiliation == AffiliationKindV5::Fte
                || (contractors && e.affiliation == AffiliationKindV5::Contractor))
            && !ctx
                .overlay()
                .owner_excluded_dept_prefixes
                .iter()
                .any(|prefix| effective_department(e, ctx.clock()).starts_with(prefix.as_str()))
    })
}

fn agent_sod_support_cites(
    ctx: &PhaseContext,
    sponsor: &str,
    agent: &EnterpriseAgentV5,
    auto_ids: &BTreeSet<String>,
) -> Result<Vec<String>, OracleError> {
    if !ctx.overlay().enforce_sponsor_agent_sod {
        return Ok(Vec::new());
    }
    let world = &ctx.state;
    let mut f_auto = CapabilityFootprint::new();
    for b in &agent.tool_bindings {
        if auto_ids.contains(&b.binding_id) {
            f_auto.extend(capability_footprint(
                ctx.catalog(),
                b.capabilities.iter().map(String::as_str),
                [b.scope_path.as_str()],
            )?);
        }
    }

    let mut result = Vec::new();
    for (aid, plan) in &ctx.semantic.p1_account_outcomes {
        if plan.account_action != AccountReconActionV5::KeepActive
            || plan.bound_person_id.as_deref() != Some(sponsor)
        {
            continue;
        }
        let account = &world.idp_accounts[aid.as_str()];
        let (_, deny_fp) = group_fp(ctx, account)?;
        for group_id in group_closure(account, &world.groups) {
            let group = &world.groups[group_id.as_str()];
            if group.effect != ib_v5_schemas::GroupEffectV5::Allow {
                continue;
            }
            let mut caps: BTreeSet<&str> =
                group.capabilities.iter().map(String::as_str).collect();
            for role_id in &group.granted_role_ids {
                caps.extend(
                    world.roles[role_id.as_str()]
                        .capabilities
                        .iter()
                        .map(String::as_str),
                );
            }
            let mut fp =
                capability_footprint(ctx.catalog(), caps, [group.scope_path.as_str()])?;
            fp.retain(|item| !deny_fp.contains(item));
            if !sod_witnesses(&f_auto, &fp, ctx.sod_rules()).is_empty() {
                result.push(format!("AGENT-REG-V5-01:{}", group.evidence_id));
            }
        }
        for grant_id in &plan.retained_grant_ids {
            let Some(grant) = world.grants.get(grant_id.as_str()) else {
                continue;
            };
            if grant.grant_mode != GrantModeV5::Standing {
                continue;
            }
            let gfp = grant_fp(ctx, account, grant_id)?;
            if !sod_witnesses(&f_auto, &gfp, ctx.sod_rules()).is_empty() {
                result.push(format!("AGENT-REG-V5-01:{}", grant.evidence_id));
            }
        }
    }

    for (nhi_id, outcome) in &ctx.semantic.p1_nhi_outcomes {
        if outcome.resolved_owner_person_id.as_deref() != Some(sponsor)
            || !matches!(
                outcome.nhi_action,
                NHIGovernanceActionV5::AttributeOwner | NHIGovernanceActionV5::FlagSharedException
            )
        {
            continue;
        }
        let target = &world.target_nhis[nhi_id.as_str()];
        let fp = capability_footprint(
            ctx.catalog(),
            target.capabilities.iter().map(String::as_str),
            [target.scope_path.as_str()],
        )?;
        if !sod_witnesses(&f_auto, &fp, ctx.sod_rules()).is_empty() {
            result.push(format!("AGENT-REG-V5-01:{}", target.evidence_id));
        }
    }

    for nhi in world.baseline_nhis.values() {
        if nhi.owner_person_id == sponsor && nhi.status == ActiveDisabledStatusV5::Active {
            let fp = capability_footprint(
                ctx.catalog(),
                nhi.capabilities.iter().map(String::as_str),
                [nhi.scope_path.as_str()],
            )?;
            if !sod_witnesses(&f_auto, &fp, ctx.sod_rules()).is_empty() {
                result.push(format!("AGENT-REG-V5-01:{}", nhi.evidence_id));
            }
        }
    }

    for prior in world.baseline_agents.values() {
        if prior.sponsor_person_id == sponsor && prior.status == BaselineAgentStatusV5::Active {
            let fp = capability_footprint(
                ctx.catalog(),
                prior.autonomous_capabilities.iter().map(String::as_str),
                [prior.scope_path.as_str()],
            )?;
            if !sod_witnesses(&f_auto, &fp, ctx.sod_rules()).is_empty() {
                result.push(format!("AGENT-REG-V5-01:{}", prior.evidence_id));
            }
        }
    }

    for (prior_id, prior) in &ctx.semantic.p1_agent_outcomes_partial {
        if prior_id == &agent.agent_id
            || prior.agent_action != AgentRegistryActionV5::RegisterActive
            || prior.resolved_sponsor_person_id.as_deref() != Some(sponsor)
        {
            continue;
        }
        let prior_entity = &world.enterprise_agents[prior_id.as_str()];
        let mut fp = CapabilityFootprint::new();
        for bid in &prior.active_autonomous_binding_ids {
            if let Some(b) = prior_entity
                .tool_bindings
                .iter()
                .find(|x| &x.binding_id == bid)
            {
                fp.extend(capability_footprint(
                    ctx.catalog(),
                    b.capabilities.iter().map(String::as_str),
                    [b.scope_path.as_str()],
                )?);
            }
        }
        if !sod_witnesses(&f_auto, &fp, ctx.sod_rules()).is_empty() {
            result.push(format!("AGENT-REG-V5-01:{}", prior_entity.evidence_id));
        }
    }

    Ok(result)
}

/// P1.6 enterprise-agent attestation, sponsor caps, and tool-scope SoD.
pub fn phase1_agents(
    ctx: &mut PhaseContext,
    conn_results: &BTreeMap<String, ConnectorOutcomeV5>,
) -> Result<Vec<AtomicMutationV5>, OracleError> {
    let world = ctx.state.clone();
    let clock = ctx.clock();
    let standing = ctx.semantic.p1_k_standing_person.clone();
    let fnhi = ctx.semantic.p1_f_nhi.clone();
    let account_plans = ctx.semantic.p1_account_outcomes.clone();

    let mut kspons: BTreeMap<String, CapabilityFootprint> = BTreeMap::new();
    let mut mprod: BTreeMap<String, i64> = BTreeMap::new();
    for baseline in world.baseline_agents.values() {
        if baseline.status == BaselineAgentStatusV5::Active {
            let fp = capability_footprint(
                ctx.catalog(),
                baseline.autonomous_capabilities.iter().map(String::as_str),
                [baseline.scope_path.as_str()],
            )?;
            kspons
                .entry(baseline.sponsor_person_id.clone())
                .or_default()
                .extend(fp);
            if baseline.environment == EnvironmentKindV5::Prod {
                *mprod.entry(baseline.sponsor_person_id.clone()).or_insert(0) += 1;
            }
        }
    }

    let mut target_agents = ctx.queue.target_agent_ids.clone();
    target_agents.sort();
    let mut outputs = Vec::new();
    let mut outcomes = BTreeMap::new();
    let mut prod_registered: BTreeMap<String, Vec<String>> = BTreeMap::new();

    for agent_id in target_agents {
        let agent = world
            .enterprise_agents
            .get(&agent_id)
            .ok_or_else(|| OracleError::InvalidInput(format!("missing agent {agent_id:?}")))?
            .clone();
        let sponsor = agent.candidate_sponsor_person_id.clone();
        let mut auto_ids = BTreeSet::new();
        let mut hitl_ids = BTreeSet::new();
        let mut resolved_sponsor: Option<String> = None;

        let sponsor_ok = agent_sponsor_eligible(
            ctx,
            &sponsor,
            &account_plans,
            ctx.overlay().allow_contractor_agent_sponsors,
        );
        let action = if agent.manifest_digest_etag != agent.approved_registry_digest_etag
            || !(0..=ctx.overlay().freshness_sla_s)
                .contains(&(clock - agent.attestation_synced_at_s))
        {
            AgentRegistryActionV5::SuspendUnattestedOrStale
        } else if !sponsor_ok
            || (agent.environment == EnvironmentKindV5::Prod
                && mprod.get(&sponsor).copied().unwrap_or(0)
                    >= ctx.overlay().max_prod_agents_per_sponsor)
        {
            AgentRegistryActionV5::SuspendSponsorInvalid
        } else {
            resolved_sponsor = Some(sponsor.clone());
            for binding in &agent.tool_bindings {
                let healthy = conn_results
                    .get(&binding.tool_connector_id)
                    .is_some_and(|c| !c.halted_fail_closed);
                if !healthy {
                    continue;
                }
                if binding.requested_mode == AgentToolModeV5::HitlRequired {
                    hitl_ids.insert(binding.binding_id.clone());
                } else if !ctx
                    .overlay()
                    .critical_jit_tiers
                    .contains(&binding.privilege_tier)
                {
                    auto_ids.insert(binding.binding_id.clone());
                } else if ctx.overlay().permit_hitl_step_up_for_autonomous {
                    hitl_ids.insert(binding.binding_id.clone());
                }
            }

            if auto_ids.is_empty() && hitl_ids.is_empty() {
                AgentRegistryActionV5::SuspendNoActiveTools
            } else {
                let mut f_auto = CapabilityFootprint::new();
                let mut f_all = CapabilityFootprint::new();
                for binding in &agent.tool_bindings {
                    if auto_ids.contains(&binding.binding_id) {
                        let fp = capability_footprint(
                            ctx.catalog(),
                            binding.capabilities.iter().map(String::as_str),
                            [binding.scope_path.as_str()],
                        )?;
                        f_auto.extend(fp.iter().cloned());
                        f_all.extend(fp);
                    } else if hitl_ids.contains(&binding.binding_id) {
                        let fp = capability_footprint(
                            ctx.catalog(),
                            binding.capabilities.iter().map(String::as_str),
                            [binding.scope_path.as_str()],
                        )?;
                        f_all.extend(fp);
                    }
                }
                let self_sod = !sod_witnesses(&f_all, &f_all, ctx.sod_rules()).is_empty();
                let mut combined_sponsor = CapabilityFootprint::new();
                if let Some(s_fp) = standing.get(&sponsor) {
                    combined_sponsor.extend(s_fp.iter().cloned());
                }
                if let Some(n_fp) = fnhi.get(&sponsor) {
                    combined_sponsor.extend(n_fp.iter().cloned());
                }
                if let Some(a_fp) = kspons.get(&sponsor) {
                    combined_sponsor.extend(a_fp.iter().cloned());
                }
                let sponsor_sod = ctx.overlay().enforce_sponsor_agent_sod
                    && !sod_witnesses(&f_auto, &combined_sponsor, ctx.sod_rules()).is_empty();
                if self_sod || sponsor_sod {
                    AgentRegistryActionV5::QuarantineSodViolation
                } else {
                    kspons.entry(sponsor.clone()).or_default().extend(f_auto);
                    if agent.environment == EnvironmentKindV5::Prod {
                        *mprod.entry(sponsor.clone()).or_insert(0) += 1;
                        prod_registered
                            .entry(sponsor.clone())
                            .or_default()
                            .push(agent_id.clone());
                    }
                    AgentRegistryActionV5::RegisterActive
                }
            }
        };

        let active_auto: Vec<String> = if action == AgentRegistryActionV5::RegisterActive {
            auto_ids.iter().cloned().collect()
        } else {
            Vec::new()
        };
        let active_hitl: Vec<String> = if action == AgentRegistryActionV5::RegisterActive {
            hitl_ids.iter().cloned().collect()
        } else {
            Vec::new()
        };

        let mut pre_ids = vec![agent_id.clone()];
        if world.hr_persons.contains_key(&sponsor) {
            pre_ids.push(sponsor.clone());
        }
        if matches!(
            action,
            AgentRegistryActionV5::RegisterActive
                | AgentRegistryActionV5::SuspendNoActiveTools
                | AgentRegistryActionV5::QuarantineSodViolation
        ) {
            for (aid, plan) in &account_plans {
                if plan.account_action == AccountReconActionV5::KeepActive
                    && plan.bound_person_id.as_deref() == Some(sponsor.as_str())
                {
                    pre_ids.push(aid.clone());
                }
            }
            for b in &agent.tool_bindings {
                pre_ids.push(b.tool_connector_id.clone());
            }
        }

        let mut cites = vec![format!("AGENT-REG-V5-01:{}", agent.evidence_id)];
        if action != AgentRegistryActionV5::SuspendUnattestedOrStale
            && let Some(person) = world.hr_persons.get(&sponsor)
        {
            cites.push(format!("AGENT-REG-V5-01:{}", person.evidence_id));
            if agent.environment == EnvironmentKindV5::Prod {
                for baseline in world.baseline_agents.values() {
                    if baseline.sponsor_person_id == sponsor
                        && baseline.environment == EnvironmentKindV5::Prod
                        && baseline.status == BaselineAgentStatusV5::Active
                    {
                        cites.push(format!("AGENT-REG-V5-01:{}", baseline.evidence_id));
                    }
                }
                if let Some(priors) = prod_registered.get(&sponsor) {
                    for prior_id in priors {
                        if prior_id != &agent_id {
                            cites.push(format!(
                                "AGENT-REG-V5-01:{}",
                                world.enterprise_agents[prior_id.as_str()].evidence_id
                            ));
                        }
                    }
                }
            }
        }
        if matches!(
            action,
            AgentRegistryActionV5::RegisterActive
                | AgentRegistryActionV5::SuspendNoActiveTools
                | AgentRegistryActionV5::QuarantineSodViolation
        ) {
            for binding in &agent.tool_bindings {
                cites.push(format!(
                    "AGENT-REG-V5-01:{}",
                    world.connector_schemas[binding.tool_connector_id.as_str()].evidence_id
                ));
                cites.push(format!("AGENT-REG-V5-01:{}", binding.evidence_id));
            }
            if action == AgentRegistryActionV5::QuarantineSodViolation {
                cites.extend(agent_sod_support_cites(ctx, &sponsor, &agent, &auto_ids)?);
            }
        }

        let trace_ctx = TraceContextV5 {
            eligible_sponsor: Some(sponsor_ok),
            sponsor_person_id: Some(sponsor),
            auto_binding_ids: auto_ids.into_iter().collect(),
            hitl_binding_ids: hitl_ids.into_iter().collect(),
            ..Default::default()
        };

        let pre = version_tokens(&world, pre_ids.iter().map(String::as_str))?;
        let cite = sorted_unique(cites);
        let mutation = AtomicMutationV5::AgentRegistry(AgentRegistryMutationV5 {
            target_entity_id: agent_id.clone(),
            if_match: pre,
            cite,
            mutation_kind: AgentRegistryMutationKind::AgentRegistryMutation,
            agent_action: action,
            resolved_sponsor_person_id: resolved_sponsor.clone(),
            active_autonomous_binding_ids: active_auto.clone(),
            active_hitl_binding_ids: active_hitl.clone(),
        });
        record_trace_mutation(ctx, mutation.clone(), trace_ctx);
        outcomes.insert(
            agent_id,
            AgentOutcomeV5 {
                agent_action: action,
                resolved_sponsor_person_id: resolved_sponsor,
                active_autonomous_binding_ids: active_auto,
                active_hitl_binding_ids: active_hitl,
            },
        );
        ctx.semantic.p1_agent_outcomes_partial = outcomes.clone();
        outputs.push(mutation);
    }

    ctx.semantic.p1_agent_outcomes = outcomes;
    ctx.semantic.p1_k_spons_auto = kspons;
    Ok(outputs)
}

/// P1.7 provider-native effective permissions and unioned escalation edges.
pub fn phase1_ciem(
    ctx: &mut PhaseContext,
    account_mutations: &[AtomicMutationV5],
    nhi_mutations: &[AtomicMutationV5],
    agent_mutations: &[AtomicMutationV5],
) -> Result<Vec<AtomicMutationV5>, OracleError> {
    let world = ctx.state.clone();
    let mut account_actions = HashMap::new();
    for m in account_mutations {
        if let AtomicMutationV5::AccountRecon(a) = m {
            account_actions.insert(a.target_entity_id.as_str(), a.account_action);
        }
    }
    let mut nhi_actions = HashMap::new();
    for m in nhi_mutations {
        if let AtomicMutationV5::NhiGovernance(n) = m {
            nhi_actions.insert(n.target_entity_id.as_str(), n.nhi_action);
        }
    }
    let mut agent_actions = HashMap::new();
    for m in agent_mutations {
        if let AtomicMutationV5::AgentRegistry(a) = m {
            agent_actions.insert(a.target_entity_id.as_str(), a.agent_action);
        }
    }

    let principal_live = |pid: &str| -> bool {
        if pid.starts_with("acct_") {
            account_actions.get(pid).copied() == Some(AccountReconActionV5::KeepActive)
        } else if pid.starts_with("nhi_") {
            matches!(
                nhi_actions.get(pid).copied(),
                Some(
                    NHIGovernanceActionV5::AttributeOwner
                        | NHIGovernanceActionV5::FlagSharedException
                )
            )
        } else if pid.starts_with("agt_") {
            agent_actions.get(pid).copied() == Some(AgentRegistryActionV5::RegisterActive)
        } else {
            false
        }
    };

    let mut target_tasks = ctx.queue.target_ciem_task_ids.clone();
    target_tasks.sort();
    let mut mutations = Vec::new();

    for task_id in target_tasks {
        let task = world
            .cloud_ciem_tasks
            .get(&task_id)
            .ok_or_else(|| OracleError::InvalidInput(format!("missing ciem task {task_id:?}")))?
            .clone();
        let forbidden_set: BTreeSet<&str> = task
            .forbidden_standing_cloud_actions
            .iter()
            .map(String::as_str)
            .collect();

        let mut principals_sorted = task.principals.clone();
        principals_sorted.sort_by(|a, b| a.cprin_id.cmp(&b.cprin_id));

        let mut findings = Vec::with_capacity(principals_sorted.len());
        let mut principal_live_map = BTreeMap::new();

        for cp in &principals_sorted {
            let (effective, escalation): (BTreeSet<String>, CIEMEscalationVectorV5) = match cp
                .cloud_provider
            {
                CloudProviderV5::Aws => {
                    let id_allow: BTreeSet<&str> =
                        cp.aws_identity_allow.iter().map(String::as_str).collect();
                    let res_allow: BTreeSet<&str> = cp
                        .aws_resource_policy_allow
                        .iter()
                        .map(String::as_str)
                        .collect();
                    let raw: BTreeSet<&str> = if cp.aws_same_account_resource {
                        id_allow.union(&res_allow).copied().collect()
                    } else {
                        id_allow.intersection(&res_allow).copied().collect()
                    };
                    let boundary: BTreeSet<&str> = match &cp.aws_permission_boundary_allow {
                        Some(b) => b.iter().map(String::as_str).collect(),
                        None => raw.clone(),
                    };
                    let scp: BTreeSet<&str> =
                        cp.aws_scp_allow.iter().map(String::as_str).collect();
                    let deny: BTreeSet<&str> =
                        cp.aws_explicit_deny.iter().map(String::as_str).collect();
                    let bounded: BTreeSet<String> = raw
                        .iter()
                        .filter(|a| boundary.contains(*a) && scp.contains(*a) && !deny.contains(*a))
                        .map(|s| (*s).to_owned())
                        .collect();
                    let passrole = bounded.contains("iam:PassRole")
                        && bounded.contains("compute:RunWorkload")
                        && !cp.aws_passrole_target_actions.is_empty();
                    let assume = bounded.contains("sts:AssumeRole")
                        && cp.aws_trust_policy_valid
                        && !cp.aws_assumerole_chain_actions.is_empty();
                    let mut eff = bounded;
                    if passrole {
                        eff.extend(cp.aws_passrole_target_actions.iter().cloned());
                    }
                    if assume {
                        eff.extend(cp.aws_assumerole_chain_actions.iter().cloned());
                    }
                    let esc = if passrole {
                        CIEMEscalationVectorV5::AwsPassroleExec
                    } else if assume {
                        CIEMEscalationVectorV5::AwsAssumeroleChain
                    } else {
                        CIEMEscalationVectorV5::None
                    };
                    (eff, esc)
                }
                CloudProviderV5::Azure => {
                    let mut rbac_union: BTreeSet<&str> = BTreeSet::new();
                    for (allows, not_actions) in &cp.az_role_assignments {
                        let not_set: BTreeSet<&str> =
                            not_actions.iter().map(String::as_str).collect();
                        for a in allows {
                            if !not_set.contains(a.as_str()) {
                                rbac_union.insert(a.as_str());
                            }
                        }
                    }
                    let deny_set: BTreeSet<&str> = cp
                        .az_deny_assignment_actions
                        .iter()
                        .map(String::as_str)
                        .collect();
                    let bounded: BTreeSet<String> = if cp.az_exempt_from_deny_assignment {
                        rbac_union.into_iter().map(str::to_owned).collect()
                    } else {
                        rbac_union
                            .into_iter()
                            .filter(|a| !deny_set.contains(a))
                            .map(str::to_owned)
                            .collect()
                    };
                    let role_write = bounded
                        .contains("Microsoft.Authorization/roleAssignments/write")
                        && !cp.az_abac_delegation_constrained
                        && !cp
                            .az_unconstrained_role_assign_write_target_actions
                            .is_empty();
                    let app_cred = bounded.contains("Application.ReadWrite.All")
                        && !cp.az_entra_app_cred_target_actions.is_empty();
                    let mut eff = bounded;
                    if role_write {
                        eff.extend(
                            cp.az_unconstrained_role_assign_write_target_actions
                                .iter()
                                .cloned(),
                        );
                    }
                    if app_cred {
                        eff.extend(cp.az_entra_app_cred_target_actions.iter().cloned());
                    }
                    let esc = if role_write {
                        CIEMEscalationVectorV5::AzureRoleAssignWrite
                    } else if app_cred {
                        CIEMEscalationVectorV5::AzureEntraAppCred
                    } else {
                        CIEMEscalationVectorV5::None
                    };
                    (eff, esc)
                }
                CloudProviderV5::Gcp => {
                    let deny: BTreeSet<&str> =
                        cp.gcp_v2_deny_actions.iter().map(String::as_str).collect();
                    let mut bounded = BTreeSet::new();
                    for (tag, actions) in &cp.gcp_allow_bindings {
                        if tag == "*" || tag == &cp.gcp_resource_env_tag {
                            for a in actions {
                                if !deny.contains(a.as_str()) {
                                    bounded.insert(a.clone());
                                }
                            }
                        }
                    }
                    let sa = (bounded.contains("iam.serviceAccounts.actAs")
                        || bounded.contains("iam.serviceAccounts.getAccessToken"))
                        && !cp.gcp_sa_impersonation_target_actions.is_empty();
                    let wif = cp.gcp_wif_unscoped_repo_claim;
                    let mut eff = bounded;
                    if sa {
                        eff.extend(cp.gcp_sa_impersonation_target_actions.iter().cloned());
                    }
                    let esc = if wif {
                        CIEMEscalationVectorV5::GcpWifUnscopedClaim
                    } else if sa {
                        CIEMEscalationVectorV5::GcpSaImpersonation
                    } else {
                        CIEMEscalationVectorV5::None
                    };
                    (eff, esc)
                }
            };

            let forbidden: Vec<String> = effective
                .into_iter()
                .filter(|a| forbidden_set.contains(a.as_str()))
                .collect();
            let live = principal_live(&cp.linked_principal_id);
            principal_live_map.insert(cp.linked_principal_id.clone(), live);
            let remediation = if !live {
                CIEMRemediationActionV5::RevokeOrphanCloudPrincipal
            } else if escalation == CIEMEscalationVectorV5::GcpWifUnscopedClaim {
                CIEMRemediationActionV5::LockWifAttributeCondition
            } else if escalation != CIEMEscalationVectorV5::None {
                CIEMRemediationActionV5::StripEscalationEdge
            } else if !forbidden.is_empty() {
                CIEMRemediationActionV5::AttachBoundaryOrDeny
            } else {
                CIEMRemediationActionV5::KeepCompliant
            };
            findings.push(CIEMPrincipalFindingV5 {
                cprin_id: cp.cprin_id.clone(),
                effective_forbidden_actions: forbidden,
                escalation_vector: escalation,
                remediation_action: remediation,
            });
        }

        let mut pre_ids = vec![task_id.clone()];
        for cp in &task.principals {
            pre_ids.push(cp.linked_principal_id.clone());
        }
        let mut cites = vec![format!("CIEM-V5-01:{}", task.evidence_id)];
        for (cp, finding) in principals_sorted.iter().zip(findings.iter()) {
            if finding.remediation_action == CIEMRemediationActionV5::RevokeOrphanCloudPrincipal {
                let ev = world
                    .idp_accounts
                    .get(cp.linked_principal_id.as_str())
                    .map(|a| a.evidence_id.as_str())
                    .or_else(|| {
                        world
                            .target_nhis
                            .get(cp.linked_principal_id.as_str())
                            .map(|n| n.evidence_id.as_str())
                    })
                    .or_else(|| {
                        world
                            .enterprise_agents
                            .get(cp.linked_principal_id.as_str())
                            .map(|a| a.evidence_id.as_str())
                    });
                if let Some(ev_id) = ev {
                    cites.push(format!("CIEM-V5-01:{ev_id}"));
                }
            }
        }

        let pre = version_tokens(&world, pre_ids.iter().map(String::as_str))?;
        let cite = sorted_unique(cites);
        let mutation = AtomicMutationV5::CloudCiem(CloudCIEMMutationV5 {
            target_entity_id: task_id,
            if_match: pre,
            cite,
            mutation_kind: CloudCIEMMutationKind::CloudCIEMMutation,
            findings,
        });
        let trace_ctx = TraceContextV5 {
            principal_live: principal_live_map,
            ..Default::default()
        };
        record_trace_mutation(ctx, mutation.clone(), trace_ctx);
        mutations.push(mutation);
    }
    Ok(mutations)
}

const JOINER_STEPS: [JoinerStepV5; 5] = [
    JoinerStepV5::VerifyHrAuthAndEffectiveDate,
    JoinerStepV5::CorrelateImmutableIdOrCreateAccount,
    JoinerStepV5::ProvisionBirthrightAccess,
    JoinerStepV5::BindTelemetryAndMfaPolicy,
    JoinerStepV5::EmitJoinerAuditReceipt,
];

const MOVER_STEPS: [MoverStepV5; 5] = [
    MoverStepV5::SnapshotPriorGrantsAndBridges,
    MoverStepV5::RevokeOutOfScopeNonBridgedGrants,
    MoverStepV5::ProvisionNewDeptBirthrights,
    MoverStepV5::EnforcePostMoverSodAndRebindOwnership,
    MoverStepV5::TriggerTargetedMoverCertification,
];

const LEAVER_STEPS: [LeaverStepV5; 5] = [
    LeaverStepV5::DisableIdpAndSsoSessions,
    LeaverStepV5::RevokeRefreshAndOboTrees,
    LeaverStepV5::TransferOwnedNhiAndAgentCustody,
    LeaverStepV5::DeprovisionSpokeEntitlementsOrDispatchItsm,
    LeaverStepV5::ApplyMailboxArchiveOrLegalHoldRetention,
];

struct BirthrightPartitions {
    active: Vec<String>,
    converted: Vec<String>,
    quarantined: Vec<String>,
}

fn birthright_partitions(
    ctx: &PhaseContext,
    task: &ib_v5_schemas::JMLWorkflowTaskV5,
) -> Result<BirthrightPartitions, OracleError> {
    let world = &ctx.state;
    let mut tier_bad = BTreeSet::new();
    for rule in &task.birthright_catalog {
        let role = &world.roles[rule.role_id.as_str()];
        if (rule.allowed_affiliations.contains(&AffiliationKindV5::Vendor)
            && matches!(
                role.privilege_tier,
                PrivilegeTierV5::Elevated | PrivilegeTierV5::Admin | PrivilegeTierV5::Critical
            ))
            || (rule
                .allowed_affiliations
                .contains(&AffiliationKindV5::Contractor)
                && role.privilege_tier == PrivilegeTierV5::Critical)
            || (rule.grant_mode == GrantModeV5::Standing
                && ctx.overlay().critical_jit_tiers.contains(&role.privilege_tier)
                && !role.is_jit_capable_target)
        {
            tier_bad.insert(rule.rule_id.as_str());
        }
    }

    let remaining: Vec<_> = task
        .birthright_catalog
        .iter()
        .filter(|r| !tier_bad.contains(r.rule_id.as_str()))
        .collect();
    let mut sod_bad = BTreeSet::new();
    for i in 0..remaining.len() {
        for j in (i + 1)..remaining.len() {
            let first = remaining[i];
            let second = remaining[j];
            let aff_overlap = first
                .allowed_affiliations
                .iter()
                .any(|a| second.allowed_affiliations.contains(a));
            if !aff_overlap {
                continue;
            }
            let s1 = resolve_scope(ctx.catalog(), &first.dept_prefix)?;
            let s2 = resolve_scope(ctx.catalog(), &second.dept_prefix)?;
            if s1.is_disjoint(&s2) {
                continue;
            }
            let f1 = capability_footprint(
                ctx.catalog(),
                world.roles[first.role_id.as_str()]
                    .capabilities
                    .iter()
                    .map(String::as_str),
                [first.scope_path.as_str()],
            )?;
            let f2 = capability_footprint(
                ctx.catalog(),
                world.roles[second.role_id.as_str()]
                    .capabilities
                    .iter()
                    .map(String::as_str),
                [second.scope_path.as_str()],
            )?;
            if !sod_witnesses(&f1, &f2, ctx.sod_rules()).is_empty() {
                sod_bad.insert(first.rule_id.as_str());
                sod_bad.insert(second.rule_id.as_str());
            }
        }
    }

    let mut quarantined = BTreeSet::new();
    quarantined.extend(tier_bad.iter().copied());
    quarantined.extend(sod_bad.iter().copied());

    let mut converted = BTreeSet::new();
    let mut active = BTreeSet::new();
    for rule in &task.birthright_catalog {
        if quarantined.contains(rule.rule_id.as_str()) {
            continue;
        }
        let role = &world.roles[rule.role_id.as_str()];
        if rule.grant_mode == GrantModeV5::Standing
            && ctx.overlay().critical_jit_tiers.contains(&role.privilege_tier)
        {
            converted.insert(rule.rule_id.clone());
        } else {
            active.insert(rule.rule_id.clone());
        }
    }
    Ok(BirthrightPartitions {
        active: active.into_iter().collect(),
        converted: converted.into_iter().collect(),
        quarantined: quarantined.into_iter().map(str::to_owned).collect(),
    })
}

fn canonical_workflow_spec(task: &ib_v5_schemas::JMLWorkflowTaskV5) -> JMLWorkflowSpecV5 {
    JMLWorkflowSpecV5 {
        joiner_steps: JOINER_STEPS.to_vec(),
        mover_steps: MOVER_STEPS.to_vec(),
        leaver_steps: LEAVER_STEPS.to_vec(),
        joiner_contractor_sponsor_policy: match task.contractor_sponsor_check_mode {
            ContractorSponsorCheckModeV5::StrictPreProvision => {
                JoinerContractorSponsorPolicyV5::RequireActiveFteSponsorPreProvision
            }
            ContractorSponsorCheckModeV5::ExemptInternalConversion => {
                JoinerContractorSponsorPolicyV5::AllowInternalConversionCarryoverSponsor
            }
        },
        no_show_disposition: NoShowDispositionV5::FullRevokeAndDisableNeverStarted,
        legal_hold_disposition: match task.legal_hold_scope_mode {
            LegalHoldScopeModeV5::SuspendPreserveArchive => {
                LegalHoldDispositionV5::SuspendAuthRevokeEntitlementsPreserveHoldData
            }
            LegalHoldScopeModeV5::IsolateForensicSnapshot => {
                LegalHoldDispositionV5::SuspendAuthIsolateForensicSnapshot
            }
        },
        orphan_handoff_policy: match task.orphan_custody_mode {
            OrphanCustodyModeV5::HandoffToManagerElseSuspend => {
                OrphanHandoffPolicyV5::ReassignToActiveManagerOrEscrow
            }
            OrphanCustodyModeV5::ImmediateSuspendAll => {
                OrphanHandoffPolicyV5::ImmediateSuspendAllOwnedEntities
            }
        },
        spoke_error_policy: SpokeErrorPolicyV5::FailClosedRetryQueueAndAlert,
        disconnected_target_policy: if task.has_disconnected_spokes {
            DisconnectedTargetPolicyV5::DispatchItsmAndVerifyPostCloseAggregation
        } else {
            DisconnectedTargetPolicyV5::NotApplicableAutomatedOnly
        },
    }
}

fn is_ordered<T: PartialEq>(actual: &[T], normative: &[T]) -> bool {
    let pos = |item: &T| actual.iter().position(|x| x == item);
    normative
        .windows(2)
        .all(|w| matches!((pos(&w[0]), pos(&w[1])), (Some(a), Some(b)) if a < b))
}

fn workflow_simulates(
    task: &ib_v5_schemas::JMLWorkflowTaskV5,
    spec: &JMLWorkflowSpecV5,
    events: &[JMLTraceEventV5],
) -> bool {
    if !is_ordered(&spec.joiner_steps, &JOINER_STEPS)
        || !is_ordered(&spec.mover_steps, &MOVER_STEPS)
        || !is_ordered(&spec.leaver_steps, &LEAVER_STEPS)
    {
        return false;
    }
    for event in events {
        if event.event_kind == JMLTraceEventKindV5::NoShowCancel
            && spec.no_show_disposition != NoShowDispositionV5::FullRevokeAndDisableNeverStarted
        {
            return false;
        }
        if event.spoke_transient_error
            && spec.spoke_error_policy != SpokeErrorPolicyV5::FailClosedRetryQueueAndAlert
        {
            return false;
        }
        if event.event_kind == JMLTraceEventKindV5::Joiner
            && matches!(
                event.affiliation,
                AffiliationKindV5::Contractor
                    | AffiliationKindV5::Intern
                    | AffiliationKindV5::Vendor
            )
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

/// P1.8 JML workflow specification and birthright partition evaluation.
pub fn phase1_workflows(
    ctx: &mut PhaseContext,
    sealed_events: &[JMLTraceEventV5],
) -> Result<Vec<AtomicMutationV5>, OracleError> {
    let world = ctx.state.clone();
    let mut target_wfls = ctx.queue.target_jml_workflow_ids.clone();
    target_wfls.sort();
    let mut mutations = Vec::new();

    for task_id in target_wfls {
        let task = world
            .jml_workflow_tasks
            .get(&task_id)
            .ok_or_else(|| OracleError::InvalidInput(format!("missing jml task {task_id:?}")))?
            .clone();
        let BirthrightPartitions {
            active,
            converted,
            quarantined,
        } = birthright_partitions(ctx, &task)?;
        let spec = canonical_workflow_spec(&task);
        let mut events = task.visible_trace_events.clone();
        events.extend(sealed_events.iter().cloned());
        if !workflow_simulates(&task, &spec, &events) {
            return Err(OracleError::Invariant(format!(
                "Canonical JML workflow does not satisfy task {task_id}"
            )));
        }

        let mut cites = vec![format!("JML-WF-V5-01:{}", task.evidence_id)];
        for br in &task.birthright_catalog {
            cites.push(format!("JML-WF-V5-01:{}", br.evidence_id));
            cites.push(format!(
                "JML-WF-V5-01:{}",
                world.roles[br.role_id.as_str()].evidence_id
            ));
        }
        for ev in &task.visible_trace_events {
            cites.push(format!("JML-WF-V5-01:{}", ev.evidence_id));
        }

        let pre = version_tokens(&world, [task_id.as_str()])?;
        let cite = sorted_unique(cites);
        let mutation = AtomicMutationV5::JmlWorkflow(JMLWorkflowMutationV5 {
            target_entity_id: task_id,
            if_match: pre,
            cite,
            mutation_kind: JMLWorkflowMutationKind::JMLWorkflowMutation,
            active_birthright_rule_ids: active.clone(),
            converted_eligible_brule_ids: converted.clone(),
            quarantined_birthright_rule_ids: quarantined.clone(),
            authored_spec: spec,
        });
        let trace_ctx = TraceContextV5 {
            active_birthright_rule_ids: active,
            converted_eligible_brule_ids: converted,
            quarantined_birthright_rule_ids: quarantined,
            simulated_event_ids: events.into_iter().map(|e| e.event_id).collect(),
            has_disconnected_spokes: Some(task.has_disconnected_spokes),
            conversion_carryover_sponsor_allowed: Some(
                task.contractor_sponsor_check_mode
                    != ContractorSponsorCheckModeV5::StrictPreProvision,
            ),
            ..Default::default()
        };
        record_trace_mutation(ctx, mutation.clone(), trace_ctx);
        mutations.push(mutation);
    }
    Ok(mutations)
}

const fn tier_weight(tier: PrivilegeTierV5) -> i64 {
    match tier {
        PrivilegeTierV5::Read => 1,
        PrivilegeTierV5::Standard => 2,
        PrivilegeTierV5::Elevated => 4,
        PrivilegeTierV5::Admin => 8,
        PrivilegeTierV5::Critical => 16,
    }
}

pub(crate) fn for_each_combination<T: Clone>(
    items: &[T],
    k: usize,
    f: &mut impl FnMut(&[T]),
) {
    let mut current = Vec::with_capacity(k);
    combine_rec(items, k, 0, &mut current, f);
}

fn combine_rec<T: Clone>(
    items: &[T],
    k: usize,
    start: usize,
    current: &mut Vec<T>,
    f: &mut impl FnMut(&[T]),
) {
    if current.len() == k {
        f(current);
        return;
    }
    let remaining_needed = k - current.len();
    for i in start..=(items.len() - remaining_needed) {
        current.push(items[i].clone());
        combine_rec(items, k, i + 1, current, f);
        current.pop();
    }
}

/// P1.9 exhaustive state-coupled ZSP refactoring optimization.
pub fn phase1_optimizations(ctx: &mut PhaseContext) -> Result<Vec<AtomicMutationV5>, OracleError> {
    let world = ctx.state.clone();
    let plans = ctx.semantic.p1_account_outcomes.clone();
    let live_control_ids = ctx.semantic.p1_live_controls.clone();
    let controls: Vec<CompensatingControlV5> = live_control_ids
        .iter()
        .map(|id| world.compensating_controls[id.as_str()].clone())
        .collect();
    let k_nhi = ctx.semantic.p1_f_nhi.clone();
    let k_spons = ctx.semantic.p1_k_spons_auto.clone();

    let mut target_opts = ctx.queue.target_opt_task_ids.clone();
    target_opts.sort();
    let mut outputs = Vec::new();

    for task_id in target_opts {
        let task = world
            .optimization_tasks
            .get(&task_id)
            .ok_or_else(|| OracleError::InvalidInput(format!("missing opt task {task_id:?}")))?
            .clone();
        if task.phase_kind != OptimizationPhaseKindV5::P1ZspRefactor {
            continue;
        }
        let mut candidates = task.candidates.clone();
        candidates.sort_by(|a, b| a.candidate_id.cmp(&b.candidate_id));
        let all_ids: BTreeSet<String> = candidates.iter().map(|c| c.candidate_id.clone()).collect();

        let mut touched = BTreeSet::new();
        for (pid, _, _) in &task.required_person_caps {
            touched.insert(pid.clone());
        }
        for c in &candidates {
            if let Some(ref pid) = c.target_person_id {
                touched.insert(pid.clone());
            }
        }

        let mut all_people = touched.clone();
        all_people.extend(world.hr_persons.keys().cloned());
        let mut active_accounts_by_person: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for person_id in &all_people {
            let mut aids: Vec<String> = plans
                .iter()
                .filter_map(|(aid, plan)| {
                    if plan.account_action == AccountReconActionV5::KeepActive
                        && plan.bound_person_id.as_deref() == Some(person_id.as_str())
                    {
                        Some(aid.clone())
                    } else {
                        None
                    }
                })
                .collect();
            aids.sort();
            active_accounts_by_person.insert(person_id.clone(), aids);
        }

        let candidate_map: BTreeMap<String, OptCandidateActionV5> = candidates
            .iter()
            .map(|c| (c.candidate_id.clone(), c.clone()))
            .collect();

        let world_c = world.clone();
        let plans_c = plans.clone();
        let live_cc_ids_c = live_control_ids.clone();
        let controls_c = controls.clone();
        let k_nhi_c = k_nhi.clone();
        let k_spons_c = k_spons.clone();
        let task_c = task.clone();
        let touched_c = touched.clone();
        let active_accts_c = active_accounts_by_person.clone();
        let all_ids_c = all_ids.clone();

        let feasibility: OptimizationFeasibilityFn = Arc::new(move |selected_ids: &[String]| {
            let selected_set: BTreeSet<String> = selected_ids.iter().cloned().collect();
            if !selected_set.is_subset(&all_ids_c) {
                let unknown: Vec<String> = selected_set.difference(&all_ids_c).cloned().collect();
                return OptimizationFeasibilityV5 {
                    feasible: false,
                    coverage_ok: false,
                    policy_ok: false,
                    details: OptimizationDetailsV5 {
                        unknown_candidate_ids: unknown,
                        ..Default::default()
                    },
                };
            }
            let selected: Vec<&OptCandidateActionV5> = selected_set
                .iter()
                .map(|cid| &candidate_map[cid.as_str()])
                .collect();

            let mut converts: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
            let mut assignments: BTreeMap<String, Vec<&OptCandidateActionV5>> = BTreeMap::new();
            let mut action_valid = true;
            let mut policy_ok = true;

            for candidate in &selected {
                match candidate.action_kind {
                    OptActionKindV5::ConvertGrantToEligible => {
                        let gid_opt = candidate.target_grant_id.as_deref();
                        let grant_opt = gid_opt.and_then(|gid| world_c.grants.get(gid));
                        let owner_opt = grant_opt.map(|g| g.account_id.as_str());
                        let plan_opt = owner_opt.and_then(|aid| plans_c.get(aid));
                        let valid = match (gid_opt, grant_opt, plan_opt) {
                            (Some(gid), Some(grant), Some(plan)) => {
                                plan.account_action == AccountReconActionV5::KeepActive
                                    && plan.bound_person_id == candidate.target_person_id
                                    && plan.retained_grant_ids.contains(gid)
                                    && grant.grant_mode == GrantModeV5::Standing
                                    && world_c.roles[grant.role_id.as_str()].is_jit_capable_target
                            }
                            _ => false,
                        };
                        if !valid {
                            action_valid = false;
                        } else if let (Some(pid), Some(gid)) =
                            (&candidate.target_person_id, gid_opt)
                        {
                            converts
                                .entry(pid.clone())
                                .or_default()
                                .insert(gid.to_owned());
                        }
                    }
                    OptActionKindV5::AssignRoleStanding => {
                        let pid = candidate.target_person_id.as_deref().unwrap_or("");
                        let accts = active_accts_c.get(pid).map(Vec::as_slice).unwrap_or(&[]);
                        if accts.is_empty()
                            || candidate
                                .role_id
                                .as_deref()
                                .is_none_or(|r| !world_c.roles.contains_key(r))
                            || candidate.scope_path.is_none()
                        {
                            action_valid = false;
                        } else if let Some(ref pid_owned) = candidate.target_person_id {
                            assignments
                                .entry(pid_owned.clone())
                                .or_default()
                                .push(candidate);
                        }
                    }
                    OptActionKindV5::SelectReviewerAccount => {
                        action_valid = false;
                    }
                }
            }

            let mut fpost: BTreeMap<String, CapabilityFootprint> = BTreeMap::new();
            let mut fpost_account: BTreeMap<String, CapabilityFootprint> = BTreeMap::new();
            let mut active_people: BTreeSet<String> = BTreeSet::new();
            for plan in plans_c.values() {
                if plan.account_action == AccountReconActionV5::KeepActive
                    && let Some(ref pid) = plan.bound_person_id
                {
                    active_people.insert(pid.clone());
                }
            }
            let people_to_eval: BTreeSet<String> = active_people
                .union(&touched_c)
                .cloned()
                .collect();

            for person_id in &people_to_eval {
                let account_ids = active_accts_c
                    .get(person_id.as_str())
                    .map(Vec::as_slice)
                    .unwrap_or(&[]);
                let mut person_fp = CapabilityFootprint::new();
                for aid in account_ids {
                    let acct = &world_c.idp_accounts[aid.as_str()];
                    let Ok((grp, deny)) =
                        group_footprints(acct, &world_c.groups, &world_c.roles, &world_c.scope_catalog)
                    else {
                        action_valid = false;
                        continue;
                    };
                    let mut acct_fp = grp.clone();
                    person_fp.extend(grp);
                    for gid in &plans_c[aid.as_str()].retained_grant_ids {
                        if let Some(grant) = world_c.grants.get(gid.as_str())
                            && grant.grant_mode == GrantModeV5::Standing
                            && !converts
                                .get(person_id.as_str())
                                .is_some_and(|s| s.contains(gid.as_str()))
                            && let Ok(gfp) = effective_grant_footprint(
                                grant,
                                &world_c.roles,
                                &deny,
                                &world_c.scope_catalog,
                            )
                        {
                            acct_fp.extend(gfp.iter().cloned());
                            person_fp.extend(gfp);
                        }
                    }
                    if aid == &account_ids[0]
                        && let Some(cands) = assignments.get(person_id.as_str())
                    {
                        for cand in cands {
                            if let (Some(rid), Some(sp)) = (&cand.role_id, &cand.scope_path)
                                && let Some(role) = world_c.roles.get(rid.as_str())
                                && let Ok(mut fp) = capability_footprint(
                                    &world_c.scope_catalog,
                                    role.capabilities.iter().map(String::as_str),
                                    [sp.as_str()],
                                )
                            {
                                fp.retain(|item| !deny.contains(item));
                                acct_fp.extend(fp.iter().cloned());
                                person_fp.extend(fp);
                            }
                        }
                    }
                    fpost_account.insert(aid.clone(), acct_fp);
                }
                fpost.insert(person_id.clone(), person_fp);
            }

            let mut post_controls: Vec<&CompensatingControlV5> = Vec::new();
            for cc_id in &live_cc_ids_c {
                let cc = &world_c.compensating_controls[cc_id.as_str()];
                let monitor_person = plans_c.get(&cc.monitor_account_id).and_then(|p| {
                    if p.account_action == AccountReconActionV5::KeepActive {
                        p.bound_person_id.as_deref()
                    } else {
                        None
                    }
                });
                if monitor_person.is_none() {
                    continue;
                }
                if let Ok(required) = capability_footprint(
                    &world_c.scope_catalog,
                    [cc.required_monitor_capability.as_str()],
                    [cc.covered_scope_path.as_str()],
                ) && let Some(mon_fp) = fpost_account.get(&cc.monitor_account_id)
                    && required.is_subset(mon_fp)
                {
                    post_controls.push(cc);
                }
            }
            for cc in &controls_c {
                if cc.preserve_if_live && !post_controls.iter().any(|c| c.cc_id == cc.cc_id) {
                    policy_ok = false;
                }
            }

            let mut coverage_ok = true;
            for (person_id, cap, scope_path) in &task_c.required_person_caps {
                if let Ok(needed) = capability_footprint(
                    &world_c.scope_catalog,
                    [cap.as_str()],
                    [scope_path.as_str()],
                ) {
                    let empty = CapabilityFootprint::new();
                    let p_fp = fpost.get(person_id.as_str()).unwrap_or(&empty);
                    if !needed.is_subset(p_fp) {
                        coverage_ok = false;
                    }
                } else {
                    coverage_ok = false;
                }
            }

            let empty_fp = CapabilityFootprint::new();
            for person_id in &active_people {
                let footprint = fpost.get(person_id.as_str()).unwrap_or(&empty_fp);
                if unmitigated_witnesses(
                    footprint,
                    footprint,
                    person_id,
                    post_controls.iter().copied(),
                    &world_c.overlay.forbidden_sod_rules,
                    &world_c.scope_catalog,
                )
                .is_ok_and(|w| !w.is_empty())
                {
                    policy_ok = false;
                }
                if world_c.overlay.enforce_cross_nhi_owner_sod
                    && !sod_witnesses(
                        footprint,
                        k_nhi_c.get(person_id.as_str()).unwrap_or(&empty_fp),
                        &world_c.overlay.forbidden_sod_rules,
                    )
                    .is_empty()
                {
                    policy_ok = false;
                }
                if world_c.overlay.enforce_sponsor_agent_sod
                    && !sod_witnesses(
                        footprint,
                        k_spons_c.get(person_id.as_str()).unwrap_or(&empty_fp),
                        &world_c.overlay.forbidden_sod_rules,
                    )
                    .is_empty()
                {
                    policy_ok = false;
                }
            }

            let mut post_live_control_ids: Vec<String> =
                post_controls.iter().map(|c| c.cc_id.clone()).collect();
            post_live_control_ids.sort();
            let conv_map: BTreeMap<String, Vec<String>> = converts
                .into_iter()
                .map(|(k, v)| (k, v.into_iter().collect()))
                .collect();

            OptimizationFeasibilityV5 {
                feasible: action_valid && coverage_ok && policy_ok,
                coverage_ok,
                policy_ok,
                details: OptimizationDetailsV5 {
                    touched_person_ids: touched_c.iter().cloned().collect(),
                    post_live_control_ids,
                    post_footprints: fpost,
                    post_account_footprints: fpost_account,
                    conversion_ids_by_person: conv_map,
                    action_valid,
                    ..Default::default()
                },
            }
        });

        let candidate_ids: Vec<String> = candidates.iter().map(|c| c.candidate_id.clone()).collect();
        let cand_by_id: HashMap<&str, &OptCandidateActionV5> = candidates
            .iter()
            .map(|c| (c.candidate_id.as_str(), c))
            .collect();

        let mut best_objective: Option<i64> = None;
        let mut best_subsets: Vec<Vec<String>> = Vec::new();
        for size in 0..=candidate_ids.len() {
            for_each_combination(&candidate_ids, size, &mut |subset| {
                let verdict = feasibility(subset);
                if !verdict.feasible {
                    return;
                }
                let mut conv_by_person: BTreeMap<&str, BTreeSet<&str>> = BTreeMap::new();
                let mut additions: BTreeMap<&str, Vec<&OptCandidateActionV5>> = BTreeMap::new();
                let mut objective: i64 = 0;
                for cid in subset {
                    let cand = cand_by_id[cid.as_str()];
                    objective += cand.action_cost;
                    if cand.action_kind == OptActionKindV5::ConvertGrantToEligible
                        && let (Some(pid), Some(gid)) =
                            (cand.target_person_id.as_deref(), cand.target_grant_id.as_deref())
                    {
                        conv_by_person.entry(pid).or_default().insert(gid);
                    } else if cand.action_kind == OptActionKindV5::AssignRoleStanding
                        && let Some(pid) = cand.target_person_id.as_deref()
                    {
                        additions.entry(pid).or_default().push(cand);
                    }
                }
                for person_id in &touched {
                    if let Some(aids) = active_accounts_by_person.get(person_id.as_str()) {
                        for aid in aids {
                            for gid in &plans[aid.as_str()].retained_grant_ids {
                                if let Some(grant) = world.grants.get(gid.as_str())
                                    && grant.grant_mode == GrantModeV5::Standing
                                    && !conv_by_person
                                        .get(person_id.as_str())
                                        .is_some_and(|s| s.contains(gid.as_str()))
                                {
                                    objective += tier_weight(
                                        world.roles[grant.role_id.as_str()].privilege_tier,
                                    );
                                }
                            }
                        }
                    }
                    if let Some(cands) = additions.get(person_id.as_str()) {
                        for c in cands {
                            if let Some(rid) = c.role_id.as_deref() {
                                objective += tier_weight(world.roles[rid].privilege_tier);
                            }
                        }
                    }
                }
                let mut sorted_subset = subset.to_vec();
                sorted_subset.sort();
                match best_objective {
                    None => {
                        best_objective = Some(objective);
                        best_subsets = vec![sorted_subset];
                    }
                    Some(b) if objective < b => {
                        best_objective = Some(objective);
                        best_subsets = vec![sorted_subset];
                    }
                    Some(b) if objective == b => {
                        best_subsets.push(sorted_subset);
                    }
                    _ => {}
                }
            });
        }

        let Some(best_obj) = best_objective else {
            return Err(OracleError::OptimizationNoFeasible(task_id));
        };
        if best_subsets.len() != 1 {
            return Err(OracleError::OptimizationTiedOptima(
                task_id,
                best_subsets.len(),
            ));
        }

        let selected_ids = best_subsets.into_iter().next().expect("len == 1");
        let final_feasibility = feasibility(&selected_ids);
        let post_f = &final_feasibility.details.post_footprints;
        let post_controls: BTreeSet<&str> = final_feasibility
            .details
            .post_live_control_ids
            .iter()
            .map(String::as_str)
            .collect();

        let mut relevant_controls: Vec<&CompensatingControlV5> = Vec::new();
        for cc in &controls {
            let monitor_person = plans
                .get(&cc.monitor_account_id)
                .and_then(|p| p.bound_person_id.as_deref());
            if !touched.contains(&cc.monitored_person_id)
                && !monitor_person.is_some_and(|mp| touched.contains(mp))
            {
                continue;
            }
            let mut relevant = cc.preserve_if_live;
            if !relevant
                && post_controls.contains(cc.cc_id.as_str())
                && let Some(fp) = post_f.get(&cc.monitored_person_id)
            {
                for witness in sod_witnesses(fp, fp, ctx.sod_rules()) {
                    if let Some(rule) = ctx.sod_rules().iter().find(|r| r.rule_code == witness.0)
                        && syntactically_mitigates(
                            cc,
                            &cc.monitored_person_id,
                            &witness,
                            rule,
                            ctx.catalog(),
                        )?
                    {
                        relevant = true;
                        break;
                    }
                }
            }
            if relevant {
                relevant_controls.push(cc);
            }
        }

        let mut pre_ids = vec![task_id.clone()];
        for person_id in &touched {
            if let Some(aids) = active_accounts_by_person.get(person_id.as_str()) {
                pre_ids.extend(aids.iter().cloned());
            }
        }
        for cc in &relevant_controls {
            pre_ids.push(cc.cc_id.clone());
        }
        let mut cites = vec![format!("OPT-V5-01:{}", task.evidence_id)];
        for cc in &relevant_controls {
            cites.push(format!("OPT-V5-01:{}", cc.evidence_id));
        }

        let rel_cc_ids = sorted_unique(relevant_controls.iter().map(|c| c.cc_id.clone()));
        let pre = version_tokens(&world, pre_ids.iter().map(String::as_str))?;
        let cite = sorted_unique(cites);
        let mutation = AtomicMutationV5::Optimization(OptimizationMutationV5 {
            target_entity_id: task_id,
            if_match: pre,
            cite,
            mutation_kind: OptimizationMutationKind::OptimizationMutation,
            selected_ids,
            objective_value: best_obj,
        });
        let trace_ctx = TraceContextV5 {
            feasibility: Some(feasibility),
            selected_feasibility: Some(final_feasibility),
            touched_person_ids: touched.into_iter().collect(),
            relevant_control_ids: rel_cc_ids,
            ..Default::default()
        };
        record_trace_mutation(ctx, mutation.clone(), trace_ctx);
        outputs.push(mutation);
    }

    Ok(outputs)
}
