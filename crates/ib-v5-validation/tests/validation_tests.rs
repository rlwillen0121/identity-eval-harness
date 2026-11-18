use ib_v5_generator::{V5Suite, write_v5_dataset};
use ib_v5_validation::{
    GateStatusV5, V5ValidationReport, ValidationOptions, repository_root, validate_v5_dataset,
};
use serde_json::{Map, Value, json};

#[test]
fn report_excludes_live_gate_unknowns_but_not_offline_unknowns() {
    let report = V5ValidationReport {
        suite_id: "v5_dev".to_owned(),
        dataset_dir: "/tmp/v5_dev".to_owned(),
        episode_count: 1,
        gate_results: vec![
            ib_v5_validation::GateResultV5::with_evidence(
                "G-ECON-01",
                GateStatusV5::Unknown,
                json!({"run_artifact": null}),
                vec!["model run is unavailable".to_owned()],
            ),
            ib_v5_validation::GateResultV5::with_evidence(
                "G-CAL-01",
                GateStatusV5::Unknown,
                json!({}),
                vec![],
            ),
        ],
    };
    assert!(report.passed());
    assert!(!report.full_milestone_passed());
    assert_eq!(
        report.live_gates_unmeasured(),
        vec!["G-ECON-01".to_owned(), "G-CAL-01".to_owned()]
    );
    assert_eq!(
        report.errors(),
        vec!["G-ECON-01: model run is unavailable".to_owned()]
    );
    assert_eq!(
        report.unknown_gates(),
        vec!["G-ECON-01".to_owned(), "G-CAL-01".to_owned()]
    );
    let mapping = report.to_mapping();
    assert_eq!(mapping["offline_passed"], Value::Bool(true));
    assert_eq!(mapping["full_milestone_passed"], Value::Bool(false));
}

#[test]
fn offline_unknown_gate_prevents_passed_report() {
    let report = V5ValidationReport {
        suite_id: "v5_dev".to_owned(),
        dataset_dir: "/tmp/v5_dev".to_owned(),
        episode_count: 0,
        gate_results: vec![ib_v5_validation::GateResultV5::new(
            "G-DET-01",
            GateStatusV5::Unknown,
            Map::new(),
            vec!["offline proof was skipped".to_owned()],
        )],
    };
    assert!(!report.passed());
    assert_eq!(report.unknown_gates(), vec!["G-DET-01".to_owned()]);
}

#[test]
fn validation_orchestrates_fixture_gates_without_running_baselines() {
    let temp = tempfile::tempdir().expect("tempdir");
    write_v5_dataset(V5Suite::V5Dev, temp.path(), 26_110_117, false)
        .expect("write disposable v5_dev dataset");
    let dataset_dir = temp.path();
    let options = ValidationOptions {
        repository_root: Some(repository_root()),
        run_baselines: false,
        ..ValidationOptions::default()
    };
    let report = validate_v5_dataset(&dataset_dir, &options).expect("validate generated dataset");

    assert_eq!(report.suite_id, "v5_dev");
    assert!(report.episode_count > 0);
    assert_eq!(
        report
            .gate_results
            .iter()
            .filter(|gate| gate.gate_id == "G-DET-01")
            .count(),
        2,
        "Python validation emits the double-build gate and the final aggregate with this ID"
    );
    assert_eq!(
        report.gate_results.last().map(|gate| gate.gate_id.as_str()),
        Some("G-DET-01")
    );
    assert_eq!(
        report
            .gate_results
            .iter()
            .find(|gate| gate.gate_id == "G-ORACLE-REPLAY-V5")
            .map(|gate| gate.status),
        Some(GateStatusV5::Pass)
    );
    assert_eq!(
        report
            .gate_results
            .iter()
            .find(|gate| gate.gate_id == "G-IGA-REVIEW-01")
            .map(|gate| gate.status),
        Some(GateStatusV5::Pass)
    );
    assert_eq!(
        report
            .gate_results
            .iter()
            .find(|gate| gate.gate_id == "G-BASE-01")
            .map(|gate| gate.status),
        Some(GateStatusV5::Unknown)
    );
    assert!(!report.passed());
}
