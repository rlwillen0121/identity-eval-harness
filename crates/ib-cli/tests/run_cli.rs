use std::path::Path;
use std::process::{Command, Output};

use serde_json::Value;
use tempfile::tempdir;

fn binary() -> &'static str {
    env!("CARGO_BIN_EXE_identitybench")
}

fn run_cli(root: &Path, args: &[&str]) -> Output {
    Command::new(binary())
        .arg("--root")
        .arg(root)
        .arg("--no-env-file")
        .args(args)
        .output()
        .expect("identitybench process starts")
}

fn workspace_root() -> &'static Path {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
}

#[test]
fn run_cli_executes_only_the_local_replay_provider_and_persists_artifacts() {
    let temp = tempdir().expect("temporary workspace");
    let dataset = temp.path().join("datasets/v5_draft5/v5_dev");
    std::fs::create_dir_all(&dataset).expect("active Draft 5 dataset directory");
    let active_dataset = workspace_root().join("datasets/v5_draft5/v5_dev");
    for name in ["SEAL.json", "episodes.jsonl", "oracles.jsonl"] {
        std::fs::copy(active_dataset.join(name), dataset.join(name))
            .expect("copy the active Draft 5 dataset");
    }

    let replay = temp.path().join("responses.jsonl");
    std::fs::write(&replay, b"").expect("empty local replay log");
    let config = temp.path().join("model.yaml");
    std::fs::write(
        &config,
        format!(
            "model:\n  provider: replay\n  model: offline-replay\n  concurrency: 3\n  extra:\n    path: {:?}\nrun_config:\n  max_transient_retries: 0\n",
            replay
        ),
    )
    .expect("local model config");
    let pricing = temp.path().join("pricing.json");
    std::fs::write(
        &pricing,
        br#"{"uncached_input_micros_per_million":0,"cached_input_micros_per_million":0,"output_micros_per_million":0,"reasoning_micros_per_million":0}"#,
    )
    .expect("local pricing artifact");
    let output_dir = temp.path().join("run");
    // Run with a temporary root that has no source checkout. Source identity must be embedded
    // in the compiled binary and work for installed binaries as well as workspace builds.
    let result = run_cli(
        temp.path(),
        &[
            "v5",
            "run",
            "--config",
            config.to_str().unwrap(),
            "--pricing",
            pricing.to_str().unwrap(),
            "--suite",
            "v5_dev",
            "--out",
            output_dir.to_str().unwrap(),
            "--repeats",
            "1",
        ],
    );
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let result: Value = serde_json::from_slice(&result.stdout).expect("run result JSON");
    assert_eq!(result["episode_count"], 12);
    assert_eq!(result["planned_repeats_per_episode"], 1);
    assert_eq!(result["model_id"], "replay:offline-replay");
    assert!(output_dir.join("manifest.json").is_file());
    assert!(output_dir.join("episodes.jsonl").is_file());
    assert!(!output_dir.join("checkpoint.json").exists());
    let manifest: Value = serde_json::from_slice(
        &std::fs::read(output_dir.join("manifest.json")).expect("run manifest"),
    )
    .expect("manifest JSON");
    assert_eq!(manifest["run_episode_record_count"], 12);
    assert_eq!(
        manifest["dataset_seal_sha256"],
        ib_core::hashing::sha256_hex(&std::fs::read(active_dataset.join("SEAL.json")).unwrap())
    );
    let source_hashes = manifest["config"]["rust_source_sha256"]
        .as_object()
        .expect("embedded Rust source identity map");
    assert!(source_hashes.contains_key("crates/ib-cli/src/run.rs"));
    assert!(source_hashes.contains_key("crates/ib-cli/src/run_config.rs"));
    assert!(source_hashes.contains_key("crates/ib-cli/src/main.rs"));
    assert!(source_hashes.contains_key("crates/ib-cli/src/artifacts.rs"));
    assert!(source_hashes.contains_key("crates/ib-cli/Cargo.toml"));
    assert!(source_hashes.contains_key("crates/ib-cli/build.rs"));
    assert!(source_hashes.contains_key("Cargo.lock"));
    assert!(source_hashes.contains_key("crates/ib-v5-runner/src/prompts.rs"));
    assert!(source_hashes.contains_key("crates/ib-providers/src/factory.rs"));
    assert!(source_hashes.contains_key("crates/ib-providers/src/replay.rs"));
    assert!(manifest["config"].get("prompt_sha256").is_some());

    let resumed = run_cli(
        temp.path(),
        &[
            "v5",
            "run",
            "--config",
            config.to_str().unwrap(),
            "--pricing",
            pricing.to_str().unwrap(),
            "--suite",
            "v5_dev",
            "--out",
            output_dir.to_str().unwrap(),
            "--repeats",
            "1",
            "--resume",
        ],
    );
    assert!(
        resumed.status.success(),
        "{}",
        String::from_utf8_lossy(&resumed.stderr)
    );
    assert_eq!(
        std::fs::read(output_dir.join("dataset/SEAL.json")).unwrap(),
        std::fs::read(dataset.join("SEAL.json")).unwrap()
    );
}

