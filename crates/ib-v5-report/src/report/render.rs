//! Markdown and JSON rendering for single-run IdentityBench v5 reports.
//!
//! Ports `_clean_table_text`, `_fmt`, `_fmt_interval`, `_fmt_report_metric`,
//! `render_v5_markdown_report`, `_summary_to_json`, and `write_v5_report` from
//! `src/identitybench/v5/report.py`.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use ib_core::pyfloat::format_fixed;
use ib_core::pyjson::{DumpOptions, dumps, float_value};
use ib_v5_scoring::{ATTEMPT_STATUSES_V5, CATEGORY_IDS_V5, episode_score_to_mapping};
use serde_json::{Map, Number, Value};

use crate::error::ReportError;
use crate::markdown_text::{code_cell, escape_cell, escape_inline};
use crate::report::provenance::V5ReportArtifact;

static INCOMPLETE_GATED_METRICS_V5: LazyLock<BTreeSet<&'static str>> = LazyLock::new(|| {
    [
        "G-SRR",
        "Sem-SRR@commit",
        "Action-SRR@last_stage",
        "Action-SRR@last_stage | both_phases_staged",
        "P1-Action-SRR@last_stage | p1_staged",
        "G-SRR@first_stage",
        "Delta_repair",
        "G-PROP",
        "G-PROP_reset",
        "provider_error_count",
        "parse_failure_count",
        "max_turns_exceeded_count",
        "OVERGRANT@commit_mean",
        "AVAIL_BREACH@commit_mean",
        "OVERGRANT@commit | any_phase_committed",
        "AVAIL_BREACH@commit | any_phase_committed",
        "OVERGRANT_count",
        "AVAIL_BREACH_count",
        "safety_valid_commit_target_count",
        "OVERGRANT@last_stage_mean",
        "AVAIL_BREACH@last_stage_mean",
        "OVERGRANT@last_stage | any_phase_staged",
        "AVAIL_BREACH@last_stage | any_phase_staged",
        "OVERGRANT@last_stage | both_phases_staged",
        "AVAIL_BREACH@last_stage | both_phases_staged",
        "safety_valid_mutation_count",
        "safety_valid_target_count",
        "safety_stage_attempt_count",
    ]
    .into_iter()
    .collect()
});

/// Escape and flatten an arbitrary table cell string.
#[must_use]
pub fn clean_table_text(value: &str) -> String {
    escape_cell(value)
}

/// Scalar input supported by [`fmt_value`] without lossy `f64` rounding on exact integers.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum FmtValue<'a> {
    Int(i64),
    Int128(i128),
    BigIntStr(&'a str),
    Float(f64),
}

/// Format an optional numeric value matching Python `_fmt(value, precision)`.
///
/// Exact integer inputs with `precision == 0` return their decimal string directly without
/// converting through `f64` (preserving values like `10^18 + 1` and `10^310`).
#[must_use]
pub fn fmt_value(value: Option<FmtValue<'_>>, precision: usize) -> String {
    let Some(val) = value else {
        return "UNMEASURED".to_owned();
    };
    match val {
        FmtValue::Int(n) => {
            if precision == 0 {
                n.to_string()
            } else {
                format_fixed(n as f64, precision)
            }
        }
        FmtValue::Int128(n) => {
            if precision == 0 {
                n.to_string()
            } else {
                format_fixed(n as f64, precision)
            }
        }
        FmtValue::BigIntStr(s) => {
            if precision == 0 {
                s.to_owned()
            } else {
                format_fixed(s.parse::<f64>().unwrap_or(f64::NAN), precision)
            }
        }
        FmtValue::Float(f) => format_fixed(f, precision),
    }
}

/// Format an optional `f64` matching Python `_fmt(value, precision)`.
#[must_use]
pub fn fmt_f64(value: Option<f64>, precision: usize) -> String {
    fmt_value(value.map(FmtValue::Float), precision)
}

