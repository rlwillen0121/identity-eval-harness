//! Exercise the installed executable without an interpreter or a source checkout.
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::{Value, json};
use tempfile::tempdir;

fn workspace() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn invoke(binary: &Path, root: &Path, args: &[&str]) -> Output {
    Command::new(binary)
        .env_clear()
        .env("PATH", root.join("empty-path"))
        .current_dir(root)
        .arg("--root")
        .arg(root)
        .args(args)
        .output()
        .expect("standalone executable starts")
}

fn successful_json(output: Output) -> Value {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("command emits JSON")
}

#[test]
fn installed_binary_builds_validates_scores_and_replays_without_python() {
    let temp = tempdir().unwrap();
    let root = temp.path();
    std::fs::create_dir(root.join("empty-path")).unwrap();
    let binary = root.join("identitybench");
    std::fs::copy(env!("CARGO_BIN_EXE_identitybench"), &binary).unwrap();
    // Offline paths must not attempt to load provider configuration.
    std::fs::write(
        root.join(".env"),
        "this is deliberately not an env assignment",
    )
    .unwrap();
    for name in [
        "SPEC-v5.md",
        "SPEC-v5.0-draft.5.md",
        "SPEC-v5-ADDENDUM-01.md",
        "SPEC-v5-DIRECTIVE-01.md",
    ] {
        std::fs::copy(workspace().join(name), root.join(name)).unwrap();
    }
    let dataset = root.join("datasets/v5_draft5/v5_dev");
    std::fs::create_dir_all(&dataset).unwrap();
    for name in ["SEAL.json", "episodes.jsonl", "oracles.jsonl"] {
        std::fs::copy(
            workspace().join("datasets/v5_draft5/v5_dev").join(name),
            dataset.join(name),
        )
        .unwrap();
    }
    assert!(!root.join("src").exists());
    assert!(!root.join(".venv").exists());
    assert!(!root.join("Cargo.toml").exists());

    successful_json(invoke(
        &binary,
        root,
        &["v5", "build", "--suite", "v5_dev", "--out", "generated"],
    ));
    assert!(root.join("generated/SEAL.json").is_file());

    let validation = invoke(
        &binary,
        root,
        &[
            "v5",
            "validate",
            "--suite",
            "v5_dev",
            "--double-build",
            "--skip-baselines",
        ],
    );
    // A source-free artifact bundle has no independent conformance/review evidence.
    assert_eq!(validation.status.code(), Some(1));
    let validation: Value = serde_json::from_slice(&validation.stdout).unwrap();
    let gates = validation["gate_results"].as_array().unwrap();
    assert!(
        gates
            .iter()
            .any(|gate| gate["gate_id"] == "G-SEAL-01" && gate["status"] == "PASS")
    );
    assert!(
        gates
            .iter()
            .any(|gate| gate["gate_id"] == "G-SPEC-01" && gate["status"] == "UNKNOWN")
    );

    successful_json(invoke(
        &binary,
        root,
        &[
            "v5",
            "score",
            "--suite",
            "v5_dev",
            "--baseline",
            "oracle_team_replay",
            "--out",
            "baseline",
        ],
    ));
    let report = successful_json(invoke(
        &binary,
        root,
        &[
            "v5",
            "report",
            "--run",
            "baseline",
            "--out",
            "baseline-report",
        ],
    ));
    assert_eq!(report["is_complete"], true);

    let replay = root.join("empty-responses.jsonl");
    std::fs::write(&replay, "").unwrap();
    std::fs::write(
        root.join("model.json"),
        serde_json::to_vec(&json!({
            "model": {
                "provider": "replay", "model": "offline-portability",
                "extra": {"path": replay}
            },
            "run_config": {"max_transient_retries": 0}
        }))
        .unwrap(),
    )
    .unwrap();
    std::fs::write(
        root.join("pricing.json"),
        br#"{"uncached_input_micros_per_million":0,"cached_input_micros_per_million":0,"output_micros_per_million":0,"reasoning_micros_per_million":0}"#,
    )
    .unwrap();
    successful_json(invoke(
        &binary,
        root,
        &[
            "--no-env-file",
            "v5",
            "run",
            "--suite",
            "v5_dev",
            "--config",
            "model.json",
            "--pricing",
            "pricing.json",
            "--repeats",
            "1",
            "--out",
            "replay-run",
        ],
    ));
    let report = successful_json(invoke(
        &binary,
        root,
        &[
            "v5",
            "report",
            "--run",
            "replay-run",
            "--out",
            "replay-report",
        ],
    ));
    assert_eq!(report["is_complete"], false);
    // Provider failures are recorded, but do not count as observed scoring slots.
    assert_eq!(report["observed_slots"], 0);
    assert_eq!(report["planned_slots"], 12);
    let records = std::fs::read_to_string(root.join("replay-run/episodes.jsonl")).unwrap();
    let records: Vec<Value> = records
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(records.len(), 12);
    assert!(
        records
            .iter()
            .all(|record| record["provider_error"] == true)
    );
}
