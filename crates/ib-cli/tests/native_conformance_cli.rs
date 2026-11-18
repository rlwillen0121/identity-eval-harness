//! End-to-end smoke coverage for the native conformance CLI.
//!
//! The tiny external executables below print rows assembled from the committed,
//! independently authored Draft 5 reference expectations. The fixture is diagnostic
//! process evidence; it never claims the 1702-case G-SPEC gate.
#![cfg(unix)]

use std::collections::BTreeSet;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use ib_v5_conformance::shared::{
    P1_EPISODE_KEYS, PINNED_PUBLIC_SCHEMA_SHA256, PROTOCOL, canonical_json_bytes, freeze_case,
    strict_jsonl_loads,
};
use serde_json::{Map, Value, json};
use tempfile::tempdir_in;

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf()
}

fn invoke(binary: &Path, root: &Path, args: &[String]) -> Output {
    Command::new(binary)
        .arg("--root")
        .arg(root)
        .args(args)
        .output()
        .expect("identitybench starts")
}

fn invoke_cli(root: &Path, args: &[String]) -> Output {
    invoke(Path::new(env!("CARGO_BIN_EXE_identitybench")), root, args)
}

fn json_success(output: Output) -> Value {
    assert!(
        output.status.success(),
        "stdout={}\nstderr={}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("CLI output is JSON")
}

fn case_and_independent_rows(root: &Path) -> (Value, Value, Value) {
    let episodes_path =
        root.join("reports/v5/conformance/inputs/draft5_v1/v5_dev/selected-episodes.jsonl");
    let episode_raw = fs::read(episodes_path).expect("read selected dev episodes");
    let episode = strict_jsonl_loads(&episode_raw)
        .expect("parse selected dev episodes")
        .into_iter()
        .next()
        .expect("dev episode exists");
    let episode_object = episode.as_object().expect("episode is an object");
    let episode_id = episode_object["episode_id"].as_str().unwrap();
    let mut public_episode = Map::new();
    for key in P1_EPISODE_KEYS {
        public_episode.insert(key.to_owned(), episode_object[key].clone());
    }
    let rendered_card = fs::read_to_string(
        root.join("reports/v5/conformance/inputs/draft5_v1/v5_dev/rendered-card.txt"),
    )
    .expect("read rendered public policy card");
    let case = json!({
        "protocol": PROTOCOL,
        "case_id": format!("v5_dev/{episode_id}/p1/derive_phase"),
        "operation": "derive_phase",
        "suite_id": "v5_dev",
        "phase_index": 1,
        "public_episode": public_episode,
        "rendered_card": rendered_card,
        "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
        "stimulus": {},
    });

    let full_reference: Value = serde_json::from_slice(
        &fs::read(root.join("reports/v5/conformance/v5_draft5_dev/full_spec_reference.json"))
            .expect("read committed full-spec reference expectations"),
    )
    .expect("parse full-spec reference expectations");
    let agent_reference: Value = serde_json::from_slice(
        &fs::read(root.join("reports/v5/conformance/v5_draft5_dev/agent_view_reference.json"))
            .expect("read committed agent-view reference expectations"),
    )
    .expect("parse agent-view reference expectations");
    let full_expectation = expectation(&full_reference, episode_id);
    let agent_expectation = expectation(&agent_reference, episode_id);
    let full_row = reference_result(
        &case,
        "full_spec_blind",
        full_expectation,
        &full_reference["policy_card_anchor_map"],
    );
    let agent_row = reference_result(
        &case,
        "agent_view_only",
        agent_expectation,
        &agent_reference["policy_card_anchor_map"],
    );
    (case, full_row, agent_row)
}

fn expectation<'a>(reference: &'a Value, episode_id: &str) -> &'a Value {
    reference["phase_expectations"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| {
            row["episode_id"].as_str() == Some(episode_id) && row["phase_index"].as_i64() == Some(1)
        })
        .expect("independent expectation for selected episode phase one")
}