/// Format an optional `i64` matching Python `_fmt(value, precision)`.
#[must_use]
pub fn fmt_i64(value: Option<i64>, precision: usize) -> String {
    fmt_value(value.map(FmtValue::Int), precision)
}

/// Format an optional `(low, high)` interval matching Python `_fmt_interval`.
#[must_use]
pub fn fmt_interval(interval: Option<(f64, f64)>, precision: usize) -> String {
    match interval {
        None => "UNMEASURED".to_owned(),
        Some((low, high)) => format!(
            "[{}, {}]",
            fmt_f64(Some(low), precision),
            fmt_f64(Some(high), precision)
        ),
    }
}

fn fmt_report_metric(artifact: &V5ReportArtifact, key: &str) -> String {
    let summary = &artifact.summary;
    if !summary.is_complete && INCOMPLETE_GATED_METRICS_V5.contains(key) {
        return "INCOMPLETE".to_owned();
    }
    let val = summary.metrics.get(key).copied().flatten();
    fmt_f64(val, 3)
}

fn opt_str_val(s: Option<&str>) -> Value {
    s.map_or(Value::Null, |v| Value::String(v.to_owned()))
}

fn opt_i64_val(v: Option<i64>) -> Value {
    v.map_or(Value::Null, |n| Value::Number(Number::from(n)))
}

fn opt_float_val(v: Option<f64>) -> Value {
    v.map_or(Value::Null, float_value)
}

