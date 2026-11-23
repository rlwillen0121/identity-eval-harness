use std::process::Command;

#[test]
fn invalid_calibration_input_does_not_write_partial_report() {
    let temp = tempfile::tempdir().unwrap();
    let output_dir = temp.path().join("calibration-output");
    let first = temp.path().join("missing-frontier-a");
    let second = temp.path().join("missing-frontier-b");
    let output = Command::new(env!("CARGO_BIN_EXE_identitybench"))
        .args([
            "v52",
            "calibration-report",
            "--frontier",
            first.to_str().unwrap(),
            "--frontier",
            second.to_str().unwrap(),
            "--out",
            output_dir.to_str().unwrap(),
        ])
        .output()
        .unwrap();

    assert_eq!(output.status.code(), Some(3));
    assert!(!output_dir.join("calibration_report.json").exists());
    assert!(!output_dir.join("calibration_report.md").exists());
}
