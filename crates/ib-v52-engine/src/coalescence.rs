//! Multi-source record matching, attribute coalescence, multi-affiliation resolution,
//! spoke correlation, and service-account classification.

use std::collections::{BTreeSet, HashSet};

use ib_v52_schemas::{
    AffiliationTypeV52, AuthoritativeSourceRuleV52, BrownfieldSpokeAccountV52,
    EnterpriseBrownfieldWorldV52, IgaDeploymentConfigV52, RawSourceRecordV52,
    ServiceAccountClassifierV52,
};
use indexmap::IndexMap;

use crate::transforms::{TransformBudget, TransformEvalContext, parse_affiliation};

/// Coalesced identity cube before/after lifecycle & username resolution.
#[derive(Debug, Clone)]
pub struct CoalescedIdentity<'a> {
    pub person_key: String,
    pub matched_sources: Vec<String>,
    pub matched_records: IndexMap<String, &'a RawSourceRecordV52>,
    pub attributes: IndexMap<String, String>,
    pub primary_affiliation: AffiliationTypeV52,
    pub active_affiliations: Vec<AffiliationTypeV52>,
}

/// Deterministic spoke correlation and service-account classification output.
#[derive(Debug, Clone, Default)]
pub struct SpokeCorrelationState {
    /// `(spoke_id, account_id) -> person_key`
    pub account_to_person: IndexMap<(String, String), String>,
    /// `person_key -> spoke_id -> primary non-admin human account_id`
    pub person_to_spoke_primary: IndexMap<String, IndexMap<String, String>>,
    /// `person_key -> spoke_id -> privileged secondary human admin account_id` (e.g. `adm_*`)
    pub person_to_spoke_admin_secondary: IndexMap<String, IndexMap<String, String>>,
    /// `person_key -> spoke_id -> all correlated account_ids` (primary + admin_secondary)
    pub person_to_spoke_accounts: IndexMap<String, IndexMap<String, Vec<String>>>,
    /// Set of `(spoke_id, account_id)` correlated as privileged admin secondary accounts.
    pub admin_secondary_account_set: HashSet<(String, String)>,
    /// Sorted `spoke_id:account_id` list of correlated privileged admin secondary accounts.
    pub admin_secondary_accounts: Vec<String>,
    /// Sorted `spoke_id:account_id` list of uncorrelated accounts classified as `SERVICE_ACCOUNT_EXEMPT`.
    pub exempt_service_accounts: Vec<String>,
    /// Set of `(spoke_id, account_id)` classified as `SERVICE_ACCOUNT_EXEMPT`.
    pub exempt_service_account_set: HashSet<(String, String)>,
    /// Sorted `spoke_id:account_id` list of uncorrelated accounts NOT classified as service accounts.
    pub uncorrelated_non_exempt_accounts: Vec<String>,
}

/// Coalesce all canonical persons in `world.canonical_person_keys` in ascending order.
pub fn coalesce_identities<'a>(
    world: &'a EnterpriseBrownfieldWorldV52,
    config: &IgaDeploymentConfigV52,
    cutover_date: &str,
) -> Result<(IndexMap<String, CoalescedIdentity<'a>>, HashSet<String>), String> {
    coalesce_identities_with_budget(world, config, cutover_date, &mut TransformBudget::default())
}

