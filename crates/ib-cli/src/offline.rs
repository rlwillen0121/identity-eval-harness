//! Offline v5 commands; none of these paths load a provider or an environment file.
use std::path::Path;

use anyhow::{Result, ensure};
use ib_core::{hashing::sha256_hex, jcs_dumps};
use ib_v5_accounting::summarize_descriptive_usage_v5;
use ib_v5_baselines::{ALL_V5_BASELINES, default_run_config_for_baseline_v5};
use ib_v5_generator::{V5Suite, load_v5_dataset, write_v5_dataset};
use ib_v5_report::{report_from_raw_artifacts, write_v5_report};
use ib_v5_validation::{ValidationOptions, validate_v5_dataset};
use serde_json::{Value, json};

use crate::{BuildArgs, ReportArgs, ScoreArgs, ValidateArgs, artifacts, paths};

pub fn build(root: &Path, args: BuildArgs) -> Result<Value> {
    let suite = args.suite.parse::<V5Suite>()?;
    if suite != V5Suite::V5Dev && !args.freeze {
        return Err(crate::usage_error(format!(
            "writing {} requires --freeze",
            args.suite
        )));
    }
    let target = args
        .out
        .as_deref()
        .map(|p| paths::resolve(root, p))
        .unwrap_or_else(|| root.join("datasets").join(&args.suite));
    // Preserve immutable sealed artifacts even when --freeze authorizes a new held-out build.
    for name in ["SEAL.json", "episodes.jsonl", "oracles.jsonl"] {
        ensure!(
            !target.join(name).try_exists()?,
            "output already contains dataset artifacts; use a new output directory"
        );
    }
    let seal = write_v5_dataset(suite, &target, args.seed, args.freeze)?;
    Ok(json!({"dataset_dir": target, "seal": seal}))
}

pub fn validate(root: &Path, args: ValidateArgs) -> Result<(Value, u8)> {
    let dataset = args
        .dataset
        .as_deref()
        .map(|p| paths::resolve(root, p))
        .unwrap_or_else(|| paths::default_dataset(root, &args.suite));
    let proof = match args.blind_proof.as_deref() {
        Some(path) => Some(paths::provenance_input(root, path)?),
        None if paths::same_path(&dataset, &paths::default_dataset(root, &args.suite))? => {
            Some(paths::default_draft5_receipt(root, &args.suite))
        }
        None => None,
    };
    let iga = args
        .iga_review
        .as_deref()
        .map(|p| paths::provenance_input(root, p))
        .transpose()?;
    // Native receipts require a trusted identity from this compiled executable.
    // If capture fails, the native G-SPEC gate receives no trusted identity and fails
    // closed; archival Python receipt validation does not depend on this value.
    let native_build_identity = proof
        .as_ref()
        .and_then(|_| crate::conformance::current_native_build_identity(root).ok());
    let options = ValidationOptions {
        repository_root: Some(root.to_path_buf()),
        suite: Some(args.suite),
        double_build: args.double_build,
        blind_proof_path: proof,
        iga_review_manifest_path: iga,
        run_baselines: !args.skip_baselines,
        native_build_identity,
        ..Default::default()
    };
    let report = validate_v5_dataset(&dataset, &options)?;
    Ok((report.to_mapping(), if report.passed() { 0 } else { 1 }))
}

pub fn report(root: &Path, args: ReportArgs) -> Result<Value> {
    let run = paths::resolve(root, &args.run);
    let out = args
        .out
        .as_deref()
        .map(|p| paths::resolve(root, p))
        .unwrap_or_else(|| run.join("report"));
    let artifact = report_from_raw_artifacts(&run)?;
    let (report, summary) = write_v5_report(&artifact, &out)?;
    Ok(json!({"report": report, "summary": summary,
        "is_complete": artifact.summary.is_complete,
        "observed_slots": artifact.summary.observed_slots,
        "planned_slots": artifact.summary.planned_slots}))
}