#[test]
fn run_cli_rejects_output_that_aliases_or_overlaps_the_sealed_dataset() {
    let temp = tempdir().expect("temporary workspace");
    let dataset = temp.path().join("dataset");
    let built = run_cli(
        workspace_root(),
        &[
            "v5",
            "build",
            "--suite",
            "v5_dev",
            "--out",
            dataset.to_str().unwrap(),
        ],
    );
    assert!(
        built.status.success(),
        "{}",
        String::from_utf8_lossy(&built.stderr)
    );
    let sealed_bytes = ["SEAL.json", "episodes.jsonl", "oracles.jsonl"]
        .map(|name| std::fs::read(dataset.join(name)).expect("sealed input bytes"));

    let replay = temp.path().join("responses.jsonl");
    std::fs::write(&replay, b"").expect("empty local replay log");
    let config = temp.path().join("model.yaml");
    std::fs::write(
        &config,
        format!(
            "model:\n  provider: replay\n  model: offline-replay\n  extra:\n    path: {:?}\n",
            replay
        ),
    )
    .expect("local model config");
    let pricing = temp.path().join("pricing.json");
    std::fs::write(
        &pricing,
        br#"{"uncached_input_micros_per_million":0,"cached_input_micros_per_million":0,"output_micros_per_million":0,"reasoning_micros_per_million":0}"#,
    )
    .expect("local pricing artifact");

    let mismatched_suite_out = temp.path().join("mismatched-suite-run");
    let mismatched_suite = run_cli(
        temp.path(),
        &[
            "v5",
            "run",
            "--config",
            config.to_str().unwrap(),
            "--pricing",
            pricing.to_str().unwrap(),
            "--suite",
            "v5_core",
            "--dataset",
            dataset.to_str().unwrap(),
            "--out",
            mismatched_suite_out.to_str().unwrap(),
            "--repeats",
            "1",
        ],
    );
    assert!(!mismatched_suite.status.success());
    assert!(
        String::from_utf8_lossy(&mismatched_suite.stderr)
            .contains("dataset suite does not match --suite")
    );
    assert!(!mismatched_suite_out.exists());

    let run_with_output = |output: &Path| {
        run_cli(
            workspace_root(),
            &[
                "v5",
                "run",
                "--config",
                config.to_str().unwrap(),
                "--pricing",
                pricing.to_str().unwrap(),
                "--suite",
                "v5_dev",
                "--dataset",
                dataset.to_str().unwrap(),
                "--out",
                output.to_str().unwrap(),
                "--repeats",
                "1",
            ],
        )
    };

    let exact = run_with_output(&dataset);
    assert!(!exact.status.success());
    assert!(String::from_utf8_lossy(&exact.stderr).contains("overlaps a sealed dataset"));

    #[cfg(unix)]
    {
        let alias = temp.path().join("dataset-alias");
        std::os::unix::fs::symlink(&dataset, &alias).expect("dataset output alias");
        let linked = run_with_output(&alias);
        assert!(!linked.status.success());
        assert!(String::from_utf8_lossy(&linked.stderr).contains("overlaps a sealed dataset"));

        let nested_output = temp.path().join("nested-output");
        std::fs::create_dir(&nested_output).expect("nested output directory");
        std::os::unix::fs::symlink(&dataset, nested_output.join("dataset"))
            .expect("nested dataset alias");
        let nested = run_with_output(&nested_output);
        assert!(!nested.status.success());
        assert!(String::from_utf8_lossy(&nested.stderr).contains("overlaps a sealed dataset"));
    }

    for (index, name) in ["SEAL.json", "episodes.jsonl", "oracles.jsonl"]
        .iter()
        .enumerate()
    {
        assert_eq!(
            std::fs::read(dataset.join(name)).unwrap(),
            sealed_bytes[index]
        );
    }
}