pub(crate) fn coalesce_identities_with_budget<'a>(
    world: &'a EnterpriseBrownfieldWorldV52,
    config: &IgaDeploymentConfigV52,
    cutover_date: &str,
    budget: &mut TransformBudget,
) -> Result<(IndexMap<String, CoalescedIdentity<'a>>, HashSet<String>), String> {
    let mut sorted_rules: Vec<&AuthoritativeSourceRuleV52> = config
        .identity_profile
        .authoritative_sources
        .iter()
        .collect();
    sorted_rules.sort_by(|a, b| {
        (a.priority_rank, a.source_id.as_str()).cmp(&(b.priority_rank, b.source_id.as_str()))
    });

    let declared_sources: HashSet<String> =
        sorted_rules.iter().map(|r| r.source_id.clone()).collect();

    let mut sorted_person_keys: Vec<String> = world.canonical_person_keys.clone();
    sorted_person_keys.sort();
    sorted_person_keys.dedup();

    let mut cubes = IndexMap::with_capacity(sorted_person_keys.len());

    for person_key in sorted_person_keys {
        let mut known_keys: IndexMap<String, String> = IndexMap::new();
        known_keys.insert("person_key".to_owned(), person_key.clone());

        let mut matched_sources = Vec::new();
        let mut matched_records: IndexMap<String, &'a RawSourceRecordV52> = IndexMap::new();

        for rule in &sorted_rules {
            let Some(records) = world.identity_sources.get(&rule.source_id) else {
                continue;
            };

            let mut best: Option<&'a RawSourceRecordV52> = None;
            for rec in records {
                if is_ignored_record(rec, &rule.ignore_records_with_status) {
                    continue;
                }
                if !record_matches_person(rec, rule, &known_keys) {
                    continue;
                }
                best = match best {
                    None => Some(rec),
                    Some(prev) => {
                        let prev_key = (
                            prev.attributes
                                .get("last_updated")
                                .map(String::as_str)
                                .unwrap_or(""),
                            prev.record_id.as_str(),
                        );
                        let curr_key = (
                            rec.attributes
                                .get("last_updated")
                                .map(String::as_str)
                                .unwrap_or(""),
                            rec.record_id.as_str(),
                        );
                        if curr_key > prev_key {
                            Some(rec)
                        } else {
                            Some(prev)
                        }
                    }
                };
            }

            if let Some(rec) = best {
                matched_sources.push(rule.source_id.clone());
                matched_records.insert(rule.source_id.clone(), rec);
                for (k, v) in &rec.attributes {
                    let trimmed = v.trim();
                    if !trimmed.is_empty() && !known_keys.contains_key(k) {
                        known_keys.insert(k.clone(), trimmed.to_owned());
                    }
                }
            }
        }

        let eval_ctx = TransformEvalContext {
            transforms: &config.transforms,
            declared_sources: &declared_sources,
            matched_records: &matched_records,
            cutover_date,
        };

        let mut attributes = IndexMap::new();
        for (attr_name, expr) in &config.identity_profile.attribute_mappings {
            let val = budget.evaluate(expr, &eval_ctx).map_err(|err| {
                TransformBudget::with_context(err, || {
                    format!("person '{person_key}' attribute '{attr_name}' transform error")
                })
            })?;
            attributes.insert(attr_name.clone(), val);
        }

        let (primary_affiliation, active_affiliations) =
            resolve_affiliations(&attributes, &config.identity_profile.affiliation_policy);

        cubes.insert(
            person_key.clone(),
            CoalescedIdentity {
                person_key,
                matched_sources,
                matched_records,
                attributes,
                primary_affiliation,
                active_affiliations,
            },
        );
    }

    Ok((cubes, declared_sources))
}

fn is_ignored_record(rec: &RawSourceRecordV52, ignore_statuses: &[String]) -> bool {
    if ignore_statuses.is_empty() {
        return false;
    }
    let rec_status = rec.attributes.get("record_status").map(String::as_str);
    let status = rec.attributes.get("status").map(String::as_str);
    ignore_statuses.iter().any(|ignored| {
        rec_status.is_some_and(|s| s == ignored) || status.is_some_and(|s| s == ignored)
    })
}

fn record_matches_person(
    rec: &RawSourceRecordV52,
    rule: &AuthoritativeSourceRuleV52,
    known_keys: &IndexMap<String, String>,
) -> bool {
    for mk in &rule.match_keys {
        let Some(expected_val) = known_keys.get(&mk.identity_key_name) else {
            continue;
        };
        let exp = expected_val.trim();
        if exp.is_empty() {
            continue;
        }
        let Some(rec_val) = rec.attributes.get(&mk.source_field) else {
            continue;
        };
        let rv = rec_val.trim();
        if rv.is_empty() {
            continue;
        }
        let matched = if mk.case_insensitive {
            exp.eq_ignore_ascii_case(rv)
        } else {
            exp == rv
        };
        if matched {
            return true;
        }
    }
    false
}

