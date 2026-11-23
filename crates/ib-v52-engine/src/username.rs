//! Deterministic `sAMAccountName` and `UPN` generation with collision detection,
//! 20-character truncation suffix math, anti-recycling, and brownfield preservation.

use std::collections::HashSet;

use ib_v52_schemas::{
    BrownfieldSpokeAccountV52, CollisionSourceV52, EnterpriseBrownfieldWorldV52,
    IgaDeploymentConfigV52, LifecycleStateIdV52, TransformExprV52, UsernameBasePatternV52,
};
use indexmap::IndexMap;

use crate::coalescence::{CoalescedIdentity, SpokeCorrelationState};
use crate::lifecycle::EvaluatedLifecycle;
use crate::transforms::{TransformBudget, TransformEvalContext, strip_diacritics};

/// Generated username identifiers for a single identity.
#[derive(Debug, Clone)]
pub struct GeneratedUsername {
    pub sam_account_name: String,
    pub upn: String,
    pub preserved_from_brownfield: bool,
}

/// Extract the lowercased local-part before `@` (or the whole string if no `@`).
#[must_use]
pub fn upn_local_part(upn: &str) -> String {
    upn.split('@')
        .next()
        .unwrap_or(upn)
        .trim()
        .to_ascii_lowercase()
}

