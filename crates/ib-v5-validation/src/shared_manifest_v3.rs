//! Adapter for the explicit v3 shared-corpus manifest envelope.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};
use std::sync::LazyLock;

use ib_core::hashing::sha256_hex;
use ib_v5_conformance::strict_json_loads;
use ib_v5_schemas::V5_BASE_SPEC_SHA256;
use regex::Regex;
use serde_json::{Map, Value, json};

static SHA256_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\A[a-f0-9]{64}\z").expect("valid SHA-256 regex"));

const PROTOCOL: &str = "identitybench.v5.shared-conformance.v1";
const STIMULUS_SOURCE_FILES: [(&str, &str); 4] = [
    ("derive_phase", "stimuli/phase_inputs.jsonl"),
    ("safety", "stimuli/recipes.jsonl"),
    ("validate_submission", "stimuli/recipes.jsonl"),
    ("score_trace", "stimuli/traces.jsonl"),
];

/// Validate a v3 corpus manifest and adapt its explicit source bindings to the shared checker.
pub(crate) fn adapt_v3_manifest(
    manifest: &Map<String, Value>,
    manifest_path: &Path,
    repository_root: &Path,
    corpus_files: &BTreeMap<String, (Vec<u8>, PathBuf)>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Option<Map<String, Value>> {
    let expected_fields = BTreeSet::from([
        "artifact_status",
        "case_index",
        "corpus_version",
        "counts",
        "coverage_intent_only",
        "envelope_contract",
        "family_metadata",
        "normative_inputs",
        "observed_coverage",
        "packet_sources",
        "phase_checkpoint",
        "protocol",
        "stimulus_sources",
    ]);
    let (missing, extra) = field_difference(manifest, &expected_fields);
    if !missing.is_empty() || !extra.is_empty() {
        if !missing.is_empty() {
            gaps.push(format!(
                "v3 shared manifest omits fields {}",
                py_string_list(&missing)
            ));
        }
        if !extra.is_empty() {
            errors.push(format!(
                "v3 shared manifest has unsupported fields {}",
                py_string_list(&extra)
            ));
        }
        return None;
    }
    if manifest.get("corpus_version").and_then(Value::as_str) != Some("v3")
        || manifest.get("protocol").and_then(Value::as_str) != Some(PROTOCOL)
    {
        errors.push("v3 shared manifest version or protocol is incorrect".to_owned());
        return None;
    }

    let Some((root, manifest_path, manifest_relative)) =
        manifest_path_relative(manifest_path, repository_root)
    else {
        errors.push("v3 shared manifest is outside the repository".to_owned());
        return None;
    };
    let supported_manifest_paths = [
        vec![
            "reports",
            "v5",
            "conformance",
            "shared_inputs",
            "v3",
            "manifest.json",
        ],
        vec![
            "reports",
            "v5",
            "conformance",
            "shared_inputs",
            "v3-cursor-literal",
            "manifest.json",
        ],
    ];
    if !supported_manifest_paths.iter().any(|parts| {
        parts
            .iter()
            .map(|part| (*part).to_owned())
            .collect::<Vec<_>>()
            == manifest_relative
    }) {
        errors.push("v3 shared manifest is not at its versioned public input path".to_owned());
        return None;
    }

    let expected_counts = json!({
        "episodes": 180,
        "operations": {
            "derive_phase": 360,
            "safety": 497,
            "score_trace": 60,
            "validate_submission": 785,
        },
        "phase_cases": 360,
        "phase_target_tuples": 3435,
        "recipe_cases": 1282,
        "total_cases": 1702,
        "trace_cases": 60,
    });
    if !manifest
        .get("counts")
        .is_some_and(|value| python_json_equal(value, &expected_counts))
    {
        errors
            .push("v3 shared corpus counts differ from the frozen 1702-case inventory".to_owned());
        return None;
    }

    let Some(normative) = manifest.get("normative_inputs").and_then(Value::as_object) else {
        gaps.push("v3 shared manifest normative input bindings are incomplete".to_owned());
        return None;
    };
    let normative_fields =
        BTreeSet::from(["schemas_path", "schemas_sha256", "spec_path", "spec_sha256"]);
    let (normative_missing, normative_extra) = field_difference(normative, &normative_fields);
    if !normative_missing.is_empty() || !normative_extra.is_empty() {
        gaps.push("v3 shared manifest normative input bindings are incomplete".to_owned());
        return None;
    }
    if normative.get("spec_path").and_then(Value::as_str) != Some("SPEC-v5.md")
        || normative.get("spec_sha256").and_then(Value::as_str) != Some(V5_BASE_SPEC_SHA256)
    {
        errors.push("v3 shared manifest SPEC-v5.md binding is stale".to_owned());
    }
    let schema_sha = normative.get("schemas_sha256").and_then(Value::as_str);
    if normative.get("schemas_path").and_then(Value::as_str)
        != Some("src/identitybench/v5/schemas.py")
        || !schema_sha.is_some_and(|sha| {
            sha == ib_v5_conformance::PINNED_PUBLIC_SCHEMA_SHA256
                || sha == ib_v5_conformance::HISTORICAL_PINNED_PUBLIC_SCHEMA_SHA256
        })
    {
        errors.push("v3 shared manifest public schema binding is stale".to_owned());
    }

    let Some(case_index) = manifest.get("case_index").and_then(Value::as_array) else {
        gaps.push("v3 shared manifest does not contain the full ordered case_index".to_owned());
        return None;
    };
    if case_index.len() != 1702 {
        gaps.push("v3 shared manifest does not contain the full ordered case_index".to_owned());
        return None;
    }
    let common_case_fields = BTreeSet::from([
        "canonical_case_bytes",
        "canonical_case_sha256",
        "case_id",
        "episode_id",
        "operation",
        "phase_index",
        "suite_id",
    ]);
    let mut normalized_index = Vec::with_capacity(case_index.len());
    for (index, row) in case_index.iter().enumerate() {
        let Some(row_object) = row.as_object() else {
            errors.push(format!(
                "v3 manifest case_index row {index} has an invalid shape"
            ));
            continue;
        };
        let operation = row_object.get("operation").and_then(Value::as_str);
        let operation_fields = if operation == Some("derive_phase") {
            BTreeSet::from(["source_file"])
        } else {
            BTreeSet::from([
                "input_ref",
                "stimulus_record_id",
                "stimulus_record_index",
                "stimulus_source_file",
            ])
        };
        let expected_row_fields: BTreeSet<&str> = common_case_fields
            .iter()
            .copied()
            .chain(operation_fields.iter().copied())
            .collect();
        let (row_missing, row_extra) = field_difference(row_object, &expected_row_fields);
        if !row_missing.is_empty() || !row_extra.is_empty() {
            errors.push(format!(
                "v3 manifest case_index row {index} has an invalid shape"
            ));
            continue;
        }
        let case_id = row_object.get("case_id").and_then(Value::as_str);
        let digest = row_object
            .get("canonical_case_sha256")
            .and_then(Value::as_str);
        let byte_count = row_object
            .get("canonical_case_bytes")
            .and_then(strict_integer);
        let suite_id = row_object.get("suite_id").and_then(Value::as_str);
        let episode_id = row_object.get("episode_id").and_then(Value::as_str);
        if case_id.is_none_or(str::is_empty)
            || episode_id.is_none_or(str::is_empty)
            || !matches!(suite_id, Some("v5_dev" | "v5_lite" | "v5_core" | "v5_full"))
            || !operation
                .is_some_and(|value| STIMULUS_SOURCE_FILES.iter().any(|(op, _)| *op == value))
            || byte_count.is_none_or(|count| count <= 0)
            || !digest.is_some_and(|value| SHA256_RE.is_match(value))
        {
            errors.push(format!(
                "v3 manifest case_index row {index} has invalid identity/hash fields"
            ));
            continue;
        }
        let phase_index = row_object.get("phase_index").and_then(strict_integer);
        if operation == Some("derive_phase") {
            let source = operation_source("derive_phase");
            if !phase_index.is_some_and(|phase| matches!(phase, 1 | 2))
                || row_object.get("source_file").and_then(Value::as_str) != Some(source)
            {
                errors.push(format!(
                    "v3 phase case_index row {index} has invalid phase_index"
                ));
            }
        } else {
            let expected_source = operation.and_then(operation_source_opt);
            let record_index = row_object
                .get("stimulus_record_index")
                .and_then(strict_integer);
            if !phase_index.is_some_and(|phase| matches!(phase, 1 | 2))
                || row_object
                    .get("stimulus_source_file")
                    .and_then(Value::as_str)
                    != expected_source
                || row_object
                    .get("input_ref")
                    .and_then(Value::as_str)
                    .is_none()
                || row_object
                    .get("stimulus_record_id")
                    .and_then(Value::as_str)
                    .is_none()
                || record_index.is_none_or(|value| value < 0)
            {
                errors.push(format!(
                    "v3 stimulus case_index row {index} has invalid source references"
                ));
            }
        }
        normalized_index.push(Value::Object(row_object.clone()));
    }
    if normalized_index.len() != case_index.len() {
        return None;
    }

    let Some(phase_metadata) = manifest.get("phase_checkpoint").and_then(Value::as_object) else {
        gaps.push("v3 manifest phase_checkpoint descriptor is incomplete".to_owned());
        return None;
    };
    let phase_fields = BTreeSet::from([
        "path",
        "phase_case_count",
        "phase_inputs_path",
        "phase_inputs_sha256",
        "preserved_checkpoint_bytes",
        "sha256",
    ]);
    let (phase_missing, phase_extra) = field_difference(phase_metadata, &phase_fields);
    if !phase_missing.is_empty() || !phase_extra.is_empty() {
        gaps.push("v3 manifest phase_checkpoint descriptor is incomplete".to_owned());
        return None;
    }
    if phase_metadata.get("path").and_then(Value::as_str) != Some("phase_checkpoint.json")
        || !phase_metadata
            .get("phase_case_count")
            .is_some_and(|value| python_json_equal(value, &json!(360)))
        || phase_metadata
            .get("phase_inputs_path")
            .and_then(Value::as_str)
            != Some("stimuli/phase_inputs.jsonl")
        || phase_metadata.get("preserved_checkpoint_bytes") != Some(&Value::Bool(true))
        || !is_sha_value(phase_metadata.get("sha256"))
        || !is_sha_value(phase_metadata.get("phase_inputs_sha256"))
    {
        errors.push("v3 manifest phase_checkpoint descriptor is malformed".to_owned());
        return None;
    }
    let checkpoint_ref_path = manifest_path
        .parent()
        .unwrap_or(manifest_path.as_path())
        .join("phase_checkpoint.json");
    let Some(checkpoint_ref) = relative_to_root(&checkpoint_ref_path, &root) else {
        errors.push("v3 phase checkpoint path escapes its versioned input root".to_owned());
        return None;
    };
    let checkpoint_binding = json!({
        "path": checkpoint_ref,
        "sha256": phase_metadata.get("sha256").cloned().unwrap_or(Value::Null),
    });
    let Some((checkpoint_bytes, _checkpoint_path)) = shared_bound_bytes(
        &root,
        Some(&checkpoint_binding),
        "v3 phase checkpoint",
        errors,
        gaps,
    ) else {
        return None;
    };
    let Some(checkpoint) =
        shared_json_object(&checkpoint_bytes, "v3 phase checkpoint", errors, gaps)
    else {
        return None;
    };
    if checkpoint.get("protocol") != Some(&Value::String(PROTOCOL.to_owned()))
        || checkpoint.get("corpus_version") != Some(&Value::String("v3".to_owned()))
        || !checkpoint
            .get("normative_inputs")
            .is_some_and(|value| python_json_equal(value, &Value::Object(normative.clone())))
    {
        errors.push("v3 phase checkpoint version or normative bindings differ".to_owned());
    }
    let Some(phase_artifact) = checkpoint
        .get("phase_input_artifact")
        .and_then(Value::as_object)
    else {
        gaps.push("v3 phase checkpoint has no complete phase_input_artifact descriptor".to_owned());
        return None;
    };
    let phase_artifact_fields = BTreeSet::from([
        "byte_length",
        "canonicalization",
        "case_digest",
        "path",
        "row_count",
        "sha256",
    ]);
    let (artifact_missing, artifact_extra) =
        field_difference(phase_artifact, &phase_artifact_fields);
    if !artifact_missing.is_empty() || !artifact_extra.is_empty() {
        gaps.push("v3 phase checkpoint has no complete phase_input_artifact descriptor".to_owned());
        return None;
    }
    if phase_artifact.get("path") != phase_metadata.get("phase_inputs_path")
        || !phase_artifact
            .get("row_count")
            .is_some_and(|value| python_json_equal(value, &json!(360)))
        || phase_artifact.get("sha256") != phase_metadata.get("phase_inputs_sha256")
        || phase_artifact
            .get("canonicalization")
            .and_then(Value::as_str)
            != Some("UTF-8 JSON; sorted keys; compact separators; trailing LF per record")
        || phase_artifact.get("case_digest").and_then(Value::as_str)
            != Some("SHA-256 over each full canonical envelope without trailing LF")
    {
        errors.push(
            "v3 phase input descriptor differs from the manifest checkpoint binding".to_owned(),
        );
    }
    let phase_rows = &normalized_index[..360];
    let expected_phase_index: Vec<Value> = phase_rows
        .iter()
        .map(|row| {
            json!({
                "case_id": row.get("case_id"),
                "canonical_case_sha256": row.get("canonical_case_sha256"),
                "canonical_case_bytes": row.get("canonical_case_bytes"),
                "episode_id": row.get("episode_id"),
                "phase_index": row.get("phase_index"),
                "suite_id": row.get("suite_id"),
            })
        })
        .collect();
    if phase_rows
        .iter()
        .any(|row| row.get("operation").and_then(Value::as_str) != Some("derive_phase"))
        || !checkpoint
            .get("phase_case_index")
            .is_some_and(|value| python_json_equal(value, &Value::Array(expected_phase_index)))
    {
        errors.push(
            "v3 phase checkpoint case index differs from the ordered corpus prefix".to_owned(),
        );
    }
    let phase_targets = checkpoint.get("phase_target_index");
    if !phase_targets
        .and_then(Value::as_array)
        .is_some_and(|rows| rows.len() == 3435)
    {
        errors.push("v3 phase checkpoint target index has the wrong size".to_owned());
    } else if let Some(rows) = phase_targets.and_then(Value::as_array) {
        let mut target_keys = BTreeSet::new();
        for row in rows {
            let Some(row) = row.as_object() else { continue };
            let expected_fields = BTreeSet::from([
                "episode_id",
                "mutation_kind",
                "phase_index",
                "queue_field",
                "suite_id",
                "target_entity_id",
            ]);
            if !has_exact_fields(row, &expected_fields) {
                continue;
            }
            target_keys.insert((
                python_hashable_value(row.get("suite_id").unwrap_or(&Value::Null)),
                python_hashable_value(row.get("episode_id").unwrap_or(&Value::Null)),
                python_hashable_value(row.get("phase_index").unwrap_or(&Value::Null)),
                python_hashable_value(row.get("mutation_kind").unwrap_or(&Value::Null)),
                python_hashable_value(row.get("target_entity_id").unwrap_or(&Value::Null)),
                python_hashable_value(row.get("queue_field").unwrap_or(&Value::Null)),
            ));
        }
        if target_keys.len() != 3435 {
            errors.push(
                "v3 phase checkpoint target index contains malformed or duplicate rows".to_owned(),
            );
        }
    }

    let Some(stimuli) = manifest.get("stimulus_sources").and_then(Value::as_array) else {
        gaps.push("v3 manifest must bind exactly the recipe and trace stimulus sources".to_owned());
        return None;
    };
    if stimuli.len() != 2 {
        gaps.push("v3 manifest must bind exactly the recipe and trace stimulus sources".to_owned());
        return None;
    }
    let mut stimulus_artifacts: BTreeMap<String, Map<String, Value>> = BTreeMap::new();
    let source_fields =
        BTreeSet::from(["copied_byte_exact_from", "path", "record_count", "sha256"]);
    for row in stimuli {
        let Some(row) = row.as_object() else {
            errors.push("v3 stimulus source descriptor has an invalid shape".to_owned());
            continue;
        };
        if !has_exact_fields(row, &source_fields) {
            errors.push("v3 stimulus source descriptor has an invalid shape".to_owned());
            continue;
        }
        let source_path = row.get("path").and_then(Value::as_str);
        let digest = row.get("sha256").and_then(Value::as_str);
        let count = row.get("record_count").and_then(strict_integer);
        let Some(source_path) = source_path
            .filter(|path| matches!(*path, "stimuli/recipes.jsonl" | "stimuli/traces.jsonl"))
        else {
            errors.push("v3 stimulus source path is unsupported".to_owned());
            continue;
        };
        let label = if source_path.ends_with("recipes.jsonl") {
            "recipes"
        } else {
            "traces"
        };
        let expected_count = if label == "recipes" { 1282 } else { 60 };
        if stimulus_artifacts.contains_key(label)
            || count != Some(expected_count)
            || !digest.is_some_and(|value| SHA256_RE.is_match(value))
            || row
                .get("copied_byte_exact_from")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
        {
            errors.push(format!(
                "v3 {label} stimulus source binding is malformed or duplicated"
            ));
            continue;
        }
        let mut adapted_source = Map::new();
        adapted_source.insert("path".to_owned(), Value::String(source_path.to_owned()));
        adapted_source.insert(
            "sha256".to_owned(),
            Value::String(digest.unwrap_or_default().to_owned()),
        );
        adapted_source.insert("row_count".to_owned(), json!(expected_count));
        stimulus_artifacts.insert(label.to_owned(), adapted_source);
    }
    if stimulus_artifacts
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>()
        != BTreeSet::from(["recipes", "traces"])
    {
        gaps.push("v3 manifest does not bind both recipe and trace sources".to_owned());
        return None;
    }

    let Some(family_metadata) = manifest.get("family_metadata").and_then(Value::as_object) else {
        gaps.push("v3 manifest does not bind the public family map".to_owned());
        return None;
    };
    let family_sha = family_metadata.get("sha256").and_then(Value::as_str);
    if family_metadata.get("path").and_then(Value::as_str) != Some("family_map.json")
        || !family_sha.is_some_and(|sha| SHA256_RE.is_match(sha))
    {
        gaps.push("v3 manifest does not bind the public family map".to_owned());
        return None;
    }

    let expected_artifacts = [
        (
            "phase_inputs",
            phase_artifact.get("path"),
            phase_artifact.get("sha256"),
        ),
        (
            "recipes",
            stimulus_artifacts
                .get("recipes")
                .and_then(|meta| meta.get("path")),
            stimulus_artifacts
                .get("recipes")
                .and_then(|meta| meta.get("sha256")),
        ),
        (
            "traces",
            stimulus_artifacts
                .get("traces")
                .and_then(|meta| meta.get("path")),
            stimulus_artifacts
                .get("traces")
                .and_then(|meta| meta.get("sha256")),
        ),
        (
            "family_map",
            family_metadata.get("path"),
            family_metadata.get("sha256"),
        ),
    ];
    for (label, source_path, digest) in expected_artifacts {
        let Some((raw, bound_path)) = corpus_files.get(label) else {
            gaps.push(format!("v3 corpus outer receipt omits {label}"));
            continue;
        };
        let (Some(source_path), Some(digest)) = (
            source_path.and_then(Value::as_str),
            digest.and_then(Value::as_str),
        ) else {
            errors.push(format!("v3 manifest has a malformed {label} binding"));
            continue;
        };
        let expected_path = manifest_path
            .parent()
            .unwrap_or(manifest_path.as_path())
            .join(source_path);
        let expected_resolved = match expected_path.canonicalize() {
            Ok(path) => path,
            Err(_) => {
                gaps.push(format!(
                    "v3 {label} source path is unavailable or outside repository"
                ));
                continue;
            }
        };
        let Ok(expected_relative) = expected_resolved.strip_prefix(&root) else {
            gaps.push(format!(
                "v3 {label} source path is unavailable or outside repository"
            ));
            continue;
        };
        let expected_rel = expected_relative.to_string_lossy().replace('\\', "/");
        let bound_resolved = bound_path
            .canonicalize()
            .unwrap_or_else(|_| bound_path.clone());
        if bound_resolved != expected_resolved {
            errors.push(format!(
                "v3 outer {label} artifact path differs from the corpus manifest"
            ));
        }
        if sha256_hex(raw) != digest {
            errors.push(format!(
                "v3 corpus {label} bytes differ from the corpus manifest"
            ));
        }
        let bound_relative = bound_path
            .strip_prefix(repository_root)
            .ok()
            .map(|relative| relative.to_string_lossy().replace('\\', "/"));
        if bound_relative.as_deref() != Some(expected_rel.as_str()) {
            errors.push(format!("v3 corpus {label} resolved path is inconsistent"));
        }
    }
    if let (Some((phase_raw, _)), Some(byte_length)) = (
        corpus_files.get("phase_inputs"),
        phase_artifact.get("byte_length").and_then(python_int_value),
    ) {
        if i128::try_from(phase_raw.len()).ok() != Some(byte_length) {
            errors.push("v3 phase input byte length differs from the checkpoint".to_owned());
        }
    }

    let mut adapted = manifest.clone();
    adapted.insert("all_case_count".to_owned(), json!(1702));
    adapted.insert(
        "all_case_index".to_owned(),
        Value::Array(normalized_index.clone()),
    );
    adapted.insert(
        "phase_input_artifact".to_owned(),
        Value::Object(phase_artifact.clone()),
    );
    let stimulus_value = stimulus_artifacts
        .into_iter()
        .map(|(label, metadata)| (label, Value::Object(metadata)))
        .collect::<Map<_, _>>();
    adapted.insert(
        "stimulus_artifacts".to_owned(),
        Value::Object(stimulus_value),
    );
    let mut adapted_normative = normative.clone();
    adapted_normative.insert(
        "family_map_sha256".to_owned(),
        Value::String(family_sha.unwrap_or_default().to_owned()),
    );
    adapted.insert(
        "normative_inputs".to_owned(),
        Value::Object(adapted_normative),
    );
    adapted.insert(
        "_v3_case_index_metadata".to_owned(),
        Value::Array(normalized_index),
    );
    adapted.insert(
        "_v3_phase_target_index".to_owned(),
        phase_targets.cloned().unwrap_or(Value::Null),
    );
    if !errors.is_empty() {
        return None;
    }
    Some(adapted)
}

fn field_difference(
    object: &Map<String, Value>,
    expected: &BTreeSet<&str>,
) -> (Vec<String>, Vec<String>) {
    let actual: BTreeSet<&str> = object.keys().map(String::as_str).collect();
    (
        expected
            .difference(&actual)
            .map(|field| (*field).to_owned())
            .collect(),
        actual
            .difference(expected)
            .map(|field| (*field).to_owned())
            .collect(),
    )
}

fn has_exact_fields(object: &Map<String, Value>, expected: &BTreeSet<&str>) -> bool {
    let (missing, extra) = field_difference(object, expected);
    missing.is_empty() && extra.is_empty()
}

fn strict_integer(value: &Value) -> Option<i128> {
    match value {
        Value::Number(number) => number
            .as_i64()
            .map(i128::from)
            .or_else(|| number.as_u64().map(i128::from)),
        _ => None,
    }
}

fn python_int_value(value: &Value) -> Option<i128> {
    match value {
        Value::Bool(true) => Some(1),
        Value::Bool(false) => Some(0),
        Value::Number(number) => number
            .as_i64()
            .map(i128::from)
            .or_else(|| number.as_u64().map(i128::from)),
        _ => None,
    }
}

fn python_json_equal(left: &Value, right: &Value) -> bool {
    if let (Some(left), Some(right)) = (python_number(left), python_number(right)) {
        return left == right;
    }
    match (left, right) {
        (Value::Null, Value::Null) => true,
        (Value::String(left), Value::String(right)) => left == right,
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| python_json_equal(left, right))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left.iter().all(|(key, value)| {
                    right
                        .get(key)
                        .is_some_and(|other| python_json_equal(value, other))
                })
        }
        _ => false,
    }
}

