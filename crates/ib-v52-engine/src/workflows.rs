//! Deterministic Day-2 lifecycle, leave, contingent/B2B-guest expiry, stale HR replay,
//! and headless access-request resolution.

use std::collections::{BTreeSet, HashMap, HashSet};

use ib_v52_schemas::{
    AffiliationTypeV52, DateCompareOpV52, Day2EventV52, EnterpriseBrownfieldWorldV52,
    ExpectedDay2OutcomeV52, GroupDispositionV52, IgaDeploymentConfigV52, LifecycleStateIdV52,
};
use indexmap::IndexMap;

use crate::coalescence::{CoalescedIdentity, SpokeCorrelationState};
use crate::lifecycle::{EvaluatedLifecycle, evaluate_single_lifecycle};
use crate::roles::{
    compute_person_effective_entitlements, expand_entitlements_transitively,
    find_sod_conflicts_transitive,
};
use crate::transforms::{eval_date_compare, lifecycle_state_to_str};
use crate::username::GeneratedUsername;

/// Maximum upward hops allowed when walking a live manager or sponsor chain (`3` hops max;
/// a 4th hop falls back to `fallback_governance_mailbox` / `fallback_vms_pmo_queue`).
pub const MAX_LIVE_MANAGER_CHAIN_HOPS: usize = 3;