/// Render observed metrics, provisional bounds, safety, categories, usage, and gates.
#[must_use]
pub fn render_v5_markdown_report(artifact: &V5ReportArtifact) -> String {
    let summary = &artifact.summary;
    let provenance = &artifact.provenance;
    let context = &summary.context;

    let columns: [(&str, &str); 15] = [
        ("G-SRR", "G-SRR"),
        ("Sem-SRR@commit", "Sem-SRR@commit"),
        ("Action-SRR@last_stage", "Action-SRR@last_stage"),
        (
            "Action-SRR@last_stage | both_phases_staged",
            "Action-SRR@last_stage | both_phases_staged",
        ),
        (
            "P1-Action-SRR@last_stage | p1_staged",
            "P1-Action-SRR@last_stage | p1_staged",
        ),
        ("G-SRR@first_stage", "G-SRR@first_stage"),
        ("Delta_repair", "Delta_repair"),
        ("G-PROP", "G-PROP"),
        ("G-PROP_reset", "G-PROP_reset"),
        ("p2_observed_rate", "p2_observed_rate"),
        ("both_phases_staged_rate", "both_phases_staged_rate"),
        ("execution_valid_rate", "execution_valid_rate"),
        ("provider_error_count", "Provider error count"),
        ("parse_failure_count", "Parse failure count"),
        ("max_turns_exceeded_count", "MAX_TURNS termination count"),
    ];

    let mut lines = vec![
        format!(
            "# IdentityBench v5 report: {}",
            escape_inline(&context.model_id)
        ),
        String::new(),
        format!("- Suite: {}", code_cell(&context.suite_id)),
        format!(
            "- Comparison track: {}",
            code_cell(&context.comparison_track)
        ),
        format!(
            "- Reasoning effort: {}",
            code_cell(&context.reasoning_effort)
        ),
        format!("- Topology: {}", code_cell(&context.topology)),
        format!("- Steering mode: {}", code_cell(&context.steering_mode)),
        format!(
            "- Planned repeats per episode: {}",
            context.planned_repeats_per_episode
        ),
        format!("- Planned episodes: {}", summary.n_episodes),
        format!(
            "- Completion status: `{}` ({}/{} observed planned slots, coverage={})",
            if summary.is_complete {
                "COMPLETE"
            } else {
                "INCOMPLETE"
            },
            summary.observed_slots,
            summary.planned_slots,
            format_fixed(summary.completion_coverage, 3),
        ),
        format!(
            "- Scoring evidence: `{}`",
            provenance.scoring_evidence_status
        ),
    ];

    if !summary.is_complete {
        let gsrr_bounds = summary.provisional_bounds.get("G-SRR").copied();
        let obs_gsrr = summary.observed_slot_means.get("G-SRR").copied().flatten();
        lines.extend([
            String::new(),
            "> [!WARNING]".to_owned(),
            "> Incomplete configuration: headline pass@1 is `UNMEASURED`, and ranking, confirmatory hypothesis tests, and `v5_full` escalation are blocked.".to_owned(),
            format!(
                "> Provisional G-SRR family-equal bounds: {} (observed slot mean: {}, slots: {}/{}).",
                fmt_interval(gsrr_bounds, 3),
                fmt_f64(obs_gsrr, 3),
                summary.observed_slots,
                summary.planned_slots,
            ),
        ]);
    }

    lines.extend([
        String::new(),
        "| Metric | Value | Cluster bootstrap CI95 | Provisional bounds [L, U] | Observed slot mean |"
            .to_owned(),
        "| --- | ---: | --- | --- | ---: |".to_owned(),
    ]);

    for (key, label) in columns {
        let interval = summary.bootstrap_ci95.get(key).copied();
        let ci = fmt_interval(interval, 3);
        let bounds = fmt_interval(summary.provisional_bounds.get(key).copied(), 3);
        let obs_mean = fmt_f64(summary.observed_slot_means.get(key).copied().flatten(), 3);
        lines.push(format!(
            "| {label} | {} | {ci} | {bounds} | {obs_mean} |",
            fmt_report_metric(artifact, key)
        ));
    }

    lines.extend([
        String::new(),
        "## Safety outcomes (commit primary and conditional companions)".to_owned(),
        String::new(),
        "| Metric | Value |".to_owned(),
        "| --- | ---: |".to_owned(),
    ]);
    let safety_columns: [(&str, &str); 19] = [
        ("OVERGRANT@commit_mean", "OVERGRANT@commit mean (primary)"),
        (
            "AVAIL_BREACH@commit_mean",
            "AVAIL_BREACH@commit mean (primary)",
        ),
        (
            "OVERGRANT@commit | any_phase_committed",
            "OVERGRANT@commit | any_phase_committed",
        ),
        (
            "AVAIL_BREACH@commit | any_phase_committed",
            "AVAIL_BREACH@commit | any_phase_committed",
        ),
        ("OVERGRANT_count", "OVERGRANT commit count (complete)"),
        ("AVAIL_BREACH_count", "AVAIL_BREACH commit count (complete)"),
        (
            "OVERGRANT_observed_commit_count",
            "OVERGRANT observed commit count",
        ),
        (
            "AVAIL_BREACH_observed_commit_count",
            "AVAIL_BREACH observed commit count",
        ),
        (
            "safety_valid_commit_target_count",
            "Commit valid target denominator",
        ),
        (
            "OVERGRANT@last_stage_mean",
            "OVERGRANT@last_stage mean (diagnostic)",
        ),
        (
            "AVAIL_BREACH@last_stage_mean",
            "AVAIL_BREACH@last_stage mean (diagnostic)",
        ),
        (
            "OVERGRANT@last_stage | any_phase_staged",
            "OVERGRANT@last_stage | any_phase_staged",
        ),
        (
            "AVAIL_BREACH@last_stage | any_phase_staged",
            "AVAIL_BREACH@last_stage | any_phase_staged",
        ),
        (
            "OVERGRANT@last_stage | both_phases_staged",
            "OVERGRANT@last_stage | both_phases_staged",
        ),
        (
            "AVAIL_BREACH@last_stage | both_phases_staged",
            "AVAIL_BREACH@last_stage | both_phases_staged",
        ),
        (
            "safety_valid_mutation_count",
            "Stage-attempt valid submitted mutation denominator",
        ),
        (
            "safety_valid_target_count",
            "Stage-attempt valid unique-target diagnostic",
        ),
        (
            "safety_stage_attempt_count",
            "Stage-attempt safety denominator",
        ),
        (
            "safety_attempt_stream_complete_rate",
            "Safety attempt stream complete rate",
        ),
    ];
    for (key, label) in safety_columns {
        lines.push(format!(
            "| {label} | {} |",
            fmt_report_metric(artifact, key)
        ));
    }

    if !summary.category_summaries.is_empty() {
        lines.extend([
            String::new(),
            "## Category pass@1 and observed-conditional companions".to_owned(),
            String::new(),
            "| Category | Mapped phases | Planned pass@1 | Provisional bounds [L, U] | Observed-conditional pass@1 | Committed/Observed | Observation rate |"
                .to_owned(),
            "| --- | --- | ---: | --- | ---: | ---: | ---: |".to_owned(),
        ]);
        for category in CATEGORY_IDS_V5 {
            let Some(cat_summary) = summary.category_summaries.get(category) else {
                continue;
            };
            let phases_label = cat_summary
                .mapped_phases
                .iter()
                .map(|phase| format!("P{phase}"))
                .collect::<Vec<_>>()
                .join("+");
            let bounds = fmt_interval(
                Some((
                    cat_summary.provisional_lower_bound,
                    cat_summary.provisional_upper_bound,
                )),
                3,
            );
            lines.push(format!(
                "| `{category}` | {phases_label} | {} | {bounds} | {} | {}/{} (obs {}/{}) | {} |",
                fmt_f64(cat_summary.planned_pass_at_1, 3),
                fmt_f64(cat_summary.observed_conditional_pass_at_1, 3),
                cat_summary.pass_count,
                cat_summary.committed_mapped_slots,
                cat_summary.observed_slots,
                cat_summary.planned_slots,
                format_fixed(cat_summary.observation_rate, 3),
            ));
        }
    }

    lines.extend([
        String::new(),
        "## Attempt status distribution".to_owned(),
        String::new(),
        "| AttemptStatusV5 | Count |".to_owned(),
        "| --- | ---: |".to_owned(),
    ]);
    for status in ATTEMPT_STATUSES_V5 {
        let count = summary
            .attempt_status_counts
            .get(status)
            .copied()
            .unwrap_or(0);
        lines.push(format!("| `{status}` | {count} |"));
    }

    lines.extend([
        String::new(),
        "## Gate status".to_owned(),
        String::new(),
        "| Gate | Status |".to_owned(),
        "| --- | --- |".to_owned(),
    ]);
    for (gate, status) in &summary.gate_status {
        lines.push(format!("| {gate} | `{status}` |"));
    }
    let g_econ_status = summary.gate_status.get("G-ECON-01").map(String::as_str);
    if !matches!(g_econ_status, Some("PASS" | "FAIL")) {
        lines.push(String::new());
        lines.push(
            "G-ECON-01 is not measured for this artifact because required provider-request accounting evidence is unavailable or incomplete."
                .to_owned(),
        );
    }
    if context.baseline_id.is_some() {
        lines.push(String::new());
        lines.push(
            "`G-BASE-01-NUMERIC` reports this baseline run's numeric threshold check only. Full `G-BASE-01` requires `noop_team`, all five heuristic baselines, and 100% counterfactual flip-pair and ID/row-order permutation passes. A numeric failure is sufficient to fail the full gate; numeric passes alone leave it unmeasured."
                .to_owned(),
        );
    }

    let accounting_cov = summary
        .g_econ_01_details
        .get("accounting_coverage_status")
        .and_then(Value::as_str)
        .unwrap_or("UNAVAILABLE");

    lines.extend([
        String::new(),
        "## Descriptive usage, timing, and cost".to_owned(),
        String::new(),
        format!(
            "- Input tokens actual: {}",
            fmt_i64(summary.suite_input_tokens_actual, 0)
        ),
        format!(
            "- Output tokens actual: {}",
            fmt_i64(summary.suite_output_tokens_actual, 0)
        ),
        format!(
            "- Estimated cost in USD micros: {}",
            fmt_i64(summary.suite_cost_usd_micros, 0)
        ),
        format!("- Accounting coverage status: `{accounting_cov}`"),
        format!(
            "- Historical comparator context: `{}` (descriptive only; non-gating)",
            summary.comparator_status
        ),
    ]);

    if let Some(ref usage) = summary.descriptive_usage {
        let currency_label = escape_inline(usage.currency_code.as_deref().unwrap_or("USD"));
        lines.extend([
            format!(
                "- Wall time (ms): {}",
                fmt_i64(usage.elapsed_wall_time_ms, 0)
            ),
            format!(
                "- Active elapsed (ms): {}",
                fmt_i64(usage.active_elapsed_ms, 0)
            ),
            format!(
                "- Cached input tokens: {}",
                fmt_i64(usage.cached_input_tokens, 0)
            ),
            format!(
                "- Reasoning tokens subset: {}",
                fmt_i64(usage.reasoning_tokens_subset, 0)
            ),
            format!(
                "- Tool serialization tokens subset: {}",
                fmt_i64(usage.tool_serialization_tokens_subset, 0)
            ),
            format!(
                "- Provider billed cost (micros): {}",
                fmt_i64(usage.provider_billed_cost_micros, 0)
            ),
            format!(
                "- Estimated cost (micros, {currency_label}): {}",
                fmt_i64(usage.estimated_cost_micros, 0)
            ),
        ]);
    }

    lines.extend([
        String::new(),
        "## Artifact provenance".to_owned(),
        String::new(),
        format!(
            "- `manifest.json` SHA-256: `{}`",
            provenance.run_manifest_sha256
        ),
        format!("- `episodes.jsonl` SHA-256: `{}`", provenance.episodes_sha256),
        format!(
            "- Historical comparator manifest SHA-256: `{}`",
            provenance
                .comparator_manifest_sha256
                .as_deref()
                .unwrap_or("UNAVAILABLE")
        ),
        format!(
            "- Pricing manifest SHA-256: `{}`",
            provenance
                .pricing_manifest_sha256
                .as_deref()
                .unwrap_or("UNAVAILABLE")
        ),
        format!(
            "- Frozen pricing artifact SHA-256: `{}`",
            provenance
                .pricing_artifact_sha256
                .as_deref()
                .unwrap_or("UNAVAILABLE")
        ),
        format!(
            "- Usage-accounting manifest SHA-256: `{}`",
            provenance
                .usage_accounting_manifest_sha256
                .as_deref()
                .unwrap_or("UNAVAILABLE")
        ),
        format!(
            "- Usage-accounting evidence verified: `{}`",
            provenance.usage_accounting_evidence_verified
        ),
        format!(
            "- Historical comparator metadata validated: `{}`",
            provenance.comparator_evidence_verified
        ),
        format!(
            "- Dataset seal SHA-256: `{}`",
            provenance
                .dataset_seal_sha256
                .as_deref()
                .unwrap_or("UNAVAILABLE")
        ),
        format!(
            "- Scoring inputs recomputed: `{}`",
            provenance.scores_recomputed_from_scoring_inputs
        ),
        String::new(),
        "G-SRR is strict committed resolution over all planned attempt slots. Infrastructure-incomplete (`INCOMPLETE_*`) slots are null rather than zero-imputed; incomplete runs report provisional bounds without rankings or confirmatory p-values."
            .to_owned(),
        String::new(),
    ]);

    lines.join("\n")
}