fn reference_result(
    case: &Value,
    evaluator_id: &str,
    expectation: &Value,
    anchor_map: &Value,
) -> Value {
    let frozen = freeze_case(case).expect("freeze test PublicCase");
    let episode_id = case["public_episode"]["episode_id"].as_str().unwrap();
    let suite_id = case["suite_id"].as_str().unwrap();
    let phase_index = case["phase_index"].as_i64().unwrap();
    let mut mutations = expectation["mutations"]
        .as_array()
        .unwrap()
        .iter()
        .map(flatten_reference_mutation)
        .collect::<Vec<_>>();
    mutations.sort_by(|left, right| {
        (
            left["mutation_kind"].as_str().unwrap_or(""),
            left["target_entity_id"].as_str().unwrap_or(""),
        )
            .cmp(&(
                right["mutation_kind"].as_str().unwrap_or(""),
                right["target_entity_id"].as_str().unwrap_or(""),
            ))
    });
    let mut rules = BTreeSet::new();
    let targets = mutations
        .iter()
        .map(|mutation| {
            for citation in mutation["cite"].as_array().unwrap() {
                if let Some((prefix, _)) = citation.as_str().and_then(|value| value.split_once(':'))
                {
                    rules.insert(prefix.to_owned());
                }
            }
            json!({
                "suite_id": suite_id,
                "episode_id": episode_id,
                "phase_index": phase_index,
                "mutation_kind": mutation["mutation_kind"],
                "target_entity_id": mutation["target_entity_id"],
            })
        })
        .collect::<Vec<_>>();
    json!({
        "protocol": PROTOCOL,
        "evaluator_id": evaluator_id,
        "case_id": frozen.case_id(),
        "input_sha256": frozen.input_sha256,
        "status": "ok",
        "payload": {
            "mutations": mutations,
            "policy_card_anchor_map": anchor_map,
        },
        "materialization": null,
        "coverage": {
            "phase_targets": targets,
            "diagnostics": [],
            "rules": rules.into_iter().collect::<Vec<_>>(),
            "safety_cases": [],
            "snapshots": [],
        },
        "gaps": [],
    })
}

fn flatten_reference_mutation(mutation: &Value) -> Value {
    let mut output = mutation["semantic_fields"].as_object().unwrap().clone();
    for field in ["mutation_kind", "target_entity_id", "if_match", "cite"] {
        output.insert(field.to_owned(), mutation[field].clone());
    }
    Value::Object(output)
}

fn write_case_jsonl(path: &Path, case: &Value) {
    let mut bytes = canonical_json_bytes(case).expect("canonical case bytes");
    bytes.push(b'\n');
    fs::write(path, bytes).expect("write test case JSONL");
}

fn write_literal_evaluator(path: &Path, row: &Value) {
    let row =
        String::from_utf8(canonical_json_bytes(row).expect("canonical reference row")).unwrap();
    let shell_row = format!("'{}'", row.replace('\'', "'\\''"));
    let script =
        format!("#!/bin/sh\nwhile IFS= read -r input; do :; done\nprintf '%s\\n' {shell_row}\n");
    fs::write(path, script).expect("write synthetic external evaluator");
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))
        .expect("make evaluator executable");
}

fn copy_embedded_source_map(root: &Path, destination: &Path, freeze: &Value) {
    let files = freeze["native_build"]["source_identity"]
        .as_object()
        .expect("source identity object");
    for relative in files.keys() {
        let source = root.join(relative);
        let target = destination.join(relative);
        fs::create_dir_all(target.parent().unwrap()).expect("create source-map directory");
        fs::copy(source, target).expect("copy source-map member");
    }
}

