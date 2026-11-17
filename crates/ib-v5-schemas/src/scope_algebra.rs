//! Finite leaf-scope capability algebra and common identity predicates for v5.
//!
//! Mirrors `identitybench.v5.scope_algebra` (SPEC-v5.md §3.0).

use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};

use indexmap::IndexMap;

use crate::schemas::{
    CompensatingControlV5, EngagementStatusV5, EngagementV5, GrantV5, GroupEffectV5, GroupV5,
    HRPersonV5, IdPAccountV5, RoleV5, SchemaValidationError, ScopeCatalogV5, ScopedSoDRuleV5,
    SoDScopeModeV5,
};

pub type CapabilityLeaf = (String, String);
pub type CapabilityFootprint = BTreeSet<CapabilityLeaf>;
pub type SoDWitness = (String, String, String, String, String);

/// Generic lookup abstraction so algebra helpers work with `IndexMap`, `HashMap`, or `BTreeMap`.
pub trait KeyLookup<V> {
    fn get_by_str(&self, key: &str) -> Option<&V>;
}

impl<V> KeyLookup<V> for IndexMap<String, V> {
    fn get_by_str(&self, key: &str) -> Option<&V> {
        self.get(key)
    }
}

impl<V, S: std::hash::BuildHasher> KeyLookup<V> for HashMap<String, V, S> {
    fn get_by_str(&self, key: &str) -> Option<&V> {
        self.get(key)
    }
}

impl<V> KeyLookup<V> for BTreeMap<String, V> {
    fn get_by_str(&self, key: &str) -> Option<&V> {
        self.get(key)
    }
}

impl<V, S: std::hash::BuildHasher> KeyLookup<V> for HashMap<&str, &V, S> {
    fn get_by_str(&self, key: &str) -> Option<&V> {
        self.get(key).copied()
    }
}

impl<V> KeyLookup<V> for BTreeMap<&str, &V> {
    fn get_by_str(&self, key: &str) -> Option<&V> {
        self.get(key).copied()
    }
}

/// Resolve a path using the catalog's exact-or-descendant semantics.
pub fn resolve_scope(
    scope_catalog: &ScopeCatalogV5,
    scope_path: &str,
) -> Result<BTreeSet<String>, SchemaValidationError> {
    scope_catalog.leaves(scope_path)
}

/// Return whether every leaf in `left_path` is also in `right_path`.
pub fn scope_leq(
    scope_catalog: &ScopeCatalogV5,
    left_path: &str,
    right_path: &str,
) -> Result<bool, SchemaValidationError> {
    let left = resolve_scope(scope_catalog, left_path)?;
    let right = resolve_scope(scope_catalog, right_path)?;
    Ok(left.is_subset(&right))
}

/// Return the intersection of resolved paths; zero paths meet to the universe.
pub fn scope_meet(
    scope_catalog: &ScopeCatalogV5,
    scope_paths: &[&str],
) -> Result<BTreeSet<String>, SchemaValidationError> {
    let mut leaves: BTreeSet<String> = scope_catalog.leaf_scopes.iter().cloned().collect();
    for scope_path in scope_paths {
        let resolved = resolve_scope(scope_catalog, scope_path)?;
        leaves = leaves.intersection(&resolved).cloned().collect();
    }
    Ok(leaves)
}

/// Freeze a footprint and reject leaves outside the finite catalog.
pub fn normalize_footprint<'a>(
    footprint: impl IntoIterator<Item = (&'a str, &'a str)>,
    scope_catalog: &ScopeCatalogV5,
) -> Result<CapabilityFootprint, SchemaValidationError> {
    let universe: BTreeSet<&str> = scope_catalog
        .leaf_scopes
        .iter()
        .map(String::as_str)
        .collect();
    let mut normalized = BTreeSet::new();
    for (capability, leaf) in footprint {
        if !universe.contains(leaf) {
            return Err(SchemaValidationError::validation(format!(
                "Unknown leaf scope {leaf:?}"
            )));
        }
        normalized.insert((capability.to_owned(), leaf.to_owned()));
    }
    Ok(normalized)
}

/// Expand capabilities over the union of the supplied scope paths.
pub fn capability_footprint<'a>(
    scope_catalog: &ScopeCatalogV5,
    capabilities: impl IntoIterator<Item = &'a str>,
    scope_paths: impl IntoIterator<Item = &'a str>,
) -> Result<CapabilityFootprint, SchemaValidationError> {
    let cap_vec: Vec<&str> = capabilities.into_iter().collect();
    let mut leaves = BTreeSet::new();
    for path in scope_paths {
        leaves.extend(resolve_scope(scope_catalog, path)?);
    }
    let mut out = BTreeSet::new();
    for cap in cap_vec {
        for leaf in &leaves {
            out.insert((cap.to_owned(), leaf.clone()));
        }
    }
    Ok(out)
}