/// Execute a sealed Day-2 event sequence deterministically over post-cutover identity state.
#[must_use]
#[allow(clippy::too_many_arguments)]
pub fn evaluate_day2_events(
    world: &EnterpriseBrownfieldWorldV52,
    config: &IgaDeploymentConfigV52,
    cubes: &IndexMap<String, CoalescedIdentity<'_>>,
    lifecycles: &IndexMap<String, EvaluatedLifecycle>,
    usernames: &IndexMap<String, GeneratedUsername>,
    birthright_by_person: &IndexMap<String, Vec<String>>,
    correlation: &SpokeCorrelationState,
    cutover_date: &str,
    day2_events: &[Day2EventV52],
) -> IndexMap<String, ExpectedDay2OutcomeV52> {
    let mut effective_states: HashMap<String, LifecycleStateIdV52> = lifecycles
        .iter()
        .map(|(pk, lc)| (pk.clone(), lc.lifecycle_state))
        .collect();

    // Track latest effective date per person (initialized from cutover_date + matched source records).
    let mut latest_effective_date: HashMap<String, String> = HashMap::new();
    for (pk, cube) in cubes {
        let mut latest = cutover_date.trim().to_owned();
        for rec in cube.matched_records.values() {
            if let Some(lu) = rec
                .attributes
                .get("last_updated")
                .map(|s| s.trim())
                .filter(|s| !s.is_empty())
                && eval_date_compare(lu, DateCompareOpV52::Gt, &latest, cutover_date)
            {
                latest = lu.to_owned();
            }
        }
        latest_effective_date.insert(pk.clone(), latest);
    }

    let all_spokes: BTreeSet<String> = world
        .spoke_accounts
        .keys()
        .chain(world.mock_endpoints.keys())
        .cloned()
        .collect();

    // Seed each person's effective entitlements across BOTH primary AND admin_secondary spoke
    // accounts + assigned birthright entitlements, transitively expanded through `world.group_catalog`.
    let mut person_entitlements: HashMap<String, BTreeSet<String>> =
        compute_person_effective_entitlements(world, correlation, birthright_by_person)
            .into_iter()
            .collect();

    let mut outcomes = IndexMap::with_capacity(day2_events.len());

    for event in day2_events {
        match event {
            Day2EventV52::GardenLeaveActivated {
                event_id,
                person_key,
                effective_date,
            } => {
                effective_states.insert(person_key.clone(), LifecycleStateIdV52::GardenLeave);
                update_latest_date(
                    &mut latest_effective_date,
                    person_key,
                    effective_date,
                    cutover_date,
                );
                let gl_wf = &config.workflows.garden_leave;
                let gl_rule = config
                    .lifecycle_states
                    .states
                    .get(&LifecycleStateIdV52::GardenLeave);

                let preserved_hr_payroll_active = gl_wf.preserve_hr_payroll_feed_active
                    && gl_rule.is_some_and(|r| {
                        r.keep_hr_payroll_untouched
                            && !r.sso_enabled
                            && !r.vpn_enabled
                            && r.revoke_active_tokens_immediately
                    });

                let notice_target = gl_wf
                    .notify_security_and_legal_queue
                    .as_deref()
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .map(str::to_owned);

                outcomes.insert(
                    event_id.clone(),
                    ExpectedDay2OutcomeV52 {
                        event_id: event_id.clone(),
                        resolved_custodian_or_notice_target: notice_target,
                        set_ooo_auto_reply: !gl_wf.silent_access_cut_no_termination_ooo,
                        transferred_mailbox_and_drive: false,
                        preserved_hr_payroll_active,
                        grace_suspended_without_delete: false,
                        headless_decision: None,
                        webhook_signed_and_idempotent: false,
                    },
                );
            }
            Day2EventV52::EmergencyTermination {
                event_id,
                person_key,
                termination_date,
            } => {
                effective_states.insert(person_key.clone(), LifecycleStateIdV52::Terminated);
                update_latest_date(
                    &mut latest_effective_date,
                    person_key,
                    termination_date,
                    cutover_date,
                );
                let wf = &config.workflows.emergency_leaver;

                let initial_mgr = cubes
                    .get(person_key)
                    .and_then(|c| c.attributes.get("manager_person_key"))
                    .map(|s| s.trim())
                    .filter(|s| !s.is_empty());

                let resolved = if wf.walk_live_manager_chain {
                    walk_live_chain(
                        person_key,
                        initial_mgr,
                        &wf.eligible_custodian_states,
                        cubes,
                        usernames,
                        &effective_states,
                        MAX_LIVE_MANAGER_CHAIN_HOPS,
                    )
                    .unwrap_or_else(|| wf.fallback_governance_mailbox.clone())
                } else {
                    initial_mgr
                        .and_then(|mk| email_for_person(mk, cubes, usernames))
                        .unwrap_or_else(|| wf.fallback_governance_mailbox.clone())
                };

                let target = if resolved.trim().is_empty() {
                    None
                } else {
                    Some(resolved)
                };

                outcomes.insert(
                    event_id.clone(),
                    ExpectedDay2OutcomeV52 {
                        event_id: event_id.clone(),
                        resolved_custodian_or_notice_target: target,
                        set_ooo_auto_reply: wf.set_exchange_ooo_auto_reply
                            && wf.include_resolved_custodian_email_in_ooo,
                        transferred_mailbox_and_drive: wf
                            .transfer_mailbox_and_onedrive_to_custodian,
                        preserved_hr_payroll_active: false,
                        grace_suspended_without_delete: false,
                        headless_decision: None,
                        webhook_signed_and_idempotent: false,
                    },
                );
            }
            Day2EventV52::ContingentContractExpired {
                event_id,
                person_key,
                expired_date,
            }
            | Day2EventV52::B2bGuestSponsorExpired {
                event_id,
                person_key,
                expired_date,
            } => {
                effective_states.insert(
                    person_key.clone(),
                    LifecycleStateIdV52::ContingentExpiredGrace,
                );
                update_latest_date(
                    &mut latest_effective_date,
                    person_key,
                    expired_date,
                    cutover_date,
                );
                let wf = &config.workflows.contingent_expiry;
                let eligible = if config
                    .workflows
                    .emergency_leaver
                    .eligible_custodian_states
                    .is_empty()
                {
                    vec![LifecycleStateIdV52::Active]
                } else {
                    config
                        .workflows
                        .emergency_leaver
                        .eligible_custodian_states
                        .clone()
                };

                let sponsor_key = cubes
                    .get(person_key)
                    .and_then(|c| c.attributes.get("sponsor_person_key"))
                    .map(|s| s.trim())
                    .filter(|s| !s.is_empty());
                let mgr_key = cubes
                    .get(person_key)
                    .and_then(|c| c.attributes.get("manager_person_key"))
                    .map(|s| s.trim())
                    .filter(|s| !s.is_empty());

                let resolved = if wf.walk_live_sponsor_then_manager_for_extension_notice {
                    walk_live_chain(
                        person_key,
                        sponsor_key,
                        &eligible,
                        cubes,
                        usernames,
                        &effective_states,
                        MAX_LIVE_MANAGER_CHAIN_HOPS,
                    )
                    .or_else(|| {
                        walk_live_chain(
                            person_key,
                            mgr_key,
                            &eligible,
                            cubes,
                            usernames,
                            &effective_states,
                            MAX_LIVE_MANAGER_CHAIN_HOPS,
                        )
                    })
                    .unwrap_or_else(|| wf.fallback_vms_pmo_queue.clone())
                } else {
                    sponsor_key
                        .or(mgr_key)
                        .and_then(|k| email_for_person(k, cubes, usernames))
                        .unwrap_or_else(|| wf.fallback_vms_pmo_queue.clone())
                };

                let target = if resolved.trim().is_empty() {
                    None
                } else {
                    Some(resolved)
                };

                let grace_suspended_without_delete =
                    wf.disable_auth_but_retain_account_during_grace && wf.grace_period_days > 0;

                outcomes.insert(
                    event_id.clone(),
                    ExpectedDay2OutcomeV52 {
                        event_id: event_id.clone(),
                        resolved_custodian_or_notice_target: target,
                        set_ooo_auto_reply: false,
                        transferred_mailbox_and_drive: false,
                        preserved_hr_payroll_active: false,
                        grace_suspended_without_delete,
                        headless_decision: None,
                        webhook_signed_and_idempotent: false,
                    },
                );
            }
            Day2EventV52::StaleHrEventReplay {
                event_id,
                person_key,
                stale_effective_date,
                replayed_hr_status,
            } => {
                let curr_state = effective_states
                    .get(person_key)
                    .copied()
                    .unwrap_or(LifecycleStateIdV52::InactiveOrphan);
                let latest_for_person = latest_effective_date
                    .get(person_key)
                    .map(String::as_str)
                    .unwrap_or(cutover_date);
                let is_stale = eval_date_compare(
                    stale_effective_date,
                    DateCompareOpV52::Lt,
                    latest_for_person,
                    cutover_date,
                );

                let resulting_state = evaluate_stale_replay_resulting_state(
                    person_key,
                    curr_state,
                    is_stale,
                    replayed_hr_status,
                    cubes,
                    config,
                    cutover_date,
                    &all_spokes,
                );

                if resulting_state != curr_state {
                    effective_states.insert(person_key.clone(), resulting_state);
                }

                outcomes.insert(
                    event_id.clone(),
                    ExpectedDay2OutcomeV52 {
                        event_id: event_id.clone(),
                        resolved_custodian_or_notice_target: Some(format!(
                            "state:{}",
                            lifecycle_state_to_str(resulting_state)
                        )),
                        set_ooo_auto_reply: false,
                        transferred_mailbox_and_drive: false,
                        preserved_hr_payroll_active: resulting_state
                            == LifecycleStateIdV52::GardenLeave
                            && config
                                .workflows
                                .garden_leave
                                .preserve_hr_payroll_feed_active,
                        grace_suspended_without_delete: false,
                        headless_decision: None,
                        webhook_signed_and_idempotent: false,
                    },
                );
            }
            Day2EventV52::HeadlessAccessRequest {
                event_id,
                requester_person_key,
                requested_role_or_entitlement_id,
                manager_approved,
                app_owner_approved,
                target_spoke_ticket_closed_without_aggregation,
                approver_person_key,
                approver_is_pto_delegate_of_requester,
            } => {
                let wf = &config.workflows.headless_access_request;
                let requester_cube = cubes.get(requester_person_key);
                let requester_state = effective_states
                    .get(requester_person_key)
                    .copied()
                    .unwrap_or(LifecycleStateIdV52::InactiveOrphan);

                let requested_ents: Vec<String> = config
                    .role_model
                    .roles
                    .iter()
                    .find(|r| r.role_id == *requested_role_or_entitlement_id)
                    .map(|r| r.entitlement_ids.clone())
                    .unwrap_or_else(|| vec![requested_role_or_entitlement_id.clone()]);

                let expanded_requested =
                    expand_entitlements_transitively(requested_ents.iter(), world);

                let is_privileged = expanded_requested.iter().any(|ent| {
                    world
                        .group_catalog
                        .get(ent)
                        .is_some_and(|g| g.is_privileged_admin)
                        || config
                            .role_model
                            .group_classifications
                            .get(ent)
                            .is_some_and(|c| {
                                c.disposition == GroupDispositionV52::PrivilegedAdminRequestOnly
                            })
                });

                let is_contractor_or_vendor = requester_cube.is_some_and(|c| {
                    matches!(
                        c.primary_affiliation,
                        AffiliationTypeV52::ContingentWorker
                            | AffiliationTypeV52::Vendor
                            | AffiliationTypeV52::B2bGuest
                    ) || c
                        .active_affiliations
                        .contains(&AffiliationTypeV52::ContingentWorker)
                        || c.active_affiliations.contains(&AffiliationTypeV52::Vendor)
                        || c.active_affiliations
                            .contains(&AffiliationTypeV52::B2bGuest)
                });

                let mut candidate_entitlements = person_entitlements
                    .get(requester_person_key)
                    .cloned()
                    .unwrap_or_default();
                for ent in &expanded_requested {
                    candidate_entitlements.insert(ent.clone());
                }

                let has_sod_conflict = !find_sod_conflicts_transitive(
                    &candidate_entitlements,
                    world,
                    &config.role_model.sod_policies,
                )
                .is_empty();

                let is_self_approval = approver_person_key
                    .as_deref()
                    .map(str::trim)
                    .is_some_and(|a| !a.is_empty() && a == requester_person_key);
                let is_delegate_self_or_cycle =
                    *approver_is_pto_delegate_of_requester || is_self_approval;

                let state_ok = wf.eligible_requester_states.contains(&requester_state);
                let contractor_ok = !(wf.deny_contractor_privileged_admin_roles
                    && is_contractor_or_vendor
                    && is_privileged);
                let manager_ok = !wf.require_live_manager_approval || *manager_approved;
                let app_owner_ok =
                    !wf.require_app_owner_approval_for_requestable || *app_owner_approved;
                let sod_ok = !wf.block_sod_violating_requests || !has_sod_conflict;
                let delegate_ok =
                    !wf.block_delegate_self_approval_and_cycles || !is_delegate_self_or_cycle;

                let approved = state_ok
                    && contractor_ok
                    && manager_ok
                    && app_owner_ok
                    && sod_ok
                    && delegate_ok;

                let verify_post_close = wf
                    .post_provisioning_webhook
                    .as_ref()
                    .is_some_and(|w| w.verify_post_ticket_close_aggregation);
                let webhook_signed_and_idempotent = approved
                    && wf.post_provisioning_webhook.as_ref().is_some_and(|w| {
                        !w.endpoint_id.trim().is_empty()
                            && !w.sign_hmac_sha256_header.trim().is_empty()
                            && w.include_idempotency_key
                    });

                let decision = if approved {
                    if *target_spoke_ticket_closed_without_aggregation && verify_post_close {
                        // Do NOT mutate live effective entitlements while target readback verification is pending!
                        "approved_pending_itsm_verify".to_owned()
                    } else {
                        let entry = person_entitlements
                            .entry(requester_person_key.clone())
                            .or_default();
                        for ent in &expanded_requested {
                            entry.insert(ent.clone());
                        }
                        "approved_provisioned".to_owned()
                    }
                } else {
                    "denied".to_owned()
                };

                outcomes.insert(
                    event_id.clone(),
                    ExpectedDay2OutcomeV52 {
                        event_id: event_id.clone(),
                        resolved_custodian_or_notice_target: None,
                        set_ooo_auto_reply: false,
                        transferred_mailbox_and_drive: false,
                        preserved_hr_payroll_active: false,
                        grace_suspended_without_delete: false,
                        headless_decision: Some(decision),
                        webhook_signed_and_idempotent,
                    },
                );
            }
        }
    }

    outcomes
}

