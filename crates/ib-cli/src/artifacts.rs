//! Canonical run artifacts shared by deterministic scoring and provider execution.
use std::io::Write;
use std::path::Path;

use anyhow::{Result, ensure};
use ib_core::{hashing::sha256_hex, jcs_dumps};
use ib_v5_generator::LoadedV5Dataset;
use ib_v5_schemas::{EpisodeAttemptV5, EpisodeV5, SealedEpisodeOracleV5};
use ib_v5_scoring::{
    BuildEpisodeAttemptOptionsV5, EpisodeExecutionV5, ScoreExecutionOptionsV5,
    build_episode_attempt_v5, episode_score_to_mapping, episode_scoring_input_to_mapping,
    score_execution_v5,
};
use serde_json::{Value, json};

/// Replace complete artifacts atomically; a failed write keeps the prior checkpoint readable.
pub fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(parent)?;
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    file.write_all(bytes)?;
    file.as_file().sync_all()?;
    file.persist(path)?;
    Ok(())
}

pub fn copy_dataset_artifacts(artifacts: &LoadedV5Dataset, out: &Path) -> Result<Value> {
    let directory = out.join("dataset");
    let files = [
        ("episodes.jsonl", &artifacts.episodes_bytes),
        ("oracles.jsonl", &artifacts.oracles_bytes),
        ("SEAL.json", &artifacts.seal_bytes),
    ];
    // Resume reuses the original copied bytes. A conflicting copy is an input
    // integrity error, not permission to replace sealed artifacts.
    preflight_dataset_copy(artifacts, out)?;
    std::fs::create_dir_all(&directory)?;
    for (name, raw) in files {
        let path = directory.join(name);
        if !path.try_exists()? {
            atomic_write(&path, raw)?;
        }
    }
    Ok(json!({
        "dataset_artifact_paths": {"episodes":"dataset/episodes.jsonl",
            "oracles":"dataset/oracles.jsonl","seal":"dataset/SEAL.json"},
        "dataset_episodes_sha256": sha256_hex(&artifacts.episodes_bytes),
        "dataset_oracles_sha256": sha256_hex(&artifacts.oracles_bytes),
        "dataset_seal_sha256": sha256_hex(&artifacts.seal_bytes)
    }))
}

/// Check an existing run's copied dataset without creating or changing output artifacts.
pub fn preflight_dataset_copy(artifacts: &LoadedV5Dataset, out: &Path) -> Result<()> {
    let directory = out.join("dataset");
    crate::paths::ensure_dataset_copy_separate(&directory, &artifacts.dataset_dir)?;
    for (name, raw) in [
        ("episodes.jsonl", &artifacts.episodes_bytes),
        ("oracles.jsonl", &artifacts.oracles_bytes),
        ("SEAL.json", &artifacts.seal_bytes),
    ] {
        let path = directory.join(name);
        if path.try_exists()? {
            ensure!(
                std::fs::read(path)?.as_slice() == raw.as_slice(),
                "existing copied dataset does not match the sealed input"
            );
        }
    }
    Ok(())
}

pub fn write_records(records: &[Value], out: &Path) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    for record in records {
        bytes.extend(jcs_dumps(record)?);
        bytes.push(b'\n');
    }
    atomic_write(&out.join("episodes.jsonl"), &bytes)?;
    Ok(bytes)
}

pub fn write_manifest(out: &Path, manifest: &Value) -> Result<()> {
    let mut bytes = jcs_dumps(manifest)?;
    bytes.push(b'\n');
    atomic_write(&out.join("manifest.json"), &bytes)
}

pub fn run_manifest(
    artifacts: &LoadedV5Dataset,
    raw: &[u8],
    records: &[Value],
    config: Value,
    config_sha: &str,
    copied_dataset: Value,
) -> Value {
    let mut result = json!({
        "schema_version": "identitybench-v5-run-v1",
        "suite_id": artifacts.seal["suite"],
        "run_episode_record_count": records.len(),
        "run_episodes_sha256": sha256_hex(raw),
        "run_config_sha256": config_sha,
        "config": config
    });
    if let Some(copied) = copied_dataset.as_object() {
        result.as_object_mut().unwrap().extend(copied.clone());
    }
    result
}

pub fn execution_record(
    execution: &EpisodeExecutionV5,
    episode: &EpisodeV5,
    oracle: &SealedEpisodeOracleV5,
    config_sha: &str,
    options: ScoreExecutionOptionsV5<'_>,
) -> Result<(Value, EpisodeAttemptV5)> {
    ensure!(
        episode.episode_id == oracle.episode_id,
        "sealed oracle does not match the episode"
    );
    let (input, score) = score_execution_v5(execution, episode, oracle, options)?;
    let attempt = build_episode_attempt_v5(
        &score,
        config_sha,
        BuildEpisodeAttemptOptionsV5 {
            attempt_id: None,
            completed_assistant_generations: execution.completed_assistant_generations,
            tool_call_count: execution.tool_calls_executed,
            retry_request_count: execution.retry_request_count,
            elapsed_wall_time_ms: execution.elapsed_wall_time_ms,
            terminal_outcome_elapsed_ms: execution.terminal_outcome_elapsed_ms,
            active_elapsed_ms: execution.active_elapsed_ms,
            terminal_reservation_ordinal: execution.terminal_reservation_ordinal.clone(),
            terminal_cause: execution.terminal_cause.clone(),
            context_limit_cause: execution.context_limit_cause.clone(),
            provider_requests: execution.provider_requests.clone(),
        },
    )?;
    let mut row = execution.to_json_value()?;
    row["scoring_input"] = episode_scoring_input_to_mapping(&input);
    row["score"] = episode_score_to_mapping(&score);
    row["attempt"] = serde_json::to_value(&attempt)?;
    Ok((row, attempt))
}