/// Return whether `left` is a subset of `right`.
#[must_use]
pub fn footprint_leq(left: &CapabilityFootprint, right: &CapabilityFootprint) -> bool {
    left.is_subset(right)
}

/// Intersect footprints; with no operands, return the empty finite footprint.
#[must_use]
pub fn footprint_meet(footprints: &[&CapabilityFootprint]) -> CapabilityFootprint {
    let Some((first, rest)) = footprints.split_first() else {
        return BTreeSet::new();
    };
    let mut result = (*first).clone();
    for fp in rest {
        result.retain(|item| fp.contains(item));
    }
    result
}

/// Union footprints.
#[must_use]
pub fn footprint_join(footprints: &[&CapabilityFootprint]) -> CapabilityFootprint {
    let mut result = BTreeSet::new();
    for fp in footprints {
        result.extend(fp.iter().cloned());
    }
    result
}

/// Return the cycle-safe breadth-first closure of delivered account groups.
#[must_use]
pub fn group_closure<G: KeyLookup<GroupV5>>(
    account: &IdPAccountV5,
    groups_by_id: &G,
) -> BTreeSet<String> {
    let mut visited = BTreeSet::new();
    let mut queue: VecDeque<String> = account
        .group_ids
        .iter()
        .filter(|gid| groups_by_id.get_by_str(gid).is_some())
        .cloned()
        .collect();
    while let Some(group_id) = queue.pop_front() {
        if !visited.insert(group_id.clone()) {
            continue;
        }
        if let Some(group) = groups_by_id.get_by_str(&group_id) {
            for parent_id in &group.member_of_group_ids {
                if groups_by_id.get_by_str(parent_id).is_some()
                    && !visited.contains(parent_id.as_str())
                {
                    queue.push_back(parent_id.clone());
                }
            }
        }
    }
    visited
}

/// Return `(F_grp, F_deny)` for cycle-safe group closure and deny masking.
pub fn group_footprints<G: KeyLookup<GroupV5>, R: KeyLookup<RoleV5>>(
    account: &IdPAccountV5,
    groups_by_id: &G,
    roles_by_id: &R,
    scope_catalog: &ScopeCatalogV5,
) -> Result<(CapabilityFootprint, CapabilityFootprint), SchemaValidationError> {
    let mut allow = BTreeSet::new();
    let mut deny = BTreeSet::new();
    for group_id in group_closure(account, groups_by_id) {
        let group = groups_by_id.get_by_str(&group_id).ok_or_else(|| {
            SchemaValidationError::validation(format!("missing group {group_id:?}"))
        })?;
        let mut capabilities: BTreeSet<&str> =
            group.capabilities.iter().map(String::as_str).collect();
        for role_id in &group.granted_role_ids {
            let role = roles_by_id.get_by_str(role_id).ok_or_else(|| {
                SchemaValidationError::validation(format!("missing role {role_id:?}"))
            })?;
            capabilities.extend(role.capabilities.iter().map(String::as_str));
        }
        let fp = capability_footprint(scope_catalog, capabilities, [group.scope_path.as_str()])?;
        match group.effect {
            GroupEffectV5::Deny => deny.extend(fp),
            GroupEffectV5::Allow => allow.extend(fp),
        }
    }
    allow.retain(|item| !deny.contains(item));
    Ok((allow, deny))
}

/// Expand a grant role over its scope and subtract the account's group DENYs.
pub fn effective_grant_footprint<R: KeyLookup<RoleV5>>(
    grant: &GrantV5,
    roles_by_id: &R,
    group_deny: &CapabilityFootprint,
    scope_catalog: &ScopeCatalogV5,
) -> Result<CapabilityFootprint, SchemaValidationError> {
    let role = roles_by_id.get_by_str(&grant.role_id).ok_or_else(|| {
        SchemaValidationError::validation(format!("missing role {:?}", grant.role_id))
    })?;
    let mut raw = capability_footprint(
        scope_catalog,
        role.capabilities.iter().map(String::as_str),
        [grant.scope_path.as_str()],
    )?;
    raw.retain(|item| !group_deny.contains(item));
    Ok(raw)
}

