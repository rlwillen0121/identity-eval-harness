//! Deterministic attribute-sync execution and STRICTLY anti-oracle go-live dry-run telemetry.
//!
//! IMPORTANT: Nothing in this module may read, reference, or infer from `SealedEpisodeOracleV51`,
//! `gold_config`, `day2_events`, `tombstone_archive`, `GardenLeave` policy rubrics, `SoD` rubrics,
//! or `golive_plan.ordered_phases`.

use std::collections::{BTreeSet, HashMap};

use ib_v51_schemas::{
    BrownfieldSpokeAccountV51, EnterpriseBrownfieldWorldV51, GoLiveDryRunResultV51,
    IgaDeploymentConfigV51,
};
use indexmap::IndexMap;

use crate::coalescence::{CoalescedIdentity, SpokeCorrelationState};
use crate::lifecycle::EvaluatedLifecycle;
use crate::transforms::affiliation_to_str;
use crate::username::GeneratedUsername;

/// Complete deterministic output of attribute sync & operational directory checks.
#[derive(Debug, Clone)]
pub struct AttributeSyncRunOutput {
    /// Sorted `spoke_id:account_id` -> resulting target attributes after attribute sync.
    pub synced_spoke_attributes: IndexMap<String, IndexMap<String, String>>,
    /// Every `spoke_id:account_id.target_attribute` where a populated attribute was overwritten with blank/null.
    pub null_overwrite_targets: Vec<String>,
    /// Every target directory/API schema validation error observed during sync & handle checks.
    pub schema_errors: Vec<String>,
    /// Live/disabled directory handle collisions against `world.spoke_accounts` (never tombstones).
    pub live_directory_handle_collisions: Vec<String>,
    /// Operational warnings (strictly non-oracle).
    pub operational_warnings: Vec<String>,
    /// Number of spoke accounts with >= 1 attribute modification.
    pub modified_account_count: usize,
    /// Total number of spoke accounts evaluated across all spokes.
    pub total_spoke_account_count: usize,
}

