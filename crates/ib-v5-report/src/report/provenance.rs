//! Strict offline artifact provenance verification and raw trajectory regrading.
//!
//! Ports the artifact loading, validation, and provenance sections of
//! `src/identitybench/v5/report.py`.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use ib_core::hashing::sha256_hex;
use ib_core::jcs_dumps;
use ib_v5_generator::{LoadedV5Dataset, load_v5_dataset};
use ib_v5_schemas::{
    ContextLimitCauseV5, DescriptiveUsageSummaryV5, EpisodeAttemptV5,
    HistoricalComparatorManifestV5, ProviderRequestUsageV5, RegimeIdV5, RunConfigurationV5,
    StrictV5Model,
};
use ib_v5_scoring::{
    ATTEMPT_STATUSES_V5, AggregateRunOptionsV5, BuildEpisodeAttemptOptionsV5, COMPARISON_TRACKS_V5,
    EpisodeScoreV5, PLANNED_REPEATS_MODEL_V5, PricingRatesV5, RunCalibrationMetadataV5,
    RunScoreSummaryV5, RunScoringContextV5, ScoreExecutionOptionsV5, VerifiedUsageAccountingV5,
    aggregate_run_scores, build_episode_attempt_v5, episode_score_from_mapping,
    episode_scoring_input_from_mapping, episode_scoring_input_to_mapping, score_execution_raw_v5,
    verify_usage_accounting_artifacts_v5,
};
use serde_json::{Map, Number, Value};

use crate::error::ReportError;
use crate::report::json_util::{
    as_strict_i64, is_sha256_str, is_sha256_value, parse_json_strict, read_bytes_optional,
    safe_artifact_file,
};

const PRICING_RATE_KEYS: [&str; 4] = [
    "uncached_input_micros_per_million",
    "cached_input_micros_per_million",
    "output_micros_per_million",
    "reasoning_micros_per_million",
];

const INCOMPLETE_METRIC_KEYS: [&str; 33] = [
    "G-SRR",
    "Sem-SRR@commit",
    "Action-SRR@last_stage",
    "Action-SRR@last_stage | both_phases_staged",
    "P1-Action-SRR@last_stage | p1_staged",
    "G-SRR@first_stage",
    "Delta_repair",
    "G-PROP",
    "G-PROP_reset",
    "p1_staged_rate",
    "p2_observed_rate",
    "both_phases_staged_rate",
    "OVERGRANT@commit_mean",
    "AVAIL_BREACH@commit_mean",
    "OVERGRANT@commit | any_phase_committed",
    "AVAIL_BREACH@commit | any_phase_committed",
    "OVERGRANT@last_stage_mean",
    "AVAIL_BREACH@last_stage_mean",
    "OVERGRANT@last_stage | any_phase_staged",
    "AVAIL_BREACH@last_stage | any_phase_staged",
    "OVERGRANT@last_stage | both_phases_staged",
    "AVAIL_BREACH@last_stage | both_phases_staged",
    "OVERGRANT_count",
    "AVAIL_BREACH_count",
    "OVERGRANT_observed_commit_count",
    "AVAIL_BREACH_observed_commit_count",
    "safety_valid_mutation_count",
    "safety_valid_target_count",
    "safety_stage_attempt_count",
    "safety_attempt_stream_complete_rate",
    "OVERGRANT_commit_count",
    "AVAIL_BREACH_commit_count",
    "safety_valid_commit_target_count",
];

/// Cryptographic and verification provenance for a v5 run report.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArtifactProvenanceV5 {
    pub run_manifest_sha256: String,
    pub episodes_sha256: String,
    pub comparator_manifest_sha256: Option<String>,
    pub pricing_manifest_sha256: Option<String>,
    pub dataset_seal_sha256: Option<String>,
    pub episode_record_count: usize,
    pub scores_recomputed_from_scoring_inputs: bool,
    pub scoring_evidence_status: String,
    pub comparator_evidence_verified: bool,
    pub pricing_artifact_sha256: Option<String>,
    pub usage_accounting_manifest_sha256: Option<String>,
    pub usage_accounting_evidence_verified: bool,
}

impl ArtifactProvenanceV5 {
    /// Construct provenance with the standard verified defaults.
    #[must_use]
    pub fn verified(
        run_manifest_sha256: impl Into<String>,
        episodes_sha256: impl Into<String>,
        comparator_manifest_sha256: Option<String>,
        pricing_manifest_sha256: Option<String>,
        dataset_seal_sha256: Option<String>,
        episode_record_count: usize,
    ) -> Self {
        Self {
            run_manifest_sha256: run_manifest_sha256.into(),
            episodes_sha256: episodes_sha256.into(),
            comparator_manifest_sha256,
            pricing_manifest_sha256,
            dataset_seal_sha256,
            episode_record_count,
            scores_recomputed_from_scoring_inputs: true,
            scoring_evidence_status: "VERIFIED".to_owned(),
            comparator_evidence_verified: false,
            pricing_artifact_sha256: None,
            usage_accounting_manifest_sha256: None,
            usage_accounting_evidence_verified: false,
        }
    }
}

/// Verified single-run report bundle combining aggregated scores and provenance.
#[derive(Debug, Clone, PartialEq)]
pub struct V5ReportArtifact {
    pub summary: RunScoreSummaryV5,
    pub provenance: ArtifactProvenanceV5,
}

/// Optional external artifact paths for [`report_from_raw_artifacts_with_options`].
#[derive(Debug, Clone, Default)]
pub struct ReportOptionsV5<'a> {
    pub comparator_manifest_path: Option<&'a Path>,
    pub pricing_manifest_path: Option<&'a Path>,
    pub dataset_seal_path: Option<&'a Path>,
}

/// Strictly parse an optional descriptive `HistoricalComparatorManifestV5`.
///
/// Historical v4.1 comparators provide descriptive context only and never gate
/// v5 capability, completion, or spend acceptance.
pub fn validate_comparator_manifest(
    raw_json: &[u8],
    expected_pricing_manifest_sha256: Option<&str>,
) -> Result<HistoricalComparatorManifestV5, ReportError> {
    if let Some(expected) = expected_pricing_manifest_sha256
        && !is_sha256_str(expected)
    {
        return Err(ReportError::value(
            "expected pricing manifest digest must be a lowercase SHA-256",
        ));
    }
    let decoded = parse_json_strict(raw_json, "HistoricalComparatorManifestV5")?;
    if !decoded.is_object() {
        return Err(ReportError::value(
            "HistoricalComparatorManifestV5 must be a JSON object",
        ));
    }
    let text = std::str::from_utf8(raw_json).map_err(|_| {
        ReportError::value("HistoricalComparatorManifestV5 is not valid UTF-8 JSON")
    })?;
    let manifest = HistoricalComparatorManifestV5::from_strict_json_str(text).map_err(|_| {
        ReportError::value("HistoricalComparatorManifestV5 failed strict schema validation")
    })?;
    if !is_sha256_str(&manifest.source_spec_sha256) {
        return Err(ReportError::value(
            "HistoricalComparatorManifestV5 source_spec_sha256 must be SHA-256",
        ));
    }
    if let Some(ref pricing_sha) = manifest.pricing_manifest_sha256 {
        if !is_sha256_str(pricing_sha) {
            return Err(ReportError::value(
                "HistoricalComparatorManifestV5 pricing_manifest_sha256 must be SHA-256",
            ));
        }
        if let Some(expected) = expected_pricing_manifest_sha256
            && pricing_sha != expected
        {
            return Err(ReportError::value(
                "HistoricalComparatorManifestV5 pricing digest disagrees with run pricing digest",
            ));
        }
    }
    for entry in &manifest.entries {
        if !is_sha256_str(&entry.v41_run_manifest_sha256) {
            return Err(ReportError::value(format!(
                "invalid historical manifest digest for {:?}",
                entry.model_id
            )));
        }
    }
    Ok(manifest)
}

