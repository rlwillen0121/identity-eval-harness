use std::path::{Path, PathBuf};

use ib_v5_report::{render_v5_markdown_report, report_from_raw_artifacts, write_v5_report};
use tempfile::tempdir;

fn real_pilot_run_dir(run_name: &str) -> Option<PathBuf> {
    // Default local runs are optional; an explicit root makes the requested run required.
    let explicit_root = std::env::var_os("IDENTITYBENCH_REAL_PILOT_ROOT");
    let root = explicit_root
        .as_ref()
        .map(PathBuf::from)
        .unwrap_or_else(|| Path::new(env!("CARGO_MANIFEST_DIR")).join("../../runs"));
    let run_dir = root.join(run_name);
    if run_dir.exists() {
        Some(run_dir)
    } else if explicit_root.is_some() {
        panic!("explicit real-pilot root is missing requested run: {run_name}")
    } else {
        None
    }
}

fn assert_pilot_byte_exact(run_dir: &Path, ref_report: &[u8], ref_summary: &[u8]) {
    let run_dir_str = run_dir.display().to_string();

    let artifact = report_from_raw_artifacts(run_dir)
        .unwrap_or_else(|e| panic!("report_from_raw_artifacts failed for {run_dir_str}: {e}"));

    let out = tempdir().expect("create tempdir");
    let (report_path, summary_path) =
        write_v5_report(&artifact, out.path()).expect("write_v5_report");

    let actual_report = std::fs::read(&report_path).expect("read report.md");
    let actual_summary = std::fs::read(&summary_path).expect("read summary.json");

    assert_eq!(
        render_v5_markdown_report(&artifact).as_bytes(),
        actual_report.as_slice()
    );

    if actual_report != ref_report {
        let actual_str = String::from_utf8_lossy(&actual_report);
        let ref_str = String::from_utf8_lossy(ref_report);
        for (idx, (a, b)) in actual_str.lines().zip(ref_str.lines()).enumerate() {
            if a != b {
                panic!(
                    "report.md mismatch for {run_dir_str} at line {}:\n  rust:   {a}\n  python: {b}",
                    idx + 1
                );
            }
        }
        panic!(
            "report.md byte mismatch for {run_dir_str}: rust len {}, python len {}",
            actual_report.len(),
            ref_report.len()
        );
    }

    if actual_summary != ref_summary {
        let actual_str = String::from_utf8_lossy(&actual_summary);
        let ref_str = String::from_utf8_lossy(ref_summary);
        for (idx, (a, b)) in actual_str.lines().zip(ref_str.lines()).enumerate() {
            if a != b {
                panic!(
                    "summary.json mismatch for {run_dir_str} at line {}:\n  rust:   {a}\n  python: {b}",
                    idx + 1
                );
            }
        }
        panic!(
            "summary.json byte mismatch for {run_dir_str}: rust len {}, python len {}",
            actual_summary.len(),
            ref_summary.len()
        );
    }
}

#[test]
fn real_pilot_t0_report_and_summary_byte_exact_parity() {
    let Some(run_dir) = real_pilot_run_dir("v5_draft5_pilot_v3_gemini_hydrogen_t0") else {
        return;
    };
    assert_pilot_byte_exact(
        &run_dir,
        include_bytes!("fixtures/pilot_t0_report.md"),
        include_bytes!("fixtures/pilot_t0_summary.json"),
    );
}

#[test]
fn real_pilot_t5_report_and_summary_byte_exact_parity() {
    let Some(run_dir) = real_pilot_run_dir("v5_draft5_pilot_v3_gemini_hydrogen_t5") else {
        return;
    };
    assert_pilot_byte_exact(
        &run_dir,
        include_bytes!("fixtures/pilot_t5_report.md"),
        include_bytes!("fixtures/pilot_t5_summary.json"),
    );
}