/// Simulate attribute sync against `world.spoke_accounts` and collect operational diagnostics.
#[must_use]
pub fn execute_attribute_sync(
    world: &EnterpriseBrownfieldWorldV51,
    config: &IgaDeploymentConfigV51,
    cubes: &IndexMap<String, CoalescedIdentity<'_>>,
    lifecycles: &IndexMap<String, EvaluatedLifecycle>,
    usernames: &IndexMap<String, GeneratedUsername>,
    correlation: &SpokeCorrelationState,
) -> AttributeSyncRunOutput {
    let mut synced_spoke_attributes: IndexMap<String, IndexMap<String, String>> = IndexMap::new();
    let mut null_overwrite_targets: Vec<String> = Vec::new();
    let mut schema_errors: Vec<String> = Vec::new();
    let mut operational_warnings: Vec<String> = Vec::new();
    let mut modified_accounts: BTreeSet<String> = BTreeSet::new();
    let mut total_spoke_account_count: usize = 0;

    // Build person_key -> AD distinguishedName lookup for `resolve_manager_to_ad_dn`.
    let ad_accounts_by_id: HashMap<&str, &BrownfieldSpokeAccountV51> = world
        .spoke_accounts
        .get("ad")
        .map(|accts| accts.iter().map(|a| (a.account_id.as_str(), a)).collect())
        .unwrap_or_default();

    let mut person_to_ad_dn: HashMap<&str, &str> = HashMap::new();
    for (person_key, spoke_map) in &correlation.person_to_spoke_primary {
        if let Some(ad_acct_id) = spoke_map.get("ad")
            && let Some(ad_acct) = ad_accounts_by_id.get(ad_acct_id.as_str())
            && !ad_acct.native_dn_or_key.trim().is_empty()
        {
            person_to_ad_dn.insert(person_key.as_str(), ad_acct.native_dn_or_key.as_str());
        }
    }

    let mut spoke_ids: Vec<&String> = world.spoke_accounts.keys().collect();
    spoke_ids.sort();

    for spoke_id in spoke_ids {
        let accounts = &world.spoke_accounts[spoke_id];
        for acct in accounts {
            total_spoke_account_count += 1;
            let acct_key = format!("{spoke_id}:{}", acct.account_id);
            let owner = correlation
                .account_to_person
                .get(&(spoke_id.clone(), acct.account_id.clone()));

            if let Some(person_key) = owner {
                let cube = &cubes[person_key];
                let lc = &lifecycles[person_key];
                let uname = &usernames[person_key];
                let mut synced_attrs = acct.attributes.clone();

                for mapping in &config.attribute_sync.mappings {
                    if mapping.spoke_id != *spoke_id {
                        continue;
                    }
                    if !mapping.sync_only_in_states.contains(&lc.lifecycle_state) {
                        continue;
                    }

                    let raw_val = match mapping.source_identity_attribute.as_str() {
                        "sam_account_name" => uname.sam_account_name.as_str(),
                        "upn" => uname.upn.as_str(),
                        "primary_affiliation" => affiliation_to_str(&cube.primary_affiliation),
                        "person_key" => cube.person_key.as_str(),
                        other => cube.attributes.get(other).map(String::as_str).unwrap_or(""),
                    };

                    let value_to_write: String = if mapping.resolve_manager_to_ad_dn {
                        let mgr_key = raw_val.trim();
                        if mgr_key.is_empty() {
                            String::new()
                        } else {
                            person_to_ad_dn
                                .get(mgr_key)
                                .copied()
                                .unwrap_or("")
                                .to_owned()
                        }
                    } else {
                        raw_val.to_owned()
                    };

                    let existing_val = acct
                        .attributes
                        .get(&mapping.target_attribute)
                        .map(String::as_str)
                        .unwrap_or("");

                    if value_to_write.trim().is_empty() {
                        if mapping.skip_if_null_or_blank {
                            continue;
                        }
                        synced_attrs.insert(mapping.target_attribute.clone(), String::new());
                        if !existing_val.trim().is_empty() {
                            null_overwrite_targets
                                .push(format!("{acct_key}.{}", mapping.target_attribute));
                            modified_accounts.insert(acct_key.clone());
                        }
                    } else {
                        // Validate target directory/API schema constraints.
                        validate_target_attribute_schema(
                            spoke_id,
                            &acct_key,
                            mapping,
                            &value_to_write,
                            &mut schema_errors,
                        );
                        if existing_val != value_to_write {
                            modified_accounts.insert(acct_key.clone());
                        }
                        synced_attrs.insert(mapping.target_attribute.clone(), value_to_write);
                    }
                }

                synced_spoke_attributes.insert(acct_key, synced_attrs);
            } else if correlation
                .exempt_service_account_set
                .contains(&(spoke_id.clone(), acct.account_id.clone()))
                && !config.attribute_sync.exclude_service_accounts
            {
                // Uncorrelated service account included in attribute sync when exclude_service_accounts == false!
                for mapping in &config.attribute_sync.mappings {
                    if mapping.spoke_id != *spoke_id {
                        continue;
                    }
                    let existing_val = acct
                        .attributes
                        .get(&mapping.target_attribute)
                        .map(String::as_str)
                        .unwrap_or("");
                    if !mapping.skip_if_null_or_blank && !existing_val.trim().is_empty() {
                        null_overwrite_targets
                            .push(format!("{acct_key}.{}", mapping.target_attribute));
                        modified_accounts.insert(acct_key.clone());
                    }
                }
            }
        }
    }

    if !config.attribute_sync.exclude_service_accounts
        && !correlation.exempt_service_accounts.is_empty()
    {
        operational_warnings.push(
            "attribute_sync.exclude_service_accounts is false; exempt service accounts are exposed to sync"
                .to_owned(),
        );
    }

    // Check AD sAMAccountName 20-char schema limit across all evaluated identities.
    for (person_key, uname) in usernames {
        if uname.sam_account_name.chars().count() > 20 {
            schema_errors.push(format!(
                "ad:{person_key}.sAMAccountName '{}' exceeds 20-character AD schema limit (len={})",
                uname.sam_account_name,
                uname.sam_account_name.chars().count()
            ));
        }
    }

    // Check live & disabled directory handle collisions (strictly against `world.spoke_accounts` only).
    let live_directory_handle_collisions =
        detect_live_directory_collisions(world, usernames, correlation);

    if !correlation.uncorrelated_non_exempt_accounts.is_empty() {
        operational_warnings.push(format!(
            "{} uncorrelated spoke account(s) would be targeted for disablement",
            correlation.uncorrelated_non_exempt_accounts.len()
        ));
    }
    if !null_overwrite_targets.is_empty() {
        operational_warnings.push(format!(
            "{} populated target attribute(s) would be overwritten with null/blank values",
            null_overwrite_targets.len()
        ));
    }
    if !schema_errors.is_empty() {
        operational_warnings.push(format!(
            "{} target schema validation error(s) detected in dry-run",
            schema_errors.len()
        ));
    }
    if !live_directory_handle_collisions.is_empty() {
        operational_warnings.push(format!(
            "{} sAMAccountName collision(s) detected against existing directory accounts",
            live_directory_handle_collisions.len()
        ));
    }

    AttributeSyncRunOutput {
        synced_spoke_attributes,
        null_overwrite_targets,
        schema_errors,
        live_directory_handle_collisions,
        operational_warnings,
        modified_account_count: modified_accounts.len(),
        total_spoke_account_count,
    }
}