fn python_number(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(true) => Some(1.0),
        Value::Bool(false) => Some(0.0),
        Value::Number(number) => number.as_f64(),
        _ => None,
    }
}

fn is_sha_value(value: Option<&Value>) -> bool {
    value
        .and_then(Value::as_str)
        .is_some_and(|sha| SHA256_RE.is_match(sha))
}

fn operation_source(operation: &str) -> &'static str {
    operation_source_opt(operation).unwrap_or("")
}

fn operation_source_opt(operation: &str) -> Option<&'static str> {
    STIMULUS_SOURCE_FILES
        .iter()
        .find_map(|(candidate, path)| (*candidate == operation).then_some(*path))
}

fn py_string_list(values: &[String]) -> String {
    format!(
        "[{}]",
        values
            .iter()
            .map(|value| format!("'{}'", value.replace('\\', "\\\\").replace('\'', "\\'")))
            .collect::<Vec<_>>()
            .join(", ")
    )
}

fn python_hashable_value(value: &Value) -> String {
    match value {
        Value::Null => "null".to_owned(),
        Value::Bool(true) => "number:1".to_owned(),
        Value::Bool(false) => "number:0".to_owned(),
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                format!("number:{value}")
            } else if let Some(value) = number.as_u64() {
                format!("number:{value}")
            } else if let Some(value) = number.as_f64() {
                if value == 0.0 {
                    "number:0".to_owned()
                } else if value.fract() == 0.0
                    && value >= i64::MIN as f64
                    && value <= i64::MAX as f64
                {
                    format!("number:{value:.0}")
                } else {
                    format!("float:{:016x}", value.to_bits())
                }
            } else {
                "number:invalid".to_owned()
            }
        }
        Value::String(value) => format!("string:{}:{value}", value.len()),
        Value::Array(items) => format!(
            "array:[{}]",
            items
                .iter()
                .map(python_hashable_value)
                .collect::<Vec<_>>()
                .join(",")
        ),
        Value::Object(items) => format!(
            "object:{{{}}}",
            items
                .iter()
                .map(|(key, child)| format!("{}:{}", key.len(), python_hashable_value(child)))
                .collect::<Vec<_>>()
                .join(",")
        ),
    }
}

