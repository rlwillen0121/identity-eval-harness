//! Offline executable tests for the `identitybench v51` command surface.
//!
//! Datasets are built into temporary directories; the committed `datasets/v51/` suites are only
//! read. Runs use the `replay` provider with an empty recording, so no provider is contacted.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use serde_json::Value;

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap()
}

fn identitybench(args: &[&str]) -> Output {
    let root = repo_root();
    Command::new(env!("CARGO_BIN_EXE_identitybench"))
        .arg("--root")
        .arg(&root)
        .args(args)
        .current_dir(std::env::temp_dir())
        .output()
        .expect("spawn identitybench")
}

fn code(output: &Output) -> i32 {
    output.status.code().expect("exit code")
}

fn stdout_json(output: &Output) -> Value {
    serde_json::from_slice(&output.stdout).unwrap_or_else(|e| {
        panic!(
            "stdout is not JSON ({e}); stderr={}",
            String::from_utf8_lossy(&output.stderr)
        )
    })
}

fn path_arg(path: &Path) -> &str {
    path.to_str().expect("UTF-8 temp path")
}

fn build_dev(out: &Path) {
    let output = identitybench(&["v51", "build", "--suite", "v51_dev", "--out", path_arg(out)]);
    assert_eq!(
        code(&output),
        0,
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn gate_map(report: &Value) -> Vec<(String, bool)> {
    report["gates"]
        .as_array()
        .expect("gates array")
        .iter()
        .map(|g| {
            (
                g["gate_id"].as_str().unwrap().to_owned(),
                g["passed"].as_bool().unwrap(),
            )
        })
        .collect()
}

#[test]
fn only_the_v51_command_family_is_exposed() {
    let help = Command::new(env!("CARGO_BIN_EXE_identitybench"))
        .arg("--help")
        .output()
        .unwrap();
    assert_eq!(code(&help), 0);
    let text = String::from_utf8(help.stdout).unwrap();
    assert!(text.contains("v5.1"), "{text}");
    assert!(text.contains("  v51 "), "{text}");
    assert!(!text.contains("  v5 "), "{text}");

    let version = Command::new(env!("CARGO_BIN_EXE_identitybench"))
        .arg("--version")
        .output()
        .unwrap();
    assert_eq!(
        String::from_utf8(version.stdout).unwrap().trim(),
        "identitybench 5.1.0"
    );

    for retired in [
        vec!["v5", "validate", "--suite", "v5_dev"],
        vec!["v5", "run", "--help"],
    ] {
        assert_eq!(code(&identitybench(&retired)), 2, "{retired:?}");
    }

    let v51_help = identitybench(&["v51", "--help"]);
    let text = String::from_utf8(v51_help.stdout).unwrap();
    for sub in ["build", "validate", "run", "report"] {
        assert!(text.contains(&format!("  {sub} ")), "{sub}: {text}");
    }
    let run_help = String::from_utf8(identitybench(&["v51", "run", "--help"]).stdout).unwrap();
    for flag in [
        "--config",
        "--pricing",
        "--suite",
        "--dataset",
        "--out",
        "--repeats",
        "--resume",
        "--env-file",
        "--no-env-file",
    ] {
        assert!(run_help.contains(flag), "{flag}: {run_help}");
    }
}

#[test]
fn syntactic_and_semantic_usage_errors_use_exit_two() {
    let dir = tempfile::tempdir().unwrap();
    // Held-out suites require --freeze and are rejected before any write.
    let held_out = dir.path().join("core");
    let output = identitybench(&[
        "v51",
        "build",
        "--suite",
        "v51_core",
        "--out",
        path_arg(&held_out),
    ]);
    assert_eq!(code(&output), 2);
    assert!(!held_out.exists());

    assert_eq!(
        code(&identitybench(&["v51", "validate", "--suite", "v5_dev"])),
        2
    );
    assert_eq!(
        code(&identitybench(&[
            "v51",
            "run",
            "--config",
            "c.yaml",
            "--pricing",
            "p.json",
            "--repeats",
            "2"
        ])),
        2
    );
    let missing_env = identitybench(&[
        "--env-file",
        path_arg(&dir.path().join("missing.env")),
        "v51",
        "run",
        "--config",
        "c.yaml",
        "--pricing",
        "p.json",
    ]);
    assert_eq!(code(&missing_env), 2);
}

#[test]
fn build_is_deterministic_and_validates_with_double_build() {
    let dir = tempfile::tempdir().unwrap();
    let built = dir.path().join("v51_dev");
    build_dev(&built);

    // The default seed reproduces the committed sealed dev suite byte-for-byte.
    let sealed = repo_root().join("datasets/v51/v51_dev");
    for name in ["episodes.jsonl", "oracles.jsonl", "SEAL.json"] {
        assert!(
            std::fs::read(built.join(name)).unwrap() == std::fs::read(sealed.join(name)).unwrap(),
            "{name} differs from the sealed v51_dev suite"
        );
    }

    // Refuse to overwrite an existing dataset.
    let again = identitybench(&[
        "v51",
        "build",
        "--suite",
        "v51_dev",
        "--out",
        path_arg(&built),
    ]);
    assert_eq!(code(&again), 3);

    let output = identitybench(&[
        "v51",
        "validate",
        "--dataset",
        path_arg(&built),
        "--double-build",
    ]);
    assert_eq!(
        code(&output),
        0,
        "{}",
        String::from_utf8_lossy(&output.stdout)
    );
    let report = stdout_json(&output);
    assert_eq!(report["passed"], Value::Bool(true));
    assert_eq!(report["episode_count"], 12);
    assert_eq!(
        gate_map(&report),
        [
            ("G-SEAL-V51".to_owned(), true),
            ("G-GOLD-REPLAY-V51".to_owned(), true),
            ("G-ANTI-ORACLE-V51".to_owned(), true),
            ("G-DET-V51".to_owned(), true),
        ]
    );
}

#[test]
fn tampered_seal_fails_validation_with_exit_one() {
    let dir = tempfile::tempdir().unwrap();
    let built = dir.path().join("v51_dev");
    build_dev(&built);
    let seal_path = built.join("SEAL.json");
    let mut seal: Value = serde_json::from_slice(&std::fs::read(&seal_path).unwrap()).unwrap();
    seal["episodes_sha256"] = Value::String("0".repeat(64));
    std::fs::write(&seal_path, serde_json::to_vec_pretty(&seal).unwrap()).unwrap();

    let output = identitybench(&["v51", "validate", "--dataset", path_arg(&built)]);
    assert_eq!(code(&output), 1);
    let report = stdout_json(&output);
    assert_eq!(report["passed"], Value::Bool(false));
    assert_eq!(gate_map(&report)[0], ("G-SEAL-V51".to_owned(), false));
}

#[test]
fn offline_replay_run_resume_and_report() {
    let dir = tempfile::tempdir().unwrap();
    let dataset = dir.path().join("v51_dev");
    build_dev(&dataset);
    let recording = dir.path().join("empty-responses.jsonl");
    std::fs::write(&recording, b"").unwrap();
    let config = dir.path().join("replay.yaml");
    std::fs::write(
        &config,
        format!(
            "model:\n  provider: replay\n  model: offline-replay\n  extra:\n    path: {}\nrun_config:\n  max_assistant_generations: 4\n",
            recording.display()
        ),
    )
    .unwrap();
    let pricing = repo_root().join("examples/v51/pricing-synthetic-zero.json");
    let run_dir = dir.path().join("run");
    let run_args = |resume: bool| {
        let mut args = vec![
            "--no-env-file",
            "v51",
            "run",
            "--config",
            path_arg(&config),
            "--pricing",
            path_arg(&pricing),
            "--suite",
            "v51_dev",
            "--dataset",
            path_arg(&dataset),
            "--out",
            path_arg(&run_dir),
        ];
        if resume {
            args.push("--resume");
        }
        identitybench(&args)
    };

    let output = run_args(false);
    assert_eq!(
        code(&output),
        0,
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let result = stdout_json(&output);
    assert_eq!(result["episode_count"], 12);
    assert_eq!(result["model_id"], "replay:offline-replay");
    // Every slot is a provider error (empty recording), so nothing completes or costs money.
    assert_eq!(result["summary"]["completed_slots"], 0);
    assert_eq!(result["summary"]["is_complete"], Value::Bool(false));
    for name in [
        "episodes.jsonl",
        "manifest.json",
        "summary.json",
        "report.md",
    ] {
        assert!(run_dir.join(name).is_file(), "{name}");
    }

    let resumed = run_args(true);
    assert_eq!(
        code(&resumed),
        0,
        "{}",
        String::from_utf8_lossy(&resumed.stderr)
    );
    assert_eq!(stdout_json(&resumed)["episode_count"], 12);

    let report_dir = dir.path().join("report");
    let report = identitybench(&[
        "v51",
        "report",
        "--run",
        path_arg(&run_dir),
        "--out",
        path_arg(&report_dir),
    ]);
    assert_eq!(code(&report), 0);
    let report = stdout_json(&report);
    assert_eq!(report["summary"]["completed_slots"], 0);
    assert!(report_dir.join("summary.json").is_file());
    assert!(report_dir.join("report.md").is_file());

    // Output overlapping the sealed input is rejected before any provider is constructed.
    let overlap = identitybench(&[
        "--no-env-file",
        "v51",
        "run",
        "--config",
        path_arg(&config),
        "--pricing",
        path_arg(&pricing),
        "--dataset",
        path_arg(&dataset),
        "--out",
        path_arg(&dataset.join("run")),
    ]);
    assert_eq!(code(&overlap), 3);
    assert!(!dataset.join("run").exists());
}

#[test]
fn example_run_configs_and_pricing_parse() {
    let examples = repo_root().join("examples/v51");
    let mut configs = 0;
    for entry in std::fs::read_dir(&examples).unwrap() {
        let path = entry.unwrap().path();
        match path.extension().and_then(|e| e.to_str()) {
            Some("yaml") => {
                ib_v51_runtime::load_run_config_v51(&path)
                    .unwrap_or_else(|e| panic!("{}: {e:#}", path.display()));
                configs += 1;
            }
            Some("json") => {
                ib_v51_runtime::load_pricing_v51(&path)
                    .unwrap_or_else(|e| panic!("{}: {e:#}", path.display()));
            }
            _ => {}
        }
    }
    assert!(configs >= 2);
}