fn strict_pricing_rates(
    pricing_json: &[u8],
    expected_pricing_manifest_sha256: &str,
) -> Result<(PricingRatesV5, String), ReportError> {
    let actual_sha = sha256_hex(pricing_json);
    if actual_sha != expected_pricing_manifest_sha256 {
        return Err(ReportError::value(
            "frozen pricing artifact SHA-256 does not match the run digest",
        ));
    }
    let decoded = parse_json_strict(pricing_json, "frozen pricing artifact")?;
    let Some(obj) = decoded.as_object() else {
        return Err(ReportError::value(
            "frozen pricing artifact must contain exactly the four retail rates",
        ));
    };
    let expected_keys: BTreeSet<&str> = PRICING_RATE_KEYS.into_iter().collect();
    let actual_keys: BTreeSet<&str> = obj.keys().map(String::as_str).collect();
    if actual_keys != expected_keys {
        return Err(ReportError::value(
            "frozen pricing artifact must contain exactly the four retail rates",
        ));
    }
    let mut parsed = [0_i64; 4];
    for (idx, key) in PRICING_RATE_KEYS.iter().enumerate() {
        let val = as_strict_i64(&obj[*key])
            .filter(|&n| n >= 0)
            .ok_or_else(|| {
                ReportError::value(format!(
                    "frozen pricing rate {key} must be a nonnegative integer"
                ))
            })?;
        parsed[idx] = val;
    }
    let rates = PricingRatesV5 {
        uncached_input_micros_per_million: parsed[0],
        cached_input_micros_per_million: parsed[1],
        output_micros_per_million: parsed[2],
        reasoning_micros_per_million: parsed[3],
    };
    Ok((rates, actual_sha))
}

fn parse_opt_bool_field(
    obj: &Map<String, Value>,
    field: &str,
) -> Result<Option<bool>, ReportError> {
    match obj.get(field) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Bool(b)) => Ok(Some(*b)),
        Some(_) => Err(ReportError::value(format!(
            "calibration_metadata.{field} must be a bool"
        ))),
    }
}

fn parse_bool_field_default_false(
    obj: &Map<String, Value>,
    field: &str,
) -> Result<bool, ReportError> {
    match obj.get(field) {
        None | Some(Value::Null) => Ok(false),
        Some(Value::Bool(b)) => Ok(*b),
        Some(_) => Err(ReportError::value(format!(
            "calibration_metadata.{field} must be a bool"
        ))),
    }
}

fn parse_calibration_metadata(
    raw: Option<&Value>,
) -> Result<Option<RunCalibrationMetadataV5>, ReportError> {
    let Some(raw) = raw.filter(|v| !v.is_null()) else {
        return Ok(None);
    };
    let Some(obj) = raw.as_object() else {
        return Err(ReportError::value(
            "calibration_metadata must be a JSON object when present",
        ));
    };
    let mut meta = RunCalibrationMetadataV5::default();
    if obj.contains_key("preflight_completed_before_freeze") {
        meta.preflight_completed_before_freeze =
            parse_opt_bool_field(obj, "preflight_completed_before_freeze")?;
    }
    if obj.contains_key("dev_cap_selected_before_held_out") {
        meta.dev_cap_selected_before_held_out =
            parse_opt_bool_field(obj, "dev_cap_selected_before_held_out")?;
    }
    if obj.contains_key("hypotheses_registered") {
        meta.hypotheses_registered = parse_opt_bool_field(obj, "hypotheses_registered")?;
    }
    if obj.contains_key("confirmatory_families_frozen") {
        meta.confirmatory_families_frozen =
            parse_opt_bool_field(obj, "confirmatory_families_frozen")?;
    }
    if let Some(v) = parse_opt_bool_field(obj, "no_score_target_gating")? {
        meta.no_score_target_gating = Some(v);
    }
    if let Some(v) = parse_opt_bool_field(obj, "no_dropped_incomplete_slots")? {
        meta.no_dropped_incomplete_slots = Some(v);
    }
    if obj.contains_key("effort_is_highest_valid") {
        meta.effort_is_highest_valid = parse_opt_bool_field(obj, "effort_is_highest_valid")?;
    }
    if obj.contains_key("budget_calculation_documented") {
        meta.budget_calculation_documented =
            parse_opt_bool_field(obj, "budget_calculation_documented")?;
    }
    if obj.contains_key("preregistered_track_disclosed") {
        meta.preregistered_track_disclosed =
            parse_opt_bool_field(obj, "preregistered_track_disclosed")?;
    }
    if obj.contains_key("topology_reviewed_and_frozen") {
        meta.topology_reviewed_and_frozen =
            parse_opt_bool_field(obj, "topology_reviewed_and_frozen")?;
    }
    if obj.contains_key("matched_role_configs_disclosed") {
        meta.matched_role_configs_disclosed =
            parse_opt_bool_field(obj, "matched_role_configs_disclosed")?;
    }
    if obj.contains_key("auditor_checker_disclosed") {
        meta.auditor_checker_disclosed = parse_opt_bool_field(obj, "auditor_checker_disclosed")?;
    }
    if obj.contains_key("shared_cap_disclosed") {
        meta.shared_cap_disclosed = parse_opt_bool_field(obj, "shared_cap_disclosed")?;
    }
    if obj.contains_key("contrast_multiplicity_disclosed") {
        meta.contrast_multiplicity_disclosed =
            parse_opt_bool_field(obj, "contrast_multiplicity_disclosed")?;
    }
    if obj.contains_key("policy_delivery_complete") {
        meta.policy_delivery_complete = parse_opt_bool_field(obj, "policy_delivery_complete")?;
    }
    if obj.contains_key("policy_card_truncated") {
        meta.policy_card_truncated = parse_bool_field_default_false(obj, "policy_card_truncated")?;
    }
    if obj.contains_key("compaction_applied") {
        meta.compaction_applied = parse_bool_field_default_false(obj, "compaction_applied")?;
    }
    if obj.contains_key("context_compatibility_disclosed") {
        meta.context_compatibility_disclosed =
            parse_opt_bool_field(obj, "context_compatibility_disclosed")?;
    }
    if obj.contains_key("context_headroom_violated") {
        meta.context_headroom_violated =
            parse_bool_field_default_false(obj, "context_headroom_violated")?;
    }
    if obj.contains_key("dev_prefix_selection_evidence") {
        meta.dev_prefix_selection_evidence =
            parse_opt_bool_field(obj, "dev_prefix_selection_evidence")?;
    }
    if obj.contains_key("counterfactual_noninterference_passed") {
        meta.counterfactual_noninterference_passed =
            parse_opt_bool_field(obj, "counterfactual_noninterference_passed")?;
    }
    Ok(Some(meta))
}

fn first_present<'a>(obj: &'a Map<String, Value>, keys: &[&str]) -> Option<&'a Value> {
    for key in keys {
        if let Some(val) = obj.get(*key) {
            return Some(val);
        }
    }
    None
}

