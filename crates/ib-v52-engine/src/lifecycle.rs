//! Deterministic lifecycle state machine evaluation and per-spoke disposition resolution.

use std::collections::BTreeSet;

use ib_v52_schemas::{
    AffiliationTypeV52, EnterpriseBrownfieldWorldV52, IgaDeploymentConfigV52, LifecycleStateIdV52,
    LifecycleStateRuleV52, SpokeStateDispositionV52,
};
use indexmap::IndexMap;

use crate::coalescence::CoalescedIdentity;
use crate::transforms::{affiliation_to_str, eval_compare_op, strict_iso_date_days};

/// Resolved lifecycle state and operational flags for a single identity.
#[derive(Debug, Clone)]
pub struct EvaluatedLifecycle {
    pub lifecycle_state: LifecycleStateIdV52,
    pub sso_enabled: bool,
    pub vpn_enabled: bool,
    pub tokens_revoked: bool,
    pub oauth_tokens_and_consents_revoked: bool,
    pub preserve_mailbox_and_drive: bool,
    pub block_purge: bool,
    pub keep_hr_payroll_untouched: bool,
    pub spoke_dispositions: IndexMap<String, SpokeStateDispositionV52>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ContingentExpiryWindow {
    NotExpired,
    WithinGrace,
    PastGrace,
}

/// Evaluate lifecycle state and spoke dispositions for every coalesced identity.
#[must_use]
pub fn evaluate_lifecycle_states(
    world: &EnterpriseBrownfieldWorldV52,
    config: &IgaDeploymentConfigV52,
    cubes: &IndexMap<String, CoalescedIdentity<'_>>,
    cutover_date: &str,
) -> IndexMap<String, EvaluatedLifecycle> {
    let all_spokes: BTreeSet<String> = world
        .spoke_accounts
        .keys()
        .chain(world.mock_endpoints.keys())
        .cloned()
        .collect();

    let mut out = IndexMap::with_capacity(cubes.len());
    for (person_key, cube) in cubes {
        let eval = evaluate_single_lifecycle(cube, config, cutover_date, &all_spokes);
        out.insert(person_key.clone(), eval);
    }
    out
}

/// Evaluate the lifecycle state for a single identity cube.
#[must_use]
pub fn evaluate_single_lifecycle(
    cube: &CoalescedIdentity<'_>,
    config: &IgaDeploymentConfigV52,
    cutover_date: &str,
    all_spokes: &BTreeSet<String>,
) -> EvaluatedLifecycle {
    let primary_aff_str = affiliation_to_str(&cube.primary_affiliation);
    let active_affs_str = cube
        .active_affiliations
        .iter()
        .map(affiliation_to_str)
        .collect::<Vec<_>>()
        .join(",");
    let grace_period_days = config
        .lifecycle_states
        .states
        .get(&LifecycleStateIdV52::ContingentExpiredGrace)
        .and_then(|rule| rule.grace_period_days)
        .unwrap_or_default();
    let expiry_window = contingent_expiry_window(cube, cutover_date, grace_period_days);
    let is_contingent_expired_str = if expiry_window == ContingentExpiryWindow::WithinGrace {
        "true"
    } else {
        "false"
    };
    // A past-grace contract is an effective termination for lifecycle ordering. Keep this as a
    // derived value: source payroll data and the configured precedence remain untouched.
    let hr_payroll_status = if expiry_window == ContingentExpiryWindow::PastGrace {
        "TERMINATED"
    } else {
        cube.attributes
            .get("hr_payroll_status")
            .map(String::as_str)
            .unwrap_or("")
    };

    let order = &config.lifecycle_states.evaluation_order;
    let mut matched: Option<(LifecycleStateIdV52, &LifecycleStateRuleV52)> = None;

    for (idx, state_id) in order.iter().enumerate() {
        let Some(rule) = config.lifecycle_states.states.get(state_id) else {
            continue;
        };
        let is_last = idx + 1 == order.len();
        let rule_matches = if rule.all_of.is_empty() {
            is_last
        } else {
            rule.all_of.iter().all(|clause| {
                let actual = match clause.identity_attribute.as_str() {
                    "primary_affiliation" => primary_aff_str,
                    "active_affiliations" => active_affs_str.as_str(),
                    "is_contingent_expired" => is_contingent_expired_str,
                    "person_key" => cube.person_key.as_str(),
                    "hr_payroll_status" => hr_payroll_status,
                    other => cube.attributes.get(other).map(String::as_str).unwrap_or(""),
                };
                eval_compare_op(actual, clause.cmp, clause.value.as_deref())
            })
        };

        if rule_matches {
            matched = Some((*state_id, rule));
            break;
        }
    }

    match matched {
        Some((state_id, rule)) => {
            let mut spoke_dispositions = IndexMap::with_capacity(all_spokes.len());
            for spoke_id in all_spokes {
                let disp = rule
                    .spoke_disposition_overrides
                    .get(spoke_id)
                    .copied()
                    .unwrap_or(rule.default_spoke_disposition);
                spoke_dispositions.insert(spoke_id.clone(), disp);
            }
            let oauth_revoked = !rule.sso_enabled
                && rule.revoke_active_tokens_immediately
                && config
                    .lifecycle_states
                    .revoke_oauth_and_refresh_trees_on_disable;
            EvaluatedLifecycle {
                lifecycle_state: state_id,
                sso_enabled: rule.sso_enabled,
                vpn_enabled: rule.vpn_enabled,
                tokens_revoked: rule.revoke_active_tokens_immediately,
                oauth_tokens_and_consents_revoked: oauth_revoked,
                preserve_mailbox_and_drive: rule.preserve_mailbox_and_drive_data,
                block_purge: rule.block_account_purge,
                keep_hr_payroll_untouched: rule.keep_hr_payroll_untouched,
                spoke_dispositions,
            }
        }
        None => {
            let mut spoke_dispositions = IndexMap::with_capacity(all_spokes.len());
            for spoke_id in all_spokes {
                spoke_dispositions.insert(
                    spoke_id.clone(),
                    SpokeStateDispositionV52::DisableAuthPreserveAccountAndData,
                );
            }
            EvaluatedLifecycle {
                lifecycle_state: LifecycleStateIdV52::InactiveOrphan,
                sso_enabled: false,
                vpn_enabled: false,
                tokens_revoked: true,
                oauth_tokens_and_consents_revoked: config
                    .lifecycle_states
                    .revoke_oauth_and_refresh_trees_on_disable,
                preserve_mailbox_and_drive: false,
                block_purge: false,
                keep_hr_payroll_untouched: true,
                spoke_dispositions,
            }
        }
    }
}

fn contingent_expiry_window(
    cube: &CoalescedIdentity<'_>,
    cutover_date: &str,
    grace_period_days: u32,
) -> ContingentExpiryWindow {
    let is_external_affiliation = |affiliation: &AffiliationTypeV52| {
        matches!(
            affiliation,
            &AffiliationTypeV52::ContingentWorker
                | &AffiliationTypeV52::Vendor
                | &AffiliationTypeV52::B2bGuest
        )
    };
    let has_external_affiliation = cube.active_affiliations.iter().any(is_external_affiliation);
    let has_other_active_affiliation = cube
        .active_affiliations
        .iter()
        .any(|affiliation| !is_external_affiliation(affiliation));
    if !has_external_affiliation || has_other_active_affiliation {
        return ContingentExpiryWindow::NotExpired;
    }
    if cube
        .attributes
        .get("hr_payroll_status")
        .is_some_and(|status| status.trim().eq_ignore_ascii_case("TERMINATED"))
        || cube
            .attributes
            .get("academic_status")
            .is_some_and(|status| status.trim().eq_ignore_ascii_case("ENROLLED"))
    {
        return ContingentExpiryWindow::NotExpired;
    }
    let contract_end = cube
        .attributes
        .get("contract_end_date")
        .map(String::as_str)
        .unwrap_or("")
        .trim();
    let (Some(contract_end_day), Some(cutover_day)) = (
        strict_iso_date_days(contract_end),
        strict_iso_date_days(cutover_date.trim()),
    ) else {
        return ContingentExpiryWindow::NotExpired;
    };
    let elapsed_days = cutover_day - contract_end_day;
    if elapsed_days <= 0 {
        return ContingentExpiryWindow::NotExpired;
    }
    if elapsed_days <= i64::from(grace_period_days) {
        ContingentExpiryWindow::WithinGrace
    } else {
        ContingentExpiryWindow::PastGrace
    }
}
