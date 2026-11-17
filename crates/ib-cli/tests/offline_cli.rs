use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::Value;

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf()
}

fn cli(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_identitybench"))
        .args(["--root", root().to_str().unwrap()])
        .args(args)
        .output()
        .unwrap()
}

fn success(output: Output) -> Value {
    assert!(
        output.status.success(),
        "stdout={} stderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn executable_lists_only_the_native_v5_group() {
    let output = cli(&["v5", "--help"]);
    assert!(output.status.success());
    let help = String::from_utf8(output.stdout).unwrap();
    for command in ["build", "validate", "score", "run", "report"] {
        assert!(help.contains(command));
    }
    assert_eq!(cli(&["v41", "--help"]).status.code(), Some(2));
    assert_eq!(
        cli(&["v5", "score", "--repeats", "2"]).status.code(),
        Some(2)
    );
}

#[test]
fn offline_build_never_reads_explicit_env_and_preserves_existing_seals() {
    let dir = tempfile::tempdir().unwrap();
    let output_dir = dir.path().join("dataset");
    let missing_env = dir.path().join("missing.env");
    let args = [
        "--env-file",
        missing_env.to_str().unwrap(),
        "v5",
        "build",
        "--suite",
        "v5_dev",
        "--seed",
        "20261114",
        "--out",
        output_dir.to_str().unwrap(),
    ];
    let payload = success(cli(&args));
    assert_eq!(payload["seal"]["episode_count"], 12);
    let reference = root().join("datasets/v5_draft5/v5_dev");
    for name in ["SEAL.json", "episodes.jsonl", "oracles.jsonl"] {
        assert_eq!(
            std::fs::read(output_dir.join(name)).unwrap(),
            std::fs::read(reference.join(name)).unwrap(),
            "{name}"
        );
    }
    assert!(!cli(&args).status.success());
    assert_eq!(
        std::fs::read(output_dir.join("SEAL.json")).unwrap(),
        std::fs::read(reference.join("SEAL.json")).unwrap()
    );
}

#[test]
fn heldout_build_requires_explicit_freeze_without_creating_output() {
    let dir = tempfile::tempdir().unwrap();
    let output_dir = dir.path().join("new-lite");
    let output = cli(&[
        "v5",
        "build",
        "--suite",
        "v5_lite",
        "--out",
        output_dir.to_str().unwrap(),
    ]);
    assert!(!output.status.success());
    assert!(!output_dir.exists());
}

#[test]
fn native_validation_executes_offline_proofs_without_nested_runtime() {
    let output = cli(&[
        "v5",
        "validate",
        "--suite",
        "v5_dev",
        "--dataset",
        "datasets/v5_draft5/v5_dev",
    ]);
    assert_eq!(
        output.status.code(),
        Some(1),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let report: Value = serde_json::from_slice(&output.stdout).expect("validation report JSON");
    let gates = report["gate_results"].as_array().unwrap();
    for id in ["NEGATIVE-MUTATION-V5", "G-BASE-01", "G-NONINTERFERENCE-V5"] {
        let gate = gates.iter().find(|gate| gate["gate_id"] == id).unwrap();
        assert_eq!(gate["status"], "PASS", "{id}: {}", gate["errors"]);
    }
    // Python Draft5 dev validator projection: ordered annotation alternatives must be
    // preserved, including duplicate candidates, rather than sampled from JSON types.
    let negative = gates
        .iter()
        .find(|gate| gate["gate_id"] == "NEGATIVE-MUTATION-V5")
        .unwrap();
    let expected: Value =
        serde_json::from_str(include_str!("fixtures/dynamic_negative_python.json")).unwrap();
    assert_eq!(negative["evidence"]["dynamic_single_field"], expected);
}

#[test]
fn score_to_report_pipeline_has_bound_attempts_and_recomputed_scores() {
    let dir = tempfile::tempdir().unwrap();
    let run = dir.path().join("run");
    let report = dir.path().join("report");
    let scored = success(cli(&[
        "--no-env-file",
        "v5",
        "score",
        "--suite",
        "v5_dev",
        "--dataset",
        "datasets/v5_draft5/v5_dev",
        "--baseline",
        "oracle_team_replay",
        "--out",
        run.to_str().unwrap(),
    ]));
    assert_eq!(scored["repeats"], 1);
    let manifest: Value =
        serde_json::from_slice(&std::fs::read(run.join("manifest.json")).unwrap()).unwrap();
    assert_eq!(
        manifest["descriptive_usage_summary"]["planned_attempt_slots"],
        12
    );
    assert_eq!(
        manifest["descriptive_usage_summary"]["completed_attempts"],
        12
    );
    let rows = std::fs::read_to_string(run.join("episodes.jsonl")).unwrap();
    assert_eq!(rows.lines().count(), 12);
    for line in rows.lines() {
        let row: Value = serde_json::from_str(line).unwrap();
        assert_eq!(row["attempt"]["attempt_status"], "COMPLETED");
        assert_eq!(row["attempt"]["g_srr"], true);
        assert_eq!(
            row["attempt"]["run_config_sha256"],
            manifest["run_config_sha256"]
        );
        assert_eq!(ib_core::jcs_dumps(&row).unwrap(), line.as_bytes());
    }
    let rendered = success(cli(&[
        "--no-env-file",
        "v5",
        "report",
        "--run",
        run.to_str().unwrap(),
        "--out",
        report.to_str().unwrap(),
    ]));
    assert_eq!(rendered["is_complete"], true);
    assert_eq!(rendered["observed_slots"], 12);
    assert_eq!(rendered["planned_slots"], 12);
    assert!(report.join("report.md").is_file());
    assert!(report.join("summary.json").is_file());
    // Report must independently reject a corrupted execution record, even if a cached score
    // remains present in it.
    std::fs::write(
        run.join("episodes.jsonl"),
        rows.replace("oracle_team_replay", "changed_baseline"),
    )
    .unwrap();
    assert!(
        !cli(&["v5", "report", "--run", run.to_str().unwrap()])
            .status
            .success()
    );
}

#[test]
fn sensitive_validation_receipt_alias_is_rejected_before_loading() {
    let dir = tempfile::tempdir().unwrap();
    let target = dir.path().join(".env");
    std::fs::write(&target, b"CANARY_NOT_PUBLIC").unwrap();
    #[cfg(unix)]
    {
        let alias = dir.path().join("proof.json");
        std::os::unix::fs::symlink(&target, &alias).unwrap();
        let output = cli(&["v5", "validate", "--blind-proof", alias.to_str().unwrap()]);
        assert!(!output.status.success());
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains("sensitive input"));
        assert!(!stderr.contains("CANARY_NOT_PUBLIC"));
    }
}

#[test]
fn score_refuses_output_overlapping_sealed_input_including_symlink_aliases() {
    let dir = tempfile::tempdir().unwrap();
    let dataset = dir.path().join("dataset");
    std::fs::create_dir(&dataset).unwrap();
    let source = root().join("datasets/v5_draft5/v5_dev");
    let files = ["SEAL.json", "episodes.jsonl", "oracles.jsonl"];
    let expected: Vec<Vec<u8>> = files
        .iter()
        .map(|name| {
            let bytes = std::fs::read(source.join(name)).unwrap();
            std::fs::write(dataset.join(name), &bytes).unwrap();
            bytes
        })
        .collect();
    let mut targets = vec![
        dataset.clone(),
        dataset.join("child"),
        dir.path().to_path_buf(),
    ];
    #[cfg(unix)]
    {
        let alias = dir.path().join("alias");
        std::os::unix::fs::symlink(&dataset, &alias).unwrap();
        targets.push(alias);
    }
    for target in targets {
        let output = cli(&[
            "v5",
            "score",
            "--dataset",
            dataset.to_str().unwrap(),
            "--baseline",
            "noop_team",
            "--out",
            target.to_str().unwrap(),
        ]);
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("overlaps"));
        for (name, bytes) in files.iter().zip(&expected) {
            assert_eq!(&std::fs::read(dataset.join(name)).unwrap(), bytes);
        }
    }
    #[cfg(unix)]
    {
        let run = dir.path().join("nested-alias-run");
        std::fs::create_dir(&run).unwrap();
        std::os::unix::fs::symlink(&dataset, run.join("dataset")).unwrap();
        let output = cli(&[
            "v5",
            "score",
            "--dataset",
            dataset.to_str().unwrap(),
            "--baseline",
            "noop_team",
            "--out",
            run.to_str().unwrap(),
        ]);
        assert!(!output.status.success());
        assert!(String::from_utf8_lossy(&output.stderr).contains("overlaps"));
        assert!(!run.join("episodes.jsonl").exists());
        for (name, bytes) in files.iter().zip(&expected) {
            assert_eq!(&std::fs::read(dataset.join(name)).unwrap(), bytes);
        }
    }
}

#[test]
fn score_preflights_every_baseline_dataset_copy_before_writing_any_run() {
    let dir = tempfile::tempdir().unwrap();
    let output = dir.path().join("runs");
    let later_copy = output.join("constant_workflow_template").join("dataset");
    std::fs::create_dir_all(&later_copy).unwrap();
    let sentinel = b"mismatched copied seal\n";
    std::fs::write(later_copy.join("SEAL.json"), sentinel).unwrap();

    let output_result = cli(&[
        "v5",
        "score",
        "--suite",
        "v5_dev",
        "--dataset",
        root().join("datasets/v5_draft5/v5_dev").to_str().unwrap(),
        "--baseline",
        "constant_conservative_deny",
        "--baseline",
        "constant_workflow_template",
        "--out",
        output.to_str().unwrap(),
    ]);
    assert!(!output_result.status.success());
    assert!(
        String::from_utf8_lossy(&output_result.stderr)
            .contains("existing copied dataset does not match the sealed input")
    );
    assert!(
        !output.join("constant_conservative_deny").exists(),
        "an earlier baseline must not be written before all outputs are checked"
    );
    assert_eq!(
        std::fs::read(later_copy.join("SEAL.json")).unwrap(),
        sentinel
    );
}