fn manifest_context(
    raw_manifest: &Map<String, Value>,
    suite_id: &str,
) -> Result<RunScoringContextV5, ReportError> {
    let config_obj = match raw_manifest
        .get("config")
        .or_else(|| raw_manifest.get("run_config"))
    {
        Some(v) => v
            .as_object()
            .ok_or_else(|| ReportError::value("run manifest config must be an object"))?,
        None => raw_manifest,
    };

    let model_id = config_obj.get("model_id").and_then(Value::as_str);
    let effort = first_present(
        config_obj,
        &["reasoning_effort", "reasoning_effort_label", "effort"],
    )
    .and_then(Value::as_str);
    let topology = first_present(config_obj, &["topology", "topology_id"]).and_then(Value::as_str);
    let steering =
        first_present(config_obj, &["steering_mode", "steering"]).and_then(Value::as_str);

    let mut missing = Vec::new();
    if model_id.is_none() {
        missing.push("model_id");
    }
    if effort.is_none() {
        missing.push("reasoning_effort");
    }
    if topology.is_none() {
        missing.push("topology");
    }
    if steering.is_none() {
        missing.push("steering_mode");
    }
    if !missing.is_empty() {
        return Err(ReportError::value(format!(
            "run manifest lacks scoring config fields: {}",
            missing.join(", ")
        )));
    }
    let model_id = model_id.expect("checked Some").to_owned();
    let effort = effort.expect("checked Some").to_owned();
    let topology = topology.expect("checked Some").to_owned();
    let steering = steering.expect("checked Some").to_owned();

    let manifest_suite = raw_manifest
        .get("suite_id")
        .or_else(|| raw_manifest.get("suite"))
        .or_else(|| config_obj.get("suite_id"));
    if let Some(ms) = manifest_suite.filter(|v| !v.is_null())
        && ms.as_str() != Some(suite_id)
    {
        return Err(ReportError::value(
            "run manifest suite_id disagrees with the episode score rows",
        ));
    }

    let pricing_sha = match raw_manifest
        .get("pricing_manifest_sha256")
        .or_else(|| config_obj.get("pricing_manifest_sha256"))
        .filter(|v| !v.is_null())
    {
        Some(v) => {
            let s = v.as_str().filter(|s| is_sha256_str(s)).ok_or_else(|| {
                ReportError::value("run manifest pricing digest must be lowercase SHA-256")
            })?;
            Some(s.to_owned())
        }
        None => None,
    };

    let model_tier = match config_obj.get("model_tier").filter(|v| !v.is_null()) {
        None => None,
        Some(v) => match v.as_str() {
            Some("frontier" | "fast") => Some(v.as_str().expect("checked").to_owned()),
            _ => {
                return Err(ReportError::value(
                    "model_tier must be null, 'frontier', or 'fast'",
                ));
            }
        },
    };

    let baseline_id = match config_obj.get("baseline_id").filter(|v| !v.is_null()) {
        None => None,
        Some(v) => Some(
            v.as_str()
                .ok_or_else(|| ReportError::value("baseline_id must be a string when present"))?
                .to_owned(),
        ),
    };

    let wave_execution_mode = match raw_manifest
        .get("wave_execution_mode")
        .or_else(|| config_obj.get("wave_execution_mode"))
        .or_else(|| config_obj.get("coupling_mode"))
        .filter(|v| !v.is_null())
    {
        None => None,
        Some(v) => Some(
            v.as_str()
                .ok_or_else(|| {
                    ReportError::value("wave_execution_mode must be a string when present")
                })?
                .to_owned(),
        ),
    };

    let run_config_sha = match raw_manifest
        .get("run_config_sha256")
        .or_else(|| config_obj.get("run_config_sha256"))
        .filter(|v| !v.is_null())
    {
        None => None,
        Some(v) => {
            let s = v
                .as_str()
                .filter(|s| is_sha256_str(s))
                .ok_or_else(|| ReportError::value("run_config_sha256 must be lowercase SHA-256"))?;
            Some(s.to_owned())
        }
    };

    let default_repeats: i64 = if baseline_id.is_some() {
        1
    } else {
        PLANNED_REPEATS_MODEL_V5 as i64
    };
    let planned_repeats = match raw_manifest
        .get("planned_repeats_per_episode")
        .or_else(|| config_obj.get("planned_repeats_per_episode"))
    {
        None => default_repeats,
        Some(v) => as_strict_i64(v).ok_or_else(|| {
            ReportError::value("planned_repeats_per_episode must be 1 (baselines) or 3 (models)")
        })?,
    };
    if !matches!(planned_repeats, 1 | 3) {
        return Err(ReportError::value(
            "planned_repeats_per_episode must be 1 (baselines) or 3 (models)",
        ));
    }

    let comparison_track = match raw_manifest
        .get("comparison_track")
        .or_else(|| config_obj.get("comparison_track"))
        .or_else(|| config_obj.get("effort_track"))
    {
        None => "PRIMARY_HIGHEST".to_owned(),
        Some(v) => {
            let s = v
                .as_str()
                .ok_or_else(|| ReportError::value(format!("unsupported comparison_track: {v}")))?;
            if !COMPARISON_TRACKS_V5.contains(&s) {
                return Err(ReportError::value(format!(
                    "unsupported comparison_track: {s:?}"
                )));
            }
            s.to_owned()
        }
    };

    let topology_profile_sha = match raw_manifest
        .get("topology_profile_sha256")
        .or_else(|| config_obj.get("topology_profile_sha256"))
        .filter(|v| !v.is_null())
    {
        None => None,
        Some(v) => {
            let s = v.as_str().filter(|s| is_sha256_str(s)).ok_or_else(|| {
                ReportError::value("topology_profile_sha256 must be lowercase SHA-256")
            })?;
            Some(s.to_owned())
        }
    };

    let primary_prompt_sha = match raw_manifest
        .get("primary_prompt_bundle_sha256")
        .or_else(|| config_obj.get("primary_prompt_bundle_sha256"))
        .filter(|v| !v.is_null())
    {
        None => None,
        Some(v) => {
            let s = v.as_str().filter(|s| is_sha256_str(s)).ok_or_else(|| {
                ReportError::value("primary_prompt_bundle_sha256 must be lowercase SHA-256")
            })?;
            Some(s.to_owned())
        }
    };

    let max_generations = match raw_manifest
        .get("max_generations")
        .or_else(|| config_obj.get("max_assistant_generations_per_episode"))
        .or_else(|| config_obj.get("max_generations"))
        .filter(|v| !v.is_null())
    {
        None => None,
        Some(v) => {
            let n = as_strict_i64(v)
                .filter(|n| matches!(*n, 50 | 100))
                .ok_or_else(|| ReportError::value("max_generations must be 50, 100, or None"))?;
            Some(n)
        }
    };

    let cap_disclosed = match raw_manifest
        .get("cap_disclosed")
        .or_else(|| config_obj.get("cap_disclosed"))
    {
        None => false,
        Some(Value::Bool(b)) => *b,
        Some(_) => return Err(ReportError::value("cap_disclosed must be a bool")),
    };

    let calibration_meta = parse_calibration_metadata(
        raw_manifest
            .get("calibration_metadata")
            .or_else(|| config_obj.get("calibration_metadata")),
    )?;

    Ok(RunScoringContextV5 {
        suite_id: suite_id.to_owned(),
        model_id,
        reasoning_effort: effort,
        topology,
        steering_mode: steering,
        pricing_manifest_sha256: pricing_sha,
        model_tier,
        baseline_id,
        wave_execution_mode,
        run_config_sha256: run_config_sha,
        planned_repeats_per_episode: usize::try_from(planned_repeats).expect("1 or 3"),
        comparison_track,
        topology_profile_sha256: topology_profile_sha,
        primary_prompt_bundle_sha256: primary_prompt_sha,
        max_generations,
        cap_disclosed,
        expected_episode_grid: None,
        calibration_metadata: calibration_meta,
    })
}

fn validate_run_config_hash(
    raw_manifest: &Map<String, Value>,
    run_config: &Map<String, Value>,
) -> Result<String, ReportError> {
    let declared = raw_manifest
        .get("run_config_sha256")
        .filter(|v| !v.is_null());
    let nested_declared = run_config.get("run_config_sha256").filter(|v| !v.is_null());
    let effective = match (declared, nested_declared) {
        (None, Some(n)) => Some(n),
        (Some(d), Some(n)) if d != n => {
            return Err(ReportError::value(
                "run-config digest declarations disagree",
            ));
        }
        (Some(d), _) => Some(d),
        (None, None) => None,
    };
    let declared_str = effective
        .and_then(Value::as_str)
        .filter(|s| is_sha256_str(s))
        .ok_or_else(|| {
            ReportError::value("run manifest must declare a lowercase run_config_sha256")
        })?;
    let canonical_bytes = jcs_dumps(&Value::Object(run_config.clone()))?;
    let actual = sha256_hex(&canonical_bytes);
    if actual != declared_str {
        return Err(ReportError::value(
            "run_config_sha256 does not match canonical run config",
        ));
    }
    Ok(actual)
}

fn canonical_topology(value: Option<&Value>) -> Option<&str> {
    let s = value.and_then(Value::as_str).filter(|s| !s.is_empty())?;
    Some(match s {
        "SINGLE_AGENT_UNIFIED" => "T0_SINGLE_MONOLITH",
        "MULTI_AGENT_SPECIALIZED" => "T5_HIERARCHICAL_PIPELINE",
        other => other,
    })
}