/// Generate `sAMAccountName` and `UPN` for all identities in ascending `person_key` order.
pub fn generate_usernames(
    world: &EnterpriseBrownfieldWorldV52,
    config: &IgaDeploymentConfigV52,
    cubes: &IndexMap<String, CoalescedIdentity<'_>>,
    declared_sources: &HashSet<String>,
    lifecycles: &IndexMap<String, EvaluatedLifecycle>,
    correlation: &SpokeCorrelationState,
    cutover_date: &str,
) -> Result<IndexMap<String, GeneratedUsername>, String> {
    generate_usernames_with_budget(
        world,
        config,
        cubes,
        declared_sources,
        lifecycles,
        correlation,
        cutover_date,
        &mut TransformBudget::default(),
    )
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn generate_usernames_with_budget(
    world: &EnterpriseBrownfieldWorldV52,
    config: &IgaDeploymentConfigV52,
    cubes: &IndexMap<String, CoalescedIdentity<'_>>,
    declared_sources: &HashSet<String>,
    lifecycles: &IndexMap<String, EvaluatedLifecycle>,
    correlation: &SpokeCorrelationState,
    cutover_date: &str,
    budget: &mut TransformBudget,
) -> Result<IndexMap<String, GeneratedUsername>, String> {
    let policy = &config.username_policy;
    let check_live = policy
        .collision_check_sources
        .contains(&CollisionSourceV52::LiveDirectoryAccounts);
    let check_disabled = policy
        .collision_check_sources
        .contains(&CollisionSourceV52::DisabledDirectoryAccounts);
    let check_legal_hold = policy
        .collision_check_sources
        .contains(&CollisionSourceV52::LegalHoldAccounts);
    let check_tombstones = policy
        .collision_check_sources
        .contains(&CollisionSourceV52::TerminatedTombstoneArchive);
    let check_reserved = policy
        .collision_check_sources
        .contains(&CollisionSourceV52::ReservedAliases);

    // Pre-collect static forbidden handles (tombstones & reserved aliases).
    let mut static_forbidden_handles: HashSet<String> = HashSet::new();
    let mut static_forbidden_upns: HashSet<String> = HashSet::new();

    if check_tombstones {
        for tomb in &world.tombstone_archive {
            let sam_low = tomb.sam_account_name.trim().to_ascii_lowercase();
            if !sam_low.is_empty() {
                static_forbidden_handles.insert(sam_low);
            }
            let upn_low = tomb.upn.trim().to_ascii_lowercase();
            if !upn_low.is_empty() {
                static_forbidden_upns.insert(upn_low.clone());
                let local = upn_local_part(&upn_low);
                if !local.is_empty() {
                    static_forbidden_handles.insert(local);
                }
            }
        }
    }

    if check_reserved {
        for alias in &world.reserved_aliases {
            let low = alias.trim().to_ascii_lowercase();
            if !low.is_empty() {
                static_forbidden_handles.insert(low);
            }
        }
    }

    // Index AD & Entra directory accounts with owner and legal-hold flags.
    struct DirAccountEntry<'a> {
        owner: Option<&'a str>,
        enabled: bool,
        is_legal_hold: bool,
        sam_lower: Option<String>,
        upn_lower: Option<String>,
        upn_local_lower: Option<String>,
    }

    let mut dir_entries: Vec<DirAccountEntry<'_>> = Vec::new();
    for spoke_id in ["ad", "entra"] {
        if let Some(accounts) = world.spoke_accounts.get(spoke_id) {
            for acct in accounts {
                let owner = correlation
                    .account_to_person
                    .get(&(spoke_id.to_owned(), acct.account_id.clone()))
                    .map(String::as_str);
                let acct_lh = acct
                    .attributes
                    .get("legal_hold")
                    .is_some_and(|v| v.trim().eq_ignore_ascii_case("true"));
                let owner_lh = owner.is_some_and(|pk| {
                    lifecycles
                        .get(pk)
                        .is_some_and(|lc| lc.lifecycle_state == LifecycleStateIdV52::LegalHold)
                });
                let sam_lower = acct
                    .attributes
                    .get("sAMAccountName")
                    .map(|s| s.trim().to_ascii_lowercase())
                    .filter(|s| !s.is_empty());
                let upn_lower = acct
                    .attributes
                    .get("userPrincipalName")
                    .map(|s| s.trim().to_ascii_lowercase())
                    .filter(|s| !s.is_empty());
                let upn_local_lower = upn_lower
                    .as_deref()
                    .map(upn_local_part)
                    .filter(|s| !s.is_empty());

                dir_entries.push(DirAccountEntry {
                    owner,
                    enabled: acct.enabled,
                    is_legal_hold: acct_lh || owner_lh,
                    sam_lower,
                    upn_lower,
                    upn_local_lower,
                });
            }
        }
    }

    // Helper lookup for AD accounts by account_id.
    let ad_accounts_by_id: IndexMap<&str, &BrownfieldSpokeAccountV52> = world
        .spoke_accounts
        .get("ad")
        .map(|accts| {
            accts
                .iter()
                .map(|a| (a.account_id.as_str(), a))
                .collect::<IndexMap<_, _>>()
        })
        .unwrap_or_default();

    let mut assigned_handles: HashSet<String> = HashSet::new();
    let mut assigned_upns: HashSet<String> = HashSet::new();
    let mut results = IndexMap::with_capacity(cubes.len());

    // Cubes are already sorted by ascending `person_key`.
    for (person_key, cube) in cubes {
        let upn_domain = policy
            .upn_domain_by_affiliation
            .get(&cube.primary_affiliation)
            .unwrap_or(&policy.default_upn_domain);

        // 1. Check brownfield identifier preservation on correlated AD account.
        if policy.preserve_correlated_identifiers
            && let Some(ad_acct_id) = correlation
                .person_to_spoke_primary
                .get(person_key)
                .and_then(|m| m.get("ad"))
            && let Some(ad_acct) = ad_accounts_by_id.get(ad_acct_id.as_str())
        {
            let existing_sam = ad_acct
                .attributes
                .get("sAMAccountName")
                .map(|s| s.trim())
                .unwrap_or("");
            let existing_upn = ad_acct
                .attributes
                .get("userPrincipalName")
                .map(|s| s.trim())
                .unwrap_or("");
            let domain_suffix = format!("@{upn_domain}");
            if !existing_sam.is_empty()
                && existing_upn
                    .to_ascii_lowercase()
                    .ends_with(&domain_suffix.to_ascii_lowercase())
            {
                assigned_handles.insert(existing_sam.to_ascii_lowercase());
                let upn_low = existing_upn.to_ascii_lowercase();
                assigned_handles.insert(upn_local_part(&upn_low));
                assigned_upns.insert(upn_low);
                results.insert(
                    person_key.clone(),
                    GeneratedUsername {
                        sam_account_name: existing_sam.to_owned(),
                        upn: existing_upn.to_owned(),
                        preserved_from_brownfield: true,
                    },
                );
                continue;
            }
        }

        // 2. Build forbidden handle set for this person.
        let mut forbidden_handles = static_forbidden_handles.clone();
        let mut forbidden_upns = static_forbidden_upns.clone();
        forbidden_handles.extend(assigned_handles.iter().cloned());
        forbidden_upns.extend(assigned_upns.iter().cloned());

        for entry in &dir_entries {
            if entry.owner == Some(person_key.as_str()) {
                continue;
            }
            let include = if entry.is_legal_hold {
                check_legal_hold || (entry.enabled && check_live)
            } else if entry.enabled {
                check_live
            } else {
                check_disabled
            };
            if include {
                if let Some(ref s) = entry.sam_lower {
                    forbidden_handles.insert(s.clone());
                }
                if let Some(ref l) = entry.upn_local_lower {
                    forbidden_handles.insert(l.clone());
                }
                if let Some(ref u) = entry.upn_lower {
                    forbidden_upns.insert(u.clone());
                }
            }
        }

        // 3. Build base handle strings.
        let preferred_first = cube
            .attributes
            .get("preferred_first_name")
            .map(String::as_str)
            .unwrap_or("")
            .trim();
        let legal_first = cube
            .attributes
            .get("legal_first_name")
            .map(String::as_str)
            .unwrap_or("")
            .trim();
        let first_raw = if policy.use_preferred_first_name && !preferred_first.is_empty() {
            preferred_first
        } else {
            legal_first
        };
        let middle_raw = cube
            .attributes
            .get("middle_name")
            .map(String::as_str)
            .unwrap_or("")
            .trim();
        let last_raw = cube
            .attributes
            .get("legal_last_name")
            .map(String::as_str)
            .unwrap_or("")
            .trim();

        let normalize = |raw: &str| -> String {
            let mut s = if policy.strip_diacritics {
                strip_diacritics(raw)
            } else {
                raw.to_owned()
            };
            if policy.lowercase {
                s = s.to_lowercase();
            }
            s
        };

        let first_norm = normalize(first_raw);
        let middle_norm = normalize(middle_raw);
        let last_norm = normalize(last_raw);

        let (sam_base, dot_upn_base) = match &policy.base_pattern {
            UsernameBasePatternV52::FirstInitialLast => {
                let f_init = first_ascii_alnum(&first_norm);
                let l_part = filter_sam_chars(&last_norm, &policy.allowed_sam_punctuation);
                (format!("{f_init}{l_part}"), None)
            }
            UsernameBasePatternV52::FirstDotLast => {
                let f_clean = filter_sam_chars(&first_norm, &policy.allowed_sam_punctuation);
                let l_clean = filter_sam_chars(&last_norm, &policy.allowed_sam_punctuation);
                let dotted = join_dot(&f_clean, &l_clean);
                let sam = filter_sam_chars(&dotted, &policy.allowed_sam_punctuation);
                (sam, Some(dotted))
            }
            UsernameBasePatternV52::FirstInitialMiddleInitialLast => {
                let f_init = first_ascii_alnum(&first_norm);
                let m_init = first_ascii_alnum(&middle_norm);
                let l_part = filter_sam_chars(&last_norm, &policy.allowed_sam_punctuation);
                (format!("{f_init}{m_init}{l_part}"), None)
            }
            UsernameBasePatternV52::CustomTransformRef { transform_name } => {
                let eval_ctx = TransformEvalContext {
                    transforms: &config.transforms,
                    declared_sources,
                    matched_records: &cube.matched_records,
                    cutover_date,
                };
                let evaluated = budget
                    .evaluate(
                        &TransformExprV52::RefTransform {
                            name: transform_name.clone(),
                        },
                        &eval_ctx,
                    )
                    .map_err(|err| {
                        TransformBudget::with_context(err, || {
                            format!("person '{person_key}' custom username transform error")
                        })
                    })?;
                let normed = normalize(&evaluated);
                (
                    filter_sam_chars(&normed, &policy.allowed_sam_punctuation),
                    None,
                )
            }
        };

        let sam_base = if sam_base.is_empty() {
            filter_sam_chars(&normalize(person_key), &policy.allowed_sam_punctuation)
        } else {
            sam_base
        };

        // 4. Truncate & resolve sAMAccountName collision.
        let max_len = policy.sam_account_name_max_len.max(1);
        let initial_sam = truncate_chars(&sam_base, max_len);
        let chosen_sam = if !forbidden_handles.contains(&initial_sam.to_ascii_lowercase()) {
            initial_sam
        } else {
            let mut counter = policy.collision_counter_start;
            loop {
                let suffix = counter.to_string();
                let candidate = if policy.reserve_len_for_collision_suffix {
                    let prefix_len = max_len.saturating_sub(suffix.len());
                    format!("{}{suffix}", truncate_chars(&sam_base, prefix_len))
                } else {
                    format!("{}{suffix}", truncate_chars(&sam_base, max_len))
                };
                if !forbidden_handles.contains(&candidate.to_ascii_lowercase()) {
                    break candidate;
                }
                counter = next_collision_counter(counter)?;
            }
        };

        // 5. Build UPN.
        let chosen_upn = if let Some(dotted_base) = dot_upn_base {
            let base_local = if dotted_base.is_empty() {
                chosen_sam.clone()
            } else {
                dotted_base
            };
            let initial_upn = format!("{base_local}@{upn_domain}");
            let initial_local_low = base_local.to_ascii_lowercase();
            let initial_upn_low = initial_upn.to_ascii_lowercase();
            if !forbidden_handles.contains(&initial_local_low)
                && !forbidden_upns.contains(&initial_upn_low)
            {
                initial_upn
            } else {
                let mut counter = policy.collision_counter_start;
                loop {
                    let cand_local = format!("{base_local}{counter}");
                    let cand_upn = format!("{cand_local}@{upn_domain}");
                    if !forbidden_handles.contains(&cand_local.to_ascii_lowercase())
                        && !forbidden_upns.contains(&cand_upn.to_ascii_lowercase())
                    {
                        break cand_upn;
                    }
                    counter = next_collision_counter(counter)?;
                }
            }
        } else {
            let initial_upn = format!("{chosen_sam}@{upn_domain}");
            if !forbidden_upns.contains(&initial_upn.to_ascii_lowercase()) {
                initial_upn
            } else {
                let mut counter = policy.collision_counter_start;
                loop {
                    let cand_upn = format!("{chosen_sam}{counter}@{upn_domain}");
                    if !forbidden_upns.contains(&cand_upn.to_ascii_lowercase()) {
                        break cand_upn;
                    }
                    counter = next_collision_counter(counter)?;
                }
            }
        };

        assigned_handles.insert(chosen_sam.to_ascii_lowercase());
        let chosen_upn_low = chosen_upn.to_ascii_lowercase();
        assigned_handles.insert(upn_local_part(&chosen_upn_low));
        assigned_upns.insert(chosen_upn_low);

        results.insert(
            person_key.clone(),
            GeneratedUsername {
                sam_account_name: chosen_sam,
                upn: chosen_upn,
                preserved_from_brownfield: false,
            },
        );
    }

    Ok(results)
}

