use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use ib_v5_conformance::strict_json_loads;
use serde_json::{Map, Value};
use tempfile::tempdir;

use super::adapt_v3_manifest;

const CURSOR_LITERAL_FIXTURE: &str = concat!(
    "reports/v5/conformance/shared_results/v1/full-conformance-v3/prepared/",
    "full-conformance-v3-cursor-literal/corpus-snapshot-v2/reports/v5/",
    "conformance/shared_inputs/v3-cursor-literal"
);

fn repository_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn read_manifest(path: &Path) -> Map<String, Value> {
    let raw = fs::read(path).expect("fixture manifest readable");
    match strict_json_loads(&raw).expect("fixture manifest parses") {
        Value::Object(value) => value,
        _ => panic!("fixture manifest is an object"),
    }
}

fn copy_tree(source: &Path, destination: &Path) {
    fs::create_dir_all(destination).expect("destination directory created");
    for entry in fs::read_dir(source).expect("source directory readable") {
        let entry = entry.expect("source entry readable");
        let source_path = entry.path();
        let destination_path = destination.join(entry.file_name());
        if source_path.is_dir() {
            copy_tree(&source_path, &destination_path);
        } else if fs::hard_link(&source_path, &destination_path).is_err() {
            fs::copy(&source_path, &destination_path).expect("fixture file copied");
        }
    }
}

fn corpus_files(input_root: &Path) -> BTreeMap<String, (Vec<u8>, PathBuf)> {
    [
        ("phase_inputs", "stimuli/phase_inputs.jsonl"),
        ("recipes", "stimuli/recipes.jsonl"),
        ("traces", "stimuli/traces.jsonl"),
        ("family_map", "family_map.json"),
    ]
    .into_iter()
    .map(|(label, relative)| {
        let path = input_root.join(relative);
        (
            label.to_owned(),
            (fs::read(&path).expect("source artifact readable"), path),
        )
    })
    .collect()
}

#[test]
fn adapts_the_frozen_cursor_literal_v3_manifest() {
    let source_root = repository_root().join(CURSOR_LITERAL_FIXTURE);
    let temporary = tempdir().expect("temporary repository root created");
    let repository = temporary.path();
    let input_relative = Path::new("reports/v5/conformance/shared_inputs/v3-cursor-literal");
    let input_root = repository.join(input_relative);
    copy_tree(&source_root, &input_root);
    let manifest_path = input_root.join("manifest.json");
    let manifest = read_manifest(&manifest_path);
    let files = corpus_files(&input_root);
    let mut errors = Vec::new();
    let mut gaps = Vec::new();

    let adapted = adapt_v3_manifest(
        &manifest,
        &manifest_path,
        repository,
        &files,
        &mut errors,
        &mut gaps,
    )
    .expect("the complete v3 envelope adapts");

    assert!(errors.is_empty(), "unexpected errors: {errors:?}");
    assert!(gaps.is_empty(), "unexpected gaps: {gaps:?}");
    assert_eq!(
        adapted.get("all_case_count").and_then(Value::as_i64),
        Some(1702)
    );
    assert_eq!(
        adapted
            .get("all_case_index")
            .and_then(Value::as_array)
            .map(Vec::len),
        Some(1702)
    );
    assert_eq!(
        adapted
            .get("stimulus_artifacts")
            .and_then(Value::as_object)
            .map(Map::len),
        Some(2)
    );
    assert!(adapted.contains_key("_v3_case_index_metadata"));
    assert!(adapted.contains_key("_v3_phase_target_index"));
}

#[test]
fn incomplete_v3_envelope_is_a_gap_and_fails_closed() {
    let root = repository_root();
    let manifest_path = root.join(CURSOR_LITERAL_FIXTURE).join("manifest.json");
    let mut manifest = read_manifest(&manifest_path);
    manifest.remove("phase_checkpoint");
    let mut errors = Vec::new();
    let mut gaps = Vec::new();

    let adapted = adapt_v3_manifest(
        &manifest,
        &manifest_path,
        &root,
        &BTreeMap::new(),
        &mut errors,
        &mut gaps,
    );

    assert!(adapted.is_none());
    assert!(errors.is_empty());
    assert!(
        gaps.iter()
            .any(|gap| gap == "v3 shared manifest omits fields ['phase_checkpoint']")
    );
}