fn validate_episode_run_context(
    record: &Map<String, Value>,
    context: &RunScoringContextV5,
    run_config: &Map<String, Value>,
    line_number: usize,
) -> Result<(), ReportError> {
    let reject = |field: &str| -> ReportError {
        ReportError::value(format!(
            "raw episode line {line_number} {field} disagrees with run configuration"
        ))
    };

    if record.get("effort").and_then(Value::as_str) != Some(context.reasoning_effort.as_str()) {
        return Err(reject("effort"));
    }
    if canonical_topology(record.get("topology_id"))
        != Some(
            canonical_topology(Some(&Value::String(context.topology.clone())))
                .unwrap_or(&context.topology),
        )
    {
        return Err(reject("topology"));
    }
    if record.get("steering_mode").and_then(Value::as_str) != Some(context.steering_mode.as_str()) {
        return Err(reject("steering_mode"));
    }
    let Some(expected_wave) = context.wave_execution_mode.as_deref() else {
        return Err(reject("wave_execution_mode"));
    };
    if record.get("wave_execution_mode").and_then(Value::as_str) != Some(expected_wave) {
        return Err(reject("wave_execution_mode"));
    }

    if let Some(ref baseline_id) = context.baseline_id {
        let expected_model_id = format!("baseline:{baseline_id}");
        if context.model_id != expected_model_id
            || record.get("provider_kind").and_then(Value::as_str) != Some("baseline")
            || record.get("model_id").and_then(Value::as_str) != Some(expected_model_id.as_str())
            || record.get("baseline_id").and_then(Value::as_str) != Some(baseline_id.as_str())
        {
            return Err(reject("baseline model identity"));
        }
        return Ok(());
    }

    let provider = run_config
        .get("provider")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    let model = run_config
        .get("model")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty());
    let model_label = match run_config.get("model_label") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => Some(s.as_str()),
        Some(_) => return Err(reject("model/provider identity")),
    };
    let (Some(provider), Some(model)) = (provider, model) else {
        return Err(reject("model/provider identity"));
    };
    let expected_display_id = match model_label {
        Some(label) if !label.is_empty() => label.to_owned(),
        _ => format!("{provider}:{model}"),
    };
    if context.model_id != expected_display_id
        || record.get("provider_kind").and_then(Value::as_str) != Some(provider)
    {
        return Err(reject("model/provider identity"));
    }
    let Some(raw_model_id) = record.get("model_id").and_then(Value::as_str) else {
        return Err(reject("model identity"));
    };
    let Some((kind_prefix, role_models)) = raw_model_id.split_once(':') else {
        return Err(reject("model identity"));
    };
    if kind_prefix != provider {
        return Err(reject("model identity"));
    }
    let mut model_components: BTreeMap<&str, &str> = BTreeMap::new();
    for component in role_models.split(';') {
        let Some((role, role_model)) = component.split_once('=') else {
            return Err(reject("role model identity"));
        };
        if role.is_empty() || role_model.is_empty() || model_components.contains_key(role) {
            return Err(reject("role model identity"));
        }
        model_components.insert(role, role_model);
    }
    if model_components.is_empty() || model_components.values().any(|&rm| rm != model) {
        return Err(reject("role model identity"));
    }
    Ok(())
}

fn manifest_artifact_path(
    manifest: &Map<String, Value>,
    field_name: &str,
    run_dir: &Path,
) -> Result<Option<PathBuf>, ReportError> {
    let Some(raw_val) = manifest.get(field_name).filter(|v| !v.is_null()) else {
        return Ok(None);
    };
    let Some(raw_path) = raw_val.as_str().filter(|s| !s.is_empty()) else {
        return Err(ReportError::value(format!(
            "run manifest {field_name} must be a safe relative path"
        )));
    };
    Ok(Some(safe_artifact_file(
        run_dir,
        raw_path,
        &format!("run manifest {field_name}"),
    )?))
}

fn verified_usage_accounting_artifacts(
    manifest: &Map<String, Value>,
    run_dir: &Path,
) -> Result<Option<VerifiedUsageAccountingV5>, ReportError> {
    let path_key = "usage_accounting_manifest_path";
    let sha_key = "usage_accounting_manifest_sha256";
    let artifacts_key = "usage_accounting_evidence_artifacts";
    let evidence_fields_present = [path_key, sha_key, artifacts_key]
        .iter()
        .any(|k| manifest.contains_key(*k));
    if !evidence_fields_present {
        return Ok(None);
    }
    let Some(raw_manifest_val) = manifest.get(path_key).filter(|v| !v.is_null()) else {
        return Ok(None);
    };
    let Some(expected_sha_val) = manifest.get(sha_key).filter(|v| !v.is_null()) else {
        return Ok(None);
    };
    let Some(descriptors_val) = manifest.get(artifacts_key).filter(|v| !v.is_null()) else {
        return Ok(None);
    };

    let raw_manifest_path = raw_manifest_val
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            ReportError::value("usage-accounting manifest path must be a safe relative path")
        })?;
    let expected_sha = expected_sha_val
        .as_str()
        .filter(|s| is_sha256_str(s))
        .ok_or_else(|| {
            ReportError::value("usage-accounting manifest SHA-256 must be lowercase SHA-256")
        })?;

    let manifest_path =
        safe_artifact_file(run_dir, raw_manifest_path, "usage-accounting manifest path")?;
    let parent_name = manifest_path
        .parent()
        .and_then(|p| p.file_name())
        .and_then(|n| n.to_str());
    let file_name = manifest_path.file_name().and_then(|n| n.to_str());
    if file_name != Some("manifest.json") || parent_name != Some("usage_accounting") {
        return Err(ReportError::value(
            "usage-accounting manifest path must be usage_accounting/manifest.json",
        ));
    }

    let Some(manifest_raw) =
        read_bytes_optional(&manifest_path, "cannot read usage-accounting manifest")?
    else {
        return Ok(None);
    };
    let actual_sha = sha256_hex(&manifest_raw);
    if actual_sha != expected_sha {
        return Err(ReportError::value(
            "usage-accounting manifest digest disagrees with run manifest",
        ));
    }

    let descriptors = descriptors_val
        .as_array()
        .filter(|a| !a.is_empty())
        .ok_or_else(|| {
            ReportError::value("usage-accounting evidence artifacts must be a nonempty list")
        })?;

    let descriptor_fields: BTreeSet<&str> = [
        "role",
        "provider_kind",
        "model_id",
        "provider_fingerprint_sha256",
        "aggregate_usage_scope",
        "evidence_path",
        "evidence_sha256",
        "adapter_retry_limit",
    ]
    .into_iter()
    .collect();

    let mut evidence_by_role: BTreeMap<String, Vec<u8>> = BTreeMap::new();
    let mut paths_by_role: BTreeMap<String, &str> = BTreeMap::new();

    for descriptor_val in descriptors {
        let Some(descriptor) = descriptor_val.as_object() else {
            return Err(ReportError::value(
                "usage-accounting evidence artifact descriptor is malformed",
            ));
        };
        let actual_fields: BTreeSet<&str> = descriptor.keys().map(String::as_str).collect();
        if actual_fields != descriptor_fields {
            return Err(ReportError::value(
                "usage-accounting evidence artifact descriptor is malformed",
            ));
        }
        let role = descriptor
            .get("role")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty());
        let evidence_sha = descriptor
            .get("evidence_sha256")
            .and_then(Value::as_str)
            .filter(|s| is_sha256_str(s));
        let raw_evidence_path = descriptor
            .get("evidence_path")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty());
        let (Some(role), Some(evidence_sha), Some(raw_evidence_path)) =
            (role, evidence_sha, raw_evidence_path)
        else {
            return Err(ReportError::value(
                "usage-accounting evidence artifact identity is malformed",
            ));
        };
        if evidence_by_role.contains_key(role) {
            return Err(ReportError::value(
                "usage-accounting evidence artifact identity is malformed",
            ));
        }

        let evidence_path = safe_artifact_file(
            run_dir,
            raw_evidence_path,
            &format!("usage-accounting evidence path for {role}"),
        )?;
        let ev_name = evidence_path.file_name().and_then(|n| n.to_str());
        let ev_parent = evidence_path
            .parent()
            .and_then(|p| p.file_name())
            .and_then(|n| n.to_str());
        let ev_grandparent = evidence_path
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.file_name())
            .and_then(|n| n.to_str());
        let expected_ev_name = format!("{evidence_sha}.json");
        if ev_name != Some(expected_ev_name.as_str())
            || ev_parent != Some("evidence")
            || ev_grandparent != Some("usage_accounting")
        {
            return Err(ReportError::value(format!(
                "usage-accounting evidence path is not content-addressed for {role}"
            )));
        }

        let Some(evidence_raw) = read_bytes_optional(
            &evidence_path,
            &format!("cannot read usage-accounting evidence for {role}"),
        )?
        else {
            return Ok(None);
        };
        if sha256_hex(&evidence_raw) != evidence_sha {
            return Err(ReportError::value(format!(
                "usage-accounting evidence digest mismatch for {role}"
            )));
        }
        evidence_by_role.insert(role.to_owned(), evidence_raw);
        paths_by_role.insert(role.to_owned(), raw_evidence_path);
    }

    let verified =
        verify_usage_accounting_artifacts_v5(&manifest_raw, &evidence_by_role, expected_sha)?;

    for descriptor_val in descriptors {
        let descriptor = descriptor_val.as_object().expect("checked");
        let role = descriptor["role"].as_str().expect("checked");
        let Some(entry) = verified.entries_by_role.get(role) else {
            return Err(ReportError::value(format!(
                "usage-accounting artifact descriptor disagrees with manifest for {role}"
            )));
        };
        let retry_matches = as_strict_i64(&descriptor["adapter_retry_limit"])
            .is_some_and(|v| v == entry.adapter_retry_limit);
        let matches = entry.role == role
            && descriptor["provider_kind"].as_str() == Some(entry.provider_kind.as_str())
            && descriptor["model_id"].as_str() == Some(entry.model_id.as_str())
            && descriptor["provider_fingerprint_sha256"].as_str()
                == Some(entry.provider_fingerprint_sha256.as_str())
            && descriptor["aggregate_usage_scope"].as_str()
                == Some(entry.aggregate_usage_scope.as_str())
            && descriptor["evidence_sha256"].as_str() == Some(entry.evidence_sha256.as_str())
            && retry_matches;
        if !matches {
            return Err(ReportError::value(format!(
                "usage-accounting artifact descriptor disagrees with manifest for {role}"
            )));
        }
        if paths_by_role.get(role).copied() != descriptor["evidence_path"].as_str() {
            return Err(ReportError::value(format!(
                "usage-accounting evidence path changed for {role}"
            )));
        }
    }

    Ok(Some(verified))
}