#[test]
fn run_cli_rejects_invalid_repeat_count_as_usage_error() {
    let temp = tempdir().expect("temporary workspace");
    let config = temp.path().join("model.yaml");
    let pricing = temp.path().join("pricing.json");
    let output = temp.path().join("run");
    let result = run_cli(
        workspace_root(),
        &[
            "v5",
            "run",
            "--config",
            config.to_str().unwrap(),
            "--pricing",
            pricing.to_str().unwrap(),
            "--repeats",
            "2",
            "--out",
            output.to_str().unwrap(),
        ],
    );
    assert_eq!(result.status.code(), Some(2));
    assert!(String::from_utf8_lossy(&result.stderr).contains("--repeats must be 1 or 3"));
}

#[test]
fn invalid_run_config_is_sanitized_before_environment_file_warnings() {
    let temp = tempdir().expect("temporary workspace");
    let dataset = temp.path().join("dataset");
    let built = run_cli(
        workspace_root(),
        &[
            "v5",
            "build",
            "--suite",
            "v5_dev",
            "--out",
            dataset.to_str().unwrap(),
        ],
    );
    assert!(
        built.status.success(),
        "{}",
        String::from_utf8_lossy(&built.stderr)
    );

    let replay = temp.path().join("responses.jsonl");
    std::fs::write(&replay, b"").expect("empty local replay log");
    let config = temp.path().join("invalid.yaml");
    std::fs::write(
        &config,
        format!(
            "model:\n  provider: replay\n  model: offline-replay\n  extra:\n    path: {:?}\nrun_config:\n  private_marker: DO_NOT_PRINT_8675309\n",
            replay
        ),
    )
    .expect("invalid config");
    let pricing = temp.path().join("pricing.json");
    std::fs::write(
        &pricing,
        br#"{"uncached_input_micros_per_million":0,"cached_input_micros_per_million":0,"output_micros_per_million":0,"reasoning_micros_per_million":0}"#,
    )
    .expect("local pricing artifact");
    let env_file = temp.path().join("provider.env");
    std::fs::write(&env_file, b"this is intentionally malformed\n").expect("env file");

    let result = Command::new(binary())
        .arg("--root")
        .arg(workspace_root())
        .arg("--env-file")
        .arg(&env_file)
        .args([
            "v5",
            "run",
            "--config",
            config.to_str().unwrap(),
            "--pricing",
            pricing.to_str().unwrap(),
            "--suite",
            "v5_dev",
            "--dataset",
            dataset.to_str().unwrap(),
            "--out",
            temp.path().join("run").to_str().unwrap(),
            "--repeats",
            "1",
        ])
        .output()
        .expect("identitybench process starts");
    assert!(!result.status.success());
    let stderr = String::from_utf8_lossy(&result.stderr);
    assert!(stderr.contains("v5 run inputs are invalid"));
    assert!(!stderr.contains("DO_NOT_PRINT_8675309"));
    assert!(!stderr.contains("warning:"));
}