fn manifest_path_relative(
    manifest_path: &Path,
    repository_root: &Path,
) -> Option<(PathBuf, PathBuf, Vec<String>)> {
    let root = repository_root.canonicalize().ok()?;
    let path = manifest_path.canonicalize().ok()?;
    let relative = path.strip_prefix(&root).ok()?;
    let components = relative
        .components()
        .map(|part| match part {
            Component::Normal(value) => Some(value.to_string_lossy().into_owned()),
            _ => None,
        })
        .collect::<Option<Vec<_>>>()?;
    Some((root, path, components))
}

fn relative_to_root(path: &Path, root: &Path) -> Option<String> {
    let canonical = path.canonicalize().ok()?;
    let relative = canonical.strip_prefix(root).ok()?;
    Some(relative.to_string_lossy().replace('\\', "/"))
}

fn shared_bound_bytes(
    repository_root: &Path,
    reference: Option<&Value>,
    label: &str,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Option<(Vec<u8>, PathBuf)> {
    let Some(reference) = reference.and_then(Value::as_object) else {
        gaps.push(format!("shared receipt has no {label} artifact reference"));
        return None;
    };
    let required = BTreeSet::from(["path", "sha256"]);
    if !has_exact_fields(reference, &required) {
        errors.push(format!("shared {label} artifact reference fields differ"));
        return None;
    }
    let path_text = reference.get("path").and_then(Value::as_str);
    let digest = reference.get("sha256").and_then(Value::as_str);
    let (Some(path_text), Some(digest)) = (path_text, digest) else {
        errors.push(format!("shared {label} artifact reference is malformed"));
        return None;
    };
    if path_text.is_empty() || !SHA256_RE.is_match(digest) {
        errors.push(format!("shared {label} artifact reference is malformed"));
        return None;
    }
    if path_text.starts_with('/')
        || path_text.contains('\\')
        || path_text
            .split('/')
            .any(|part| matches!(part, "" | "." | ".."))
    {
        errors.push(format!("shared {label} artifact path is unsafe"));
        return None;
    }
    if !shared_artifact_path_allowed(path_text) {
        errors.push(format!(
            "shared {label} artifact is outside public dataset/conformance roots"
        ));
        return None;
    }
    if is_sensitive_path(path_text) {
        errors.push(format!("shared {label} artifact path is sensitive"));
        return None;
    }
    let root = match repository_root.canonicalize() {
        Ok(path) => path,
        Err(error) => {
            gaps.push(format!("repository root is unavailable: {error}"));
            return None;
        }
    };
    let candidate = root.join(path_text);
    let target = match candidate.canonicalize() {
        Ok(path) => path,
        Err(error) => {
            gaps.push(format!("shared {label} artifact is unavailable: {error}"));
            return None;
        }
    };
    if !target.starts_with(&root) {
        errors.push(format!("shared {label} artifact escapes repository root"));
        return None;
    }
    let Ok(relative) = target.strip_prefix(&root) else {
        errors.push(format!("shared {label} artifact escapes repository root"));
        return None;
    };
    if !shared_artifact_path_allowed(&relative.to_string_lossy().replace('\\', "/")) {
        errors.push(format!(
            "shared {label} artifact resolves outside public dataset/conformance roots"
        ));
        return None;
    }
    if is_sensitive_path(&target.to_string_lossy()) {
        errors.push(format!(
            "shared {label} artifact resolves to a sensitive path"
        ));
        return None;
    }
    let bytes = match std::fs::read(&target) {
        Ok(bytes) => bytes,
        Err(error) => {
            gaps.push(format!("shared {label} artifact is unavailable: {error}"));
            return None;
        }
    };
    if sha256_hex(&bytes) != digest {
        errors.push(format!("shared {label} artifact digest mismatch"));
        return None;
    }
    Some((bytes, target))
}

fn shared_json_object(
    raw: &[u8],
    label: &str,
    errors: &mut Vec<String>,
    _gaps: &mut Vec<String>,
) -> Option<Map<String, Value>> {
    let value = match strict_json_loads(raw) {
        Ok(value) => value,
        Err(error) => {
            errors.push(format!("shared {label} is malformed: {error}"));
            return None;
        }
    };
    match value {
        Value::Object(value) => Some(value),
        _ => {
            errors.push(format!("shared {label} must be a JSON object"));
            None
        }
    }
}

fn shared_artifact_path_allowed(path: &str) -> bool {
    let parts: Vec<&str> = path.split('/').collect();
    parts.starts_with(&["reports", "v5", "conformance"])
        || (parts.len() >= 4
            && parts[0] == "datasets"
            && parts[1] == "v5_draft5"
            && ["v5_dev", "v5_lite", "v5_core", "v5_full"].contains(&parts[2]))
        || (parts.len() >= 3
            && parts[0] == "datasets"
            && ["v5_dev", "v5_lite", "v5_core", "v5_full"].contains(&parts[1]))
}

fn is_sensitive_path(path: &str) -> bool {
    path.split(['/', '\\']).any(|part| {
        let lower = part.to_ascii_lowercase();
        part == ".env"
            || part.starts_with(".env.")
            || lower.contains("credential")
            || lower.contains("secret")
            || lower.contains("token")
    })
}

#[cfg(test)]
#[path = "shared_manifest_v3_tests.rs"]
mod tests;