fn load_run_dataset(
    manifest: &Map<String, Value>,
    run_dir: &Path,
    suite_id: &str,
) -> Result<Option<LoadedV5Dataset>, ReportError> {
    let Some(paths_val) = manifest
        .get("dataset_artifact_paths")
        .filter(|v| !v.is_null())
    else {
        return Ok(None);
    };
    let expected_episodes = manifest
        .get("dataset_episodes_sha256")
        .filter(|v| !v.is_null());
    let expected_oracles = manifest
        .get("dataset_oracles_sha256")
        .filter(|v| !v.is_null());
    let expected_seal = manifest.get("dataset_seal_sha256").filter(|v| !v.is_null());
    let (Some(exp_ep), Some(exp_or), Some(exp_seal)) =
        (expected_episodes, expected_oracles, expected_seal)
    else {
        return Ok(None);
    };
    let expected_hashes = [
        ("episodes", exp_ep),
        ("oracles", exp_or),
        ("seal", exp_seal),
    ];

    let expected_keys: BTreeSet<&str> = ["episodes", "oracles", "seal"].into_iter().collect();
    let Some(paths) = paths_val.as_object() else {
        return Err(ReportError::value(
            "dataset_artifact_paths must contain exactly episodes, oracles, seal",
        ));
    };
    if paths.keys().map(String::as_str).collect::<BTreeSet<_>>() != expected_keys {
        return Err(ReportError::value(
            "dataset_artifact_paths must contain exactly episodes, oracles, seal",
        ));
    }

    let mut resolved: BTreeMap<&str, PathBuf> = BTreeMap::new();
    for name in ["episodes", "oracles", "seal"] {
        let raw_path = paths[name]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                ReportError::value(format!(
                    "dataset artifact path {name:?} must be a safe relative path"
                ))
            })?;
        resolved.insert(
            name,
            safe_artifact_file(run_dir, raw_path, &format!("dataset {name} path"))?,
        );
    }

    let required_names = [
        ("episodes", "episodes.jsonl"),
        ("oracles", "oracles.jsonl"),
        ("seal", "SEAL.json"),
    ];
    for (name, expected_filename) in required_names {
        if resolved[name].file_name().and_then(|n| n.to_str()) != Some(expected_filename) {
            return Err(ReportError::value(
                "dataset artifact paths must point to canonical v5 dataset filenames",
            ));
        }
    }

    let dataset_dirs: BTreeSet<&Path> = resolved.values().filter_map(|p| p.parent()).collect();
    if dataset_dirs.len() != 1 {
        return Err(ReportError::value(
            "dataset episodes, oracles, and seal must share one directory",
        ));
    }
    if resolved.values().any(|p| !p.is_file()) {
        return Ok(None);
    }

    for (name, expected_val) in expected_hashes {
        let expected_sha = expected_val
            .as_str()
            .filter(|s| is_sha256_str(s))
            .ok_or_else(|| {
                ReportError::value(format!("dataset_{name}_sha256 must be lowercase SHA-256"))
            })?;
        let raw = std::fs::read(&resolved[name])?;
        if sha256_hex(&raw) != expected_sha {
            return Err(ReportError::value(format!(
                "dataset {name} artifact digest disagrees with run manifest"
            )));
        }
    }

    let dataset_dir = dataset_dirs.into_iter().next().expect("checked len == 1");
    let dataset = load_v5_dataset(dataset_dir).map_err(|err| {
        ReportError::value(format!(
            "run dataset artifact failed sealed validation: {err}"
        ))
    })?;
    if dataset.seal.get("suite").and_then(Value::as_str) != Some(suite_id) {
        return Err(ReportError::value(
            "sealed dataset suite disagrees with the run suite",
        ));
    }
    Ok(Some(dataset))
}