fn validate_target_attribute_schema(
    spoke_id: &str,
    acct_key: &str,
    mapping: &ib_v51_schemas::AttributeSyncMappingV51,
    value: &str,
    schema_errors: &mut Vec<String>,
) {
    let is_iso_c_check =
        mapping.require_iso_country_2 || (spoke_id == "ad" && mapping.target_attribute == "c");
    if is_iso_c_check {
        let chars: Vec<char> = value.chars().collect();
        let valid_iso2 = chars.len() == 2 && chars.iter().all(char::is_ascii_uppercase);
        if !valid_iso2 {
            schema_errors.push(format!(
                "{acct_key}.{}: expected 2-character uppercase ISO-3166-1 alpha-2 code, got '{value}'",
                mapping.target_attribute
            ));
        }
    }

    if let Some(max_len) = mapping.max_length
        && value.chars().count() > max_len
    {
        schema_errors.push(format!(
            "{acct_key}.{}: value '{}' exceeds configured max_length {max_len}",
            mapping.target_attribute, value
        ));
    }

    if spoke_id == "ad"
        && mapping.target_attribute == "sAMAccountName"
        && value.chars().count() > 20
    {
        schema_errors.push(format!(
            "{acct_key}.sAMAccountName: value '{value}' exceeds 20-character AD limit"
        ));
    }

    if spoke_id == "ad" && mapping.target_attribute == "manager" {
        let looks_like_dn = value.contains('=') && value.contains(',');
        if !mapping.resolve_manager_to_ad_dn || !looks_like_dn {
            schema_errors.push(format!(
                "{acct_key}.manager: AD manager attribute requires an LDAP DN (CN=...,OU=...), got '{value}'"
            ));
        }
    }
}

