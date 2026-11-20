//! Deterministic Day-2 lifecycle, leave, contingent-expiry, and headless access-request resolution.

use std::collections::{BTreeSet, HashMap, HashSet};

use ib_v51_schemas::{
    AffiliationTypeV51, Day2EventV51, EnterpriseBrownfieldWorldV51, ExpectedDay2OutcomeV51,
    GroupDispositionV51, IgaDeploymentConfigV51, LifecycleStateIdV51,
};
use indexmap::IndexMap;

use crate::coalescence::{CoalescedIdentity, SpokeCorrelationState};
use crate::lifecycle::EvaluatedLifecycle;
use crate::roles::find_sod_conflicts;
use crate::username::GeneratedUsername;

/// Execute a sealed Day-2 event sequence deterministically over post-cutover identity state.
#[must_use]
#[allow(clippy::too_many_arguments)]
pub fn evaluate_day2_events(
    world: &EnterpriseBrownfieldWorldV51,
    config: &IgaDeploymentConfigV51,
    cubes: &IndexMap<String, CoalescedIdentity<'_>>,
    lifecycles: &IndexMap<String, EvaluatedLifecycle>,
    usernames: &IndexMap<String, GeneratedUsername>,
    birthright_by_person: &IndexMap<String, Vec<String>>,
    correlation: &SpokeCorrelationState,
    day2_events: &[Day2EventV51],
) -> IndexMap<String, ExpectedDay2OutcomeV51> {
    let mut effective_states: HashMap<String, LifecycleStateIdV51> = lifecycles
        .iter()
        .map(|(pk, lc)| (pk.clone(), lc.lifecycle_state))
        .collect();

    // Seed each person's known entitlements from correlated spoke accounts + birthright entitlements.
    let mut person_entitlements: HashMap<String, BTreeSet<String>> = HashMap::new();
    for (pk, br) in birthright_by_person {
        let entry = person_entitlements.entry(pk.clone()).or_default();
        for ent in br {
            entry.insert(ent.clone());
        }
    }
    for (spoke_id, accounts) in &world.spoke_accounts {
        for acct in accounts {
            if let Some(owner) = correlation
                .account_to_person
                .get(&(spoke_id.clone(), acct.account_id.clone()))
            {
                let entry = person_entitlements.entry(owner.clone()).or_default();
                for ent in &acct.entitlement_ids {
                    entry.insert(ent.clone());
                }
            }
        }
    }

    let mut outcomes = IndexMap::with_capacity(day2_events.len());

    for event in day2_events {
        match event {
            Day2EventV51::GardenLeaveActivated {
                event_id,
                person_key,
                ..
            } => {
                effective_states.insert(person_key.clone(), LifecycleStateIdV51::GardenLeave);
                let gl_wf = &config.workflows.garden_leave;
                let gl_rule = config
                    .lifecycle_states
                    .states
                    .get(&LifecycleStateIdV51::GardenLeave);

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
                    ExpectedDay2OutcomeV51 {
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
            Day2EventV51::EmergencyTermination {
                event_id,
                person_key,
                ..
            } => {
                effective_states.insert(person_key.clone(), LifecycleStateIdV51::Terminated);
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
                    ExpectedDay2OutcomeV51 {
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
            Day2EventV51::ContingentContractExpired {
                event_id,
                person_key,
                ..
            } => {
                effective_states.insert(
                    person_key.clone(),
                    LifecycleStateIdV51::ContingentExpiredGrace,
                );
                let wf = &config.workflows.contingent_expiry;
                let eligible = if config
                    .workflows
                    .emergency_leaver
                    .eligible_custodian_states
                    .is_empty()
                {
                    vec![LifecycleStateIdV51::Active]
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
                    )
                    .or_else(|| {
                        walk_live_chain(
                            person_key,
                            mgr_key,
                            &eligible,
                            cubes,
                            usernames,
                            &effective_states,
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
                    ExpectedDay2OutcomeV51 {
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
            Day2EventV51::HeadlessAccessRequest {
                event_id,
                requester_person_key,
                requested_role_or_entitlement_id,
                manager_approved,
                app_owner_approved,
                target_spoke_ticket_closed_without_aggregation,
            } => {
                let wf = &config.workflows.headless_access_request;
                let requester_cube = cubes.get(requester_person_key);
                let requester_state = effective_states
                    .get(requester_person_key)
                    .copied()
                    .unwrap_or(LifecycleStateIdV51::InactiveOrphan);

                let requested_ents: Vec<String> = config
                    .role_model
                    .roles
                    .iter()
                    .find(|r| r.role_id == *requested_role_or_entitlement_id)
                    .map(|r| r.entitlement_ids.clone())
                    .unwrap_or_else(|| vec![requested_role_or_entitlement_id.clone()]);

                let is_privileged = requested_ents.iter().any(|ent| {
                    world
                        .group_catalog
                        .get(ent)
                        .is_some_and(|g| g.is_privileged_admin)
                        || config
                            .role_model
                            .group_classifications
                            .get(ent)
                            .is_some_and(|c| {
                                c.disposition == GroupDispositionV51::PrivilegedAdminRequestOnly
                            })
                });

                let is_contractor_or_vendor = requester_cube.is_some_and(|c| {
                    matches!(
                        c.primary_affiliation,
                        AffiliationTypeV51::ContingentWorker | AffiliationTypeV51::Vendor
                    ) || c
                        .active_affiliations
                        .contains(&AffiliationTypeV51::ContingentWorker)
                        || c.active_affiliations.contains(&AffiliationTypeV51::Vendor)
                });

                let mut candidate_entitlements = person_entitlements
                    .get(requester_person_key)
                    .cloned()
                    .unwrap_or_default();
                for ent in &requested_ents {
                    candidate_entitlements.insert(ent.clone());
                }

                let has_sod_conflict =
                    !find_sod_conflicts(&candidate_entitlements, &config.role_model.sod_policies)
                        .is_empty();

                let state_ok = wf.eligible_requester_states.contains(&requester_state);
                let contractor_ok = !(wf.deny_contractor_privileged_admin_roles
                    && is_contractor_or_vendor
                    && is_privileged);
                let manager_ok = !wf.require_live_manager_approval || *manager_approved;
                let app_owner_ok =
                    !wf.require_app_owner_approval_for_requestable || *app_owner_approved;
                let sod_ok = !wf.block_sod_violating_requests || !has_sod_conflict;

                let approved = state_ok && contractor_ok && manager_ok && app_owner_ok && sod_ok;

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
                    let entry = person_entitlements
                        .entry(requester_person_key.clone())
                        .or_default();
                    for ent in &requested_ents {
                        entry.insert(ent.clone());
                    }
                    if *target_spoke_ticket_closed_without_aggregation && verify_post_close {
                        "approved_pending_itsm_verify".to_owned()
                    } else {
                        "approved_provisioned".to_owned()
                    }
                } else {
                    "denied".to_owned()
                };

                outcomes.insert(
                    event_id.clone(),
                    ExpectedDay2OutcomeV51 {
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

fn walk_live_chain(
    subject_key: &str,
    start_key: Option<&str>,
    eligible_states: &[LifecycleStateIdV51],
    cubes: &IndexMap<String, CoalescedIdentity<'_>>,
    usernames: &IndexMap<String, GeneratedUsername>,
    effective_states: &HashMap<String, LifecycleStateIdV51>,
) -> Option<String> {
    let mut visited: HashSet<String> = HashSet::new();
    visited.insert(subject_key.to_owned());

    let mut cursor = start_key;
    while let Some(key) = cursor {
        if !visited.insert(key.to_owned()) {
            break;
        }
        let cube = cubes.get(key)?;
        let state = effective_states
            .get(key)
            .copied()
            .unwrap_or(LifecycleStateIdV51::InactiveOrphan);
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