fn resolve_affiliations(
    attributes: &IndexMap<String, String>,
    policy: &ib_v52_schemas::AffiliationPolicyV52,
) -> (AffiliationTypeV52, Vec<AffiliationTypeV52>) {
    let mut active: Vec<AffiliationTypeV52> = Vec::new();
    if let Some(csv) = attributes.get("affiliations_csv") {
        for token in csv.split(',') {
            if let Some(aff) = parse_affiliation(token)
                && !active.contains(&aff)
            {
                active.push(aff);
            }
        }
    }

    let is_enrolled_student = attributes
        .get("academic_status")
        .is_some_and(|s| s.trim() == "ENROLLED");

    if policy.preserve_active_student_on_worker_end
        && is_enrolled_student
        && !active.contains(&AffiliationTypeV52::Student)
    {
        active.push(AffiliationTypeV52::Student);
    }

    active.sort_by_key(|aff| {
        let idx = policy
            .precedence_order
            .iter()
            .position(|p| p == aff)
            .unwrap_or(usize::MAX);
        (idx, aff.clone())
    });

    if !policy.allow_additive_affiliations && active.len() > 1 {
        let primary = active[0].clone();
        let retain_student = policy.preserve_active_student_on_worker_end
            && is_enrolled_student
            && primary != AffiliationTypeV52::Student
            && active.contains(&AffiliationTypeV52::Student);
        active.retain(|aff| {
            *aff == primary || (retain_student && *aff == AffiliationTypeV52::Student)
        });
    }

    let primary = active
        .first()
        .cloned()
        .unwrap_or(AffiliationTypeV52::Affiliate);

    (primary, active)
}

/// Correlate spoke accounts to identities and classify uncorrelated service accounts.
#[must_use]
pub fn correlate_spokes_and_classify_service_accounts(
    world: &EnterpriseBrownfieldWorldV52,
    config: &IgaDeploymentConfigV52,
    cubes: &IndexMap<String, CoalescedIdentity<'_>>,
) -> SpokeCorrelationState {
    let mut state = SpokeCorrelationState::default();
    let mut admin_secondary_sorted = BTreeSet::new();
    let mut exempt_sorted = BTreeSet::new();
    let mut uncorrelated_non_exempt_sorted = BTreeSet::new();

    let admin_prefix = config
        .identity_profile
        .correlate_admin_secondary_prefix
        .as_deref()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_ascii_lowercase);

    // Sort spoke IDs for deterministic traversal.
    let mut spoke_ids: Vec<&String> = world.spoke_accounts.keys().collect();
    spoke_ids.sort();

    for spoke_id in spoke_ids {
        let accounts = &world.spoke_accounts[spoke_id];
        let rules = config
            .identity_profile
            .spoke_correlation
            .get(spoke_id)
            .map(Vec::as_slice)
            .unwrap_or(&[]);

        for acct in accounts {
            let mut matched_person: Option<String> = None;

            for rule in rules {
                let acct_val = acct
                    .attributes
                    .get(&rule.spoke_field)
                    .map(String::as_str)
                    .or_else(|| {
                        (rule.spoke_field == "account_id").then_some(acct.account_id.as_str())
                    });
                let Some(acct_val) = acct_val.map(str::trim).filter(|s| !s.is_empty()) else {
                    continue;
                };

                let mut matches: Vec<&str> = Vec::new();
                for (person_key, cube) in cubes {
                    let cube_val = cube
                        .attributes
                        .get(&rule.identity_attribute)
                        .map(String::as_str)
                        .or_else(|| {
                            (rule.identity_attribute == "person_key")
                                .then_some(cube.person_key.as_str())
                        });
                    let Some(cube_val) = cube_val.map(str::trim).filter(|s| !s.is_empty()) else {
                        continue;
                    };

                    let is_eq = if rule.case_insensitive {
                        acct_val.eq_ignore_ascii_case(cube_val)
                    } else {
                        acct_val == cube_val
                    };
                    if is_eq {
                        matches.push(person_key.as_str());
                    }
                }

                if matches.len() == 1 {
                    matched_person = Some(matches[0].to_owned());
                    break;
                }
            }

            if let Some(person_key) = matched_person {
                let is_admin_secondary = admin_prefix.as_deref().is_some_and(|pfx| {
                    acct.is_admin_secondary
                        || acct.account_id.to_ascii_lowercase().starts_with(pfx)
                        || acct
                            .attributes
                            .get("sAMAccountName")
                            .is_some_and(|s| s.trim().to_ascii_lowercase().starts_with(pfx))
                });

                state.account_to_person.insert(
                    (spoke_id.clone(), acct.account_id.clone()),
                    person_key.clone(),
                );
                if is_admin_secondary {
                    state
                        .person_to_spoke_admin_secondary
                        .entry(person_key.clone())
                        .or_default()
                        .entry(spoke_id.clone())
                        .or_insert_with(|| acct.account_id.clone());
                    state
                        .admin_secondary_account_set
                        .insert((spoke_id.clone(), acct.account_id.clone()));
                    admin_secondary_sorted.insert(format!("{spoke_id}:{}", acct.account_id));
                } else {
                    state
                        .person_to_spoke_primary
                        .entry(person_key.clone())
                        .or_default()
                        .entry(spoke_id.clone())
                        .or_insert_with(|| acct.account_id.clone());
                }
                state
                    .person_to_spoke_accounts
                    .entry(person_key)
                    .or_default()
                    .entry(spoke_id.clone())
                    .or_default()
                    .push(acct.account_id.clone());
            } else {
                let formatted = format!("{spoke_id}:{}", acct.account_id);
                let is_svc = config
                    .identity_profile
                    .service_account_classifiers
                    .iter()
                    .filter(|c| c.spoke_id == *spoke_id)
                    .any(|c| matches_service_account_classifier(acct, c));
                if is_svc {
                    state
                        .exempt_service_account_set
                        .insert((spoke_id.clone(), acct.account_id.clone()));
                    exempt_sorted.insert(formatted);
                } else {
                    uncorrelated_non_exempt_sorted.insert(formatted);
                }
            }
        }
    }

    state.admin_secondary_accounts = admin_secondary_sorted.into_iter().collect();
    state.exempt_service_accounts = exempt_sorted.into_iter().collect();
    state.uncorrelated_non_exempt_accounts = uncorrelated_non_exempt_sorted.into_iter().collect();
    state
}