fn copy_tree(source: &Path, destination: &Path) {
    let metadata = fs::symlink_metadata(source).expect("inspect copied artifact");
    assert!(
        !metadata.file_type().is_symlink(),
        "artifact tree has no symlinks"
    );
    if metadata.is_dir() {
        fs::create_dir_all(destination).expect("create copied artifact directory");
        for entry in fs::read_dir(source).expect("read artifact directory") {
            let entry = entry.expect("read artifact entry");
            copy_tree(&entry.path(), &destination.join(entry.file_name()));
        }
    } else {
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent).expect("create copied artifact parent");
        }
        fs::copy(source, destination).expect("copy artifact file");
    }
}

fn assert_no_absolute_repository_path(root: &Path, value: &Value) {
    let encoded = serde_json::to_vec(value).expect("serialize conformance artifact");
    let encoded = String::from_utf8(encoded).expect("artifact JSON is UTF-8");
    assert!(
        !encoded.contains(&root.display().to_string()),
        "portable conformance artifacts must not embed the checkout path"
    );
}

#[test]
fn native_conformance_cli_matches_independent_rows_and_rejects_mutations() {
    let root = repo_root();
    let temp = tempdir_in(root.join("reports/v5/conformance")).unwrap();
    let (case, full_row, agent_row) = case_and_independent_rows(&root);
    let cases_path = temp.path().join("cases.jsonl");
    write_case_jsonl(&cases_path, &case);
    let full_executable = temp.path().join("full-spec-reference");
    let agent_executable = temp.path().join("agent-view-reference");
    write_literal_evaluator(&full_executable, &full_row);
    write_literal_evaluator(&agent_executable, &agent_row);

    let freeze_dir = temp.path().join("diagnostic-freeze");
    let freeze_args = vec![
        "v5".to_owned(),
        "conformance".to_owned(),
        "freeze".to_owned(),
        "--cases".to_owned(),
        cases_path.display().to_string(),
        "--full-spec-executable".to_owned(),
        full_executable.display().to_string(),
        "--agent-view-executable".to_owned(),
        agent_executable.display().to_string(),
        "--out".to_owned(),
        freeze_dir.display().to_string(),
    ];
    let frozen = json_success(invoke_cli(&root, &freeze_args));
    assert_eq!(frozen["proof_scope"], "diagnostic_native_fixture");
    let freeze_path = freeze_dir.join("freeze.json");
    let freeze: Value = serde_json::from_slice(&fs::read(&freeze_path).unwrap()).unwrap();
    assert_eq!(freeze["case_count"], 1);
    assert_eq!(freeze["repository_root"], ".");
    assert_no_absolute_repository_path(&root, &freeze);

    let run = json_success(invoke_cli(
        &root,
        &[
            "v5".to_owned(),
            "conformance".to_owned(),
            "run".to_owned(),
            "--freeze".to_owned(),
            freeze_path.display().to_string(),
        ],
    ));
    let execution_path = freeze_dir.join("execution.json");
    assert!(execution_path.is_file());
    assert_eq!(
        run["schema_version"],
        "identitybench.v5.native-conformance-execution.v1"
    );
    assert_no_absolute_repository_path(&root, &run);

    let receipt_path = temp.path().join("receipt.json");
    let bound = json_success(invoke_cli(
        &root,
        &[
            "v5".to_owned(),
            "conformance".to_owned(),
            "bind".to_owned(),
            "--freeze".to_owned(),
            freeze_path.display().to_string(),
            "--execution".to_owned(),
            execution_path.display().to_string(),
            "--suite".to_owned(),
            "v5_dev".to_owned(),
            "--out".to_owned(),
            receipt_path.display().to_string(),
        ],
    ));
    assert_eq!(bound["matched_cases"], 1);
    let receipt: Value = serde_json::from_slice(&fs::read(&receipt_path).unwrap()).unwrap();
    assert_eq!(receipt["proof_scope"], "diagnostic_native_fixture");
    assert_no_absolute_repository_path(&root, &receipt);

    let verified = json_success(invoke_cli(
        &root,
        &[
            "v5".to_owned(),
            "conformance".to_owned(),
            "verify".to_owned(),
            "--receipt".to_owned(),
            receipt_path.display().to_string(),
        ],
    ));
    assert_eq!(verified["diagnostic_verified"], true);
    assert_eq!(verified["proof_scope"], "diagnostic_native_fixture");
    assert_eq!(verified["g_spec_claim"], "not_claimed");

    // Copy just the frozen source inventory and conformance artifacts to a second
    // repository root. All receipt references are relative, so verification must
    // succeed without access to the original checkout path.
    let portable_root = temp.path().join("portable-repository");
    fs::create_dir_all(&portable_root).unwrap();
    copy_embedded_source_map(&root, &portable_root, &freeze);
    let freeze_relative = freeze_dir.strip_prefix(&root).unwrap();
    copy_tree(&freeze_dir, &portable_root.join(freeze_relative));
    let receipt_relative = receipt_path.strip_prefix(&root).unwrap();
    copy_tree(&receipt_path, &portable_root.join(receipt_relative));
    let portable_verified = json_success(invoke_cli(
        &portable_root,
        &[
            "v5".to_owned(),
            "conformance".to_owned(),
            "verify".to_owned(),
            "--receipt".to_owned(),
            receipt_relative.display().to_string(),
        ],
    ));
    assert_eq!(portable_verified["diagnostic_verified"], true);
    assert_eq!(
        portable_verified["proof_scope"],
        "diagnostic_native_fixture"
    );

    let validation = invoke_cli(
        &root,
        &[
            "v5".to_owned(),
            "validate".to_owned(),
            "--suite".to_owned(),
            "v5_dev".to_owned(),
            "--skip-baselines".to_owned(),
            "--blind-proof".to_owned(),
            receipt_path.display().to_string(),
        ],
    );
    let validation_report: Value = serde_json::from_slice(&validation.stdout).unwrap();
    let gspec = validation_report["gate_results"]
        .as_array()
        .unwrap()
        .iter()
        .find(|gate| gate["gate_id"] == "G-SPEC-01")
        .unwrap();
    assert_ne!(
        gspec["status"], "PASS",
        "diagnostic fixtures cannot pass G-SPEC"
    );

    // An altered executable cannot claim the same native build identity.
    let binary_copy = temp.path().join("identitybench-mutated");
    fs::copy(env!("CARGO_BIN_EXE_identitybench"), &binary_copy).unwrap();
    fs::set_permissions(&binary_copy, fs::Permissions::from_mode(0o700)).unwrap();
    OpenOptions::new()
        .append(true)
        .open(&binary_copy)
        .unwrap()
        .write_all(b"mutation")
        .unwrap();
    assert!(
        !invoke(
            &binary_copy,
            &root,
            &[
                "v5".to_owned(),
                "conformance".to_owned(),
                "verify".to_owned(),
                "--receipt".to_owned(),
                receipt_path.display().to_string(),
            ],
        )
        .status
        .success()
    );

    // Mutating the staged independent-reference executable invalidates the bound receipt.
    let staged_reference = freeze_dir.join("executables/full_spec_blind");
    fs::set_permissions(&staged_reference, fs::Permissions::from_mode(0o700)).unwrap();
    OpenOptions::new()
        .append(true)
        .open(&staged_reference)
        .unwrap()
        .write_all(b"\n# changed after freeze\n")
        .unwrap();
    assert!(
        !invoke_cli(
            &root,
            &[
                "v5".to_owned(),
                "conformance".to_owned(),
                "verify".to_owned(),
                "--receipt".to_owned(),
                receipt_path.display().to_string(),
            ],
        )
        .status
        .success()
    );

    // A changed source tree cannot reuse the freeze, even with the same executable.
    let source_copy = temp.path().join("source-copy");
    fs::create_dir_all(&source_copy).unwrap();
    copy_embedded_source_map(&root, &source_copy, &freeze);
    OpenOptions::new()
        .append(true)
        .open(source_copy.join("crates/ib-cli/src/main.rs"))
        .unwrap()
        .write_all(b"\n// changed source\n")
        .unwrap();
    assert!(
        !invoke(
            Path::new(env!("CARGO_BIN_EXE_identitybench")),
            &source_copy,
            &[
                "v5".to_owned(),
                "conformance".to_owned(),
                "run".to_owned(),
                "--freeze".to_owned(),
                freeze_path.display().to_string(),
            ],
        )
        .status
        .success()
    );

    // A swapped evaluator_id is rejected before a receipt can be bound.
    let mut swapped_full = full_row.clone();
    swapped_full["evaluator_id"] = json!("agent_view_only");
    let swapped_executable = temp.path().join("swapped-reference");
    write_literal_evaluator(&swapped_executable, &swapped_full);
    let swapped_dir = temp.path().join("swapped-freeze");
    let swapped_args = vec![
        "v5".to_owned(),
        "conformance".to_owned(),
        "freeze".to_owned(),
        "--cases".to_owned(),
        cases_path.display().to_string(),
        "--full-spec-executable".to_owned(),
        swapped_executable.display().to_string(),
        "--agent-view-executable".to_owned(),
        agent_executable.display().to_string(),
        "--out".to_owned(),
        swapped_dir.display().to_string(),
    ];
    json_success(invoke_cli(&root, &swapped_args));
    json_success(invoke_cli(
        &root,
        &[
            "v5".to_owned(),
            "conformance".to_owned(),
            "run".to_owned(),
            "--freeze".to_owned(),
            swapped_dir.join("freeze.json").display().to_string(),
        ],
    ));
    let swapped_receipt = temp.path().join("swapped-receipt.json");
    assert!(
        !invoke_cli(
            &root,
            &[
                "v5".to_owned(),
                "conformance".to_owned(),
                "bind".to_owned(),
                "--freeze".to_owned(),
                swapped_dir.join("freeze.json").display().to_string(),
                "--execution".to_owned(),
                swapped_dir.join("execution.json").display().to_string(),
                "--suite".to_owned(),
                "v5_dev".to_owned(),
                "--out".to_owned(),
                swapped_receipt.display().to_string(),
            ],
        )
        .status
        .success()
    );
    assert!(!swapped_receipt.exists());
}