fn first_ascii_alnum(s: &str) -> String {
    s.chars()
        .find(char::is_ascii_alphanumeric)
        .map(|c| c.to_string())
        .unwrap_or_default()
}

fn filter_sam_chars(s: &str, allowed_punct: &[char]) -> String {
    s.chars()
        .filter(|c| c.is_ascii_alphanumeric() || allowed_punct.contains(c))
        .collect()
}

fn join_dot(a: &str, b: &str) -> String {
    match (a.is_empty(), b.is_empty()) {
        (true, true) => String::new(),
        (true, false) => b.to_owned(),
        (false, true) => a.to_owned(),
        (false, false) => format!("{a}.{b}"),
    }
}

fn truncate_chars(s: &str, max_len: usize) -> String {
    s.chars().take(max_len).collect()
}

fn next_collision_counter(counter: u32) -> Result<u32, String> {
    counter
        .checked_add(1)
        .ok_or_else(|| "username collision counter exhausted".to_owned())
}

#[cfg(test)]
mod security_tests {
    #[test]
    fn collision_counter_exhaustion_returns_instead_of_looping() {
        assert_eq!(super::next_collision_counter(u32::MAX - 1), Ok(u32::MAX));
        assert!(super::next_collision_counter(u32::MAX).is_err());
    }
}
