//! Birthright role assignment evaluation, group catalog classification, and SoD conflict detection.

use std::collections::BTreeSet;

use ib_v51_schemas::{
    AffiliationTypeV51, EnterpriseBrownfieldWorldV51, GroupCatalogClassificationV51,
    IgaDeploymentConfigV51, LifecycleStateIdV51, MinedRoleV51, RoleAssignmentKindV51, SodPolicyV51,
    SpokeEnvKindV51,
};
use indexmap::IndexMap;

use crate::coalescence::CoalescedIdentity;
use crate::lifecycle::EvaluatedLifecycle;
use crate::transforms::group_disposition_to_str;

/// Compute sorted, deduplicated birthright entitlements for a single identity.
#[must_use]
pub fn compute_birthright_entitlements(
    cube: &CoalescedIdentity<'_>,
    lifecycle: &EvaluatedLifecycle,
    config: &IgaDeploymentConfigV51,
) -> Vec<String> {
    let eligible_affiliations: Vec<&AffiliationTypeV51> = match lifecycle.lifecycle_state {
        LifecycleStateIdV51::Active => {
            if cube.active_affiliations.is_empty() {
                vec![&cube.primary_affiliation]
            } else {
                cube.active_affiliations.iter().collect()
            }
        }
        LifecycleStateIdV51::SeasonalFurlough => {
            let retained = &config
                .identity_profile
                .affiliation_policy
                .furlough_retained_affiliations;
            cube.active_affiliations
                .iter()
                .filter(|aff| retained.contains(*aff))
                .collect()
        }
        _ => return Vec::new(),
    };

    if eligible_affiliations.is_empty() {
        return Vec::new();
    }

    let department = cube
        .attributes
        .get("department")
        .map(String::as_str)
        .unwrap_or("")
        .trim();
    let job_code = cube
        .attributes
        .get("job_code")
        .map(String::as_str)
        .unwrap_or("")
        .trim();

    let mut entitlements = BTreeSet::new();
    for role in &config.role_model.roles {
        if role.assignment_kind != RoleAssignmentKindV51::BirthrightAuto {
            continue;
        }
        let aff_matches = eligible_affiliations
            .iter()
            .any(|aff| role.allowed_affiliations.contains(*aff));
        if !aff_matches {
            continue;
        }
        let dept_matches = role.match_departments.is_empty()
            || role.match_departments.iter().any(|d| d == department);
        if !dept_matches {
            continue;
        }
        let job_matches =
            role.match_job_codes.is_empty() || role.match_job_codes.iter().any(|j| j == job_code);
        if !job_matches {
            continue;
        }
        for ent in &role.entitlement_ids {
            entitlements.insert(ent.clone());
        }
    }

    entitlements.into_iter().collect()
}

/// Format sorted `group_id -> snake_case(disposition)` map from `config.role_model.group_classifications`.
#[must_use]
pub fn compute_group_dispositions(config: &IgaDeploymentConfigV51) -> IndexMap<String, String> {
    let mut keys: Vec<&String> = config.role_model.group_classifications.keys().collect();
    keys.sort();
    let mut out = IndexMap::with_capacity(keys.len());
    for group_id in keys {
        let cls = &config.role_model.group_classifications[group_id];
        out.insert(
            group_id.clone(),
            group_disposition_to_str(cls.disposition).to_owned(),
        );
    }
    out
}

/// Return the IDs of all active `SodPolicyV51` rules violated by `entitlements`.
#[must_use]
pub fn find_sod_conflicts(
    entitlements: &BTreeSet<String>,
    sod_policies: &[SodPolicyV51],
) -> Vec<String> {
    let mut violated = Vec::new();
    for policy in sod_policies {
        if !policy.block_co_provisioning_and_role_bundling {
            continue;
        }
        let has_left = policy
            .conflicting_left_entitlements
            .iter()
            .any(|e| entitlements.contains(e));
        let has_right = policy
            .conflicting_right_entitlements
            .iter()
            .any(|e| entitlements.contains(e));
        if has_left && has_right {
            violated.push(policy.policy_id.clone());
        }
    }
    violated
}

/// Check if a role mixes `Prod` and `NonProd` entitlements using authoritative group metadata.
#[must_use]
pub fn role_mixes_prod_and_nonprod(
    role: &MinedRoleV51,
    world: &EnterpriseBrownfieldWorldV51,
    authoritative_classifications: &IndexMap<String, GroupCatalogClassificationV51>,
) -> bool {
    let mut has_prod = false;
    let mut has_nonprod = false;

    for ent in &role.entitlement_ids {
        let env = authoritative_classifications
            .get(ent)
            .map(|c| c.environment)
            .or_else(|| {
                world.group_catalog.get(ent).map(|g| {
                    let hint = g.environment_hint.to_ascii_uppercase();
                    if hint.contains("NON")
                        || hint.contains("DEV")
                        || hint.contains("TEST")
                        || hint.contains("QA")
                        || hint.contains("UAT")
                        || hint.contains("STG")
                        || hint.contains("STAGE")
                    {
                        SpokeEnvKindV51::NonProd
                    } else {
                        SpokeEnvKindV51::Prod
                    }
                })
            });
        match env {
            Some(SpokeEnvKindV51::Prod) => has_prod = true,
            Some(SpokeEnvKindV51::NonProd) => has_nonprod = true,
            None => {}
        }
    }

    has_prod && has_nonprod
}

/// Return any entitlements in `role` that belong to service-account-only or orphaned groups.
#[must_use]
pub fn role_service_or_orphan_groups(
    role: &MinedRoleV51,
    world: &EnterpriseBrownfieldWorldV51,
) -> Vec<String> {
    role.entitlement_ids
        .iter()
        .filter(|ent| {
            world
                .group_catalog
                .get(*ent)
                .is_some_and(|g| g.is_service_account_only || g.is_orphaned_no_app)
        })
        .cloned()
        .collect()
}