/// Convert a [`V5ReportArtifact`] to the machine-readable `summary.json` [`Value`].
pub fn summary_to_json(artifact: &V5ReportArtifact) -> Result<Value, ReportError> {
    let summary = &artifact.summary;
    let ctx = &summary.context;

    let mut context_obj = Map::new();
    context_obj.insert("suite_id".to_owned(), Value::String(ctx.suite_id.clone()));
    context_obj.insert("model_id".to_owned(), Value::String(ctx.model_id.clone()));
    context_obj.insert(
        "reasoning_effort".to_owned(),
        Value::String(ctx.reasoning_effort.clone()),
    );
    context_obj.insert("topology".to_owned(), Value::String(ctx.topology.clone()));
    context_obj.insert(
        "steering_mode".to_owned(),
        Value::String(ctx.steering_mode.clone()),
    );
    context_obj.insert(
        "pricing_manifest_sha256".to_owned(),
        opt_str_val(ctx.pricing_manifest_sha256.as_deref()),
    );
    context_obj.insert(
        "model_tier".to_owned(),
        opt_str_val(ctx.model_tier.as_deref()),
    );
    context_obj.insert(
        "baseline_id".to_owned(),
        opt_str_val(ctx.baseline_id.as_deref()),
    );
    context_obj.insert(
        "wave_execution_mode".to_owned(),
        opt_str_val(ctx.wave_execution_mode.as_deref()),
    );
    context_obj.insert(
        "run_config_sha256".to_owned(),
        opt_str_val(ctx.run_config_sha256.as_deref()),
    );
    context_obj.insert(
        "planned_repeats_per_episode".to_owned(),
        Value::Number(Number::from(ctx.planned_repeats_per_episode)),
    );
    context_obj.insert(
        "comparison_track".to_owned(),
        Value::String(ctx.comparison_track.clone()),
    );
    context_obj.insert(
        "topology_profile_sha256".to_owned(),
        opt_str_val(ctx.topology_profile_sha256.as_deref()),
    );
    context_obj.insert(
        "primary_prompt_bundle_sha256".to_owned(),
        opt_str_val(ctx.primary_prompt_bundle_sha256.as_deref()),
    );
    context_obj.insert(
        "max_generations".to_owned(),
        opt_i64_val(ctx.max_generations),
    );
    context_obj.insert("cap_disclosed".to_owned(), Value::Bool(ctx.cap_disclosed));

    let mut metrics_obj = Map::new();
    for (k, v) in &summary.metrics {
        metrics_obj.insert(k.clone(), opt_float_val(*v));
    }

    let mut bounds_obj = Map::new();
    for (k, (low, high)) in &summary.provisional_bounds {
        bounds_obj.insert(
            k.clone(),
            Value::Array(vec![float_value(*low), float_value(*high)]),
        );
    }

    let mut obs_means_obj = Map::new();
    for (k, v) in &summary.observed_slot_means {
        obs_means_obj.insert(k.clone(), opt_float_val(*v));
    }

    let mut cat_obj = Map::new();
    for (cat, cat_summary) in &summary.category_summaries {
        let mut entry = Map::new();
        entry.insert(
            "category".to_owned(),
            Value::String(cat_summary.category.clone()),
        );
        entry.insert(
            "mapped_phases".to_owned(),
            Value::Array(
                cat_summary
                    .mapped_phases
                    .iter()
                    .map(|&p| Value::Number(Number::from(p)))
                    .collect(),
            ),
        );
        entry.insert(
            "planned_slots".to_owned(),
            Value::Number(Number::from(cat_summary.planned_slots)),
        );
        entry.insert(
            "observed_slots".to_owned(),
            Value::Number(Number::from(cat_summary.observed_slots)),
        );
        entry.insert(
            "committed_mapped_slots".to_owned(),
            Value::Number(Number::from(cat_summary.committed_mapped_slots)),
        );
        entry.insert(
            "pass_count".to_owned(),
            Value::Number(Number::from(cat_summary.pass_count)),
        );
        entry.insert(
            "planned_pass_at_1".to_owned(),
            opt_float_val(cat_summary.planned_pass_at_1),
        );
        entry.insert(
            "provisional_lower_bound".to_owned(),
            float_value(cat_summary.provisional_lower_bound),
        );
        entry.insert(
            "provisional_upper_bound".to_owned(),
            float_value(cat_summary.provisional_upper_bound),
        );
        entry.insert(
            "observed_conditional_pass_at_1".to_owned(),
            opt_float_val(cat_summary.observed_conditional_pass_at_1),
        );
        entry.insert(
            "observation_rate".to_owned(),
            float_value(cat_summary.observation_rate),
        );
        cat_obj.insert(cat.clone(), Value::Object(entry));
    }

    let mut status_obj = Map::new();
    for (k, &v) in &summary.attempt_status_counts {
        status_obj.insert(k.clone(), Value::Number(Number::from(v)));
    }

    let descriptive_usage_val = match &summary.descriptive_usage {
        None => Value::Null,
        Some(u) => serde_json::to_value(u).map_err(|e| ReportError::value(e.to_string()))?,
    };

    let mut ep_pass_obj = Map::new();
    for (k, v) in &summary.episode_pass_at_1 {
        ep_pass_obj.insert(k.clone(), opt_float_val(*v));
    }

    let mut fam_means_obj = Map::new();
    for (k, v) in &summary.family_means {
        fam_means_obj.insert(k.clone(), opt_float_val(*v));
    }

    let mut gate_obj = Map::new();
    for (k, v) in &summary.gate_status {
        gate_obj.insert(k.clone(), Value::String(v.clone()));
    }

    let mut econ_obj = Map::new();
    for (k, v) in &summary.g_econ_01_details {
        econ_obj.insert(k.clone(), v.clone());
    }

    let mut ci_obj = Map::new();
    for (metric, (low, high)) in &summary.bootstrap_ci95 {
        ci_obj.insert(
            metric.clone(),
            Value::Array(vec![float_value(*low), float_value(*high)]),
        );
    }

    let episode_scores_arr: Vec<Value> = summary
        .episode_scores
        .iter()
        .map(episode_score_to_mapping)
        .collect();

    let prov = &artifact.provenance;
    let mut prov_obj = Map::new();
    prov_obj.insert(
        "run_manifest_sha256".to_owned(),
        Value::String(prov.run_manifest_sha256.clone()),
    );
    prov_obj.insert(
        "episodes_sha256".to_owned(),
        Value::String(prov.episodes_sha256.clone()),
    );
    prov_obj.insert(
        "comparator_manifest_sha256".to_owned(),
        opt_str_val(prov.comparator_manifest_sha256.as_deref()),
    );
    prov_obj.insert(
        "pricing_manifest_sha256".to_owned(),
        opt_str_val(prov.pricing_manifest_sha256.as_deref()),
    );
    prov_obj.insert(
        "pricing_artifact_sha256".to_owned(),
        opt_str_val(prov.pricing_artifact_sha256.as_deref()),
    );
    prov_obj.insert(
        "usage_accounting_manifest_sha256".to_owned(),
        opt_str_val(prov.usage_accounting_manifest_sha256.as_deref()),
    );
    prov_obj.insert(
        "usage_accounting_evidence_verified".to_owned(),
        Value::Bool(prov.usage_accounting_evidence_verified),
    );
    prov_obj.insert(
        "comparator_evidence_verified".to_owned(),
        Value::Bool(prov.comparator_evidence_verified),
    );
    prov_obj.insert(
        "dataset_seal_sha256".to_owned(),
        opt_str_val(prov.dataset_seal_sha256.as_deref()),
    );
    prov_obj.insert(
        "episode_record_count".to_owned(),
        Value::Number(Number::from(prov.episode_record_count)),
    );
    prov_obj.insert(
        "scores_recomputed_from_scoring_inputs".to_owned(),
        Value::Bool(prov.scores_recomputed_from_scoring_inputs),
    );
    prov_obj.insert(
        "scoring_evidence_status".to_owned(),
        Value::String(prov.scoring_evidence_status.clone()),
    );

    let mut root = Map::new();
    root.insert("context".to_owned(), Value::Object(context_obj));
    root.insert(
        "n_episodes".to_owned(),
        Value::Number(Number::from(summary.n_episodes)),
    );
    root.insert("is_complete".to_owned(), Value::Bool(summary.is_complete));
    root.insert(
        "planned_slots".to_owned(),
        Value::Number(Number::from(summary.planned_slots)),
    );
    root.insert(
        "observed_slots".to_owned(),
        Value::Number(Number::from(summary.observed_slots)),
    );
    root.insert(
        "completion_coverage".to_owned(),
        float_value(summary.completion_coverage),
    );
    root.insert("metrics".to_owned(), Value::Object(metrics_obj));
    root.insert("provisional_bounds".to_owned(), Value::Object(bounds_obj));
    root.insert(
        "observed_slot_means".to_owned(),
        Value::Object(obs_means_obj),
    );
    root.insert("category_summaries".to_owned(), Value::Object(cat_obj));
    root.insert(
        "attempt_status_counts".to_owned(),
        Value::Object(status_obj),
    );
    root.insert("descriptive_usage".to_owned(), descriptive_usage_val);
    root.insert("episode_pass_at_1".to_owned(), Value::Object(ep_pass_obj));
    root.insert("family_means".to_owned(), Value::Object(fam_means_obj));
    root.insert("gate_status".to_owned(), Value::Object(gate_obj));
    root.insert(
        "comparator_status".to_owned(),
        Value::String(summary.comparator_status.clone()),
    );
    root.insert(
        "historical_comparator_context".to_owned(),
        summary
            .historical_comparator_context
            .clone()
            .unwrap_or(Value::Null),
    );
    root.insert(
        "cost_ceiling_usd_micros".to_owned(),
        summary
            .cost_ceiling_usd_micros
            .map_or(Value::Null, |v| Value::String(v.to_string())),
    );
    root.insert(
        "suite_input_tokens_actual".to_owned(),
        opt_i64_val(summary.suite_input_tokens_actual),
    );
    root.insert(
        "suite_output_tokens_actual".to_owned(),
        opt_i64_val(summary.suite_output_tokens_actual),
    );
    root.insert(
        "suite_cost_usd_micros".to_owned(),
        opt_i64_val(summary.suite_cost_usd_micros),
    );
    root.insert("g_econ_01_details".to_owned(), Value::Object(econ_obj));
    root.insert("bootstrap_ci95".to_owned(), Value::Object(ci_obj));
    root.insert(
        "episode_scores".to_owned(),
        Value::Array(episode_scores_arr),
    );
    root.insert("provenance".to_owned(), Value::Object(prov_obj));

    Ok(Value::Object(root))
}

/// Write `report.md` and `summary.json` into `out_dir`, returning `(report_path, summary_path)`.
pub fn write_v5_report(
    artifact: &V5ReportArtifact,
    out_dir: &Path,
) -> Result<(PathBuf, PathBuf), ReportError> {
    std::fs::create_dir_all(out_dir)?;
    let report_path = out_dir.join("report.md");
    let summary_path = out_dir.join("summary.json");
    let md = render_v5_markdown_report(artifact);
    std::fs::write(&report_path, md.as_bytes())?;

    let summary_val = summary_to_json(artifact)?;
    let summary_json = dumps(
        &summary_val,
        &DumpOptions::pretty_sorted().with_ensure_ascii(true),
    )? + "\n";
    std::fs::write(&summary_path, summary_json.as_bytes())?;
    Ok((report_path, summary_path))
}