#[allow(clippy::too_many_arguments)]
fn evaluate_stale_replay_resulting_state(
    person_key: &str,
    curr_state: LifecycleStateIdV52,
    is_stale: bool,
    replayed_hr_status: &str,
    cubes: &IndexMap<String, CoalescedIdentity<'_>>,
    config: &IgaDeploymentConfigV52,
    cutover_date: &str,
    all_spokes: &BTreeSet<String>,
) -> LifecycleStateIdV52 {
    let order = &config.lifecycle_states.evaluation_order;
    let active_pos = order
        .iter()
        .position(|s| *s == LifecycleStateIdV52::Active)
        .unwrap_or(usize::MAX);
    let curr_pos = order
        .iter()
        .position(|s| *s == curr_state)
        .unwrap_or(usize::MAX);
    let curr_rule_locks_auth = config
        .lifecycle_states
        .states
        .get(&curr_state)
        .is_some_and(|r| !r.sso_enabled && r.revoke_active_tokens_immediately);

    if is_stale {
        match curr_state {
            LifecycleStateIdV52::LegalHold
            | LifecycleStateIdV52::GardenLeave
            | LifecycleStateIdV52::SuspendedCredential => {
                // Re-evaluate against `config.lifecycle_states` with `hr_payroll_status` set to
                // `replayed_hr_status`: a properly ordered state machine keeps the protective state!
                if let Some(cube) = cubes.get(person_key) {
                    let mut replayed_cube = cube.clone();
                    replayed_cube.attributes.insert(
                        "hr_payroll_status".to_owned(),
                        replayed_hr_status.to_owned(),
                    );
                    let reeval =
                        evaluate_single_lifecycle(&replayed_cube, config, cutover_date, all_spokes);
                    if reeval.lifecycle_state == curr_state && curr_rule_locks_auth {
                        return curr_state;
                    }
                    // If `curr_state` was triggered dynamically by a prior Day-2 event (e.g. GardenLeaveActivated)
                    // rather than Day-1 attributes, honor protective precedence if ordered before Active.
                    if curr_pos < active_pos && curr_rule_locks_auth {
                        return curr_state;
                    }
                    return reeval.lifecycle_state;
                }
                if curr_pos < active_pos && curr_rule_locks_auth {
                    return curr_state;
                }
                return LifecycleStateIdV52::Active;
            }
            LifecycleStateIdV52::Terminated => {
                if curr_pos < active_pos && curr_rule_locks_auth {
                    return LifecycleStateIdV52::Terminated;
                }
                return LifecycleStateIdV52::Active;
            }
            _ => return curr_state,
        }
    }

    // Non-stale HR update: re-evaluate normally.
    if let Some(cube) = cubes.get(person_key) {
        let mut replayed_cube = cube.clone();
        replayed_cube.attributes.insert(
            "hr_payroll_status".to_owned(),
            replayed_hr_status.to_owned(),
        );
        return evaluate_single_lifecycle(&replayed_cube, config, cutover_date, all_spokes)
            .lifecycle_state;
    }
    curr_state
}