pub async fn score(root: &Path, args: ScoreArgs) -> Result<Value> {
    let dataset_dir = args
        .dataset
        .as_deref()
        .map(|p| paths::resolve(root, p))
        .unwrap_or_else(|| paths::default_dataset(root, &args.suite));
    let dataset = load_v5_dataset(&dataset_dir)?;
    ensure!(
        dataset.seal["suite"].as_str() == Some(args.suite.as_str()),
        "dataset suite does not match --suite"
    );
    let baselines = if args.baseline.is_empty() {
        ALL_V5_BASELINES
            .iter()
            .map(|s| (*s).to_owned())
            .collect::<Vec<_>>()
    } else {
        args.baseline
    };
    for id in &baselines {
        if !ALL_V5_BASELINES.contains(&id.as_str()) {
            return Err(crate::usage_error(format!("unknown baseline ID: {id}")));
        }
    }
    let base = args
        .out
        .as_deref()
        .map(|p| paths::resolve(root, p))
        .unwrap_or_else(|| root.join("runs").join(format!("{}-baselines", args.suite)));
    paths::ensure_output_separate(&base, &dataset.dataset_dir)?;
    let output_dirs: Vec<_> = baselines
        .iter()
        .map(|id| {
            if baselines.len() > 1 {
                base.join(id)
            } else {
                base.clone()
            }
        })
        .collect();
    // Validate every destination before producing any baseline records. A later
    // conflicting dataset copy must not leave earlier baseline outputs behind.
    for out in &output_dirs {
        paths::ensure_output_separate(out, &dataset.dataset_dir)?;
        artifacts::preflight_dataset_copy(&dataset, out)?;
    }
    let mut completed = Vec::new();
    for (id, out) in baselines.iter().zip(&output_dirs) {
        let config = json!({
            "model_id": format!("baseline:{id}"), "baseline_id": id, "effort": "medium",
            "topology_id": "T0_SINGLE_MONOLITH", "steering_mode": "UNSTEERED",
            "wave_execution_mode": "COUPLED", "planned_repeats_per_episode": args.repeats,
            "comparison_track": "PRIMARY_HIGHEST"
        });
        let config_sha = sha256_hex(&jcs_dumps(&config)?);
        let mut records = Vec::new();
        let mut attempts = Vec::new();
        for (episode, oracle) in dataset.episodes.iter().zip(&dataset.oracles) {
            for repeat in 1..=args.repeats {
                let mut run_config = default_run_config_for_baseline_v5(id)?;
                run_config.repeat_index = repeat as u64;
                run_config.run_config_sha256 = Some(config_sha.clone());
                let reset = (run_config.wave_execution_mode == "ORACLE_PHASE_RESET")
                    .then(|| oracle.omniscient_s2_gold.clone());
                let execution = ib_v5_baselines::run_v5_baseline_with_oracle(
                    id, episode, oracle, run_config, reset,
                )
                .await?;
                let (mut row, attempt) = artifacts::execution_record(
                    &execution,
                    episode,
                    oracle,
                    &config_sha,
                    Default::default(),
                )?;
                row["baseline_id"] = json!(id);
                row["repeat_index"] = json!(repeat);
                records.push(row);
                attempts.push(attempt);
            }
        }
        let raw = artifacts::write_records(&records, out)?;
        let copied = artifacts::copy_dataset_artifacts(&dataset, out)?;
        let usage = summarize_descriptive_usage_v5(
            &config_sha,
            dataset.episodes.len() as i64 * args.repeats,
            &attempts,
            Default::default(),
        )?;
        let mut manifest =
            artifacts::run_manifest(&dataset, &raw, &records, config, &config_sha, copied);
        manifest["descriptive_usage_summary"] = serde_json::to_value(usage)?;
        artifacts::write_manifest(out, &manifest)?;
        completed.push(out.clone());
    }
    Ok(json!({"run_dirs": completed, "suite_id": args.suite, "repeats": args.repeats}))
}