fn incomplete_report_artifact(
    manifest: &Map<String, Value>,
    manifest_raw: &[u8],
    episodes_raw: &[u8],
    records: &[Map<String, Value>],
    suite_id: &str,
) -> Result<V5ReportArtifact, ReportError> {
    let context = manifest_context(manifest, suite_id)?;
    let mut metrics: BTreeMap<String, Option<f64>> = INCOMPLETE_METRIC_KEYS
        .into_iter()
        .map(|k| (k.to_owned(), None))
        .collect();

    let all_exec_valid_bool = records
        .iter()
        .all(|r| matches!(r.get("execution_valid"), Some(Value::Bool(_))));
    let exec_valid_rate = if !records.is_empty() && all_exec_valid_bool {
        let valid_count = records
            .iter()
            .filter(|r| r.get("execution_valid") == Some(&Value::Bool(true)))
            .count();
        Some((valid_count as f64) / (records.len() as f64))
    } else {
        None
    };
    metrics.insert("execution_valid_rate".to_owned(), exec_valid_rate);
    metrics.insert("provider_error_count".to_owned(), None);
    metrics.insert("parse_failure_count".to_owned(), None);
    metrics.insert("max_turns_exceeded_count".to_owned(), None);

    let mut gate_status: BTreeMap<String, String> = BTreeMap::new();
    gate_status.insert(
        "G-ECON-01".to_owned(),
        if context.baseline_id.is_some() {
            "NOT_APPLICABLE"
        } else {
            "UNMEASURED"
        }
        .to_owned(),
    );
    gate_status.insert("G-TOK-01".to_owned(), "UNMEASURED".to_owned());
    gate_status.insert("G-TURN-01".to_owned(), "UNMEASURED".to_owned());
    gate_status.insert("G-CAL-01".to_owned(), "UNMEASURED".to_owned());
    gate_status.insert("G-CAL-02".to_owned(), "UNMEASURED".to_owned());
    gate_status.insert("G-CAL-03".to_owned(), "UNMEASURED".to_owned());
    let base_gate = if context.baseline_id.is_none() {
        "NOT_APPLICABLE"
    } else {
        "UNMEASURED"
    };
    gate_status.insert("G-BASE-01".to_owned(), base_gate.to_owned());
    gate_status.insert("G-BASE-01-NUMERIC".to_owned(), base_gate.to_owned());

    let mut status_counts: BTreeMap<String, usize> = ATTEMPT_STATUSES_V5
        .iter()
        .map(|&s| (s.to_owned(), 0))
        .collect();
    status_counts.insert("INCOMPLETE_HARNESS_FAILURE".to_owned(), records.len());

    let mut econ_details: BTreeMap<String, Value> = BTreeMap::new();
    econ_details.insert(
        "planned_slots".to_owned(),
        Value::Number(Number::from(records.len())),
    );
    econ_details.insert("observed_slots".to_owned(), Value::Number(Number::from(0)));
    econ_details.insert(
        "completion_coverage".to_owned(),
        ib_core::pyjson::float_value(0.0),
    );
    econ_details.insert("suite_input_tokens_actual".to_owned(), Value::Null);
    econ_details.insert("suite_output_tokens_actual".to_owned(), Value::Null);
    econ_details.insert("suite_cost_usd_micros".to_owned(), Value::Null);
    econ_details.insert(
        "accounting_coverage_status".to_owned(),
        Value::String("UNAVAILABLE".to_owned()),
    );
    econ_details.insert(
        "error_reason".to_owned(),
        Value::String("sealed dataset artifact unavailable".to_owned()),
    );

    let pricing_manifest_sha256 = context.pricing_manifest_sha256.clone();
    let summary = RunScoreSummaryV5 {
        context,
        n_episodes: records.len(),
        metrics,
        gate_status,
        comparator_status: "DESCRIPTIVE_NONE".to_owned(),
        cost_ceiling_usd_micros: None,
        suite_input_tokens_actual: None,
        suite_output_tokens_actual: None,
        suite_cost_usd_micros: None,
        g_econ_01_details: econ_details,
        bootstrap_ci95: BTreeMap::new(),
        episode_scores: Vec::new(),
        is_complete: false,
        planned_slots: records.len(),
        observed_slots: 0,
        completion_coverage: 0.0,
        provisional_bounds: BTreeMap::new(),
        observed_slot_means: BTreeMap::new(),
        category_summaries: BTreeMap::new(),
        attempt_status_counts: status_counts,
        descriptive_usage: None,
        episode_pass_at_1: BTreeMap::new(),
        family_means: BTreeMap::new(),
        historical_comparator_context: None,
    };

    let provenance = ArtifactProvenanceV5 {
        run_manifest_sha256: sha256_hex(manifest_raw),
        episodes_sha256: sha256_hex(episodes_raw),
        comparator_manifest_sha256: None,
        pricing_manifest_sha256,
        dataset_seal_sha256: None,
        episode_record_count: records.len(),
        scores_recomputed_from_scoring_inputs: false,
        scoring_evidence_status: "UNMEASURED_INCOMPLETE".to_owned(),
        comparator_evidence_verified: false,
        pricing_artifact_sha256: None,
        usage_accounting_manifest_sha256: None,
        usage_accounting_evidence_verified: false,
    };

    Ok(V5ReportArtifact {
        summary,
        provenance,
    })
}

fn extract_episode_attempt(
    record: &Map<String, Value>,
    recomputed: &EpisodeScoreV5,
    context: &RunScoringContextV5,
    line_number: usize,
) -> Result<Option<EpisodeAttemptV5>, ReportError> {
    if let Some(raw_attempt) = record.get("attempt").filter(|v| !v.is_null()) {
        let mut attempt: EpisodeAttemptV5 =
            serde_json::from_value(raw_attempt.clone()).map_err(|err| {
                ReportError::value(format!(
                    "episode line {line_number} attempt failed schema validation: {err}"
                ))
            })?;
        if attempt.episode_id != recomputed.episode_id {
            return Err(ReportError::value(format!(
                "episode line {line_number} attempt.episode_id mismatch"
            )));
        }
        if attempt.repeat_index != recomputed.repeat_index {
            return Err(ReportError::value(format!(
                "episode line {line_number} attempt.repeat_index mismatch"
            )));
        }
        if attempt.attempt_status.as_str() != recomputed.attempt_status {
            return Err(ReportError::value(format!(
                "episode line {line_number} attempt.attempt_status mismatch"
            )));
        }
        if let Some(ref run_sha) = context.run_config_sha256
            && &attempt.run_config_sha256 != run_sha
        {
            return Err(ReportError::value(format!(
                "episode line {line_number} attempt.run_config_sha256 mismatch"
            )));
        }
        let expected_gsrr = recomputed.g_srr_commit.map(|v| v != 0);
        let expected_sem = recomputed.sem_srr_commit.map(|v| v != 0);
        let unscored_attempt = attempt.g_srr.is_none()
            && attempt.sem_srr_commit.is_none()
            && attempt.overgrant_count.is_none()
            && attempt.avail_breach_count.is_none();
        if unscored_attempt {
            attempt.g_srr = expected_gsrr;
            attempt.sem_srr_commit = expected_sem;
            attempt.overgrant_count = recomputed.overgrant_count;
            attempt.avail_breach_count = recomputed.availability_breach_count;
        } else {
            if attempt.g_srr != expected_gsrr || attempt.sem_srr_commit != expected_sem {
                return Err(ReportError::value(format!(
                    "episode line {line_number} attempt metrics disagree with raw regrading"
                )));
            }
            if attempt.overgrant_count != recomputed.overgrant_count
                || attempt.avail_breach_count != recomputed.availability_breach_count
            {
                return Err(ReportError::value(format!(
                    "episode line {line_number} attempt safety counts disagree with commit safety"
                )));
            }
        }
        attempt.validate().map_err(|err| {
            ReportError::value(format!(
                "episode line {line_number} attempt failed schema validation: {err}"
            ))
        })?;
        return Ok(Some(attempt));
    }

    if let (Some(raw_requests), Some(run_sha)) = (
        record.get("provider_requests").filter(|v| !v.is_null()),
        context.run_config_sha256.as_deref(),
    ) {
        let req_arr = raw_requests.as_array().ok_or_else(|| {
            ReportError::value(format!(
                "episode line {line_number} provider_requests must be a list"
            ))
        })?;
        let mut provider_requests = Vec::with_capacity(req_arr.len());
        for req_val in req_arr {
            let req = ProviderRequestUsageV5::from_strict_json_value(req_val).map_err(|err| {
                ReportError::value(format!(
                    "episode line {line_number} provider_requests failed validation: {err}"
                ))
            })?;
            provider_requests.push(req);
        }
        let completed_assistant_generations = match record.get("completed_assistant_generations") {
            None | Some(Value::Null) => 0,
            Some(v) => as_strict_i64(v).ok_or_else(|| {
                ReportError::value("completed_assistant_generations must be an integer")
            })?,
        };
        let tool_call_count = match record.get("tool_call_count") {
            None | Some(Value::Null) => 0,
            Some(v) => as_strict_i64(v)
                .ok_or_else(|| ReportError::value("tool_call_count must be an integer"))?,
        };
        let retry_request_count = match record.get("retry_request_count") {
            None | Some(Value::Null) => 0,
            Some(v) => as_strict_i64(v)
                .ok_or_else(|| ReportError::value("retry_request_count must be an integer"))?,
        };
        let opt_i64 = |key: &str| -> Result<Option<i64>, ReportError> {
            match record.get(key) {
                None | Some(Value::Null) => Ok(None),
                Some(v) => as_strict_i64(v)
                    .map(Some)
                    .ok_or_else(|| ReportError::value(format!("{key} must be an integer"))),
            }
        };
        let terminal_reservation_ordinal = match record
            .get("terminal_reservation_ordinal")
            .filter(|v| !v.is_null())
        {
            None => None,
            Some(v) => Some(
                serde_json::from_value::<(i64, i64, i64, String, i64)>(v.clone()).map_err(
                    |err| {
                        ReportError::value(format!("invalid terminal_reservation_ordinal: {err}"))
                    },
                )?,
            ),
        };
        let terminal_cause = record
            .get("terminal_cause")
            .and_then(Value::as_str)
            .map(str::to_owned);
        let context_limit_cause = match record.get("context_limit_cause").filter(|v| !v.is_null()) {
            None => None,
            Some(v) => Some(
                serde_json::from_value::<ContextLimitCauseV5>(v.clone()).map_err(|err| {
                    ReportError::value(format!("invalid context_limit_cause: {err}"))
                })?,
            ),
        };
        let attempt = build_episode_attempt_v5(
            recomputed,
            run_sha,
            BuildEpisodeAttemptOptionsV5 {
                attempt_id: record.get("attempt_id").and_then(Value::as_str),
                completed_assistant_generations,
                tool_call_count,
                retry_request_count,
                elapsed_wall_time_ms: opt_i64("elapsed_wall_time_ms")?,
                terminal_outcome_elapsed_ms: opt_i64("terminal_outcome_elapsed_ms")?,
                active_elapsed_ms: opt_i64("active_elapsed_ms")?,
                terminal_reservation_ordinal,
                terminal_cause,
                context_limit_cause,
                provider_requests,
            },
        )?;
        return Ok(Some(attempt));
    }

    Ok(None)
}

