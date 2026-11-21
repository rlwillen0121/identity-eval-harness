//! Birthright role assignment evaluation, group catalog classification, transitive nested group
//! resolution (`GG -> SG -> LG`), cross-account person effective entitlements, and SoD detection.

use std::collections::{BTreeSet, HashMap};

use ib_v51_schemas::{
    AffiliationTypeV51, BrownfieldSpokeAccountV51, EnterpriseBrownfieldWorldV51,
    GroupCatalogClassificationV51, IgaDeploymentConfigV51, LifecycleStateIdV51, MinedRoleV51,
    RoleAssignmentKindV51, SodPolicyV51, SpokeEnvKindV51,
};
use indexmap::IndexMap;

use crate::coalescence::{CoalescedIdentity, SpokeCorrelationState};
use crate::lifecycle::EvaluatedLifecycle;
use crate::transforms::group_disposition_to_str;

/// Expand a seed set of entitlements/groups transitively across `world.group_catalog`
/// (`member_of_group_ids`), safely handling arbitrary nesting depth and cycles (`GG -> SG -> LG`).
#[must_use]
pub fn expand_entitlements_transitively<I, S>(
    seed_entitlements: I,
    world: &EnterpriseBrownfieldWorldV51,
) -> BTreeSet<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut visited = BTreeSet::new();
    let mut stack = Vec::new();

    for ent in seed_entitlements {
        let trimmed = ent.as_ref().trim();
        if !trimmed.is_empty() && visited.insert(trimmed.to_owned()) {
            stack.push(trimmed.to_owned());
        }
    }

    while let Some(curr) = stack.pop() {
        if let Some(grp) = world.group_catalog.get(&curr) {
            for parent in &grp.member_of_group_ids {
                let p = parent.trim();
                if !p.is_empty() && visited.insert(p.to_owned()) {
                    stack.push(p.to_owned());
                }
            }
        }
    }

    visited
}

/// Expand a seed set of entitlements transitively using a custom working copy of group parent edges.
#[must_use]
pub fn expand_entitlements_with_graph<I, S>(
    seed_entitlements: I,
    group_parents: &HashMap<String, BTreeSet<String>>,
) -> BTreeSet<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut visited = BTreeSet::new();
    let mut stack = Vec::new();

    for ent in seed_entitlements {
        let trimmed = ent.as_ref().trim();
        if !trimmed.is_empty() && visited.insert(trimmed.to_owned()) {
            stack.push(trimmed.to_owned());
        }
    }

    while let Some(curr) = stack.pop() {
        if let Some(parents) = group_parents.get(&curr) {
            for parent in parents {
                if visited.insert(parent.clone()) {
                    stack.push(parent.clone());
                }
            }
        }
    }

    visited
}

/// Compute effective transitively-expanded entitlements for a single brownfield spoke account.
#[must_use]
pub fn account_effective_entitlements(
    acct: &BrownfieldSpokeAccountV51,
    world: &EnterpriseBrownfieldWorldV51,
) -> BTreeSet<String> {
    expand_entitlements_transitively(acct.entitlement_ids.iter(), world)
}

/// Compute each person's post-cutover effective entitlements across BOTH their `primary` AND
/// `admin_secondary` correlated spoke accounts plus assigned birthright entitlements,
/// transitively expanded through `world.group_catalog`.
#[must_use]
pub fn compute_person_effective_entitlements(
    world: &EnterpriseBrownfieldWorldV51,
    correlation: &SpokeCorrelationState,
    birthright_by_person: &IndexMap<String, Vec<String>>,
) -> IndexMap<String, BTreeSet<String>> {
    let mut direct_by_person: IndexMap<String, BTreeSet<String>> = IndexMap::new();

    for (pk, br) in birthright_by_person {
        let entry = direct_by_person.entry(pk.clone()).or_default();
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
                let entry = direct_by_person.entry(owner.clone()).or_default();
                for ent in &acct.entitlement_ids {
                    entry.insert(ent.clone());
                }
            }
        }
    }

    let mut effective = IndexMap::with_capacity(direct_by_person.len());
    for (pk, direct) in direct_by_person {
        effective.insert(pk, expand_entitlements_transitively(direct.iter(), world));
    }
    effective
}

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

/// Return the IDs of all active `SodPolicyV51` rules violated by `entitlements` (direct match).
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

/// Expand `entitlements` transitively through `world.group_catalog` and return all violated `SodPolicyV51` IDs.
#[must_use]
pub fn find_sod_conflicts_transitive(
    entitlements: &BTreeSet<String>,
    world: &EnterpriseBrownfieldWorldV51,
    sod_policies: &[SodPolicyV51],
) -> Vec<String> {
    let expanded = expand_entitlements_transitively(entitlements.iter(), world);
    find_sod_conflicts(&expanded, sod_policies)
}

/// Check if a role mixes `Prod` and `NonProd` entitlements (transitively expanded) using authoritative metadata.
#[must_use]
pub fn role_mixes_prod_and_nonprod(
    role: &MinedRoleV51,
    world: &EnterpriseBrownfieldWorldV51,
    authoritative_classifications: &IndexMap<String, GroupCatalogClassificationV51>,
) -> bool {
    let mut has_prod = false;
    let mut has_nonprod = false;
    let expanded = expand_entitlements_transitively(role.entitlement_ids.iter(), world);

    for ent in &expanded {
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

/// Return any entitlements in `role` (transitively expanded) that belong to service-account-only or orphaned groups.
#[must_use]
pub fn role_service_or_orphan_groups(
    role: &MinedRoleV51,
    world: &EnterpriseBrownfieldWorldV51,
) -> Vec<String> {
    let expanded = expand_entitlements_transitively(role.entitlement_ids.iter(), world);
    expanded
        .into_iter()
        .filter(|ent| {
            world
                .group_catalog
                .get(ent)
                .is_some_and(|g| g.is_service_account_only || g.is_orphaned_no_app)
        })
        .collect()
}