/// Return symmetric, canonical witnesses for all violated scoped SoD rules.
#[must_use]
pub fn sod_witnesses<'a>(
    first: &CapabilityFootprint,
    second: &CapabilityFootprint,
    rules: impl IntoIterator<Item = &'a ScopedSoDRuleV5>,
) -> BTreeSet<SoDWitness> {
    let rules_vec: Vec<&ScopedSoDRuleV5> = rules.into_iter().collect();
    let mut witnesses = BTreeSet::new();
    for rule in rules_vec {
        for (cap_a, leaf_a) in first {
            for (cap_b, leaf_b) in second {
                if cap_a == cap_b {
                    continue;
                }
                let matches_pair = (cap_a == &rule.cap_a && cap_b == &rule.cap_b)
                    || (cap_a == &rule.cap_b && cap_b == &rule.cap_a);
                if !matches_pair {
                    continue;
                }
                if rule.scope_mode == SoDScopeModeV5::SameLeafScope && leaf_a != leaf_b {
                    continue;
                }
                let p1 = (cap_a.as_str(), leaf_a.as_str());
                let p2 = (cap_b.as_str(), leaf_b.as_str());
                let ((cap_1, leaf_1), (cap_2, leaf_2)) = if p1 <= p2 { (p1, p2) } else { (p2, p1) };
                witnesses.insert((
                    rule.rule_code.clone(),
                    cap_1.to_owned(),
                    leaf_1.to_owned(),
                    cap_2.to_owned(),
                    leaf_2.to_owned(),
                ));
            }
        }
    }
    witnesses
}

/// Check all §3.0 control conditions, including GLOBAL full-universe coverage.
pub fn syntactically_mitigates(
    control: &CompensatingControlV5,
    person_id: &str,
    witness: &SoDWitness,
    sod_rule: &ScopedSoDRuleV5,
    scope_catalog: &ScopeCatalogV5,
) -> Result<bool, SchemaValidationError> {
    let (rule_code, cap_1, leaf_1, cap_2, leaf_2) = witness;
    if control.monitored_person_id != person_id || rule_code != &sod_rule.rule_code {
        return Ok(false);
    }
    if cap_1 == cap_2 {
        return Ok(false);
    }
    let caps_match = (control.cap_a == *cap_1 && control.cap_b == *cap_2)
        || (control.cap_a == *cap_2 && control.cap_b == *cap_1);
    if !caps_match {
        return Ok(false);
    }
    let covered = resolve_scope(scope_catalog, &control.covered_scope_path)?;
    if !covered.contains(leaf_1) || !covered.contains(leaf_2) {
        return Ok(false);
    }
    if sod_rule.scope_mode == SoDScopeModeV5::Global {
        let full = resolve_scope(scope_catalog, "/*")?;
        return Ok(covered == full);
    }
    Ok(true)
}

/// Return SoD witnesses not syntactically covered by any supplied control.
pub fn unmitigated_witnesses<'a, 'b>(
    first: &CapabilityFootprint,
    second: &CapabilityFootprint,
    person_id: &str,
    controls: impl IntoIterator<Item = &'a CompensatingControlV5>,
    rules: impl IntoIterator<Item = &'b ScopedSoDRuleV5>,
    scope_catalog: &ScopeCatalogV5,
) -> Result<BTreeSet<SoDWitness>, SchemaValidationError> {
    let rules_vec: Vec<&ScopedSoDRuleV5> = rules.into_iter().collect();
    let controls_vec: Vec<&CompensatingControlV5> = controls.into_iter().collect();
    let mut unmitigated = BTreeSet::new();
    for witness in sod_witnesses(first, second, rules_vec.iter().copied()) {
        let (rule_code, cap_1, leaf_1, cap_2, leaf_2) = &witness;
        let matching_rules: Vec<&ScopedSoDRuleV5> = rules_vec
            .iter()
            .copied()
            .filter(|rule| {
                rule.rule_code == *rule_code
                    && cap_1 != cap_2
                    && ((rule.cap_a == *cap_1 && rule.cap_b == *cap_2)
                        || (rule.cap_a == *cap_2 && rule.cap_b == *cap_1))
                    && (rule.scope_mode == SoDScopeModeV5::Global || leaf_1 == leaf_2)
            })
            .collect();
        let mut any_uncovered = false;
        for rule in matching_rules {
            let mut covered_by_any = false;
            for control in &controls_vec {
                if syntactically_mitigates(control, person_id, &witness, rule, scope_catalog)? {
                    covered_by_any = true;
                    break;
                }
            }
            if !covered_by_any {
                any_uncovered = true;
                break;
            }
        }
        if any_uncovered {
            unmitigated.insert(witness);
        }
    }
    Ok(unmitigated)
}