/// Detect `sAMAccountName` collisions against live/disabled AD & Entra directory objects
/// or duplicate assignments across canonical identities. Never inspects tombstones or oracles.
#[must_use]
pub fn detect_live_directory_collisions(
    world: &EnterpriseBrownfieldWorldV51,
    usernames: &IndexMap<String, GeneratedUsername>,
    correlation: &SpokeCorrelationState,
) -> Vec<String> {
    let mut collisions = Vec::new();
    let mut seen_by_sam: HashMap<String, String> = HashMap::new();

    // Collect existing directory sAMAccountNames -> owner (if any) and account key.
    let mut existing_dir_sams: HashMap<String, Vec<(String, Option<String>)>> = HashMap::new();
    for spoke_id in ["ad", "entra"] {
        if let Some(accounts) = world.spoke_accounts.get(spoke_id) {
            for acct in accounts {
                if let Some(sam) = acct
                    .attributes
                    .get("sAMAccountName")
                    .map(|s| s.trim().to_ascii_lowercase())
                    .filter(|s| !s.is_empty())
                {
                    let owner = correlation
                        .account_to_person
                        .get(&(spoke_id.to_owned(), acct.account_id.clone()))
                        .cloned();
                    existing_dir_sams
                        .entry(sam)
                        .or_default()
                        .push((format!("{spoke_id}:{}", acct.account_id), owner));
                }
            }
        }
    }

    for (person_key, uname) in usernames {
        let sam_low = uname.sam_account_name.trim().to_ascii_lowercase();
        if sam_low.is_empty() {
            continue;
        }
        if let Some(prev_person) = seen_by_sam.get(&sam_low) {
            collisions.push(format!(
                "{person_key}: sAMAccountName '{}' duplicates assignment for {prev_person}",
                uname.sam_account_name
            ));
        } else {
            seen_by_sam.insert(sam_low.clone(), person_key.clone());
        }

        if let Some(holders) = existing_dir_sams.get(&sam_low) {
            for (acct_key, owner) in holders {
                if owner.as_deref() != Some(person_key.as_str()) {
                    collisions.push(format!(
                        "{person_key}: sAMAccountName '{}' collides with existing directory account {acct_key}",
                        uname.sam_account_name
                    ));
                    break;
                }
            }
        }
    }

    collisions
}

/// Construct the strictly anti-oracle `GoLiveDryRunResultV51` payload.
#[must_use]
pub fn build_dry_run_result(
    total_identities_evaluated: usize,
    config: &IgaDeploymentConfigV51,
    correlation: &SpokeCorrelationState,
    sync: &AttributeSyncRunOutput,
) -> GoLiveDryRunResultV51 {
    let correlated_count = correlation.account_to_person.len();
    let uncorrelated_count = correlation.exempt_service_accounts.len()
        + correlation.uncorrelated_non_exempt_accounts.len();

    let attribute_modify_ratio_bps = if sync.total_spoke_account_count == 0 {
        0
    } else {
        ((sync.modified_account_count as u64 * 10_000) / sync.total_spoke_account_count as u64)
            as u32
    };

    let gates = &config.golive_plan.safety_gates;
    let would_abort_on_configured_gates = sync.null_overwrite_targets.len()
        > config.attribute_sync.max_allowed_null_overwrites
        || sync.null_overwrite_targets.len() > gates.abort_if_null_attribute_overwrites_gt
        || correlation.uncorrelated_non_exempt_accounts.len()
            > gates.abort_if_uncorrelated_human_disable_count_gt
        || sync.schema_errors.len() > gates.abort_if_schema_validation_errors_gt
        || attribute_modify_ratio_bps > config.attribute_sync.max_modify_blast_radius_bps;

    GoLiveDryRunResultV51 {
        total_identities_evaluated,
        correlated_spoke_account_count: correlated_count,
        uncorrelated_spoke_account_count: uncorrelated_count,
        classified_service_account_count: correlation.exempt_service_accounts.len(),
        uncorrelated_accounts_targeted_for_disable: correlation
            .uncorrelated_non_exempt_accounts
            .clone(),
        null_attribute_overwrite_count: sync.null_overwrite_targets.len(),
        sample_null_overwrite_targets: sync
            .null_overwrite_targets
            .iter()
            .take(5)
            .cloned()
            .collect(),
        schema_validation_error_count: sync.schema_errors.len(),
        sample_schema_errors: sync.schema_errors.iter().take(5).cloned().collect(),
        live_directory_handle_collisions: sync
            .live_directory_handle_collisions
            .iter()
            .take(5)
            .cloned()
            .collect(),
        attribute_modify_ratio_bps,
        would_abort_on_configured_gates,
        operational_warnings: sync.operational_warnings.clone(),
    }
}
