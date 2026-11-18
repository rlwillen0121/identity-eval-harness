//! Canonical run artifacts shared by deterministic scoring and provider execution.
use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::Path;

use anyhow::{Context, Result, ensure};
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
        write_dataset_copy_leaf_if_absent(&directory.join(name), raw)?;
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
        copied_dataset_leaf_matches(&path, raw)?;
    }
    Ok(())
}

fn open_existing_dataset_copy_leaf(path: &Path) -> Result<Option<File>> {
    let before = match std::fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error).context("copied dataset artifact could not be inspected"),
    };
    ensure!(
        before.file_type().is_file(),
        "existing copied dataset artifact is not a regular file"
    );

    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let file = options
        .open(path)
        .context("copied dataset artifact could not be opened")?;
    let after = file
        .metadata()
        .context("copied dataset artifact could not be inspected")?;
    ensure!(
        after.file_type().is_file(),
        "existing copied dataset artifact is not a regular file"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        ensure!(
            before.dev() == after.dev() && before.ino() == after.ino(),
            "copied dataset artifact changed while it was opened"
        );
    }
    Ok(Some(file))
}

fn copied_dataset_leaf_matches(path: &Path, expected: &[u8]) -> Result<bool> {
    let Some(mut file) = open_existing_dataset_copy_leaf(path)? else {
        return Ok(false);
    };
    let expected_len = u64::try_from(expected.len())?;
    ensure!(
        file.metadata()?.len() == expected_len,
        "existing copied dataset does not match the sealed input"
    );
    let mut buffer = [0_u8; 8192];
    for expected_chunk in expected.chunks(buffer.len()) {
        file.read_exact(&mut buffer[..expected_chunk.len()])?;
        ensure!(
            buffer[..expected_chunk.len()] == *expected_chunk,
            "existing copied dataset does not match the sealed input"
        );
    }
    let mut extra = [0_u8; 1];
    ensure!(
        file.read(&mut extra)? == 0,
        "existing copied dataset does not match the sealed input"
    );
    Ok(true)
}

fn write_dataset_copy_leaf_if_absent(path: &Path, expected: &[u8]) -> Result<()> {
    if copied_dataset_leaf_matches(path, expected)? {
        return Ok(());
    }

    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    temporary.write_all(expected)?;
    temporary.as_file().sync_all()?;
    match temporary.persist_noclobber(path) {
        Ok(_) => Ok(()),
        Err(error) if error.error.kind() == std::io::ErrorKind::AlreadyExists => {
            ensure!(
                copied_dataset_leaf_matches(path, expected)?,
                "existing copied dataset does not match the sealed input"
            );
            Ok(())
        }
        Err(error) => Err(error.error).context("copied dataset artifact could not be written"),
    }
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

#[cfg(test)]
mod tests {
    use super::*;
    use ib_v5_generator::LoadedV5Dataset;
    use std::sync::{Arc, Barrier};

    fn test_dataset(root: &Path) -> LoadedV5Dataset {
        let dataset_dir = root.join("source");
        std::fs::create_dir_all(&dataset_dir).expect("create source dataset directory");
        LoadedV5Dataset {
            dataset_dir,
            seal: serde_json::Map::new(),
            seal_bytes: b"seal".to_vec(),
            episodes_bytes: b"episodes".to_vec(),
            oracles_bytes: b"oracles".to_vec(),
            episodes: Vec::new(),
            oracles: Vec::new(),
        }
    }

    #[test]
    fn copy_dataset_artifacts_writes_each_expected_leaf() {
        let temporary = tempfile::tempdir().expect("temp dir");
        let artifacts = test_dataset(temporary.path());
        let out = temporary.path().join("run");

        copy_dataset_artifacts(&artifacts, &out).expect("copy dataset artifacts");

        for (name, expected) in [
            ("episodes.jsonl", artifacts.episodes_bytes.as_slice()),
            ("oracles.jsonl", artifacts.oracles_bytes.as_slice()),
            ("SEAL.json", artifacts.seal_bytes.as_slice()),
        ] {
            let path = out.join("dataset").join(name);
            assert!(
                std::fs::symlink_metadata(&path)
                    .unwrap()
                    .file_type()
                    .is_file()
            );
            assert_eq!(std::fs::read(path).unwrap(), expected);
        }
    }

    #[test]
    fn preflight_rejects_nonregular_and_wrong_sized_dataset_leaves() {
        let temporary = tempfile::tempdir().expect("temp dir");
        let artifacts = test_dataset(temporary.path());

        let directory_out = temporary.path().join("directory-output");
        let directory_leaf = directory_out.join("dataset/episodes.jsonl");
        std::fs::create_dir_all(&directory_leaf).expect("create directory artifact");
        let error = preflight_dataset_copy(&artifacts, &directory_out).unwrap_err();
        assert!(error.to_string().contains("not a regular file"));

        let oversized_out = temporary.path().join("oversized-output");
        let oversized_leaf = oversized_out.join("dataset/episodes.jsonl");
        std::fs::create_dir_all(oversized_leaf.parent().unwrap()).expect("create output");
        let sparse = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&oversized_leaf)
            .expect("create sparse artifact");
        sparse
            .set_len(1024 * 1024 * 1024)
            .expect("set sparse artifact size");
        let error = preflight_dataset_copy(&artifacts, &oversized_out).unwrap_err();
        assert!(
            error
                .to_string()
                .contains("does not match the sealed input")
        );
    }

    #[cfg(unix)]
    #[test]
    fn preflight_rejects_a_symlinked_copy_leaf_even_when_bytes_match() {
        use std::os::unix::fs::symlink;

        let temporary = tempfile::tempdir().expect("temp dir");
        let artifacts = test_dataset(temporary.path());
        let out = temporary.path().join("run");
        let dataset_dir = out.join("dataset");
        std::fs::create_dir_all(&dataset_dir).expect("create output dataset directory");
        let target = out.join("matching-bytes");
        std::fs::write(&target, &artifacts.episodes_bytes).expect("write target bytes");
        symlink(&target, dataset_dir.join("episodes.jsonl")).expect("create artifact alias");

        let error = preflight_dataset_copy(&artifacts, &out).unwrap_err();
        assert!(error.to_string().contains("not a regular file"));
        assert_eq!(std::fs::read(target).unwrap(), artifacts.episodes_bytes);
    }

    #[test]
    fn concurrent_dataset_copy_creation_does_not_replace_the_winner() {
        let temporary = tempfile::tempdir().expect("temp dir");
        let destination = temporary.path().join("dataset/episodes.jsonl");
        std::fs::create_dir_all(destination.parent().unwrap()).expect("create output directory");
        let barrier = Arc::new(Barrier::new(2));
        let left_barrier = Arc::clone(&barrier);
        let left_path = destination.clone();
        let left = std::thread::spawn(move || {
            left_barrier.wait();
            write_dataset_copy_leaf_if_absent(&left_path, b"left")
        });
        let right_barrier = Arc::clone(&barrier);
        let right_path = destination.clone();
        let right = std::thread::spawn(move || {
            right_barrier.wait();
            write_dataset_copy_leaf_if_absent(&right_path, b"rght")
        });
        let left = left.join().expect("left copy thread");
        let right = right.join().expect("right copy thread");

        assert_ne!(left.is_ok(), right.is_ok());
        let stored = std::fs::read(destination).expect("read winning artifact");
        assert!(stored == b"left" || stored == b"rght");
    }
}