fn update_latest_date(
    latest_map: &mut HashMap<String, String>,
    person_key: &str,
    candidate_date: &str,
    cutover_date: &str,
) {
    let cand = candidate_date.trim();
    if cand.is_empty() {
        return;
    }
    match latest_map.get(person_key) {
        Some(prev) if !eval_date_compare(cand, DateCompareOpV52::Gt, prev, cutover_date) => {}
        _ => {
            latest_map.insert(person_key.to_owned(), cand.to_owned());
        }
    }
}

fn walk_live_chain(
    subject_key: &str,
    start_key: Option<&str>,
    eligible_states: &[LifecycleStateIdV52],
    cubes: &IndexMap<String, CoalescedIdentity<'_>>,
    usernames: &IndexMap<String, GeneratedUsername>,
    effective_states: &HashMap<String, LifecycleStateIdV52>,
    max_hops: usize,
) -> Option<String> {
    let mut visited: HashSet<String> = HashSet::new();
    visited.insert(subject_key.to_owned());

    let mut cursor = start_key;
    let mut hops = 0usize;
    while let Some(key) = cursor {
        hops += 1;
        if hops > max_hops || !visited.insert(key.to_owned()) {
            break;
        }
        let cube = cubes.get(key)?;
        let state = effective_states
            .get(key)
            .copied()
            .unwrap_or(LifecycleStateIdV52::InactiveOrphan);
        if eligible_states.contains(&state) {
            return email_for_person(key, cubes, usernames);
        }
        cursor = cube
            .attributes
            .get("manager_person_key")
            .map(|s| s.trim())
            .filter(|s| !s.is_empty());
    }
    None
}

fn email_for_person(
    person_key: &str,
    cubes: &IndexMap<String, CoalescedIdentity<'_>>,
    usernames: &IndexMap<String, GeneratedUsername>,
) -> Option<String> {
    let cube = cubes.get(person_key)?;
    if let Some(work_email) = cube
        .attributes
        .get("work_email")
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
    {
        return Some(work_email.to_owned());
    }
    usernames
        .get(person_key)
        .map(|u| u.upn.clone())
        .filter(|s| !s.trim().is_empty())
}