/// Regrade raw immutable trajectories against the run's sealed dataset.
pub fn report_from_raw_artifacts(run_dir: &Path) -> Result<V5ReportArtifact, ReportError> {
    report_from_raw_artifacts_with_options(run_dir, ReportOptionsV5::default())
}

/// Regrade raw immutable trajectories against the run's sealed dataset with optional external paths.
pub fn report_from_raw_artifacts_with_options(
    run_dir: &Path,
    options: ReportOptionsV5<'_>,
) -> Result<V5ReportArtifact, ReportError> {
    let run_dir = std::fs::canonicalize(run_dir)
        .map_err(|err| ReportError::value(format!("run artifact is incomplete: {err}")))?;
    let manifest_path = run_dir.join("manifest.json");
    let episodes_path = run_dir.join("episodes.jsonl");

    let manifest_raw = std::fs::read(&manifest_path)
        .map_err(|err| ReportError::value(format!("run artifact is incomplete: {err}")))?;
    let episodes_raw = std::fs::read(&episodes_path)
        .map_err(|err| ReportError::value(format!("run artifact is incomplete: {err}")))?;

    let manifest_val = parse_json_strict(&manifest_raw, "run manifest")?;
    let Some(manifest) = manifest_val.as_object() else {
        return Err(ReportError::value("run manifest must be a JSON object"));
    };

    let episodes_digest = sha256_hex(&episodes_raw);
    if let Some(expected_run_episodes_digest) =
        manifest.get("run_episodes_sha256").filter(|v| !v.is_null())
    {
        if !is_sha256_value(expected_run_episodes_digest) {
            return Err(ReportError::value(
                "run_episodes_sha256 must be lowercase SHA-256",
            ));
        }
        if expected_run_episodes_digest.as_str() != Some(episodes_digest.as_str()) {
            return Err(ReportError::value(
                "episodes.jsonl digest disagrees with run manifest",
            ));
        }
    }

    let episodes_text = std::str::from_utf8(&episodes_raw)
        .map_err(|_| ReportError::value("episodes.jsonl is not valid UTF-8 JSON"))?;
    let mut raw_lines: Vec<&str> = episodes_text.split('\n').collect();
    if episodes_text.ends_with('\n') {
        raw_lines.pop();
    }
    let mut records: Vec<Map<String, Value>> = Vec::with_capacity(raw_lines.len());
    for (idx, raw_line) in raw_lines.into_iter().enumerate() {
        let line_number = idx + 1;
        let line = raw_line.strip_suffix('\r').unwrap_or(raw_line);
        if line.trim().is_empty() {
            return Err(ReportError::value(format!(
                "episodes.jsonl contains a blank line at {line_number}"
            )));
        }
        let val = parse_json_strict(
            line.as_bytes(),
            &format!("episode record line {line_number}"),
        )?;
        let Some(obj) = val.as_object() else {
            return Err(ReportError::value(format!(
                "episode record line {line_number} must be an object"
            )));
        };
        records.push(obj.clone());
    }
    if records.is_empty() {
        return Err(ReportError::value(
            "episodes.jsonl contains no episode records",
        ));
    }

    if let Some(expected_count) = manifest
        .get("run_episode_record_count")
        .filter(|v| !v.is_null())
        && as_strict_i64(expected_count) != Some(records.len() as i64)
    {
        return Err(ReportError::value(
            "episode record count disagrees with run manifest",
        ));
    }

    let suite_id = manifest
        .get("suite_id")
        .or_else(|| manifest.get("suite"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| ReportError::value("run manifest must declare suite_id"))?;

    let Some(dataset) = load_run_dataset(manifest, &run_dir, suite_id)? else {
        return incomplete_report_artifact(
            manifest,
            &manifest_raw,
            &episodes_raw,
            &records,
            suite_id,
        );
    };

    let mut context = manifest_context(manifest, suite_id)?;
    let run_config_value = match manifest
        .get("config")
        .or_else(|| manifest.get("run_config"))
    {
        Some(v) => v
            .as_object()
            .ok_or_else(|| ReportError::value("run manifest execution config must be an object"))?,
        None => manifest,
    };

    let verified_run_config_sha = validate_run_config_hash(manifest, run_config_value)?;
    if context.run_config_sha256.as_deref() != Some(verified_run_config_sha.as_str()) {
        return Err(ReportError::value(
            "scoring context run-config digest disagrees with verified config",
        ));
    }
    let config_pricing = run_config_value
        .get("pricing_manifest_sha256")
        .filter(|v| !v.is_null())
        .and_then(Value::as_str);
    if config_pricing != context.pricing_manifest_sha256.as_deref() {
        return Err(ReportError::value(
            "run pricing digest disagrees with canonical run config",
        ));
    }

    let run_configuration: Option<RunConfigurationV5> = match manifest
        .get("run_configuration")
        .filter(|v| !v.is_null())
    {
        None => None,
        Some(raw_rc) => Some(RunConfigurationV5::from_strict_json_value(raw_rc).map_err(
            |err| ReportError::value(format!("run_configuration failed schema validation: {err}")),
        )?),
    };

    let pricing_path = match options.pricing_manifest_path {
        Some(p) => Some(p.to_path_buf()),
        None => manifest_artifact_path(manifest, "pricing_manifest_path", &run_dir)?,
    };
    let mut pricing_rates: Option<PricingRatesV5> = None;
    let mut pricing_artifact_digest: Option<String> = None;
    if let Some(ref p_path) = pricing_path
        && let Some(pricing_bytes) =
            read_bytes_optional(p_path, "cannot read frozen pricing artifact")?
    {
        let Some(ref expected_pricing_sha) = context.pricing_manifest_sha256 else {
            return Err(ReportError::value(
                "frozen pricing artifact is not bound to a run pricing digest",
            ));
        };
        let (rates, sha) = strict_pricing_rates(&pricing_bytes, expected_pricing_sha)?;
        pricing_rates = Some(rates);
        pricing_artifact_digest = Some(sha);
    }

    let verified_usage_accounting = verified_usage_accounting_artifacts(manifest, &run_dir)?;

    let episode_by_id: BTreeMap<&str, _> = dataset
        .episodes
        .iter()
        .map(|e| (e.episode_id.as_str(), e))
        .collect();
    let oracle_by_id: BTreeMap<&str, _> = dataset
        .oracles
        .iter()
        .map(|o| (o.episode_id.as_str(), o))
        .collect();

    let mut expected_episode_grid: Vec<(String, String, String)> = dataset
        .episodes
        .iter()
        .filter_map(|ep| {
            oracle_by_id.get(ep.episode_id.as_str()).map(|orc| {
                let reg = match orc.internal_regime_id {
                    RegimeIdV5::R1 => "R1",
                    RegimeIdV5::R2 => "R2",
                    RegimeIdV5::R3 => "R3",
                    RegimeIdV5::R4 => "R4",
                };
                (ep.episode_id.clone(), ep.family_id.clone(), reg.to_owned())
            })
        })
        .collect();
    expected_episode_grid.sort();
    context.expected_episode_grid = if expected_episode_grid.is_empty() {
        None
    } else {
        Some(expected_episode_grid)
    };

    let mut scores: Vec<EpisodeScoreV5> = Vec::with_capacity(records.len());
    let mut parsed_attempts: Vec<EpisodeAttemptV5> = Vec::new();
    let mut all_attempts_present = true;

    for (idx, record) in records.iter().enumerate() {
        let line_number = idx + 1;
        let episode_id = record
            .get("episode_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                ReportError::value(format!(
                    "episode record line {line_number} lacks an episode_id"
                ))
            })?;
        let Some(&episode) = episode_by_id.get(episode_id) else {
            return Err(ReportError::value(format!(
                "episode line {line_number} is absent from the sealed dataset"
            )));
        };
        let Some(&sealed_oracle) = oracle_by_id.get(episode_id) else {
            return Err(ReportError::value(format!(
                "episode line {line_number} is absent from the sealed dataset"
            )));
        };

        validate_episode_run_context(record, &context, run_config_value, line_number).map_err(
            |err| {
                ReportError::value(format!(
                    "raw episode line {line_number} cannot be regraded: {err}"
                ))
            },
        )?;

        let record_val = Value::Object(record.clone());
        let (recomputed_input, recomputed) = score_execution_raw_v5(
            &record_val,
            episode,
            sealed_oracle,
            ScoreExecutionOptionsV5 {
                pricing_rates,
                pricing_manifest_sha256: context.pricing_manifest_sha256.as_deref(),
                verified_usage_accounting: verified_usage_accounting.as_ref(),
            },
        )
        .map_err(|err| {
            ReportError::value(format!(
                "raw episode line {line_number} cannot be regraded: {err}"
            ))
        })?;

        if let Some(attached_input) = record.get("scoring_input").filter(|v| !v.is_null()) {
            let parsed_input =
                episode_scoring_input_from_mapping(attached_input).map_err(|err| {
                    ReportError::value(format!(
                        "invalid attached scoring_input on episode line {line_number}: {err}"
                    ))
                })?;
            if episode_scoring_input_to_mapping(&parsed_input)
                != episode_scoring_input_to_mapping(&recomputed_input)
            {
                return Err(ReportError::value(format!(
                    "attached scoring_input disagrees with raw grading on episode line {line_number}"
                )));
            }
        }

        if record.get("suite_id").and_then(Value::as_str) != Some(recomputed.suite_id.as_str())
            || record.get("family_id").and_then(Value::as_str)
                != Some(recomputed.family_id.as_str())
        {
            return Err(ReportError::value(format!(
                "episode line {line_number} identity disagrees with its sealed source"
            )));
        }

        if let Some(attached_score) = record.get("score").filter(|v| !v.is_null()) {
            let parsed_score = episode_score_from_mapping(attached_score).map_err(|err| {
                ReportError::value(format!(
                    "invalid attached score on line {line_number}: {err}"
                ))
            })?;
            if parsed_score != recomputed {
                return Err(ReportError::value(format!(
                    "attached score does not match raw regrading on line {line_number}"
                )));
            }
        }

        if let Some(attempt) = extract_episode_attempt(record, &recomputed, &context, line_number)?
        {
            parsed_attempts.push(attempt);
        } else {
            all_attempts_present = false;
        }
        scores.push(recomputed);
    }

    let suite_ids: BTreeSet<&str> = scores.iter().map(|s| s.suite_id.as_str()).collect();
    if suite_ids.len() != 1 {
        return Err(ReportError::value(
            "episode records must belong to exactly one suite",
        ));
    }

    let descriptive_usage_summary: Option<DescriptiveUsageSummaryV5> = match manifest
        .get("descriptive_usage_summary")
        .filter(|v| !v.is_null())
    {
        None => None,
        Some(raw_usage) => Some(
            DescriptiveUsageSummaryV5::from_strict_json_value(raw_usage).map_err(|err| {
                ReportError::value(format!(
                    "descriptive_usage_summary failed schema validation: {err}"
                ))
            })?,
        ),
    };

    let mut historical_manifest: Option<HistoricalComparatorManifestV5> = None;
    let mut comparator_digest: Option<String> = None;
    let comparator_path = match options.comparator_manifest_path {
        Some(p) => Some(p.to_path_buf()),
        None => manifest_artifact_path(manifest, "comparator_manifest_path", &run_dir)?,
    };
    let comparator_bytes = match comparator_path {
        Some(ref p) => read_bytes_optional(p, "cannot read historical comparator manifest")?,
        None => None,
    };
    if let Some(ref c_bytes) = comparator_bytes {
        let actual_digest = sha256_hex(c_bytes);
        if let Some(expected_val) = manifest
            .get("comparator_manifest_sha256")
            .filter(|v| !v.is_null())
        {
            let expected_str = expected_val
                .as_str()
                .filter(|s| is_sha256_str(s))
                .ok_or_else(|| {
                    ReportError::value("run manifest comparator digest must be lowercase SHA-256")
                })?;
            if expected_str != actual_digest {
                return Err(ReportError::value(
                    "comparator manifest file digest disagrees with run manifest",
                ));
            }
        }
        historical_manifest = Some(validate_comparator_manifest(
            c_bytes,
            context.pricing_manifest_sha256.as_deref(),
        )?);
        comparator_digest = Some(actual_digest);
    } else if let Some(expected_val) = manifest
        .get("comparator_manifest_sha256")
        .filter(|v| !v.is_null())
        && !is_sha256_value(expected_val)
    {
        return Err(ReportError::value(
            "run manifest comparator digest must be lowercase SHA-256",
        ));
    }

    let mut seal_digest: Option<String> = Some(sha256_hex(&dataset.seal_bytes));
    if let Some(seal_path) = options.dataset_seal_path {
        let seal_bytes = std::fs::read(seal_path)?;
        let actual_seal = sha256_hex(&seal_bytes);
        if let Some(expected_seal) = manifest
            .get("dataset_seal_sha256")
            .or_else(|| manifest.get("seal_sha256"))
            .filter(|v| !v.is_null())
            && expected_seal.as_str() != Some(actual_seal.as_str())
        {
            return Err(ReportError::value(
                "dataset seal file digest disagrees with run manifest",
            ));
        }
        seal_digest = Some(actual_seal);
    } else if let Some(raw_seal) = manifest
        .get("dataset_seal_sha256")
        .or_else(|| manifest.get("seal_sha256"))
        .filter(|v| !v.is_null())
    {
        let s = raw_seal
            .as_str()
            .filter(|s| is_sha256_str(s))
            .ok_or_else(|| {
                ReportError::value("run manifest dataset seal digest must be lowercase SHA-256")
            })?;
        seal_digest = Some(s.to_owned());
    }

    let summary = aggregate_run_scores(
        &scores,
        &context,
        AggregateRunOptionsV5 {
            historical_comparator_manifest: historical_manifest.as_ref(),
            attempts: if all_attempts_present && !parsed_attempts.is_empty() {
                Some(parsed_attempts.as_slice())
            } else {
                None
            },
            descriptive_usage_summary: descriptive_usage_summary.as_ref(),
            run_configuration: run_configuration.as_ref(),
            ..AggregateRunOptionsV5::default()
        },
    )?;

    let provenance = ArtifactProvenanceV5 {
        run_manifest_sha256: sha256_hex(&manifest_raw),
        episodes_sha256: episodes_digest,
        comparator_manifest_sha256: comparator_digest,
        pricing_manifest_sha256: context.pricing_manifest_sha256,
        dataset_seal_sha256: seal_digest,
        episode_record_count: scores.len(),
        scores_recomputed_from_scoring_inputs: true,
        scoring_evidence_status: "VERIFIED".to_owned(),
        comparator_evidence_verified: historical_manifest.is_some(),
        pricing_artifact_sha256: pricing_artifact_digest,
        usage_accounting_manifest_sha256: verified_usage_accounting
            .as_ref()
            .map(|v| v.manifest_sha256.clone()),
        usage_accounting_evidence_verified: verified_usage_accounting.is_some(),
    };

    Ok(V5ReportArtifact {
        summary,
        provenance,
    })
}