/// Evaluate whether an uncorrelated spoke account matches a single `ServiceAccountClassifierV52`.
#[must_use]
pub fn matches_service_account_classifier(
    acct: &BrownfieldSpokeAccountV52,
    classifier: &ServiceAccountClassifierV52,
) -> bool {
    let has_any_criterion = !classifier.account_prefixes.is_empty()
        || !classifier.ou_substrings.is_empty()
        || classifier.require_spn_present
        || !classifier.description_keywords.is_empty();
    if !has_any_criterion {
        return false;
    }

    if !classifier.account_prefixes.is_empty() {
        let acct_id_lower = acct.account_id.to_ascii_lowercase();
        let sam_lower = acct
            .attributes
            .get("sAMAccountName")
            .map(|s| s.to_ascii_lowercase());
        let prefix_ok = classifier.account_prefixes.iter().any(|p| {
            let p_low = p.to_ascii_lowercase();
            !p_low.is_empty()
                && (acct_id_lower.starts_with(&p_low)
                    || sam_lower.as_deref().is_some_and(|s| s.starts_with(&p_low)))
        });
        if !prefix_ok {
            return false;
        }
    }

    if !classifier.ou_substrings.is_empty() {
        let dn_lower = acct.native_dn_or_key.to_ascii_lowercase();
        let ou_ok = classifier.ou_substrings.iter().any(|sub| {
            let sub_low = sub.to_ascii_lowercase();
            !sub_low.is_empty() && dn_lower.contains(&sub_low)
        });
        if !ou_ok {
            return false;
        }
    }

    if classifier.require_spn_present && !acct.has_service_principal_name {
        return false;
    }

    if !classifier.description_keywords.is_empty() {
        let desc_lower = acct
            .attributes
            .get("description")
            .map(|s| s.to_ascii_lowercase())
            .unwrap_or_default();
        let desc_ok = classifier.description_keywords.iter().any(|kw| {
            let kw_low = kw.to_ascii_lowercase();
            !kw_low.is_empty() && desc_lower.contains(&kw_low)
        });
        if !desc_ok {
            return false;
        }
    }

    true
}