/// Evaluate ACTIVE and CONTRACTOR_TO_FTE_PENDING engagement liveness at a clock.
#[must_use]
pub fn engagement_is_live(engagement: &EngagementV5, clock_s: i64) -> bool {
    let retro_live = engagement
        .retro_terminated_effective_s
        .is_none_or(|retro| clock_s < retro);
    match engagement.eng_status {
        EngagementStatusV5::Active => {
            engagement.start_s <= clock_s
                && engagement.end_s.is_none_or(|end| clock_s < end)
                && retro_live
        }
        EngagementStatusV5::ContractorToFtePending => {
            engagement.conversion_start_s.is_some()
                && engagement
                    .conversion_grace_deadline_s
                    .is_some_and(|deadline| clock_s < deadline)
                && retro_live
        }
        EngagementStatusV5::Terminated | EngagementStatusV5::FutureHire => false,
    }
}

/// Return whether a person has any engagement live at `clock_s`.
#[must_use]
pub fn has_live_engagement(person: &HRPersonV5, clock_s: i64) -> bool {
    person
        .engagements
        .iter()
        .any(|e| engagement_is_live(e, clock_s))
}

/// Resolve the engagement department before or after its effective transfer.
#[must_use]
pub fn effective_department(engagement: &EngagementV5, clock_s: i64) -> &str {
    if let (Some(effective_at), Some(prior)) = (
        engagement.dept_transfer_effective_at_s,
        engagement.prior_dept_path.as_deref(),
    ) && clock_s < effective_at
    {
        return prior;
    }
    &engagement.dept_path
}

/// Follow manager edges on live engagements; require at least one hop.
#[must_use]
pub fn is_subordinate<P: KeyLookup<HRPersonV5>>(
    subordinate_person_id: &str,
    manager_person_id: &str,
    clock_s: i64,
    people_by_id: &P,
) -> bool {
    if subordinate_person_id == manager_person_id {
        return false;
    }
    let mut visited = BTreeSet::new();
    visited.insert(subordinate_person_id.to_owned());
    let mut queue = VecDeque::new();
    queue.push_back(subordinate_person_id.to_owned());
    while let Some(person_id) = queue.pop_front() {
        let Some(person) = people_by_id.get_by_str(&person_id) else {
            continue;
        };
        let managers: BTreeSet<&str> = person
            .engagements
            .iter()
            .filter(|e| engagement_is_live(e, clock_s))
            .filter_map(|e| e.manager_person_id.as_deref())
            .collect();
        for next_manager_id in managers {
            if next_manager_id == manager_person_id {
                return true;
            }
            if !visited.contains(next_manager_id) {
                visited.insert(next_manager_id.to_owned());
                queue.push_back(next_manager_id.to_owned());
            }
        }
    }
    false
}

/// Convenience wrapper for the finite scope and footprint lattice.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScopeAlgebraV5 {
    pub scope_catalog: ScopeCatalogV5,
}

impl ScopeAlgebraV5 {
    #[must_use]
    pub fn new(scope_catalog: ScopeCatalogV5) -> Self {
        Self { scope_catalog }
    }

    pub fn leaves(&self, scope_path: &str) -> Result<BTreeSet<String>, SchemaValidationError> {
        resolve_scope(&self.scope_catalog, scope_path)
    }

    pub fn scope_leq(
        &self,
        left_path: &str,
        right_path: &str,
    ) -> Result<bool, SchemaValidationError> {
        scope_leq(&self.scope_catalog, left_path, right_path)
    }

    pub fn scope_meet(
        &self,
        scope_paths: &[&str],
    ) -> Result<BTreeSet<String>, SchemaValidationError> {
        scope_meet(&self.scope_catalog, scope_paths)
    }

    pub fn capability_footprint<'a>(
        &self,
        capabilities: impl IntoIterator<Item = &'a str>,
        scope_paths: impl IntoIterator<Item = &'a str>,
    ) -> Result<CapabilityFootprint, SchemaValidationError> {
        capability_footprint(&self.scope_catalog, capabilities, scope_paths)
    }

    pub fn normalize<'a>(
        &self,
        footprint: impl IntoIterator<Item = (&'a str, &'a str)>,
    ) -> Result<CapabilityFootprint, SchemaValidationError> {
        normalize_footprint(footprint, &self.scope_catalog)
    }
}