#[test]
fn full_gspec_freeze_requires_a_new_output_and_exact_reconstruction_descriptor() {
    let root = repo_root();
    let temp = tempdir_in(root.join("reports/v5/conformance")).unwrap();
    let descriptor = temp.path().join("bad-descriptor.json");
    fs::write(&descriptor, b"{}\n").unwrap();
    let output = temp.path().join("full-freeze");
    let executable = temp.path().join("not-used-executable");
    fs::write(&executable, b"not executed").unwrap();
    let args = vec![
        "v5".to_owned(),
        "conformance".to_owned(),
        "freeze".to_owned(),
        "--full-gspec".to_owned(),
        descriptor.display().to_string(),
        "--dataset".to_owned(),
        root.join("datasets/v5_draft5/v5_dev").display().to_string(),
        "--reference-provenance".to_owned(),
        descriptor.display().to_string(),
        "--full-spec-executable".to_owned(),
        executable.display().to_string(),
        "--agent-view-executable".to_owned(),
        executable.display().to_string(),
        "--out".to_owned(),
        output.display().to_string(),
    ];
    assert!(!invoke_cli(&root, &args).status.success());
    assert!(
        !output.exists(),
        "invalid descriptor must not leave partial freeze output"
    );

    let existing_output = temp.path().join("existing-output");
    fs::create_dir(&existing_output).unwrap();
    fs::write(existing_output.join("preserve.txt"), b"leave this in place").unwrap();
    let mut existing_args = args;
    let out_index = existing_args
        .iter()
        .position(|value| value == "--out")
        .unwrap();
    existing_args[out_index + 1] = existing_output.display().to_string();
    assert!(!invoke_cli(&root, &existing_args).status.success());
    assert_eq!(
        fs::read(existing_output.join("preserve.txt")).unwrap(),
        b"leave this in place"
    );
}
