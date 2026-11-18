//! Blind/reference proof receipt validation (Python parity: validation.py:1215+).
//!
//! The gate deliberately fails closed for malformed or mismatched proof. Missing artifacts
//! remain `UNKNOWN` evidence, matching the offline validator's distinction between a failed
//! proof and evidence that cannot currently be inspected.

use std::collections::{BTreeMap, BTreeSet};
use std::io::Read;
use std::path::{Component, Path, PathBuf};

use ib_v5_conformance::shared::{
    CoverageRequirement, FrozenCase, PROTOCOL as SHARED_PROTOCOL, canonical_json_bytes,
    sha256_bytes, strict_json_loads, strict_jsonl_loads,
};
use ib_v5_generator::{DatasetArtifactsV5, load_v5_dataset};
use ib_v5_runtime::{RULE_IDS_V5, RULE_TO_POLICY_CARD_ANCHORS_V5};
use ib_v5_schemas::{
    V5_BASE_SPEC_SHA256, ViolationCategoryV5,
    normative_contract::{V5_APPROVED_CONFORMANCE_CONTRACT_ID, approved_v5_normative_contract},
};
use serde_json::{Map, Value, json};

use crate::{GateResultV5, GateStatusV5};

const GATE_ID: &str = "G-SPEC-01";
const SHARED_PROOF_SCHEMA: &str = "identitybench.v5.shared-proof-receipt.v1";
const SHARED_PROOF_SCOPE: &str = "G-SPEC-01";
const SHARED_FREEZE_V3: &str = "identitybench.v5.blind-output-freeze.v3";
const SHARED_FREEZE_V4: &str = "identitybench.v5.blind-output-freeze.v4";

const SHARED_PROOF_FIELDS: [&str; 8] = [
    "schema_version",
    "proof_scope",
    "suite_id",
    "spec_sha256",
    "dataset_bindings",
    "corpus",
    "freeze",
    "evaluators",
];
const SHARED_CORPUS_FIELDS: [&str; 8] = [
    "manifest",
    "phase_inputs",
    "recipes",
    "traces",
    "family_map",
    "public_cases",
    "case_index",
    "coverage",
];
const SHARED_EVALUATORS: [&str; 3] = ["full_spec_blind", "agent_view_only", "production"];
const SHARED_EVALUATOR_FIELDS: [&str; 12] = [
    "qualification",
    "wire_evaluator_id",
    "source_profile",
    "author_origin",
    "source_closure_sha256",
    "source_capture_manifest",
    "runtime_manifest",
    "runtime_archive",
    "dependency_manifest",
    "run_manifest",
    "raw_receipts",
    "raw_audits",
];
const SHA256_HEX_LEN: usize = 64;

pub(crate) struct SharedCorpus {
    pub(crate) cases: Vec<FrozenCase>,
    pub(crate) family_map: BTreeMap<String, BTreeMap<String, String>>,
    pub(crate) family_map_sha256: String,
    pub(crate) coverage: CoverageRequirement,
    pub(crate) case_manifest_sha256: String,
    pub(crate) case_index_sha256: String,
    pub(crate) coverage_sha256: String,
}

/// Validate the optional G-SPEC-01 proof artifact.
#[cfg(test)]
pub(crate) fn blind_gate(
    path: Option<&Path>,
    artifacts: &DatasetArtifactsV5,
    repository_root: &Path,
) -> GateResultV5 {
    blind_gate_with_context(path, artifacts, repository_root, None)
}

pub(crate) fn blind_gate_with_context(
    path: Option<&Path>,
    artifacts: &DatasetArtifactsV5,
    repository_root: &Path,
    native_build_identity: Option<&Value>,
) -> GateResultV5 {
    let Some(path) = path else {
        return GateResultV5::with_evidence(
            GATE_ID,
            GateStatusV5::Unknown,
            json!({"proof_artifact": null}),
            vec![
                "bound independent full-spec and agent-view references are unavailable".to_owned(),
            ],
        );
    };

    let root = repository_root;
    let root = match root.canonicalize() {
        Ok(value) => value,
        Err(error) => {
            return gate(
                GateStatusV5::Unknown,
                json!({"proof_artifact": path.to_string_lossy()}),
                vec![format!("repository root is unavailable: {error}")],
            );
        }
    };
    if is_sensitive_path(path) {
        return gate(
            GateStatusV5::Fail,
            json!({"proof_artifact": path.to_string_lossy()}),
            vec!["proof artifact path is sensitive and cannot be read".to_owned()],
        );
    }

    let lexical_path = if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    };
    if is_sensitive_path(&lexical_path) {
        return gate(
            GateStatusV5::Fail,
            json!({"proof_artifact": path.to_string_lossy()}),
            vec!["proof artifact path is sensitive and cannot be read".to_owned()],
        );
    }
    let Some(lexical_relative) = lexical_path.strip_prefix(&root).ok() else {
        return gate(
            GateStatusV5::Fail,
            json!({"proof_artifact": path.to_string_lossy()}),
            vec!["proof artifact is outside approved public dataset/conformance roots".to_owned()],
        );
    };
    if has_parent_component(&lexical_path) || !proof_artifact_path_allowed(lexical_relative) {
        return gate(
            GateStatusV5::Fail,
            json!({"proof_artifact": path.to_string_lossy()}),
            vec!["proof artifact is outside approved public dataset/conformance roots".to_owned()],
        );
    }

    // Apply the same bounded, no-symlink entry boundary to every receipt schema.
    // Schema dispatch must never read a native receipt through a weaker path.
    let mut current = root.clone();
    for component in lexical_relative.components() {
        let Component::Normal(part) = component else {
            continue;
        };
        current.push(part);
        if let Ok(metadata) = std::fs::symlink_metadata(&current)
            && metadata.file_type().is_symlink()
        {
            return gate(
                GateStatusV5::Fail,
                json!({"proof_artifact": path.to_string_lossy()}),
                vec!["proof artifact path contains a symlink".to_owned()],
            );
        }
    }

    let resolved_path = match lexical_path.canonicalize() {
        Ok(value) => value,
        Err(error) => {
            return gate(
                GateStatusV5::Unknown,
                json!({"proof_artifact": path.to_string_lossy()}),
                vec![format!("proof artifact is unavailable: {error}")],
            );
        }
    };
    if is_sensitive_path(&resolved_path) {
        return gate(
            GateStatusV5::Fail,
            json!({"proof_artifact": path.to_string_lossy()}),
            vec!["proof artifact resolves to a sensitive path and cannot be read".to_owned()],
        );
    }
    let Some(resolved_relative) = resolved_path.strip_prefix(&root).ok() else {
        return gate(
            GateStatusV5::Fail,
            json!({"proof_artifact": path.to_string_lossy()}),
            vec![
                "proof artifact resolves outside approved public dataset/conformance roots"
                    .to_owned(),
            ],
        );
    };
    if !proof_artifact_path_allowed(resolved_relative) {
        return gate(
            GateStatusV5::Fail,
            json!({"proof_artifact": path.to_string_lossy()}),
            vec![
                "proof artifact resolves outside approved public dataset/conformance roots"
                    .to_owned(),
            ],
        );
    }

    const MAX_RECEIPT_BYTES: u64 = 64 * 1024 * 1024;
    if let Ok(metadata) = std::fs::symlink_metadata(&resolved_path)
        && (!metadata.is_file() || metadata.len() > MAX_RECEIPT_BYTES)
    {
        return gate(
            GateStatusV5::Fail,
            json!({"proof_artifact": path.to_string_lossy()}),
            vec!["proof artifact must be a regular file no larger than 64 MiB".to_owned()],
        );
    }
    let read_result = std::fs::File::open(&resolved_path).and_then(|file| {
        let mut bytes = Vec::new();
        file.take(MAX_RECEIPT_BYTES + 1).read_to_end(&mut bytes)?;
        Ok(bytes)
    });
    let raw = match read_result {
        Ok(value) => value,
        Err(error) => {
            return gate(
                GateStatusV5::Unknown,
                json!({"proof_artifact": path.to_string_lossy()}),
                vec![format!("proof artifact is unavailable: {error}")],
            );
        }
    };
    if raw.len() as u64 > MAX_RECEIPT_BYTES {
        return gate(
            GateStatusV5::Fail,
            json!({"proof_artifact": path.to_string_lossy()}),
            vec!["proof artifact exceeds the 64 MiB receipt limit".to_owned()],
        );
    }
    let receipt = match strict_json_loads(&raw) {
        Ok(Value::Object(value)) => value,
        Ok(_) => {
            return gate(
                GateStatusV5::Fail,
                json!({"receipt_sha256": sha256_bytes(&raw)}),
                vec!["receipt must be a JSON object".to_owned()],
            );
        }
        Err(error) => {
            return gate(
                GateStatusV5::Unknown,
                json!({"proof_artifact": path.to_string_lossy()}),
                vec![format!("blind proof receipt is malformed: {error}")],
            );
        }
    };

    let (errors, gaps, evidence) =
        if receipt.get("schema_version").and_then(Value::as_str) == Some(SHARED_PROOF_SCHEMA) {
            match validate_shared_proof_receipt(&resolved_path, &raw, &receipt, artifacts, &root) {
                Ok(result) => result,
                Err(error) => (
                    vec![format!("shared proof validation failed closed: {error}")],
                    Vec::new(),
                    json!({"receipt_sha256": sha256_bytes(&raw)}),
                ),
            }
        } else if receipt.get("schema_version").and_then(Value::as_str)
            == Some("identitybench.v5.native-shared-proof-receipt.v1")
        {
            match crate::native_receipt::validate_native_gspec_receipt(
                &resolved_path,
                &raw,
                &receipt,
                artifacts,
                &root,
                native_build_identity,
            ) {
                Ok(result) => result,
                Err(error) => (
                    vec![format!(
                        "native shared proof validation failed closed: {error}"
                    )],
                    Vec::new(),
                    json!({"receipt_sha256": sha256_bytes(&raw)}),
                ),
            }
        } else {
            crate::reference_receipt::validate_bound_reference_receipt(
                &resolved_path,
                &raw,
                &receipt,
                artifacts,
                &root,
            )
        };
    let status = if !errors.is_empty() {
        GateStatusV5::Fail
    } else if !gaps.is_empty() {
        GateStatusV5::Unknown
    } else {
        GateStatusV5::Pass
    };
    let mut all_errors = errors;
    all_errors.extend(gaps);
    gate(status, evidence, all_errors)
}

fn gate(status: GateStatusV5, evidence: Value, errors: Vec<String>) -> GateResultV5 {
    GateResultV5::with_evidence(GATE_ID, status, evidence, errors)
}

fn has_parent_component(path: &Path) -> bool {
    path.components()
        .any(|component| component == Component::ParentDir)
}

fn is_sensitive_path(path: &Path) -> bool {
    path.components().any(|component| {
        let Component::Normal(part) = component else {
            return false;
        };
        let lower = part.to_string_lossy().to_lowercase();
        lower == ".env"
            || lower.starts_with(".env.")
            || lower.contains("credential")
            || lower.contains("secret")
            || lower.contains("token")
    })
}

fn proof_artifact_path_allowed(path: &Path) -> bool {
    let parts: Vec<String> = path
        .components()
        .filter_map(|part| match part {
            Component::Normal(value) => Some(value.to_string_lossy().into_owned()),
            _ => None,
        })
        .collect();
    if parts.len() >= 3 && parts[..3] == ["reports", "v5", "conformance"] {
        return true;
    }
    if parts.len() >= 4
        && parts[0] == "datasets"
        && parts[1] == "v5_draft5"
        && ["v5_dev", "v5_lite", "v5_core", "v5_full"].contains(&parts[2].as_str())
    {
        return true;
    }
    parts.len() >= 3
        && parts[0] == "datasets"
        && ["v5_dev", "v5_lite", "v5_core", "v5_full"].contains(&parts[1].as_str())
}

fn exact_fields(value: &Map<String, Value>, expected: &[&str]) -> (Vec<String>, Vec<String>) {
    let expected: BTreeSet<&str> = expected.iter().copied().collect();
    let actual: BTreeSet<&str> = value.keys().map(String::as_str).collect();
    let missing = expected
        .difference(&actual)
        .map(|x| (*x).to_owned())
        .collect();
    let extra = actual
        .difference(&expected)
        .map(|x| (*x).to_owned())
        .collect();
    (missing, extra)
}

fn is_sha256(value: &str) -> bool {
    value.len() == SHA256_HEX_LEN
        && value
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte))
}

fn shared_artifact_path_allowed(path: &str) -> bool {
    let parts: Vec<&str> = path.split('/').collect();
    parts.first() == Some(&"reports")
        && parts.get(1) == Some(&"v5")
        && parts.get(2) == Some(&"conformance")
        || (parts.first() == Some(&"datasets")
            && parts.get(1) == Some(&"v5_draft5")
            && parts
                .get(2)
                .is_some_and(|suite| ["v5_dev", "v5_lite", "v5_core", "v5_full"].contains(suite))
            && parts.len() >= 4)
        || (parts.first() == Some(&"datasets")
            && parts
                .get(1)
                .is_some_and(|suite| ["v5_dev", "v5_lite", "v5_core", "v5_full"].contains(suite))
            && parts.len() >= 3)
}

pub(crate) fn shared_bound_bytes(
    root: &Path,
    reference: Option<&Value>,
    label: &str,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
    require_row_count: bool,
    reject_symlinks: bool,
) -> Option<(Vec<u8>, PathBuf)> {
    let Some(reference) = reference.and_then(Value::as_object) else {
        gaps.push(format!("shared receipt has no {label} artifact reference"));
        return None;
    };
    let expected_fields: &[&str] = if require_row_count {
        &["path", "sha256", "row_count"]
    } else {
        &["path", "sha256"]
    };
    let (missing, extra) = exact_fields(reference, expected_fields);
    if !missing.is_empty() || !extra.is_empty() {
        errors.push(format!("shared {label} artifact reference fields differ"));
        return None;
    }
    let path_text = reference.get("path").and_then(Value::as_str);
    let digest = reference.get("sha256").and_then(Value::as_str);
    let (Some(path_text), Some(digest)) = (path_text, digest) else {
        errors.push(format!("shared {label} artifact reference is malformed"));
        return None;
    };
    if path_text.is_empty() || !is_sha256(digest) {
        errors.push(format!("shared {label} artifact reference is malformed"));
        return None;
    }
    if require_row_count
        && reference
            .get("row_count")
            .and_then(Value::as_i64)
            .is_none_or(|count| count < 0)
    {
        errors.push(format!(
            "shared {label} row_count must be a nonnegative integer"
        ));
        return None;
    }
    let relative = Path::new(path_text);
    if relative.is_absolute()
        || path_text.contains('\\')
        || relative.components().any(|part| {
            matches!(
                part,
                Component::ParentDir | Component::CurDir | Component::RootDir
            )
        })
        || path_text.split('/').any(str::is_empty)
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
    if is_sensitive_path(relative) {
        errors.push(format!("shared {label} artifact path is sensitive"));
        return None;
    }
    let root = match root.canonicalize() {
        Ok(value) => value,
        Err(error) => {
            gaps.push(format!("repository root is unavailable: {error}"));
            return None;
        }
    };
    let candidate = root.join(relative);
    if reject_symlinks {
        let mut current = root.clone();
        for part in relative.components() {
            let Component::Normal(part) = part else {
                continue;
            };
            current.push(part);
            match std::fs::symlink_metadata(&current) {
                Ok(meta) if meta.file_type().is_symlink() => {
                    errors.push(format!("shared {label} artifact path contains a symlink"));
                    return None;
                }
                Ok(_) => {}
                Err(error) => {
                    gaps.push(format!("shared {label} artifact is unavailable: {error}"));
                    return None;
                }
            }
        }
    }
    let target = match candidate.canonicalize() {
        Ok(value) => value,
        Err(error) => {
            gaps.push(format!("shared {label} artifact is unavailable: {error}"));
            return None;
        }
    };
    if !target.starts_with(&root) {
        errors.push(format!("shared {label} artifact escapes repository root"));
        return None;
    }
    if reject_symlinks && !target.is_file() {
        errors.push(format!("shared {label} artifact is not a regular file"));
        return None;
    }
    let Ok(resolved_relative) = target.strip_prefix(&root) else {
        errors.push(format!("shared {label} artifact escapes repository root"));
        return None;
    };
    if !proof_artifact_path_allowed(resolved_relative) {
        errors.push(format!(
            "shared {label} artifact resolves outside public dataset/conformance roots"
        ));
        return None;
    }
    if is_sensitive_path(&target) {
        errors.push(format!(
            "shared {label} artifact resolves to a sensitive path"
        ));
        return None;
    }
    let payload = match std::fs::read(&target) {
        Ok(value) => value,
        Err(error) => {
            gaps.push(format!("shared {label} artifact is unavailable: {error}"));
            return None;
        }
    };
    if sha256_bytes(&payload) != digest {
        errors.push(format!("shared {label} artifact digest mismatch"));
        return None;
    }
    Some((payload, target))
}

pub(crate) fn shared_json_object(
    raw: &[u8],
    label: &str,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
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
            let _ = gaps;
            None
        }
    }
}

pub(crate) fn shared_jsonl_rows(
    raw: &[u8],
    label: &str,
    row_count: i64,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Option<Vec<Value>> {
    let values = match strict_jsonl_loads(raw) {
        Ok(values) => values,
        Err(error) => {
            errors.push(format!("shared {label} JSONL is malformed: {error}"));
            return None;
        }
    };
    if !values.iter().all(Value::is_object) {
        errors.push(format!("shared {label} JSONL rows must be objects"));
        return None;
    }
    if i64::try_from(values.len()).ok() != Some(row_count) {
        gaps.push(format!(
            "shared {label} row count differs: expected {row_count}, found {}",
            values.len()
        ));
        return None;
    }
    let mut canonical = Vec::new();
    for value in &values {
        match canonical_json_bytes(value) {
            Ok(row) => {
                canonical.extend_from_slice(&row);
                canonical.push(b'\n');
            }
            Err(error) => {
                errors.push(format!("shared {label} JSONL is malformed: {error}"));
                return None;
            }
        }
    }
    if canonical != raw {
        errors.push(format!(
            "shared {label} JSONL is not canonical ordered UTF-8 JSONL"
        ));
        return None;
    }
    Some(values)
}

/// Reconstruct the complete public shared corpus against all exact sealed datasets.
/// Native and archival receipts deliberately share one coverage and binding contract.
pub(crate) fn reconstruct_native_gspec_corpus(
    receipt: &Map<String, Value>,
    artifacts: &DatasetArtifactsV5,
    root: &Path,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
    evidence: &mut Value,
) -> Result<Option<SharedCorpus>, String> {
    let mut dataset_bindings = BTreeMap::new();
    if let Some(rows) = receipt.get("dataset_bindings").and_then(Value::as_array) {
        for (index, row) in rows.iter().enumerate() {
            let label = format!("dataset binding {index}");
            let Some(row) = row.as_object() else {
                errors.push(format!("{label} fields differ"));
                continue;
            };
            let (missing, extra) = exact_fields(row, &["suite_id", "seal", "episodes", "oracles"]);
            if !missing.is_empty() || !extra.is_empty() {
                errors.push(format!("{label} fields differ"));
                continue;
            }
            let Some(suite) = row.get("suite_id").and_then(Value::as_str) else {
                errors.push(format!("{label} suite_id is invalid or duplicated"));
                continue;
            };
            if !["v5_dev", "v5_lite", "v5_core", "v5_full"].contains(&suite)
                || dataset_bindings.contains_key(suite)
            {
                errors.push(format!("{label} suite_id is invalid or duplicated"));
                continue;
            }
            let mut lexical_parent = None::<PathBuf>;
            let mut lexical_valid = true;
            for (name, expected) in [
                ("seal", "SEAL.json"),
                ("episodes", "episodes.jsonl"),
                ("oracles", "oracles.jsonl"),
            ] {
                let Some(path) = row
                    .get(name)
                    .and_then(Value::as_object)
                    .and_then(|reference| reference.get("path"))
                    .and_then(Value::as_str)
                else {
                    errors.push(format!("{label} does not bind seal, episodes, and oracles"));
                    lexical_valid = false;
                    break;
                };
                let path = Path::new(path);
                if path.file_name().and_then(|value| value.to_str()) != Some(expected) {
                    lexical_valid = false;
                    break;
                }
                let parent = path.parent().unwrap_or_else(|| Path::new("")).to_path_buf();
                if lexical_parent
                    .as_ref()
                    .is_some_and(|existing| existing != &parent)
                {
                    lexical_valid = false;
                    break;
                }
                lexical_parent = Some(parent);
            }
            if !lexical_valid {
                errors.push(format!(
                    "{label} dataset artifact paths are not one sealed dataset"
                ));
                continue;
            }
            let mut resolved = Vec::new();
            let mut payloads = Vec::new();
            let mut failed = false;
            for name in ["seal", "episodes", "oracles"] {
                let Some(reference) = row.get(name) else {
                    errors.push(format!("{label} does not bind seal, episodes, and oracles"));
                    failed = true;
                    break;
                };
                let Some((payload, target)) = shared_bound_bytes(
                    root,
                    Some(reference),
                    &format!("{suite} {name}"),
                    errors,
                    gaps,
                    false,
                    false,
                ) else {
                    failed = true;
                    break;
                };
                let expected_name = match name {
                    "seal" => "SEAL.json",
                    "episodes" => "episodes.jsonl",
                    _ => "oracles.jsonl",
                };
                if target.file_name().and_then(|x| x.to_str()) != Some(expected_name) {
                    errors.push(format!(
                        "{label} dataset artifact paths are not one sealed dataset"
                    ));
                    failed = true;
                    break;
                }
                resolved.push(target);
                payloads.push(payload);
            }
            if failed {
                continue;
            }
            if resolved.len() != 3
                || resolved[0].parent() != resolved[1].parent()
                || resolved[1].parent() != resolved[2].parent()
            {
                errors.push(format!(
                    "{label} dataset artifact paths are not one sealed dataset"
                ));
                continue;
            }
            let Some(dataset_dir) = resolved[0].parent() else {
                errors.push(format!(
                    "{label} dataset artifact paths are not one sealed dataset"
                ));
                continue;
            };
            let dataset = match load_v5_dataset(dataset_dir) {
                Ok(value) => value,
                Err(error) => {
                    errors.push(format!(
                        "{suite} sealed dataset failed strict validation: {error}"
                    ));
                    continue;
                }
            };
            if [
                dataset.seal_bytes.as_slice(),
                dataset.episodes_bytes.as_slice(),
                dataset.oracles_bytes.as_slice(),
            ] != [
                payloads[0].as_slice(),
                payloads[1].as_slice(),
                payloads[2].as_slice(),
            ] {
                errors.push(format!(
                    "{suite} dataset bytes differ from the referenced artifacts"
                ));
                continue;
            }
            if dataset.seal.get("suite").and_then(Value::as_str) != Some(suite) {
                errors.push(format!("{suite} seal names a different suite"));
                continue;
            }
            dataset_bindings.insert(suite.to_owned(), dataset);
        }
    } else {
        gaps.push("shared receipt dataset_bindings are unavailable".to_owned());
    }
    let required_suites: BTreeSet<&str> = ["v5_dev", "v5_lite", "v5_core", "v5_full"]
        .into_iter()
        .collect();
    let missing_suites: Vec<&str> = required_suites
        .difference(&dataset_bindings.keys().map(String::as_str).collect())
        .copied()
        .collect();
    if !missing_suites.is_empty() {
        gaps.push(format!(
            "shared proof is missing sealed dataset bindings {missing_suites:?}"
        ));
    }
    if let Some(map) = evidence.as_object_mut() {
        map.insert(
            "dataset_binding_suites".to_owned(),
            json!(dataset_bindings.keys().cloned().collect::<Vec<_>>()),
        );
    }
    if let Some(suite) = artifacts.seal.get("suite").and_then(Value::as_str) {
        match dataset_bindings.get(suite) {
            None => gaps.push("shared proof does not bind the requested sealed dataset".to_owned()),
            Some(selected)
                if selected.seal_bytes != artifacts.seal_bytes
                    || selected.episodes_bytes != artifacts.episodes_bytes
                    || selected.oracles_bytes != artifacts.oracles_bytes =>
            {
                errors.push(
                    "shared proof dataset binding differs from the requested dataset bytes"
                        .to_owned(),
                );
            }
            Some(_) => {}
        }
    }

    let corpus = receipt.get("corpus").and_then(Value::as_object);
    let Some(corpus) = corpus else {
        gaps.push("shared receipt corpus descriptors are unavailable".to_owned());
        return Ok(None);
    };
    let (missing, extra) = exact_fields(corpus, &SHARED_CORPUS_FIELDS);
    if !missing.is_empty() {
        gaps.push(format!(
            "shared corpus omits artifact references {missing:?}"
        ));
    }
    if !extra.is_empty() {
        errors.push(format!("shared corpus has unexpected fields {extra:?}"));
    }
    let mut corpus_files: BTreeMap<String, (Vec<u8>, PathBuf)> = BTreeMap::new();
    for label in SHARED_CORPUS_FIELDS {
        if let Some((payload, resolved)) = shared_bound_bytes(
            root,
            corpus.get(label),
            &format!("corpus {label}"),
            errors,
            gaps,
            false,
            false,
        ) {
            corpus_files.insert(label.to_owned(), (payload, resolved));
        }
    }
    if let Some(map) = evidence.as_object_mut() {
        map.insert(
            "verified_corpus_artifacts".to_owned(),
            json!(corpus_files.keys().cloned().collect::<Vec<_>>()),
        );
    }
    if corpus_files.len() != SHARED_CORPUS_FIELDS.len() {
        return Ok(None);
    }
    let corpus_data = validate_shared_corpus(&corpus_files, &dataset_bindings, root, errors, gaps)?;
    Ok(corpus_data)
}

fn validate_shared_proof_receipt(
    path: &Path,
    raw: &[u8],
    receipt: &Map<String, Value>,
    artifacts: &DatasetArtifactsV5,
    root: &Path,
) -> Result<(Vec<String>, Vec<String>, Value), String> {
    let mut errors = Vec::new();
    let mut gaps = Vec::new();
    let mut evidence = json!({
        "receipt_sha256": sha256_bytes(raw),
        "shared_proof_schema": SHARED_PROOF_SCHEMA,
        "tool_history_cryptographically_proven": false,
    });
    let (missing, extra) = exact_fields(receipt, &SHARED_PROOF_FIELDS);
    if !missing.is_empty() {
        gaps.push(format!("shared receipt omits fields {missing:?}"));
    }
    if !extra.is_empty() {
        errors.push(format!("shared receipt has unexpected fields {extra:?}"));
    }
    match receipt.get("proof_scope").and_then(Value::as_str) {
        Some(SHARED_PROOF_SCOPE) => {}
        Some("diagnostic_phase_checkpoint" | "diagnostic_phase_batch" | "phase_checkpoint") => {
            gaps.push("phase-only diagnostic proof cannot establish full G-SPEC-01".to_owned());
        }
        _ => errors.push("shared receipt proof_scope is unsupported".to_owned()),
    }
    if receipt.get("suite_id") != artifacts.seal.get("suite") {
        errors.push("shared receipt suite_id does not match the requested dataset".to_owned());
    }
    if receipt.get("spec_sha256").and_then(Value::as_str) != Some(V5_BASE_SPEC_SHA256) {
        errors.push("shared receipt normative SPEC-v5.md digest is stale or incorrect".to_owned());
    }

    // The shared proof is a conjunction of the corpus, freeze, and all three evaluator
    // streams. Missing parts are explicit gaps; a partial proof can never be promoted.
    let Some(corpus_data) = reconstruct_native_gspec_corpus(
        receipt,
        artifacts,
        root,
        &mut errors,
        &mut gaps,
        &mut evidence,
    )?
    else {
        return Ok((errors, gaps, evidence));
    };
    if let Some(map) = evidence.as_object_mut() {
        map.insert("raw_case_count".to_owned(), json!(corpus_data.cases.len()));
        map.insert(
            "case_manifest_sha256".to_owned(),
            json!(corpus_data.case_manifest_sha256),
        );
    }

    // Verify the exact full-conformance freeze using the same process verifier used by
    // `conformance_process`; it also re-reads the referenced source, runtime and raw streams.
    let freeze_bytes = shared_bound_bytes(
        root,
        receipt.get("freeze"),
        "blind output freeze",
        &mut errors,
        &mut gaps,
        false,
        false,
    );
    let Some((freeze_raw, freeze_path)) = freeze_bytes else {
        return Ok((errors, gaps, evidence));
    };
    let Some(freeze_value) =
        shared_json_object(&freeze_raw, "blind output freeze", &mut errors, &mut gaps)
    else {
        return Ok((errors, gaps, evidence));
    };
    let freeze_schema = freeze_value
        .get("schema_version")
        .and_then(Value::as_str)
        .unwrap_or("");
    if freeze_value.get("proof_scope").and_then(Value::as_str) != Some("full_shared_conformance") {
        gaps.push(
            "blind output freeze is diagnostic or does not cover full shared conformance"
                .to_owned(),
        );
        return Ok((errors, gaps, evidence));
    }
    if freeze_schema == SHARED_FREEZE_V3 {
        gaps.push("historical v3 freeze binds only the base SPEC and cannot qualify the current approved normative contract".to_owned());
        if let Some(map) = evidence.as_object_mut() {
            map.insert(
                "normative_contract_status".to_owned(),
                Value::String("historical_base_spec_only".to_owned()),
            );
        }
    } else if freeze_schema != SHARED_FREEZE_V4 {
        gaps.push(
            "blind output freeze uses an unsupported current conformance contract".to_owned(),
        );
        return Ok((errors, gaps, evidence));
    } else {
        if freeze_value.get("normative_contract") != Some(&approved_v5_normative_contract()) {
            errors.push(
                "v4 freeze does not bind the approved three-document normative contract".to_owned(),
            );
            return Ok((errors, gaps, evidence));
        }
        if let Some(map) = evidence.as_object_mut() {
            map.insert(
                "normative_contract_status".to_owned(),
                Value::String(V5_APPROVED_CONFORMANCE_CONTRACT_ID.to_owned()),
            );
        }
    }
    let scope = freeze_value.get("scope");
    match scope.and_then(Value::as_object) {
        None => errors.push("full-conformance freeze has no execution scope".to_owned()),
        Some(scope) => {
            let operations_ok = scope
                .get("operations")
                .and_then(Value::as_array)
                .and_then(|values| values.iter().map(Value::as_str).collect::<Option<Vec<_>>>())
                .is_some_and(|values| {
                    values
                        == [
                            "derive_phase",
                            "safety",
                            "score_trace",
                            "validate_submission",
                        ]
                });
            let phases_ok = scope
                .get("phase_indices")
                .and_then(Value::as_array)
                .and_then(|values| values.iter().map(Value::as_i64).collect::<Option<Vec<_>>>())
                .is_some_and(|values| values == [1, 2]);
            if scope.get("case_count").and_then(Value::as_i64) != Some(1702)
                || !operations_ok
                || !phases_ok
                || scope
                    .get("scope_id")
                    .and_then(Value::as_str)
                    .is_none_or(str::is_empty)
                || scope
                    .get("case_index_sha256")
                    .and_then(Value::as_str)
                    .is_none_or(|v| !is_sha256(v))
                || scope.get("turn_context").and_then(Value::as_str) != Some("T0")
                || scope.get("steering").and_then(Value::as_str) != Some("UNSTEERED")
            {
                errors.push(
                    "full-conformance freeze scope is not the exact 1702-case contract".to_owned(),
                );
            }
        }
    }
    let freeze_ref = receipt.get("freeze").and_then(Value::as_object);
    let freeze_ref_path = freeze_ref
        .and_then(|v| v.get("path"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let freeze_ref_sha = freeze_ref
        .and_then(|v| v.get("sha256"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let expected_freeze_root = if freeze_schema == SHARED_FREEZE_V4 {
        "reports/v5/conformance/shared_results/approved_addendum_v1/"
    } else {
        "reports/v5/conformance/shared_results/v1/"
    };
    if !freeze_ref_path.starts_with(expected_freeze_root) {
        gaps.push("blind output freeze is outside the verified shared-results root".to_owned());
        if let Some(map) = evidence.as_object_mut() {
            map.insert("blind_freeze_verified".to_owned(), Value::Bool(false));
        }
        return Ok((errors, gaps, evidence));
    }
    if let (Some(input_artifacts), Some(outer_corpus)) = (
        freeze_value
            .get("input_artifacts")
            .and_then(Value::as_object),
        receipt.get("corpus").and_then(Value::as_object),
    ) {
        if input_artifacts.get("manifest") != outer_corpus.get("manifest") {
            errors.push(
                "full-conformance freeze manifest ref differs from outer corpus binding".to_owned(),
            );
        }
    }
    if freeze_value.get("public_inputs")
        != receipt.get("corpus").and_then(|v| v.get("public_cases"))
    {
        errors
            .push("full-conformance public input ref differs from outer corpus binding".to_owned());
    }
    let verified_freeze = ib_v5_conformance::verify_blind_freeze_receipt(
        &Path::new(freeze_ref_path),
        freeze_ref_sha,
        root,
    );
    match verified_freeze {
        Ok(verified) => {
            if let Some(map) = evidence.as_object_mut() {
                map.insert("blind_freeze_verified".to_owned(), Value::Bool(true));
            }
            if let Err(error) = verified.assert_current() {
                errors.push(format!(
                    "verified blind freeze changed after capture: {error}"
                ));
            }
            if verified.proof_scope != "full_shared_conformance" {
                gaps.push("verified blind freeze is not full_shared_conformance".to_owned());
            }
            let expected_contract = if freeze_schema == SHARED_FREEZE_V4 {
                Some(V5_APPROVED_CONFORMANCE_CONTRACT_ID)
            } else {
                None
            };
            if verified.normative_contract_id.as_deref() != expected_contract {
                errors.push(
                    "verified freeze token does not match its versioned normative contract"
                        .to_owned(),
                );
            }
            let expected_inventory: Vec<(String, String)> = corpus_data
                .cases
                .iter()
                .map(|case| (case.case_id().to_owned(), case.input_sha256.clone()))
                .collect();
            if verified.case_inventory != expected_inventory {
                errors.push(
                    "verified blind freeze inventory differs from the exact public case order"
                        .to_owned(),
                );
            }
            if verified.case_manifest_sha256 != corpus_data.case_manifest_sha256 {
                errors.push(
                    "verified blind freeze case-manifest digest differs from reconstructed input"
                        .to_owned(),
                );
            }
            if verified.case_index_sha256 != corpus_data.case_index_sha256 {
                errors.push(
                    "verified blind freeze case-index digest differs from reconstructed input"
                        .to_owned(),
                );
            }
            if let Some(scope) = scope.and_then(Value::as_object)
                && scope.get("case_index_sha256").and_then(Value::as_str)
                    != Some(corpus_data.case_index_sha256.as_str())
            {
                errors.push(
                    "blind freeze scope case-index digest differs from reconstructed input"
                        .to_owned(),
                );
            }
        }
        Err(error) => {
            let message = error.to_string();
            if message.contains("unsupported blind freeze receipt schema")
                || message.contains("full_shared_conformance")
            {
                gaps.push(format!(
                    "full-conformance freeze verifier is not yet available: {message}"
                ));
            } else if message.contains("diagnostic") || message.contains("360 phase") {
                gaps.push(format!(
                    "diagnostic freeze cannot establish G-SPEC-01: {message}"
                ));
            } else {
                gaps.push(format!(
                    "full-conformance freeze failed verification: {message}"
                ));
            }
            if let Some(map) = evidence.as_object_mut() {
                map.insert("blind_freeze_verified".to_owned(), Value::Bool(false));
            }
            return Ok((errors, gaps, evidence));
        }
    }

    // The lower-level transport crate validates evaluator-specific source/runtime closures,
    // raw stdout/stderr bindings, audits and raw result shape. Require all three streams here.
    let evaluators = receipt.get("evaluators").and_then(Value::as_object);
    let Some(evaluators) = evaluators else {
        gaps.push(
            "shared receipt must bind full-spec, agent-view, and production streams".to_owned(),
        );
        return Ok((errors, gaps, evidence));
    };
    let actual_evaluators: BTreeSet<&str> = evaluators.keys().map(String::as_str).collect();
    let expected_evaluators: BTreeSet<&str> = SHARED_EVALUATORS.into_iter().collect();
    if actual_evaluators != expected_evaluators {
        gaps.push(
            "shared receipt must bind full-spec, agent-view, and production streams".to_owned(),
        );
        return Ok((errors, gaps, evidence));
    }

    let evidence = validate_shared_evaluator_entries(
        evaluators,
        &freeze_value,
        &corpus_data,
        root,
        &freeze_raw,
        &mut errors,
        &mut gaps,
        evidence,
    );

    let _ = (path, freeze_path, freeze_ref_sha);
    Ok((errors, gaps, evidence))
}

fn validate_shared_evaluator_entries(
    evaluators: &Map<String, Value>,
    freeze_value: &Map<String, Value>,
    corpus: &SharedCorpus,
    root: &Path,
    freeze_raw: &[u8],
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
    mut evidence: Value,
) -> Value {
    use base64::Engine as _;
    use ib_v5_conformance::process::{
        resolve_source_profile, validate_audit_receipt, verify_capture_reference,
        verify_dependency_archive_reference, verify_evaluator_audits, verify_evaluator_receipts,
        verify_full_shared_v3_run_manifest, verify_opaque_metadata_reference,
        verify_runtime_archive_reference, verify_runtime_reference,
    };
    use ib_v5_conformance::shared::{
        SCORE_METRIC_NULLABILITY_CONTRACT_ID, compare_production_result, compare_result_pair,
        verify_complete_coverage,
    };
    use ib_v5_schemas::normative_contract::{
        V5_APPROVED_ADDENDUM_PATH, V5_APPROVED_DIRECTIVE_PATH, V5_APPROVED_FULL_SPEC_PROFILE,
    };

    let mut stream_results: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    let mut closure_hashes = BTreeMap::<String, String>::new();
    let mut source_origin_provenance = Map::<String, Value>::new();
    let freeze_sha256 = sha256_bytes(freeze_raw);

    for evaluator_id in SHARED_EVALUATORS {
        let Some(entry) = evaluators.get(evaluator_id).and_then(Value::as_object) else {
            gaps.push(format!(
                "shared evaluator descriptor is unavailable: {evaluator_id}"
            ));
            continue;
        };
        let (missing, extra) = exact_fields(entry, &SHARED_EVALUATOR_FIELDS);
        if !missing.is_empty() {
            gaps.push(format!(
                "{evaluator_id} descriptor omits fields {missing:?}"
            ));
        }
        if !extra.is_empty() {
            errors.push(format!(
                "{evaluator_id} descriptor has unexpected fields {extra:?}"
            ));
        }
        if !missing.is_empty() || !extra.is_empty() {
            continue;
        }

        let profile_slot = match evaluator_id {
            "full_spec_blind" => "full_spec",
            "agent_view_only" => "agent_view",
            "production" => "production",
            _ => unreachable!("fixed evaluator IDs"),
        };
        if entry.get("wire_evaluator_id").and_then(Value::as_str) != Some(evaluator_id) {
            errors.push(format!("{evaluator_id} wire evaluator ID is incorrect"));
        }
        let (Some(source_profile), Some(author_origin), Some(closure_sha)) = (
            entry.get("source_profile").and_then(Value::as_str),
            entry.get("author_origin").and_then(Value::as_str),
            entry.get("source_closure_sha256").and_then(Value::as_str),
        ) else {
            errors.push(format!(
                "{evaluator_id} source/runtime identity is malformed"
            ));
            continue;
        };
        if source_profile.is_empty() || author_origin.is_empty() || !is_sha256(closure_sha) {
            errors.push(format!(
                "{evaluator_id} source/runtime identity is malformed"
            ));
            continue;
        }
        let profile = match resolve_source_profile(profile_slot, Some(source_profile)) {
            Ok(value) => value,
            Err(error) => {
                errors.push(format!(
                    "{evaluator_id} source profile is not registered: {error}"
                ));
                continue;
            }
        };

        let freeze_schema = freeze_value.get("schema_version").and_then(Value::as_str);
        if freeze_schema == Some(SHARED_FREEZE_V4) {
            if evaluator_id == "full_spec_blind" && source_profile != V5_APPROVED_FULL_SPEC_PROFILE
            {
                errors.push(
                    "current v4 freeze does not use the approved full-spec source profile"
                        .to_owned(),
                );
            }
            if evaluator_id == "production" && source_profile != "production_v4" {
                errors.push(
                    "current v4 freeze does not use the production_v4 source profile".to_owned(),
                );
            }
            if evaluator_id == "agent_view_only"
                && profile.source_files.iter().any(|file| {
                    file == V5_APPROVED_ADDENDUM_PATH || file == V5_APPROVED_DIRECTIVE_PATH
                })
            {
                errors.push("agent-view closure includes a normative-only document".to_owned());
            }
        } else if freeze_schema == Some(SHARED_FREEZE_V3)
            && evaluator_id == "full_spec_blind"
            && source_profile == V5_APPROVED_FULL_SPEC_PROFILE
        {
            errors.push("approved addendum profile requires a v4 freeze receipt".to_owned());
        }

        let reference_names = [
            "source_capture_manifest",
            "runtime_manifest",
            "runtime_archive",
            "dependency_manifest",
            "run_manifest",
        ];
        let mut loaded = BTreeMap::<String, (Vec<u8>, PathBuf, Map<String, Value>)>::new();
        for label in reference_names {
            let Some((payload, file_path)) = shared_bound_bytes(
                root,
                entry.get(label),
                &format!("{evaluator_id} {label}"),
                errors,
                gaps,
                false,
                false,
            ) else {
                continue;
            };
            if let Some(value) =
                shared_json_object(&payload, &format!("{evaluator_id} {label}"), errors, gaps)
            {
                loaded.insert(label.to_owned(), (payload, file_path, value));
            }
        }
        if loaded.len() != reference_names.len() {
            continue;
        }
        let (capture_raw, _, capture) = loaded.get("source_capture_manifest").expect("loaded");
        let (_, _, runtime) = loaded.get("runtime_manifest").expect("loaded");
        let (_, _, runtime_archive) = loaded.get("runtime_archive").expect("loaded");
        let (_, _, dependency) = loaded.get("dependency_manifest").expect("loaded");
        let (_, _, run_manifest) = loaded.get("run_manifest").expect("loaded");

        for (label, manifest) in [
            ("source capture", capture),
            ("runtime", runtime),
            ("runtime archive", runtime_archive),
            ("dependency", dependency),
        ] {
            let manifest_id = manifest
                .get("evaluator_id")
                .or_else(|| manifest.get("evaluator_slot"))
                .and_then(Value::as_str);
            if manifest_id != Some(profile_slot) {
                errors.push(format!("{evaluator_id} {label} identity is inconsistent"));
            }
            if manifest.get("source_profile").and_then(Value::as_str) != Some(source_profile) {
                errors.push(format!(
                    "{evaluator_id} {label} source_profile is inconsistent"
                ));
            }
            if manifest.get("author_origin").and_then(Value::as_str) != Some(author_origin) {
                errors.push(format!(
                    "{evaluator_id} {label} author_origin is inconsistent"
                ));
            }
        }

        let capture_logical_sha = capture.get("manifest_sha256").and_then(Value::as_str);
        let capture_unsigned = {
            let mut value = capture.clone();
            value.remove("manifest_sha256");
            Value::Object(value)
        };
        let capture_logical_valid = capture_logical_sha.is_some_and(is_sha256)
            && canonical_json_bytes(&capture_unsigned)
                .ok()
                .is_some_and(|raw| sha256_bytes(&raw) == capture_logical_sha.unwrap_or_default());
        let capture_raw_sha_valid = entry
            .get("source_capture_manifest")
            .and_then(Value::as_object)
            .and_then(|reference| reference.get("sha256"))
            .and_then(Value::as_str)
            .is_some_and(|expected| sha256_bytes(capture_raw) == expected);
        if !capture_logical_valid || !capture_raw_sha_valid {
            errors.push(format!(
                "{evaluator_id} source capture manifest digest is invalid"
            ));
        }
        if runtime.get("source_closure_sha256").and_then(Value::as_str) != Some(closure_sha) {
            errors.push(format!(
                "{evaluator_id} runtime source closure SHA differs from receipt"
            ));
        }
        if runtime_archive
            .get("source_closure_sha256")
            .and_then(Value::as_str)
            != Some(closure_sha)
        {
            errors.push(format!(
                "{evaluator_id} runtime archive source closure SHA differs from receipt"
            ));
        }
        if dependency
            .get("source_closure_sha256")
            .and_then(Value::as_str)
            != Some(closure_sha)
        {
            errors.push(format!(
                "{evaluator_id} dependency archive source closure SHA differs from receipt"
            ));
        }
        let archive_digest = runtime_archive
            .get("archive_manifest_sha256")
            .and_then(Value::as_str);
        let mut archive_unsigned = runtime_archive.clone();
        archive_unsigned.remove("archive_manifest_sha256");
        if !archive_digest.is_some_and(is_sha256)
            || canonical_json_bytes(&Value::Object(archive_unsigned))
                .ok()
                .is_none_or(|raw| sha256_bytes(&raw) != archive_digest.unwrap_or_default())
        {
            errors.push(format!(
                "{evaluator_id} runtime archive internal digest is invalid"
            ));
        }
        let dependency_digest = dependency.get("manifest_sha256").and_then(Value::as_str);
        let mut dependency_unsigned = dependency.clone();
        dependency_unsigned.remove("manifest_sha256");
        if !dependency_digest.is_some_and(is_sha256)
            || canonical_json_bytes(&Value::Object(dependency_unsigned))
                .ok()
                .is_none_or(|raw| sha256_bytes(&raw) != dependency_digest.unwrap_or_default())
        {
            errors.push(format!(
                "{evaluator_id} dependency archive internal digest is invalid"
            ));
        }
        if runtime_archive.get("runtime_inventory") != runtime.get("runtime_inventory") {
            errors.push(format!(
                "{evaluator_id} runtime archive inventory differs from runtime manifest"
            ));
        }
        if !runtime_archive_inventory_matches(
            evaluator_id,
            runtime.get("approved_source_files"),
            runtime.get("runtime_inventory"),
            runtime_archive.get("source_files"),
        ) {
            errors.push(format!(
                "{evaluator_id} runtime archive source files differ from runtime manifest"
            ));
        }
        for field in ["dependency_snapshot", "python_runtime"] {
            if runtime_archive.get(field) != runtime.get(field) {
                errors.push(format!(
                    "{evaluator_id} runtime archive {field} differs from runtime manifest"
                ));
            }
        }
        if dependency.get("dependency_files") != runtime.get("dependency_files") {
            errors.push(format!(
                "{evaluator_id} dependency archive file list differs from runtime manifest"
            ));
        }
        if dependency.get("dependency_snapshot") != runtime.get("dependency_snapshot") {
            errors.push(format!(
                "{evaluator_id} dependency snapshot differs from runtime manifest"
            ));
        }
        if capture.get("source_files") != runtime.get("approved_source_files") {
            errors.push(format!(
                "{evaluator_id} source capture files differ from runtime manifest"
            ));
        }
        if run_manifest.get("protocol").and_then(Value::as_str)
            != Some("identitybench.v5.shared-conformance.v1")
        {
            errors.push(format!("{evaluator_id} run manifest protocol is incorrect"));
        }
        if run_manifest
            .get("materialization_contract_id")
            .and_then(Value::as_str)
            != Some("identitybench.v5.materialization-contract.v2")
        {
            errors.push(format!(
                "{evaluator_id} run manifest materialization contract is incorrect"
            ));
        }
        if run_manifest
            .get("score_metric_nullability_contract_id")
            .and_then(Value::as_str)
            != Some(SCORE_METRIC_NULLABILITY_CONTRACT_ID)
        {
            gaps.push(format!(
                "{evaluator_id} run manifest lacks the current score-metric contract"
            ));
        }
        let mut expected_run_cases: Vec<Value> = corpus
            .cases
            .iter()
            .map(|case| json!({"case_id":case.case_id(),"input_sha256":case.input_sha256}))
            .collect();
        expected_run_cases.sort_by(|left, right| {
            left.get("case_id")
                .and_then(Value::as_str)
                .cmp(&right.get("case_id").and_then(Value::as_str))
        });
        if run_manifest.get("cases") != Some(&Value::Array(expected_run_cases)) {
            errors.push(format!(
                "{evaluator_id} run manifest case inventory differs"
            ));
        }
        if run_manifest
            .get("family_map_sha256")
            .and_then(Value::as_str)
            != Some(&corpus.family_map_sha256)
        {
            errors.push(format!(
                "{evaluator_id} run manifest family map digest differs"
            ));
        }
        if run_manifest.get("coverage_sha256").and_then(Value::as_str)
            != Some(&corpus.coverage_sha256)
        {
            errors.push(format!(
                "{evaluator_id} run manifest coverage digest differs"
            ));
        }
        let mut expected_closures = Vec::new();
        let frozen_evaluators = freeze_value.get("evaluators").and_then(Value::as_object);
        for (slot, wire_id) in [
            ("full_spec", "full_spec_blind"),
            ("agent_view", "agent_view_only"),
        ] {
            let closure = frozen_evaluators
                .and_then(|values| values.get(slot))
                .and_then(Value::as_object)
                .and_then(|value| value.get("source_closure_sha256"))
                .cloned()
                .unwrap_or(Value::Null);
            expected_closures.push(json!({
                "evaluator_slot": slot,
                "evaluator_id": wire_id,
                "source_closure_sha256": closure,
            }));
        }
        expected_closures.sort_by(|left, right| {
            left.get("evaluator_slot")
                .and_then(Value::as_str)
                .cmp(&right.get("evaluator_slot").and_then(Value::as_str))
        });
        if run_manifest.get("closures") != Some(&Value::Array(expected_closures)) {
            errors.push(format!(
                "{evaluator_id} run manifest evaluator closures differ from frozen inputs"
            ));
        }
        let adapter_digests: BTreeSet<&str> = [
            runtime
                .get("approved_source_files")
                .and_then(Value::as_array),
            runtime.get("runtime_inventory").and_then(Value::as_array),
        ]
        .into_iter()
        .flatten()
        .flat_map(|rows| rows.iter())
        .filter_map(|row| row.as_object())
        .filter(|row| {
            row.get("path").and_then(Value::as_str)
                == Some("src/identitybench/v5/conformance_shared.py")
        })
        .filter_map(|row| row.get("sha256").and_then(Value::as_str))
        .collect();
        if adapter_digests.len() != 1
            || !adapter_digests.contains(
                run_manifest
                    .get("adapter_sha256")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
            )
        {
            errors.push(format!(
                "{evaluator_id} run adapter digest is not bound to runtime closure"
            ));
        }
        if runtime.get("case_count").and_then(Value::as_i64) != Some(1702) {
            errors.push(format!(
                "{evaluator_id} runtime closure case count is not 1702"
            ));
        }

        let capture_reference = entry.get("source_capture_manifest");
        let runtime_reference = entry.get("runtime_manifest");
        let runtime_archive_reference = entry.get("runtime_archive");
        let dependency_reference = entry.get("dependency_manifest");
        let run_reference = entry.get("run_manifest");
        let raw_receipts_reference = entry.get("raw_receipts");
        let raw_audits_reference = entry.get("raw_audits");
        let inventory: Vec<(String, String)> = corpus
            .cases
            .iter()
            .map(|case| (case.case_id().to_owned(), case.input_sha256.clone()))
            .collect();
        for check in [
            verify_capture_reference(
                root,
                capture_reference.unwrap_or(&Value::Null),
                profile_slot,
                Some(&profile),
            )
            .map(|_| ()),
            verify_runtime_reference(
                root,
                runtime_reference.unwrap_or(&Value::Null),
                profile_slot,
                closure_sha,
                capture_reference.unwrap_or(&Value::Null),
                corpus.cases.len(),
                Some(&profile),
                true,
            )
            .map(|_| ()),
            verify_runtime_archive_reference(
                root,
                runtime_archive_reference.unwrap_or(&Value::Null),
                profile_slot,
                runtime,
                closure_sha,
                &corpus.case_manifest_sha256,
            )
            .map(|_| ()),
            verify_dependency_archive_reference(
                root,
                dependency_reference.unwrap_or(&Value::Null),
                profile_slot,
                runtime,
                &profile,
            )
            .map(|_| ()),
        ] {
            if let Err(error) = check {
                errors.push(format!(
                    "{evaluator_id} source/runtime verification failed: {error}"
                ));
            }
        }
        let run_manifest_check = match (
            raw_receipts_reference.and_then(Value::as_object),
            raw_audits_reference.and_then(Value::as_object),
        ) {
            (Some(receipts), Some(audits)) => verify_full_shared_v3_run_manifest(
                root,
                run_reference.unwrap_or(&Value::Null),
                profile_slot,
                closure_sha,
                &corpus.case_manifest_sha256,
                receipts,
                audits,
                corpus.cases.len(),
            ),
            _ => Err(ib_v5_conformance::process::SharedRuntimeError(
                "raw stream reference is malformed".to_owned(),
            )),
        };
        if let Err(error) = run_manifest_check {
            errors.push(format!(
                "{evaluator_id} run manifest verification failed: {error}"
            ));
        }
        if let Err(error) = verify_opaque_metadata_reference(
            root,
            entry.get("qualification").unwrap_or(&Value::Null),
            &format!("qualification:{evaluator_id}"),
        ) {
            errors.push(format!(
                "{evaluator_id} qualification reference failed verification: {error}"
            ));
        }

        let capture_path = entry
            .get("source_capture_manifest")
            .and_then(Value::as_object)
            .and_then(|value| value.get("path"))
            .and_then(Value::as_str);
        let capture_raw_sha = entry
            .get("source_capture_manifest")
            .and_then(Value::as_object)
            .and_then(|value| value.get("sha256"))
            .and_then(Value::as_str)
            .unwrap_or_default();
        let mut provenance = Map::new();
        let qualified = crate::shared_origin::validate_shared_qualification(
            evaluator_id,
            entry.get("qualification").unwrap_or(&Value::Null),
            source_profile,
            author_origin,
            closure_sha,
            capture_raw_sha,
            capture_logical_sha.unwrap_or_default(),
            capture_path,
            capture
                .get("source_files")
                .and_then(Value::as_array)
                .map(Vec::len),
            runtime,
            root,
            errors,
            gaps,
            &mut provenance,
        );
        if !provenance.is_empty() {
            source_origin_provenance.insert(evaluator_id.to_owned(), Value::Object(provenance));
        }

        if evaluator_id != "production" {
            let slot = if evaluator_id == "full_spec_blind" {
                "full_spec"
            } else {
                "agent_view"
            };
            let frozen_item = freeze_value
                .get("evaluators")
                .and_then(Value::as_object)
                .and_then(|items| items.get(slot))
                .and_then(Value::as_object);
            let Some(frozen_item) = frozen_item else {
                errors.push(format!("full-conformance freeze omits {slot} descriptor"));
                continue;
            };
            for field in [
                "source_profile",
                "author_origin",
                "source_closure_sha256",
                "source_capture_manifest",
                "runtime_manifest",
                "runtime_archive",
                "dependency_manifest",
                "raw_receipts",
                "raw_audits",
            ] {
                if entry.get(field) != frozen_item.get(field) {
                    errors.push(format!(
                        "{evaluator_id} descriptor differs from immutable blind freeze"
                    ));
                }
            }
        } else if !qualified {
            gaps.push(
                "production source/runtime readiness is not independently qualified".to_owned(),
            );
        }

        let receipt_ref = entry.get("raw_receipts");
        let audit_ref = entry.get("raw_audits");
        let receipt_file = shared_bound_bytes(
            root,
            receipt_ref,
            &format!("{evaluator_id} raw result stream"),
            errors,
            gaps,
            true,
            false,
        );
        let audit_file = shared_bound_bytes(
            root,
            audit_ref,
            &format!("{evaluator_id} raw audit stream"),
            errors,
            gaps,
            true,
            false,
        );
        let (Some((receipt_raw, _)), Some((audit_raw, _))) = (receipt_file, audit_file) else {
            continue;
        };
        let Some(receipt_count) = receipt_ref
            .and_then(Value::as_object)
            .and_then(|value| value.get("row_count"))
            .and_then(Value::as_i64)
        else {
            errors.push(format!("{evaluator_id} raw result row_count is invalid"));
            continue;
        };
        let Some(audit_count) = audit_ref
            .and_then(Value::as_object)
            .and_then(|value| value.get("row_count"))
            .and_then(Value::as_i64)
        else {
            errors.push(format!("{evaluator_id} raw audit row_count is invalid"));
            continue;
        };
        let Some(result_rows) = shared_jsonl_rows(
            &receipt_raw,
            &format!("{evaluator_id} raw result stream"),
            receipt_count,
            errors,
            gaps,
        ) else {
            continue;
        };
        let Some(audit_rows) = shared_jsonl_rows(
            &audit_raw,
            &format!("{evaluator_id} raw audit stream"),
            audit_count,
            errors,
            gaps,
        ) else {
            continue;
        };
        if receipt_count != corpus.cases.len() as i64 {
            gaps.push(format!(
                "{evaluator_id} result stream does not cover all 1702 cases"
            ));
            continue;
        }
        if audit_count != corpus.cases.len() as i64 {
            gaps.push(format!(
                "{evaluator_id} audit stream does not cover all 1702 cases"
            ));
            continue;
        }
        if let Err(error) = verify_evaluator_receipts(
            &result_rows,
            profile_slot,
            closure_sha,
            &inventory,
            &corpus.case_manifest_sha256,
            evaluator_id,
            "full_shared_conformance",
            &profile,
            true,
        ) {
            errors.push(format!(
                "{evaluator_id} raw result stream failed binding: {error}"
            ));
            continue;
        }
        let mut normalized_audits = Vec::with_capacity(audit_rows.len());
        let mut audit_failed = false;
        for row in &audit_rows {
            if evaluator_id == "production" {
                let Some(map) = row.as_object() else {
                    normalized_audits.push(row.clone());
                    continue;
                };
                let expected: BTreeSet<&str> = [
                    "audit",
                    "case_id",
                    "execution_status",
                    "input_sha256",
                    "raw_stderr_b64",
                    "raw_stdout_b64",
                    "source_closure_sha256",
                    "stderr_sha256",
                    "stdout_sha256",
                ]
                .into_iter()
                .collect();
                let actual: BTreeSet<&str> = map.keys().map(String::as_str).collect();
                let mut normalized = map.clone();
                if actual.len() == expected.len() + 1
                    && actual.contains("failure")
                    && expected.is_subset(&actual)
                {
                    if map.get("failure") != Some(&Value::Null) {
                        errors.push(
                            "production audit failure companion field is not null".to_owned(),
                        );
                        audit_failed = true;
                    }
                    normalized.remove("failure");
                }
                normalized_audits.push(Value::Object(normalized));
            } else {
                normalized_audits.push(row.clone());
            }
        }
        if audit_failed {
            continue;
        }
        if let Err(error) = verify_evaluator_audits(&normalized_audits, &result_rows, profile_slot)
        {
            errors.push(format!(
                "{evaluator_id} audit stream failed binding: {error}"
            ));
            continue;
        }

        let mut values = Vec::with_capacity(result_rows.len());
        let mut mandatory_incomplete = false;
        for (index, ((row, audit_row), case)) in result_rows
            .iter()
            .zip(&audit_rows)
            .zip(&corpus.cases)
            .enumerate()
        {
            let (Some(row), Some(audit_obj)) = (row.as_object(), audit_row.as_object()) else {
                errors.push(format!(
                    "{evaluator_id} stream row {index} is not an object"
                ));
                continue;
            };
            if row.get("case_id").and_then(Value::as_str) != Some(case.case_id())
                || audit_obj.get("case_id").and_then(Value::as_str) != Some(case.case_id())
            {
                errors.push(format!(
                    "{evaluator_id} stream case order differs at row {index}"
                ));
                continue;
            }
            if row.get("input_sha256").and_then(Value::as_str) != Some(&case.input_sha256)
                || audit_obj.get("input_sha256").and_then(Value::as_str) != Some(&case.input_sha256)
            {
                errors.push(format!(
                    "{evaluator_id} stream input hash differs at row {index}"
                ));
                continue;
            }
            if row.get("evaluator_id").and_then(Value::as_str) != Some(profile_slot) {
                errors.push(format!(
                    "{evaluator_id} stream outer profile identity differs at row {index}"
                ));
                continue;
            }
            if row.get("source_closure_sha256").and_then(Value::as_str) != Some(closure_sha)
                || audit_obj
                    .get("source_closure_sha256")
                    .and_then(Value::as_str)
                    != Some(closure_sha)
            {
                errors.push(format!(
                    "{evaluator_id} stream source closure differs at row {index}"
                ));
                continue;
            }
            if row.get("case_manifest_sha256").and_then(Value::as_str)
                != Some(&corpus.case_manifest_sha256)
            {
                errors.push(format!(
                    "{evaluator_id} stream case manifest differs at row {index}"
                ));
                continue;
            }
            if evaluator_id == "production"
                && (row
                    .get("blind_freeze_receipt_sha256")
                    .and_then(Value::as_str)
                    != Some(&freeze_sha256)
                    || row.get("blind_freeze_scope").and_then(Value::as_str)
                        != Some("full_shared_conformance"))
            {
                errors.push(
                    "production stream is not bound to the frozen blind result prefix".to_owned(),
                );
                continue;
            }
            let execution_status = row.get("execution_status").and_then(Value::as_str);
            if execution_status != Some("ok") {
                if execution_status == Some("failed") {
                    mandatory_incomplete = true;
                } else {
                    errors.push(format!(
                        "{evaluator_id} stream has an invalid execution status"
                    ));
                }
                continue;
            }
            let mut expected_fields: BTreeSet<&str> = [
                "protocol",
                "evaluator_id",
                "source_profile",
                "author_origin",
                "case_id",
                "input_sha256",
                "source_closure_sha256",
                "case_manifest_sha256",
                "execution_status",
                "result",
                "audit",
                "result_sha256",
                "stdout_sha256",
                "stderr_sha256",
                "raw_stdout_b64",
                "raw_stderr_b64",
                "exit_code",
            ]
            .into_iter()
            .collect();
            if evaluator_id == "production" {
                expected_fields.insert("blind_freeze_receipt_sha256");
                expected_fields.insert("blind_freeze_scope");
            }
            let actual_fields: BTreeSet<&str> = row.keys().map(String::as_str).collect();
            if actual_fields != expected_fields {
                errors.push(format!(
                    "{evaluator_id} successful stream row fields differ"
                ));
                continue;
            }
            if row.get("protocol").and_then(Value::as_str)
                != Some("identitybench.v5.shared-conformance.v1")
            {
                errors.push(format!("{evaluator_id} stream protocol differs"));
                continue;
            }
            if row.get("source_profile").and_then(Value::as_str) != Some(source_profile)
                || row.get("author_origin").and_then(Value::as_str) != Some(author_origin)
            {
                errors.push(format!(
                    "{evaluator_id} stream profile differs from source capture"
                ));
                continue;
            }
            let Some(exit_code) = row.get("exit_code").and_then(Value::as_i64) else {
                errors.push(format!(
                    "{evaluator_id} stream row exit_code is not an integer"
                ));
                continue;
            };
            if exit_code != 0 {
                mandatory_incomplete = true;
                continue;
            }
            let decode_stream = |field: &str| -> Result<(Vec<u8>, String), String> {
                let text = row
                    .get(field)
                    .and_then(Value::as_str)
                    .ok_or_else(|| format!("{field} is missing"))?;
                let bytes = base64::engine::general_purpose::STANDARD
                    .decode(text)
                    .map_err(|_| format!("{field} is not valid base64"))?;
                if base64::engine::general_purpose::STANDARD.encode(&bytes) != text {
                    return Err(format!("{field} is not canonical base64"));
                }
                Ok((bytes, text.to_owned()))
            };
            let decoded = (|| -> Result<Value, String> {
                let (stdout, _) = decode_stream("raw_stdout_b64")?;
                let (stderr, _) = decode_stream("raw_stderr_b64")?;
                if sha256_bytes(&stdout)
                    != row
                        .get("stdout_sha256")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                {
                    return Err("raw stdout digest mismatch".to_owned());
                }
                if sha256_bytes(&stderr)
                    != row
                        .get("stderr_sha256")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                {
                    return Err("raw stderr digest mismatch".to_owned());
                }
                if sha256_bytes(&stdout)
                    != row
                        .get("result_sha256")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                {
                    return Err("result digest differs from raw stdout".to_owned());
                }
                let raw_result = strict_json_loads(&stdout).map_err(|error| error.to_string())?;
                let raw_audit = strict_json_loads(&stderr).map_err(|error| error.to_string())?;
                if row.get("result") != Some(&raw_result) || row.get("audit") != Some(&raw_audit) {
                    return Err("parsed result/audit differs from raw streams".to_owned());
                }
                if canonical_json_bytes(&raw_result).map_err(|error| error.to_string())? != stdout
                    || canonical_json_bytes(&raw_audit).map_err(|error| error.to_string())?
                        != stderr
                {
                    return Err("raw result/audit bytes are not canonical".to_owned());
                }
                let Some(raw_audit_obj) = raw_audit.as_object() else {
                    return Err("audit is not a JSON object".to_owned());
                };
                validate_audit_receipt(raw_audit_obj).map_err(|error| error.to_string())?;
                ib_v5_conformance::shared::validate_evaluator_result(
                    &raw_result,
                    case,
                    profile_slot,
                )
                .map_err(|error| error.to_string())?;
                Ok(raw_result)
            })();
            match decoded {
                Ok(value) => {
                    if value.get("status").and_then(Value::as_str) != Some("ok") {
                        mandatory_incomplete = true;
                    }
                    values.push(value);
                }
                Err(error) => errors.push(format!(
                    "{evaluator_id} stream row {index} failed strict result validation: {error}"
                )),
            }
        }
        if mandatory_incomplete {
            gaps.push(format!(
                "{evaluator_id} has failed or non-ok mandatory case results"
            ));
        }
        if !errors.is_empty() || !gaps.is_empty() || values.len() != corpus.cases.len() {
            if values.len() != corpus.cases.len() && errors.is_empty() && gaps.is_empty() {
                gaps.push(format!("{evaluator_id} result stream is incomplete"));
            }
            continue;
        }
        stream_results.insert(evaluator_id.to_owned(), values);
        closure_hashes.insert(evaluator_id.to_owned(), closure_sha.to_owned());
    }

    if let Some(map) = evidence.as_object_mut() {
        map.insert(
            "verified_evaluator_streams".to_owned(),
            Value::Array(stream_results.keys().cloned().map(Value::String).collect()),
        );
        map.insert(
            "source_origin_provenance".to_owned(),
            Value::Object(source_origin_provenance),
        );
    }
    let expected_evaluator_ids: BTreeSet<&str> = SHARED_EVALUATORS.into_iter().collect();
    if stream_results
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>()
        != expected_evaluator_ids
    {
        return evidence;
    }
    if closure_hashes
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>()
        != expected_evaluator_ids
    {
        gaps.push("one or more evaluator source/runtime closures are unverified".to_owned());
        return evidence;
    }
    if !errors.is_empty() || !gaps.is_empty() {
        return evidence;
    }

    let mut pair_closures = BTreeMap::new();
    pair_closures.insert(
        "full_spec".to_owned(),
        closure_hashes["full_spec_blind"].clone(),
    );
    pair_closures.insert(
        "agent_view".to_owned(),
        closure_hashes["agent_view_only"].clone(),
    );
    let mut all_closures = pair_closures.clone();
    all_closures.insert(
        "production".to_owned(),
        closure_hashes["production"].clone(),
    );
    let full_stream = &stream_results["full_spec_blind"];
    let agent_stream = &stream_results["agent_view_only"];
    let production_stream = &stream_results["production"];
    let mut paired_results: Vec<(Value, Value)> = Vec::new();
    let mut incomplete = BTreeSet::new();
    let mut production_mismatches = BTreeSet::new();
    for (index, case) in corpus.cases.iter().enumerate() {
        let full = &full_stream[index];
        let agent = &agent_stream[index];
        let production = &production_stream[index];
        let materials = [
            full.get("materialization"),
            agent.get("materialization"),
            production.get("materialization"),
        ];
        let materials_equal = match materials {
            [Some(full), Some(agent), Some(prod)] => {
                let enc_full = canonical_json_bytes(full);
                let enc_agent = canonical_json_bytes(agent);
                let enc_prod = canonical_json_bytes(prod);
                matches!((enc_full, enc_agent, enc_prod), (Ok(a), Ok(b), Ok(c)) if a == b && b == c)
            }
            _ => false,
        };
        if !materials_equal {
            errors.push(format!(
                "{}: evaluator materializations differ",
                case.case_id()
            ));
            continue;
        }
        let (left, right, pair) = match compare_result_pair(case, full, agent, &pair_closures) {
            Ok(value) => value,
            Err(error) => {
                errors.push(format!(
                    "{}: reference result comparison failed: {error}",
                    case.case_id()
                ));
                continue;
            }
        };
        if pair.status == "INCOMPLETE" {
            incomplete.insert(case.case_id().to_owned());
            continue;
        }
        if pair.status != "MATCH" {
            errors.push(format!(
                "{}: independent reference judgments differ",
                case.case_id()
            ));
            continue;
        }
        let (_, production_pair) =
            match compare_production_result(case, (&left, &right), production, &all_closures) {
                Ok(value) => value,
                Err(error) => {
                    errors.push(format!(
                        "{}: production comparison failed: {error}",
                        case.case_id()
                    ));
                    continue;
                }
            };
        if production_pair.status == "INCOMPLETE" {
            incomplete.insert(case.case_id().to_owned());
            continue;
        }
        if production_pair.status != "MATCH" {
            production_mismatches.insert(case.case_id().to_owned());
            continue;
        }
        paired_results.push((full.clone(), agent.clone()));
    }
    if !incomplete.is_empty() {
        gaps.push(format!(
            "mandatory evaluator outcomes are incomplete for {} cases",
            incomplete.len()
        ));
    }
    if !production_mismatches.is_empty() {
        errors.push(format!(
            "production judgments differ from the frozen independent pair for {} cases",
            production_mismatches.len()
        ));
    }
    if let Some(map) = evidence.as_object_mut() {
        map.insert(
            "matched_blind_pairs".to_owned(),
            json!(paired_results.len()),
        );
        map.insert("incomplete_case_count".to_owned(), json!(incomplete.len()));
        map.insert(
            "production_mismatch_count".to_owned(),
            json!(production_mismatches.len()),
        );
    }
    if !errors.is_empty() || !gaps.is_empty() {
        return evidence;
    }
    match verify_complete_coverage(
        &corpus.cases,
        &paired_results,
        &corpus.coverage,
        &corpus.family_map,
    ) {
        Ok(observed) => {
            if let Some(map) = evidence.as_object_mut() {
                map.insert("matched_case_count".to_owned(), json!(paired_results.len()));
                map.insert("observed_coverage".to_owned(), observed);
                map.insert("production_matches".to_owned(), json!(corpus.cases.len()));
            }
        }
        Err(error) => gaps.push(format!("recomputed shared coverage is incomplete: {error}")),
    }
    evidence
}

fn runtime_archive_inventory_matches(
    evaluator_id: &str,
    approved_source_files: Option<&Value>,
    runtime_inventory: Option<&Value>,
    archive_source_files: Option<&Value>,
) -> bool {
    let (Some(approved), Some(inventory), Some(archive)) = (
        approved_source_files.and_then(Value::as_array),
        runtime_inventory.and_then(Value::as_array),
        archive_source_files.and_then(Value::as_array),
    ) else {
        return false;
    };
    let mut expected = BTreeMap::<String, Value>::new();
    let mut origins = BTreeMap::<String, Vec<&str>>::new();
    for (origin, rows) in [
        ("approved_source_files", approved),
        ("runtime_inventory", inventory),
    ] {
        for row in rows {
            let Some(map) = row.as_object() else {
                return false;
            };
            let (Some(path), Some(digest)) = (
                map.get("path").and_then(Value::as_str),
                map.get("sha256").and_then(Value::as_str),
            ) else {
                return false;
            };
            if path.is_empty() || !is_sha256(digest) {
                return false;
            }
            let record = row.clone();
            if let Some(existing) = expected.get(path) {
                if evaluator_id != "production"
                    || path != "src/identitybench/v5/conformance_shared.py"
                    || origins.get(path).map(Vec::as_slice)
                        != Some(["approved_source_files"].as_slice())
                    || origin != "runtime_inventory"
                    || existing != &record
                {
                    return false;
                }
                origins.entry(path.to_owned()).or_default().push(origin);
            } else {
                expected.insert(path.to_owned(), record);
                origins.insert(path.to_owned(), vec![origin]);
            }
        }
    }
    let mut actual = BTreeMap::<String, Value>::new();
    for row in archive {
        let Some(map) = row.as_object() else {
            return false;
        };
        let (Some(path), Some(digest)) = (
            map.get("path").and_then(Value::as_str),
            map.get("sha256").and_then(Value::as_str),
        ) else {
            return false;
        };
        if path.is_empty()
            || !is_sha256(digest)
            || actual.insert(path.to_owned(), row.clone()).is_some()
        {
            return false;
        }
    }
    actual == expected
}

fn validate_shared_corpus(
    corpus_files: &BTreeMap<String, (Vec<u8>, PathBuf)>,
    datasets_by_suite: &BTreeMap<String, DatasetArtifactsV5>,
    root: &Path,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Result<Option<SharedCorpus>, String> {
    let manifest_raw = corpus_files
        .get("manifest")
        .map(|v| v.0.as_slice())
        .unwrap_or_default();
    let manifest = shared_json_object(manifest_raw, "shared corpus manifest", errors, gaps);
    let Some(mut manifest) = manifest else {
        return Ok(None);
    };
    let corpus_version = manifest
        .get("corpus_version")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    if !matches!(corpus_version.as_str(), "v2" | "v3") {
        gaps.push("shared corpus manifest uses an unsupported explicit corpus_version".to_owned());
        return Ok(None);
    }
    if corpus_version == "v3" {
        let manifest_path = corpus_files
            .get("manifest")
            .map(|v| v.1.as_path())
            .unwrap_or(root);
        let Some(adapted) = crate::shared_manifest_v3::adapt_v3_manifest(
            &manifest,
            manifest_path,
            root,
            corpus_files,
            errors,
            gaps,
        ) else {
            return Ok(None);
        };
        manifest = adapted;
    }
    if corpus_version == "v2"
        && [
            "counts",
            "case_index",
            "stimulus_sources",
            "phase_checkpoint",
        ]
        .iter()
        .any(|x| manifest.contains_key(*x))
    {
        errors.push("v2 shared corpus contains contradictory v3 fields".to_owned());
    }
    if manifest.get("protocol").and_then(Value::as_str) != Some(SHARED_PROTOCOL) {
        errors.push("shared corpus protocol differs from the frozen transport protocol".to_owned());
    }
    if manifest.get("all_case_count").and_then(Value::as_i64) != Some(1702) {
        gaps.push("shared corpus does not declare the complete 1702-case inventory".to_owned());
    }
    let manifest_index = manifest.get("all_case_index").and_then(Value::as_array);
    if manifest_index.is_none_or(|rows| rows.len() != 1702) {
        gaps.push("shared corpus manifest has no complete ordered case index".to_owned());
    }
    if datasets_by_suite
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>()
        != BTreeSet::from(["v5_dev", "v5_lite", "v5_core", "v5_full"])
    {
        gaps.push("all four exact sealed suites are required to bind the shared corpus".to_owned());
        return Ok(None);
    }
    let episode_hashes: BTreeMap<String, String> = datasets_by_suite
        .iter()
        .map(|(suite, data)| (suite.clone(), sha256_bytes(&data.episodes_bytes)))
        .collect();
    let Some(normative) = manifest.get("normative_inputs").and_then(Value::as_object) else {
        gaps.push("shared corpus manifest lacks normative input bindings".to_owned());
        return Ok(None);
    };
    if normative.get("spec_sha256").and_then(Value::as_str) != Some(V5_BASE_SPEC_SHA256) {
        errors.push("shared corpus manifest SPEC-v5.md binding is stale".to_owned());
    }
    let family_map_raw = corpus_files
        .get("family_map")
        .map(|v| v.0.as_slice())
        .unwrap_or_default();
    let (family_map, family_map_sha) =
        match ib_v5_conformance::load_family_map(family_map_raw, &episode_hashes) {
            Ok(value) => value,
            Err(error) => {
                errors.push(format!(
                    "shared corpus family map failed verification: {error}"
                ));
                return Ok(None);
            }
        };
    if normative.get("family_map_sha256").and_then(Value::as_str) != Some(&family_map_sha) {
        errors.push("shared corpus family-map SHA differs from the normative manifest".to_owned());
    }
    if manifest
        .get("family_metadata")
        .and_then(Value::as_object)
        .and_then(|metadata| metadata.get("sha256"))
        .is_some_and(|digest| digest.as_str() != Some(family_map_sha.as_str()))
    {
        errors.push("shared corpus family metadata SHA differs from its bound map".to_owned());
    }
    if manifest.get("all_case_count").and_then(Value::as_i64) != Some(1702)
        || manifest_index.is_none()
    {
        return Ok(None);
    }
    let phase_meta = manifest
        .get("phase_input_artifact")
        .and_then(Value::as_object);
    let stimulus_meta = manifest
        .get("stimulus_artifacts")
        .and_then(Value::as_object);
    let (Some(phase_meta), Some(stimulus_meta)) = (phase_meta, stimulus_meta) else {
        errors.push(
            "shared corpus does not bind phase, recipe, and trace source artifacts".to_owned(),
        );
        return Ok(None);
    };
    let sources = [
        ("phase_inputs", Some(phase_meta), 360_i64),
        (
            "recipes",
            stimulus_meta.get("recipes").and_then(Value::as_object),
            1282_i64,
        ),
        (
            "traces",
            stimulus_meta.get("traces").and_then(Value::as_object),
            60_i64,
        ),
    ];
    let mut source_rows = BTreeMap::new();
    for (label, metadata, expected_count) in sources {
        let Some(metadata) = metadata else {
            errors.push(format!("shared corpus source metadata is missing: {label}"));
            continue;
        };
        let raw = corpus_files
            .get(label)
            .map(|v| v.0.as_slice())
            .unwrap_or_default();
        if metadata.get("sha256").and_then(Value::as_str) != Some(&sha256_bytes(raw)) {
            errors.push(format!(
                "shared corpus source digest differs from manifest: {label}"
            ));
        }
        let Some(count) = metadata.get("row_count").and_then(Value::as_i64) else {
            errors.push(format!(
                "shared corpus source row_count is invalid: {label}"
            ));
            continue;
        };
        if count < 0 {
            errors.push(format!(
                "shared corpus source row_count is invalid: {label}"
            ));
            continue;
        }
        if let Some(rows) = shared_jsonl_rows(raw, label, count, errors, gaps) {
            if count != expected_count {
                gaps.push(format!(
                    "shared corpus {label} has {count} rows; {expected_count} are required"
                ));
            }
            source_rows.insert(label, rows);
        }
    }
    if source_rows.len() != 3 {
        return Ok(None);
    }
    let mut phase_by_id = BTreeMap::<String, Value>::new();
    let mut phase_by_identity = BTreeMap::<(String, String, i64), Value>::new();
    let mut cases_by_id = BTreeMap::<String, FrozenCase>::new();
    let mut phase_identities = BTreeSet::new();
    let episode_total: usize = datasets_by_suite
        .values()
        .map(|dataset| dataset.episodes.len())
        .sum();
    let episodes: BTreeMap<(String, String), &ib_v5_schemas::EpisodeV5> = datasets_by_suite
        .iter()
        .flat_map(|(suite, data)| {
            data.episodes
                .iter()
                .map(move |episode| ((suite.clone(), episode.episode_id.clone()), episode))
        })
        .collect();
    if episodes.len() != episode_total {
        errors.push("sealed suites contain duplicate episode identities".to_owned());
    }
    let oracle_total: usize = datasets_by_suite
        .values()
        .map(|dataset| dataset.oracles.len())
        .sum();
    let oracles: BTreeMap<(String, String), &ib_v5_schemas::SealedEpisodeOracleV5> =
        datasets_by_suite
            .iter()
            .flat_map(|(suite, data)| {
                data.oracles
                    .iter()
                    .map(move |oracle| ((suite.clone(), oracle.episode_id.clone()), oracle))
            })
            .collect();
    if oracles.len() != oracle_total {
        errors.push("sealed suites contain duplicate oracle identities".to_owned());
    }
    if episodes.len() != 180 {
        gaps.push("sealed corpus does not contain the complete 180-episode suite set".to_owned());
    }
    for (suite, dataset) in datasets_by_suite {
        for (episode, oracle) in dataset.episodes.iter().zip(&dataset.oracles) {
            errors.extend(
                validate_obs_witness(episode, oracle)
                    .into_iter()
                    .map(|problem| format!("{suite}/{}: {problem}", episode.episode_id)),
            );
        }
    }
    // Phase 2 projection validation needs the delivered phase 1 view even when source
    // records are not ordered phase-1-first. The Python validator resolves that carryover
    // from the complete source list, so seed the lookup from all valid phase-1 rows first.
    for row in &source_rows["phase_inputs"] {
        if let Ok(frozen) = ib_v5_conformance::freeze_case(row)
            && frozen.public_case.phase_index == 1
        {
            let identity = (
                frozen.public_case.suite_id.clone(),
                frozen.public_case.episode_id().to_owned(),
                1_i64,
            );
            phase_by_identity.insert(identity, frozen.public_case.to_json_value());
        }
    }
    for row in source_rows["phase_inputs"].iter() {
        let frozen = match ib_v5_conformance::freeze_case(row) {
            Ok(value) => value,
            Err(error) => {
                errors.push(format!("phase input case is invalid: {error}"));
                continue;
            }
        };
        let case = &frozen.public_case;
        if case.operation != "derive_phase" {
            errors.push(format!("phase input {} is not derive_phase", case.case_id));
        }
        if cases_by_id.contains_key(&case.case_id) {
            errors.push(format!("duplicate shared phase case ID: {}", case.case_id));
            continue;
        }
        let identity = (
            case.suite_id.clone(),
            case.episode_id().to_owned(),
            case.phase_index,
        );
        if !phase_identities.insert(identity.clone()) {
            errors.push(format!("duplicate suite/episode/phase input: {identity:?}"));
            continue;
        }
        let Some(episode) = episodes
            .get(&(identity.0.clone(), identity.1.clone()))
            .copied()
        else {
            errors.push(format!(
                "phase input references an unbound sealed episode: {identity:?}"
            ));
            continue;
        };
        if !oracles.contains_key(&(identity.0.clone(), identity.1.clone())) {
            errors.push(format!(
                "phase input references an unbound sealed episode: {identity:?}"
            ));
            continue;
        }
        validate_phase_projection(case, episode, &identity, &phase_by_identity, errors);
        phase_by_id.insert(case.case_id.clone(), case.to_json_value());
        phase_by_identity.insert(identity, case.to_json_value());
        cases_by_id.insert(case.case_id.clone(), frozen);
    }
    let expected_phase_ids: BTreeSet<(String, String, i64)> = datasets_by_suite
        .iter()
        .flat_map(|(suite, data)| {
            data.episodes.iter().flat_map(move |episode| {
                [1_i64, 2]
                    .into_iter()
                    .map(move |phase| (suite.clone(), episode.episode_id.clone(), phase))
            })
        })
        .collect();
    if phase_identities != expected_phase_ids {
        gaps.push("phase inputs are not a complete ordered suite/episode/phase set".to_owned());
    }
    for label in ["recipes", "traces"] {
        for record in &source_rows[label] {
            let Some(base_id) = record.get("input_ref").and_then(Value::as_str) else {
                errors.push(format!(
                    "shared {label} input is invalid: recipe references a missing phase input"
                ));
                continue;
            };
            let Some(base_case) = phase_by_id.get(base_id) else {
                errors.push(format!(
                    "shared {label} input is invalid: recipe references a missing phase input"
                ));
                continue;
            };
            match ib_v5_conformance::serialize_recipe_case(base_case, record) {
                Ok(frozen) => {
                    if cases_by_id.contains_key(frozen.case_id()) {
                        errors.push(format!("duplicate shared case ID: {}", frozen.case_id()));
                    } else {
                        cases_by_id.insert(frozen.case_id().to_owned(), frozen);
                    }
                }
                Err(error) => errors.push(format!("shared {label} input is invalid: {error}")),
            }
        }
    }
    if corpus_version == "v3" {
        let mut source_records = BTreeMap::<String, (String, usize, Value)>::new();
        for source_label in ["recipes", "traces"] {
            for (record_index, record) in source_rows[source_label].iter().enumerate() {
                let Some(record_id) = record.get("recipe_id").and_then(Value::as_str) else {
                    errors.push(format!(
                        "v3 stimulus record identity is missing or duplicated: {source_label}"
                    ));
                    continue;
                };
                if source_records
                    .insert(
                        record_id.to_owned(),
                        (source_label.to_owned(), record_index, record.clone()),
                    )
                    .is_some()
                {
                    errors.push(format!(
                        "v3 stimulus record identity is missing or duplicated: {source_label}"
                    ));
                }
            }
        }
        if let Some(metadata_rows) = manifest
            .get("_v3_case_index_metadata")
            .and_then(Value::as_array)
        {
            for (index, metadata) in metadata_rows.iter().enumerate() {
                let Some(metadata) = metadata.as_object() else {
                    continue;
                };
                let Some(case_id) = metadata.get("case_id").and_then(Value::as_str) else {
                    continue;
                };
                let Some(frozen) = cases_by_id.get(case_id) else {
                    errors.push(format!(
                        "v3 case_index references a missing reconstructed case: {case_id}"
                    ));
                    continue;
                };
                let public = &frozen.public_case;
                let operation = metadata
                    .get("operation")
                    .and_then(Value::as_str)
                    .unwrap_or("");
                let source_matches = if operation == "derive_phase" {
                    metadata.get("source_file").and_then(Value::as_str)
                        == Some("stimuli/phase_inputs.jsonl")
                } else if let Some((source_label, record_index, record)) =
                    source_records.get(case_id)
                {
                    let expected_source_file = format!("stimuli/{source_label}.jsonl");
                    operation
                        == record
                            .get("operation")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                        && metadata.get("input_ref") == record.get("input_ref")
                        && metadata.get("stimulus_record_id") == record.get("recipe_id")
                        && metadata
                            .get("stimulus_record_index")
                            .and_then(Value::as_u64)
                            == Some(*record_index as u64)
                        && metadata.get("stimulus_source_file").and_then(Value::as_str)
                            == Some(expected_source_file.as_str())
                        && source_label
                            == if operation == "score_trace" {
                                "traces"
                            } else {
                                "recipes"
                            }
                } else {
                    errors.push(format!(
                        "v3 case_index references a missing stimulus record: {case_id}"
                    ));
                    false
                };
                if !source_matches
                    || metadata.get("suite_id").and_then(Value::as_str)
                        != Some(public.suite_id.as_str())
                    || metadata.get("episode_id").and_then(Value::as_str)
                        != Some(public.episode_id())
                    || metadata.get("phase_index").and_then(Value::as_i64)
                        != Some(public.phase_index)
                {
                    errors.push(format!(
                        "v3 case_index source metadata differs from reconstructed row {index}"
                    ));
                }
            }
        }
    }
    let public_rows = corpus_files
        .get("public_cases")
        .map(|v| v.0.as_slice())
        .unwrap_or_default();
    let Some(public_rows) = shared_jsonl_rows(
        public_rows,
        "complete public case inventory",
        1702,
        errors,
        gaps,
    ) else {
        return Ok(None);
    };
    let public_cases: Vec<_> = public_rows
        .iter()
        .map(ib_v5_conformance::freeze_case)
        .collect::<Result<_, _>>()
        .map_err(|e| format!("full public case inventory is malformed: {e}"))?;
    if cases_by_id.len() != 1702 || public_cases.len() != 1702 {
        gaps.push(
            "reconstructed case inventory is incomplete; all 1702 cases are required".to_owned(),
        );
        return Ok(None);
    }
    let mut seen_ids = BTreeSet::new();
    for public in &public_cases {
        if !seen_ids.insert(public.case_id().to_owned()) {
            errors.push("full public case inventory contains duplicate case IDs".to_owned());
            continue;
        }
        match cases_by_id.get(public.case_id()) {
            None => errors.push(format!(
                "public case is absent from raw phase/recipe/trace inputs: {}",
                public.case_id()
            )),
            Some(expected) if public.canonical_bytes != expected.canonical_bytes => {
                errors.push(format!(
                    "public case differs from its raw phase/recipe/trace source: {}",
                    public.case_id()
                ))
            }
            _ => {}
        }
    }
    let manifest_index = manifest_index.expect("length checked above");
    for (index, (metadata, frozen)) in manifest_index.iter().zip(&public_cases).enumerate() {
        let Some(metadata) = metadata.as_object() else {
            errors.push(format!("manifest case index row {index} is malformed"));
            continue;
        };
        let case_id = metadata.get("case_id").and_then(Value::as_str);
        let digest = metadata
            .get("canonical_case_sha256")
            .and_then(Value::as_str);
        let encoded_len = metadata.get("canonical_case_bytes").and_then(Value::as_i64);
        if case_id != Some(frozen.case_id())
            || digest != Some(frozen.input_sha256.as_str())
            || encoded_len != i64::try_from(frozen.canonical_bytes.len()).ok()
        {
            errors.push(format!(
                "manifest case index order/hash differs at row {index}"
            ));
        }
        let episode_id = frozen.public_case.episode_id();
        if metadata.get("suite_id").and_then(Value::as_str)
            != Some(frozen.public_case.suite_id.as_str())
            || metadata.get("episode_id").and_then(Value::as_str) != Some(episode_id)
            || metadata.get("phase_index").and_then(Value::as_i64)
                != Some(frozen.public_case.phase_index)
            || metadata.get("operation").and_then(Value::as_str)
                != Some(frozen.public_case.operation.as_str())
        {
            errors.push(format!(
                "manifest case index identity differs at row {index}"
            ));
        }
    }
    if corpus_version == "v3" {
        let Some(metadata_rows) = manifest
            .get("_v3_case_index_metadata")
            .and_then(Value::as_array)
        else {
            errors.push(
                "v3 manifest case metadata does not cover the reconstructed inventory".to_owned(),
            );
            return Ok(None);
        };
        if metadata_rows.len() != public_cases.len() {
            errors.push(
                "v3 manifest case metadata does not cover the reconstructed inventory".to_owned(),
            );
        } else {
            for (index, (metadata, frozen)) in metadata_rows.iter().zip(&public_cases).enumerate() {
                let Some(metadata) = metadata.as_object() else {
                    errors.push(format!("v3 case_index metadata row {index} is malformed"));
                    continue;
                };
                let public = &frozen.public_case;
                if metadata.get("case_id").and_then(Value::as_str) != Some(frozen.case_id())
                    || metadata
                        .get("canonical_case_sha256")
                        .and_then(Value::as_str)
                        != Some(frozen.input_sha256.as_str())
                    || metadata.get("canonical_case_bytes").and_then(Value::as_i64)
                        != i64::try_from(frozen.canonical_bytes.len()).ok()
                    || metadata.get("operation").and_then(Value::as_str)
                        != Some(public.operation.as_str())
                    || metadata.get("suite_id").and_then(Value::as_str)
                        != Some(public.suite_id.as_str())
                    || metadata.get("episode_id").and_then(Value::as_str)
                        != Some(public.episode_id())
                    || metadata.get("phase_index").and_then(Value::as_i64)
                        != Some(public.phase_index)
                {
                    errors.push(format!(
                        "v3 case_index metadata differs from reconstructed row {index}"
                    ));
                }
            }
        }
        let Some(declared_counts) = manifest.get("counts").and_then(Value::as_object) else {
            errors.push("v3 shared manifest counts are incomplete".to_owned());
            return Ok(None);
        };
        let Some(declared_operations) = declared_counts.get("operations") else {
            errors.push("v3 shared manifest operation counts are incomplete".to_owned());
            return Ok(None);
        };
        let observed_operations = json!({
            "derive_phase": public_cases.iter().filter(|case| case.public_case.operation == "derive_phase").count(),
            "safety": public_cases.iter().filter(|case| case.public_case.operation == "safety").count(),
            "score_trace": public_cases.iter().filter(|case| case.public_case.operation == "score_trace").count(),
            "validate_submission": public_cases.iter().filter(|case| case.public_case.operation == "validate_submission").count(),
        });
        if declared_operations != &observed_operations {
            errors.push(
                "v3 operation counts differ from the reconstructed ordered corpus".to_owned(),
            );
        }
    }
    let index_raw = corpus_files
        .get("case_index")
        .map(|v| v.0.as_slice())
        .unwrap_or_default();
    let Some(index_rows) =
        shared_jsonl_rows(index_raw, "ordered public case index", 1702, errors, gaps)
    else {
        return Ok(None);
    };
    let canonical_index: Vec<Value> = public_cases
        .iter()
        .map(|case| json!({"case_id":case.case_id(),"input_sha256":case.input_sha256}))
        .collect();
    if index_rows != canonical_index {
        errors
            .push("public case index order differs from the canonical full case JSONL".to_owned());
    }
    if BTreeSet::from_iter(
        public_cases
            .iter()
            .map(|case| case.public_case.operation.as_str()),
    )
    .len()
        != 4
    {
        gaps.push("full corpus does not exercise all four shared protocol operations".to_owned());
    }
    let case_suites: BTreeSet<&str> = public_cases
        .iter()
        .map(|case| case.public_case.suite_id.as_str())
        .collect();
    let bound_suites: BTreeSet<&str> = datasets_by_suite.keys().map(String::as_str).collect();
    if case_suites != bound_suites {
        errors.push(
            "full case corpus suite IDs differ from the four sealed suite bindings".to_owned(),
        );
    }
    let Some(coverage_obj) = corpus_files
        .get("coverage")
        .and_then(|row| shared_json_object(&row.0, "coverage requirement", errors, gaps))
    else {
        return Ok(None);
    };
    let Some(declared_coverage) = parse_coverage_requirement(&coverage_obj, errors) else {
        return Ok(None);
    };
    let expected_coverage =
        expected_coverage(&public_cases, datasets_by_suite, &family_map, errors, gaps)?;
    if declared_coverage != expected_coverage {
        errors.push(
            "declared coverage requirement differs from recomputed corpus coverage".to_owned(),
        );
    }
    if corpus_version == "v3" {
        let Some(target_rows) = manifest
            .get("_v3_phase_target_index")
            .and_then(Value::as_array)
        else {
            errors.push("v3 phase target index is unavailable".to_owned());
            return Ok(None);
        };
        let expected_targets = expected_coverage.phase_targets.clone();
        let mut observed_targets = BTreeSet::new();
        let phase_cases: BTreeMap<(String, String, i64), &FrozenCase> = public_cases
            .iter()
            .filter(|case| case.public_case.operation == "derive_phase")
            .map(|case| {
                (
                    (
                        case.public_case.suite_id.clone(),
                        case.public_case.episode_id().to_owned(),
                        case.public_case.phase_index,
                    ),
                    case,
                )
            })
            .collect();
        for row in target_rows {
            let Some(row) = row.as_object() else { continue };
            let (
                Some(suite),
                Some(episode),
                Some(phase),
                Some(kind),
                Some(target),
                Some(queue_field),
            ) = (
                row.get("suite_id").and_then(Value::as_str),
                row.get("episode_id").and_then(Value::as_str),
                row.get("phase_index").and_then(Value::as_i64),
                row.get("mutation_kind").and_then(Value::as_str),
                row.get("target_entity_id").and_then(Value::as_str),
                row.get("queue_field").and_then(Value::as_str),
            )
            else {
                continue;
            };
            observed_targets.insert((
                suite.to_owned(),
                episode.to_owned(),
                phase,
                kind.to_owned(),
                target.to_owned(),
            ));
            let case = phase_cases.get(&(suite.to_owned(), episode.to_owned(), phase));
            let manifest_field = if phase == 1 {
                "phase1_manifest"
            } else {
                "phase2_base_manifest"
            };
            let queue_value = case
                .and_then(|case| case.public_case.public_episode.get(manifest_field))
                .and_then(Value::as_object)
                .and_then(|manifest| manifest.get("work_queue"))
                .and_then(Value::as_object)
                .and_then(|queue| queue.get(queue_field))
                .and_then(Value::as_array);
            if !queue_value
                .is_some_and(|queue| queue.iter().any(|value| value.as_str() == Some(target)))
            {
                errors.push(
                    "v3 phase target queue field differs from the public work queue".to_owned(),
                );
            }
        }
        if observed_targets != expected_targets {
            errors
                .push("v3 phase target index differs from sealed gold target coverage".to_owned());
        }
    }
    let coverage_sha256 = sha256_bytes(
        &canonical_json_bytes(&ib_v5_conformance::shared::coverage_requirement_json(
            &expected_coverage,
        ))
        .map_err(|e| e.to_string())?,
    );
    let case_manifest_sha256 = shared_case_manifest_sha(&public_cases)?;
    let case_index_sha256 = shared_case_index_sha(&public_cases)?;
    let _ = manifest_index;
    Ok(Some(SharedCorpus {
        cases: public_cases,
        family_map,
        family_map_sha256: family_map_sha,
        coverage: expected_coverage,
        case_manifest_sha256,
        case_index_sha256,
        coverage_sha256,
    }))
}

fn validate_phase_projection(
    case: &ib_v5_conformance::shared::PublicCase,
    episode: &ib_v5_schemas::EpisodeV5,
    identity: &(String, String, i64),
    prior_phase_inputs: &BTreeMap<(String, String, i64), Value>,
    errors: &mut Vec<String>,
) {
    if case.schema_sha256 != ib_v5_conformance::shared::PINNED_PUBLIC_SCHEMA_SHA256 {
        errors.push(format!(
            "phase input schema source is stale: {}",
            case.case_id
        ));
    }
    if case.rendered_card != episode.policy_card.rendered_rules_markdown {
        errors.push(format!(
            "phase input rendered card differs from sealed public card: {identity:?}"
        ));
    }
    if !case.stimulus.is_empty() {
        errors.push(format!(
            "derive_phase input contains an unexpected stimulus: {identity:?}"
        ));
    }
    let (manifest, slices, q1_key, q2_key) = match case.phase_index {
        1 => (
            &episode.phase1_manifest,
            &episode.phase1_slices,
            "q1_slice_ids_p1",
            "q2_slice_ids_p1",
        ),
        2 => (
            &episode.phase2_base_manifest,
            &episode.phase2_slices,
            "q1_slice_ids_p2",
            "q2_slice_ids_p2",
        ),
        _ => {
            errors.push(format!(
                "phase input has an unsupported phase index: {identity:?}"
            ));
            return;
        }
    };
    let q1_values = case.public_episode.get(q1_key).and_then(Value::as_array);
    let q2_values = case.public_episode.get(q2_key).and_then(Value::as_array);
    let (Some(q1_values), Some(q2_values)) = (q1_values, q2_values) else {
        errors.push(format!("phase input query IDs are malformed: {identity:?}"));
        return;
    };
    let q1_ids: Option<Vec<String>> = q1_values
        .iter()
        .map(|value| value.as_str().map(str::to_owned))
        .collect();
    let q2_ids: Option<Vec<String>> = q2_values
        .iter()
        .map(|value| value.as_str().map(str::to_owned))
        .collect();
    let (Some(q1_ids), Some(q2_ids)) = (q1_ids, q2_ids) else {
        errors.push(format!("phase input query IDs are malformed: {identity:?}"));
        return;
    };
    let expected_q1 =
        match ib_v5_baselines::obs_strategy_q1(manifest, &episode.policy_card, case.phase_index) {
            Ok(value) => value,
            Err(error) => {
                errors.push(format!(
                    "phase input Q1 selection could not be recomputed: {error}"
                ));
                return;
            }
        };
    let available_q1 = expected_q1
        .iter()
        .filter(|id| slices.contains_key(*id))
        .count();
    if available_q1 != expected_q1.len() {
        errors.push(format!(
            "phase input Q1 refers to an unavailable sealed slice: {identity:?}"
        ));
        return;
    }
    let q1_slices: Vec<_> = expected_q1
        .iter()
        .filter_map(|id| slices.get(id).cloned())
        .collect();
    let expected_q2 = match ib_v5_baselines::obs_strategy_q2(
        manifest,
        &q1_slices,
        &manifest.work_queue,
        &episode.policy_card,
        case.phase_index,
    ) {
        Ok(value) => value,
        Err(error) => {
            errors.push(format!(
                "phase input Q2 selection could not be recomputed: {error}"
            ));
            return;
        }
    };
    if q1_ids != expected_q1 || q2_ids != expected_q2 {
        errors.push(format!(
            "phase input Q1/Q2 selection differs from the sealed public view: {identity:?}"
        ));
    }

    let mut selected_ids: BTreeSet<String> = q1_ids.iter().cloned().collect();
    selected_ids.extend(q2_ids.iter().cloned());
    let mut projected_slices = Map::new();
    for id in selected_ids {
        match slices
            .get(&id)
            .and_then(|slice| serde_json::to_value(slice).ok())
        {
            Some(value) => {
                projected_slices.insert(id, value);
            }
            None => errors.push(format!(
                "phase input selects an unavailable sealed slice: {identity:?}"
            )),
        }
    }
    let mut expected_episode = Map::new();
    expected_episode.insert(
        "episode_id".to_owned(),
        Value::String(episode.episode_id.clone()),
    );
    let Ok(policy_card) = serde_json::to_value(&episode.policy_card) else {
        errors.push(format!(
            "sealed policy card could not be serialized: {identity:?}"
        ));
        return;
    };
    let Ok(phase1_manifest) = serde_json::to_value(&episode.phase1_manifest) else {
        errors.push(format!(
            "sealed phase 1 manifest could not be serialized: {identity:?}"
        ));
        return;
    };
    expected_episode.insert("policy_card".to_owned(), policy_card);
    expected_episode.insert("phase1_manifest".to_owned(), phase1_manifest);
    if case.phase_index == 1 {
        expected_episode.insert("phase1_slices".to_owned(), Value::Object(projected_slices));
        expected_episode.insert("q1_slice_ids_p1".to_owned(), json!(q1_ids));
        expected_episode.insert("q2_slice_ids_p1".to_owned(), json!(q2_ids));
    } else {
        let prior_key = (identity.0.clone(), identity.1.clone(), 1_i64);
        let Some(prior_case) = prior_phase_inputs.get(&prior_key) else {
            errors.push(format!(
                "phase 2 lacks its public phase 1 carryover input: {identity:?}"
            ));
            return;
        };
        let Some(prior_episode) = prior_case.get("public_episode").and_then(Value::as_object)
        else {
            errors.push(format!(
                "phase 2 has malformed public phase 1 carryover: {identity:?}"
            ));
            return;
        };
        for field in ["phase1_slices", "q1_slice_ids_p1", "q2_slice_ids_p1"] {
            let Some(value) = prior_episode.get(field) else {
                errors.push(format!(
                    "phase 2 lacks public phase 1 carryover field {field}"
                ));
                return;
            };
            expected_episode.insert(field.to_owned(), value.clone());
        }
        let Ok(phase2_manifest) = serde_json::to_value(&episode.phase2_base_manifest) else {
            errors.push(format!(
                "sealed phase 2 manifest could not be serialized: {identity:?}"
            ));
            return;
        };
        expected_episode.insert("phase2_base_manifest".to_owned(), phase2_manifest);
        expected_episode.insert("phase2_slices".to_owned(), Value::Object(projected_slices));
        expected_episode.insert("q1_slice_ids_p2".to_owned(), json!(q1_ids));
        expected_episode.insert("q2_slice_ids_p2".to_owned(), json!(q2_ids));
    }
    if Value::Object(expected_episode) != Value::Object(case.public_episode.clone()) {
        errors.push(format!(
            "phase input public projection differs from its sealed source: {identity:?}"
        ));
    }
}

fn validate_obs_witness(
    episode: &ib_v5_schemas::EpisodeV5,
    oracle: &ib_v5_schemas::SealedEpisodeOracleV5,
) -> Vec<String> {
    use ib_v5_schemas::AuthorityTierV5;

    let mut errors = Vec::new();
    for (phase_index, manifest, payloads, sealed_req, sealed_q1, sealed_q2) in [
        (
            1_i64,
            &episode.phase1_manifest,
            &episode.phase1_slices,
            &oracle.req_slices_p1,
            &oracle.obs_q1_slices_p1,
            &oracle.obs_q2_slices_p1,
        ),
        (
            2_i64,
            &episode.phase2_base_manifest,
            &episode.phase2_slices,
            &oracle.req_slices_p2,
            &oracle.obs_q1_slices_p2,
            &oracle.obs_q2_slices_p2,
        ),
    ] {
        let q1 = match ib_v5_baselines::obs_strategy_q1(manifest, &episode.policy_card, phase_index)
        {
            Ok(value) => value,
            Err(error) => {
                errors.push(format!("phase {phase_index} Q1 selection failed: {error}"));
                continue;
            }
        };
        let q1_payload: Vec<_> = q1
            .iter()
            .filter_map(|slice_id| payloads.get(slice_id).cloned())
            .collect();
        if q1_payload.len() != q1.len() {
            errors.push(format!(
                "phase {phase_index} public Q1 refers to absent slices"
            ));
            continue;
        }
        let q2 = match ib_v5_baselines::obs_strategy_q2(
            manifest,
            &q1_payload,
            &manifest.work_queue,
            &episode.policy_card,
            phase_index,
        ) {
            Ok(value) => value,
            Err(error) => {
                errors.push(format!("phase {phase_index} Q2 selection failed: {error}"));
                continue;
            }
        };
        let observed: BTreeSet<String> = q1.iter().chain(&q2).cloned().collect();
        let sealed: BTreeSet<String> = sealed_req.iter().cloned().collect();
        if observed != sealed {
            errors.push(format!(
                "phase {phase_index} public Q1/Q2 does not equal sealed REQ_SLICES"
            ));
        }
        if q1 != *sealed_q1 || q2 != *sealed_q2 {
            errors.push(format!(
                "phase {phase_index} public Q1/Q2 differs from sealed witness"
            ));
        }
        let costs: BTreeMap<&str, i64> = manifest
            .source_directory
            .iter()
            .map(|descriptor| (descriptor.slice_id.as_str(), descriptor.credit_cost))
            .collect();
        let observed_cost: i64 = observed
            .iter()
            .filter_map(|slice_id| costs.get(slice_id.as_str()))
            .sum();
        let all_partial: Vec<&ib_v5_schemas::SliceDescriptorV5> = manifest
            .source_directory
            .iter()
            .filter(|descriptor| {
                !descriptor.is_complete_for_scope
                    && matches!(
                        descriptor.authority_tier,
                        AuthorityTierV5::AuthoritativePrimary | AuthorityTierV5::SecondaryReplica
                    )
                    && (0..=episode.policy_card.overlay.freshness_sla_s)
                        .contains(&(manifest.clock_epoch_s - descriptor.watermark_s))
            })
            .collect();
        let dump_all_cost: i64 = q1
            .iter()
            .chain(all_partial.iter().map(|descriptor| &descriptor.slice_id))
            .filter_map(|slice_id| costs.get(slice_id.as_str()))
            .sum();
        if observed_cost > manifest.query_budget {
            errors.push(format!(
                "phase {phase_index} observed Q1/Q2 exceeds query budget"
            ));
        }
        if dump_all_cost <= manifest.query_budget {
            errors.push(format!(
                "phase {phase_index} dump-all query is not over budget"
            ));
        }
        let selected_q2: BTreeSet<&str> = q2.iter().map(String::as_str).collect();
        let core_tables = core_tables_for_phase(phase_index);
        let mut sibling_groups = BTreeMap::<(String, i64, i64, i64), BTreeSet<String>>::new();
        for descriptor in &manifest.source_directory {
            let authority = matches!(
                descriptor.authority_tier,
                AuthorityTierV5::AuthoritativePrimary | AuthorityTierV5::SecondaryReplica
            );
            let fresh = (0..=episode.policy_card.overlay.freshness_sla_s)
                .contains(&(manifest.clock_epoch_s - descriptor.watermark_s));
            let has_core_table = descriptor
                .table_names
                .iter()
                .any(|name| core_tables.contains(name.as_str()));
            if !descriptor.is_complete_for_scope
                && authority
                && fresh
                && has_core_table
                && !selected_q2.contains(descriptor.slice_id.as_str())
            {
                let tier = serde_json::to_value(descriptor.authority_tier)
                    .ok()
                    .and_then(|value| value.as_str().map(str::to_owned))
                    .unwrap_or_default();
                sibling_groups
                    .entry((
                        tier,
                        descriptor.watermark_s,
                        descriptor.row_count,
                        descriptor.credit_cost,
                    ))
                    .or_default()
                    .insert(descriptor.scope_filter.clone());
            }
        }
        if !sibling_groups.values().any(|filters| filters.len() >= 2) {
            errors.push(format!(
                "phase {phase_index} lacks metadata-identical unreferenced Q2 siblings"
            ));
        }
    }
    errors
}

fn core_tables_for_phase(phase_index: i64) -> BTreeSet<&'static str> {
    if phase_index == 1 {
        [
            "hr_persons",
            "roles",
            "grants",
            "groups",
            "idp_accounts",
            "connector_schemas",
            "hr_source_mappings",
            "baseline_nhis",
            "target_nhis",
            "baseline_agents",
            "enterprise_agents",
            "cloud_ciem_tasks",
            "optimization_tasks",
            "jml_workflow_tasks",
        ]
        .into_iter()
        .collect()
    } else {
        [
            "hr_persons",
            "cae_incidents",
            "uar_campaigns",
            "historical_sessions",
            "jit_requests",
            "agent_delegation_requests",
            "optimization_tasks",
        ]
        .into_iter()
        .collect()
    }
}

pub(crate) fn shared_case_manifest_sha(cases: &[FrozenCase]) -> Result<String, String> {
    let case_rows: Vec<Value> = cases
        .iter()
        .map(|case| {
            json!({
                "case_id": case.case_id(),
                "input_sha256": case.input_sha256,
            })
        })
        .collect();
    let manifest = json!({
        "protocol": SHARED_PROTOCOL,
        "canonicalization": "canonical-json-v1",
        "cases": case_rows,
    });
    let raw = canonical_json_bytes(&manifest).map_err(|error| error.to_string())?;
    Ok(sha256_bytes(&raw))
}

pub(crate) fn shared_case_index_sha(cases: &[FrozenCase]) -> Result<String, String> {
    let index: Vec<Value> = cases
        .iter()
        .map(|case| {
            json!({
                "case_id": case.case_id(),
                "input_sha256": case.input_sha256,
            })
        })
        .collect();
    let raw = canonical_json_bytes(&Value::Array(index)).map_err(|error| error.to_string())?;
    Ok(sha256_bytes(&raw))
}

pub(crate) fn expected_coverage(
    cases: &[FrozenCase],
    datasets_by_suite: &BTreeMap<String, DatasetArtifactsV5>,
    family_map: &BTreeMap<String, BTreeMap<String, String>>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Result<CoverageRequirement, String> {
    let mut families = BTreeSet::new();
    let mut phase_targets = BTreeSet::new();
    let mut sealed_episode_ids = BTreeSet::new();
    for (suite, dataset) in datasets_by_suite {
        let mapped = family_map.get(suite).cloned().unwrap_or_default();
        for (episode, oracle) in dataset.episodes.iter().zip(&dataset.oracles) {
            sealed_episode_ids.insert((suite.clone(), episode.episode_id.clone()));
            match mapped.get(&episode.episode_id) {
                Some(family) if family == &episode.family_id => {
                    families.insert(family.clone());
                }
                _ => errors.push(format!(
                    "family map differs from sealed episode: {suite}/{}",
                    episode.episode_id
                )),
            }
            for (phase, mutations) in [
                (1_i64, &oracle.gold_p1_mutations),
                (2_i64, &oracle.gold_p2_mutations),
            ] {
                for mutation in mutations {
                    phase_targets.insert((
                        suite.clone(),
                        episode.episode_id.clone(),
                        phase,
                        mutation.mutation_kind().to_owned(),
                        mutation.target_entity_id().to_owned(),
                    ));
                }
            }
        }
    }
    let case_episodes: BTreeSet<(String, String)> = cases
        .iter()
        .map(|case| {
            (
                case.public_case.suite_id.clone(),
                case.public_case.episode_id().to_owned(),
            )
        })
        .collect();
    if case_episodes != sealed_episode_ids {
        errors.push("public cases do not cover exactly the sealed episode set".to_owned());
    }
    let anchor_labels = RULE_TO_POLICY_CARD_ANCHORS_V5.keys().cloned().collect();
    let diagnostics = all_violation_categories()
        .into_iter()
        .map(|category| {
            serde_json::to_value(category)
                .ok()
                .and_then(|value| value.as_str().map(str::to_owned))
                .ok_or_else(|| "violation category failed serialization".to_owned())
        })
        .collect::<Result<BTreeSet<_>, _>>()?;
    let rules = RULE_IDS_V5.into_iter().map(str::to_owned).collect();
    let mutation_kinds: BTreeSet<String> = [
        "CONNECTOR_SYNC_MUTATION",
        "HR_SOURCE_MAPPING_MUTATION",
        "ACCOUNT_RECON_MUTATION",
        "NHI_GOVERNANCE_MUTATION",
        "AGENT_REGISTRY_MUTATION",
        "CLOUD_CIEM_MUTATION",
        "OPTIMIZATION_MUTATION",
        "JML_WORKFLOW_MUTATION",
        "CAE_CONTAINMENT_MUTATION",
        "UAR_CAMPAIGN_MUTATION",
        "JIT_DECISION_MUTATION",
        "AGENT_DELEGATION_MUTATION",
    ]
    .into_iter()
    .map(str::to_owned)
    .collect();
    let mut safety_cases = BTreeSet::new();
    let mut trace_case_ids = BTreeSet::new();
    for case in cases {
        match case.public_case.operation.as_str() {
            "safety" => {
                let kind = case
                    .public_case
                    .stimulus
                    .get("target_selector")
                    .and_then(Value::as_object)
                    .and_then(|selector| selector.get("mutation_kind"))
                    .and_then(Value::as_str);
                match kind {
                    Some(kind) => {
                        safety_cases.insert((case.case_id().to_owned(), kind.to_owned()));
                    }
                    None => errors.push(format!(
                        "safety case has no exact mutation kind: {}",
                        case.case_id()
                    )),
                }
            }
            "score_trace" => {
                trace_case_ids.insert(case.case_id().to_owned());
            }
            _ => {}
        }
    }
    let observed_kinds: BTreeSet<String> =
        safety_cases.iter().map(|(_, kind)| kind.clone()).collect();
    if observed_kinds.len() != 12 {
        gaps.push(format!(
            "public safety cases cover {} mutation kinds; 12 are required",
            observed_kinds.len()
        ));
    }
    if !observed_kinds.is_empty() && observed_kinds != mutation_kinds {
        errors.push(
            "safety case mutation kinds differ from the complete v5 mutation union".to_owned(),
        );
    }
    Ok(CoverageRequirement {
        families,
        anchor_labels,
        phase_targets,
        diagnostics,
        rules,
        safety_cases,
        safety_kinds: mutation_kinds,
        safety_truth_pairs: [(false, false), (false, true), (true, false), (true, true)]
            .into_iter()
            .collect(),
        trace_case_ids,
        snapshot_cells: [1_i64, 2_i64]
            .into_iter()
            .flat_map(|phase| {
                ["FIRST_STAGE", "LAST_STAGE", "COMMIT"]
                    .into_iter()
                    .map(move |boundary| (phase, boundary.to_owned()))
            })
            .collect(),
    })
}

fn all_violation_categories() -> [ViolationCategoryV5; 43] {
    use ViolationCategoryV5 as V;
    [
        V::CompletenessViolation,
        V::SchemaValidationError,
        V::PreconditionMismatch,
        V::CitationMismatch,
        V::ConnectorHaltOrFreeze,
        V::ConnectorReconciliationSet,
        V::SpokeDriftRemediation,
        V::ItsmManualFulfillmentMismatch,
        V::HrFeedAuthorityMismatch,
        V::HrCorrelationOrPrecedenceMismatch,
        V::BindingOrEngagementConflict,
        V::ConvGraceBreach,
        V::CompensatingControlInvalid,
        V::MoverOrScopedSelfSod,
        V::StaleTelemetryOrRotationSla,
        V::NhiOverlayEligibilityOrCap,
        V::NhiOwnerPrecedence,
        V::AgentAttestationOrStaleDigest,
        V::AgentSponsorEligibilityOrCap,
        V::AgentScopeTieringOrSod,
        V::CiemOrphanPrincipalMismatch,
        V::CiemEscalationPathMismatch,
        V::CiemEffectivePermissionOrRemediation,
        V::OptimizationInfeasible,
        V::OptimizationSuboptimal,
        V::JmlBirthrightZspOrSodBreach,
        V::JmlWorkflowSpecUnsafe,
        V::RetroApprovalTaintMismatch,
        V::CaeRevocationDecision,
        V::CaeRefreshTreeExposure,
        V::UarBaselineOrShockFilter,
        V::UarReviewerCoiOverride,
        V::UarInheritedStandingLock,
        V::QuorumOrCustodianDepletion,
        V::JitRequesterOrScopeIneligible,
        V::TicketStaleOrRetroTainted,
        V::ScopedOrBreakglassSodCooldown,
        V::ApproverDisqualified,
        V::JitDualControlOrTtlBound,
        V::AgentDelegationPrincipalInvalid,
        V::AgentOboIntersectionOrHopBreach,
        V::AgentWorkflowOrSessionSod,
        V::AgentHitlCosignOrTtlMismatch,
    ]
}

pub(crate) fn parse_coverage_requirement(
    value: &Map<String, Value>,
    errors: &mut Vec<String>,
) -> Option<CoverageRequirement> {
    let fields = [
        "families",
        "anchor_labels",
        "phase_targets",
        "diagnostics",
        "rules",
        "safety_cases",
        "safety_kinds",
        "safety_truth_pairs",
        "trace_case_ids",
        "snapshot_cells",
    ];
    let (missing, extra) = exact_fields(value, &fields);
    if !missing.is_empty() || !extra.is_empty() {
        errors.push("coverage requirement fields differ from the shared protocol".to_owned());
        return None;
    }
    let mut strings = BTreeMap::<&str, BTreeSet<String>>::new();
    for field in [
        "families",
        "anchor_labels",
        "diagnostics",
        "rules",
        "safety_kinds",
        "trace_case_ids",
    ] {
        let Some(rows) = value.get(field).and_then(Value::as_array) else {
            errors.push(format!(
                "coverage requirement {field} must be a string array"
            ));
            continue;
        };
        let parsed: Option<Vec<String>> = rows
            .iter()
            .map(|row| row.as_str().filter(|s| !s.is_empty()).map(str::to_owned))
            .collect();
        let Some(parsed) = parsed else {
            errors.push(format!(
                "coverage requirement {field} must be a string array"
            ));
            continue;
        };
        let set: BTreeSet<String> = parsed.iter().cloned().collect();
        if set.len() != parsed.len() {
            errors.push(format!("coverage requirement {field} contains duplicates"));
        }
        strings.insert(field, set);
    }
    let mut phase_targets = BTreeSet::new();
    if let Some(rows) = value.get("phase_targets").and_then(Value::as_array) {
        for row in rows {
            let parsed = row.as_array().filter(|row| row.len() == 5).and_then(|row| {
                Some((
                    row[0].as_str()?.to_owned(),
                    row[1].as_str()?.to_owned(),
                    row[2].as_i64()?,
                    row[3].as_str()?.to_owned(),
                    row[4].as_str()?.to_owned(),
                ))
            });
            match parsed {
                Some(item) => {
                    phase_targets.insert(item);
                }
                None => errors.push(
                    "coverage requirement phase_targets contains a malformed identity".to_owned(),
                ),
            }
        }
    } else {
        errors.push("coverage requirement phase_targets must be an array".to_owned());
    }
    let mut safety_cases = BTreeSet::new();
    if let Some(rows) = value.get("safety_cases").and_then(Value::as_array) {
        for row in rows {
            let parsed = row
                .as_array()
                .filter(|row| row.len() == 2)
                .and_then(|row| Some((row[0].as_str()?.to_owned(), row[1].as_str()?.to_owned())));
            match parsed {
                Some(item) => {
                    safety_cases.insert(item);
                }
                None => errors.push(
                    "coverage requirement safety_cases contains a malformed identity".to_owned(),
                ),
            }
        }
    } else {
        errors.push("coverage requirement safety_cases must be an array".to_owned());
    }
    let mut safety_truth_pairs = BTreeSet::new();
    if let Some(rows) = value.get("safety_truth_pairs").and_then(Value::as_array) {
        for row in rows {
            let parsed = row
                .as_array()
                .filter(|row| row.len() == 2)
                .and_then(|row| Some((row[0].as_bool()?, row[1].as_bool()?)));
            match parsed {
                Some(item) => {
                    safety_truth_pairs.insert(item);
                }
                None => errors.push(
                    "coverage requirement safety_truth_pairs contains a malformed identity"
                        .to_owned(),
                ),
            }
        }
    } else {
        errors.push("coverage requirement safety_truth_pairs must be an array".to_owned());
    }
    let mut snapshot_cells = BTreeSet::new();
    if let Some(rows) = value.get("snapshot_cells").and_then(Value::as_array) {
        for row in rows {
            let parsed = row
                .as_array()
                .filter(|row| row.len() == 2)
                .and_then(|row| Some((row[0].as_i64()?, row[1].as_str()?.to_owned())));
            match parsed {
                Some(item) => {
                    snapshot_cells.insert(item);
                }
                None => errors.push(
                    "coverage requirement snapshot_cells contains a malformed identity".to_owned(),
                ),
            }
        }
    } else {
        errors.push("coverage requirement snapshot_cells must be an array".to_owned());
    }
    if errors
        .iter()
        .any(|error| error.starts_with("coverage requirement"))
    {
        return None;
    }
    Some(CoverageRequirement {
        families: strings.remove("families")?,
        anchor_labels: strings.remove("anchor_labels")?,
        phase_targets,
        diagnostics: strings.remove("diagnostics")?,
        rules: strings.remove("rules")?,
        safety_cases,
        safety_kinds: strings.remove("safety_kinds")?,
        safety_truth_pairs,
        trace_case_ids: strings.remove("trace_case_ids")?,
        snapshot_cells,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn empty_artifacts() -> DatasetArtifactsV5 {
        DatasetArtifactsV5 {
            dataset_dir: PathBuf::new(),
            seal: Map::new(),
            seal_bytes: Vec::new(),
            episodes_bytes: Vec::new(),
            oracles_bytes: Vec::new(),
            episodes: Vec::new(),
            oracles: Vec::new(),
        }
    }

    fn write_receipt(root: &Path, relative: &str, bytes: &[u8]) -> PathBuf {
        let path = root.join(relative);
        std::fs::create_dir_all(path.parent().expect("parent")).expect("create receipt dir");
        std::fs::write(&path, bytes).expect("write receipt");
        path
    }

    #[test]
    fn native_subset_receipt_is_consumed_but_cannot_certify_a_suite() {
        let root = tempfile::tempdir().expect("temp root");
        let receipt = json!({
            "schema_version": "identitybench.v5.native-shared-proof-receipt.v1",
            "proof_scope": "G-SPEC-01",
            "suite_id": "v5_dev",
            "spec_sha256": V5_BASE_SPEC_SHA256,
            "dataset_bindings": [],
            "corpus": {},
            "freeze": {},
            "evaluators": {},
        });
        write_receipt(
            root.path(),
            "reports/v5/conformance/native.json",
            &serde_json::to_vec(&receipt).unwrap(),
        );
        let result = blind_gate(
            Some(Path::new("reports/v5/conformance/native.json")),
            &empty_artifacts(),
            root.path(),
        );
        assert_ne!(result.status, GateStatusV5::Pass);
        assert!(
            !result
                .errors
                .iter()
                .any(|error| error.contains("bound reference receipt schema is unsupported")),
            "native proof must enter its dedicated validator: {:?}",
            result.errors
        );
    }

    #[test]
    fn receipt_entry_rejects_oversize_before_parsing() {
        let root = tempfile::tempdir().unwrap();
        let relative = "reports/v5/conformance/oversize.json";
        let path = write_receipt(root.path(), relative, b"{}");
        std::fs::File::options()
            .write(true)
            .open(path)
            .unwrap()
            .set_len(64 * 1024 * 1024 + 1)
            .unwrap();
        let result = blind_gate(Some(Path::new(relative)), &empty_artifacts(), root.path());
        assert_eq!(result.status, GateStatusV5::Fail);
        assert!(result.errors.iter().any(|error| error.contains("64 MiB")));
    }

    #[cfg(unix)]
    #[test]
    fn receipt_entry_rejects_symlink_components_before_dispatch() {
        let root = tempfile::tempdir().unwrap();
        let actual = write_receipt(
            root.path(),
            "reports/v5/conformance/actual/receipt.json",
            b"{}",
        );
        let base = root.path().join("reports/v5/conformance");
        std::os::unix::fs::symlink(&actual, base.join("link.json")).unwrap();
        std::os::unix::fs::symlink(base.join("actual"), base.join("alias")).unwrap();
        for relative in [
            "reports/v5/conformance/link.json",
            "reports/v5/conformance/alias/receipt.json",
        ] {
            let result = blind_gate(Some(Path::new(relative)), &empty_artifacts(), root.path());
            assert_eq!(result.status, GateStatusV5::Fail);
            assert!(
                result
                    .errors
                    .iter()
                    .any(|error| error.contains("contains a symlink"))
            );
        }
    }

    #[test]
    fn missing_blind_proof_is_unknown() {
        let result = blind_gate(None, &empty_artifacts(), Path::new("."));
        assert_eq!(result.status, GateStatusV5::Unknown);
        assert_eq!(result.gate_id, GATE_ID);
    }

    #[test]
    fn sensitive_and_outside_receipt_paths_fail_closed() {
        let root = tempfile::tempdir().expect("temp root");
        let sensitive = blind_gate(
            Some(Path::new("reports/v5/conformance/secret-receipt.json")),
            &empty_artifacts(),
            root.path(),
        );
        assert_eq!(sensitive.status, GateStatusV5::Fail);

        let outside = blind_gate(
            Some(Path::new("../outside/receipt.json")),
            &empty_artifacts(),
            root.path(),
        );
        assert_eq!(outside.status, GateStatusV5::Fail);
    }

    #[test]
    fn malformed_allowed_receipt_is_unknown() {
        let root = tempfile::tempdir().expect("temp root");
        write_receipt(root.path(), "reports/v5/conformance/receipt.json", b"{");
        let result = blind_gate(
            Some(Path::new("reports/v5/conformance/receipt.json")),
            &empty_artifacts(),
            root.path(),
        );
        assert_eq!(
            result.status,
            GateStatusV5::Unknown,
            "errors: {:?}",
            result.errors
        );
        assert!(
            result
                .errors
                .iter()
                .any(|error| error.contains("malformed"))
        );
    }

    #[test]
    fn missing_allowed_receipt_is_unknown_evidence() {
        let root = tempfile::tempdir().expect("temp root");
        std::fs::create_dir_all(root.path().join("reports/v5/conformance"))
            .expect("create allowed receipt root");
        let result = blind_gate(
            Some(Path::new("reports/v5/conformance/absent-receipt.json")),
            &empty_artifacts(),
            root.path(),
        );
        assert_eq!(result.status, GateStatusV5::Unknown);
        assert!(
            result
                .errors
                .iter()
                .any(|error| error.contains("unavailable"))
        );
    }

    #[test]
    fn shared_receipt_with_extra_claim_fails() {
        let root = tempfile::tempdir().expect("temp root");
        write_receipt(
            root.path(),
            "reports/v5/conformance/receipt.json",
            br#"{"schema_version":"identitybench.v5.shared-proof-receipt.v1","proof_scope":"G-SPEC-01","extra_claim":true}"#,
        );
        let result = blind_gate(
            Some(Path::new("reports/v5/conformance/receipt.json")),
            &empty_artifacts(),
            root.path(),
        );
        assert_eq!(result.status, GateStatusV5::Fail);
        assert!(
            result
                .errors
                .iter()
                .any(|error| error.contains("unexpected fields"))
        );
    }

    #[test]
    fn sealed_episode_obs_witnesses_reconstruct_across_all_suites() {
        let workspace = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .canonicalize()
            .expect("workspace root");
        for suite in ["v5_dev", "v5_lite", "v5_core", "v5_full"] {
            let dataset = load_v5_dataset(&workspace.join("datasets").join(suite))
                .unwrap_or_else(|error| panic!("load sealed {suite}: {error}"));
            assert_eq!(dataset.episodes.len(), dataset.oracles.len());
            for (episode, oracle) in dataset.episodes.iter().zip(&dataset.oracles) {
                assert!(
                    validate_obs_witness(episode, oracle).is_empty(),
                    "{} / {} has a mismatched observation witness",
                    suite,
                    episode.episode_id
                );
            }
        }
    }

    #[test]
    fn real_shared_corpus_sources_reconstruct_the_full_case_inventory() {
        let workspace = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../..")
            .canonicalize()
            .expect("workspace root");
        let snapshot = workspace.join(
            "reports/v5/conformance/shared_results/v1/full-conformance-v3/prepared/full-conformance-v3-cursor-literal/corpus-snapshot-v2/reports/v5/conformance/shared_inputs/v3-cursor-literal",
        );
        let read = |name: &str| {
            std::fs::read(snapshot.join(name))
                .unwrap_or_else(|error| panic!("read shared corpus source {name}: {error}"))
        };
        let phase_fixture_raw = read("stimuli/phase_inputs.jsonl");
        let recipe_raw = read("stimuli/recipes.jsonl");
        let trace_raw = read("stimuli/traces.jsonl");
        let phase_rows = strict_jsonl_loads(&phase_fixture_raw).expect("phase source JSONL");
        let canonical_jsonl = |rows: &[Value]| {
            let mut bytes = Vec::new();
            for row in rows {
                bytes.extend(canonical_json_bytes(row).expect("canonical JSON"));
                bytes.push(b'\n');
            }
            bytes
        };
        let recipe_rows = strict_jsonl_loads(&recipe_raw).expect("recipe source JSONL");
        let trace_rows = strict_jsonl_loads(&trace_raw).expect("trace source JSONL");
        assert_eq!(phase_rows.len(), 360);
        assert_eq!(recipe_rows.len(), 1282);
        assert_eq!(trace_rows.len(), 60);

        let datasets: BTreeMap<String, DatasetArtifactsV5> =
            ["v5_dev", "v5_lite", "v5_core", "v5_full"]
                .into_iter()
                .map(|suite| {
                    let dataset = load_v5_dataset(&workspace.join("datasets").join(suite))
                        .unwrap_or_else(|error| panic!("load sealed {suite}: {error}"));
                    (suite.to_owned(), dataset)
                })
                .collect();
        // The checked-in historical snapshot predates the current sealed public projection.
        // Keep its stable case IDs and stimulus records, but reconstruct its phase views from
        // the current sealed public artifacts in memory. This tests corpus parsing and binding
        // without changing any repository fixture or deriving blind reference judgments.
        let mut phase1_views = BTreeMap::<(String, String), Value>::new();
        for row in &phase_rows {
            let row = row.as_object().expect("phase fixture row is an object");
            if row.get("phase_index").and_then(Value::as_i64) != Some(1) {
                continue;
            }
            let suite = row
                .get("suite_id")
                .and_then(Value::as_str)
                .expect("suite ID");
            let old_episode = row
                .get("public_episode")
                .and_then(Value::as_object)
                .expect("public episode");
            let episode_id = old_episode
                .get("episode_id")
                .and_then(Value::as_str)
                .expect("episode ID");
            let dataset = datasets.get(suite).expect("sealed suite");
            let episode = dataset
                .episodes
                .iter()
                .find(|episode| episode.episode_id == episode_id)
                .expect("sealed episode exists");
            let manifest = &episode.phase1_manifest;
            let slices = &episode.phase1_slices;
            let q1 = ib_v5_baselines::obs_strategy_q1(manifest, &episode.policy_card, 1)
                .expect("phase 1 Q1 selection");
            let q1_slices: Vec<_> = q1
                .iter()
                .map(|id| slices.get(id).expect("selected phase 1 slice").clone())
                .collect();
            let q2 = ib_v5_baselines::obs_strategy_q2(
                manifest,
                &q1_slices,
                &manifest.work_queue,
                &episode.policy_card,
                1,
            )
            .expect("phase 1 Q2 selection");
            let selected: BTreeSet<_> = q1.iter().chain(&q2).cloned().collect();
            let projected: Map<String, Value> = selected
                .into_iter()
                .map(|id| {
                    (
                        id.clone(),
                        serde_json::to_value(slices.get(&id).expect("selected slice"))
                            .expect("serialize selected slice"),
                    )
                })
                .collect();
            let mut public = Map::new();
            public.insert(
                "episode_id".to_owned(),
                Value::String(episode_id.to_owned()),
            );
            public.insert(
                "policy_card".to_owned(),
                serde_json::to_value(&episode.policy_card).expect("serialize policy card"),
            );
            public.insert(
                "phase1_manifest".to_owned(),
                serde_json::to_value(&episode.phase1_manifest).expect("serialize phase 1 manifest"),
            );
            public.insert("phase1_slices".to_owned(), Value::Object(projected));
            public.insert("q1_slice_ids_p1".to_owned(), json!(q1));
            public.insert("q2_slice_ids_p1".to_owned(), json!(q2));
            phase1_views.insert(
                (suite.to_owned(), episode_id.to_owned()),
                Value::Object(public),
            );
        }
        let mut phase_rows: Vec<Value> = phase_rows
            .iter()
            .map(|row| {
                let mut row = row
                    .as_object()
                    .expect("phase fixture row is an object")
                    .clone();
                let suite = row
                    .get("suite_id")
                    .and_then(Value::as_str)
                    .expect("suite ID");
                let phase = row
                    .get("phase_index")
                    .and_then(Value::as_i64)
                    .expect("phase");
                let old_episode = row
                    .get("public_episode")
                    .and_then(Value::as_object)
                    .expect("public episode");
                let episode_id = old_episode
                    .get("episode_id")
                    .and_then(Value::as_str)
                    .expect("episode ID");
                let episode = datasets
                    .get(suite)
                    .expect("sealed suite")
                    .episodes
                    .iter()
                    .find(|episode| episode.episode_id == episode_id)
                    .expect("sealed episode exists");
                let mut public = phase1_views
                    .get(&(suite.to_owned(), episode_id.to_owned()))
                    .expect("phase 1 carryover")
                    .as_object()
                    .expect("phase 1 projection")
                    .clone();
                if phase == 2 {
                    let manifest = &episode.phase2_base_manifest;
                    let slices = &episode.phase2_slices;
                    let q1 = ib_v5_baselines::obs_strategy_q1(manifest, &episode.policy_card, 2)
                        .expect("phase 2 Q1 selection");
                    let q1_slices: Vec<_> = q1
                        .iter()
                        .map(|id| slices.get(id).expect("selected phase 2 slice").clone())
                        .collect();
                    let q2 = ib_v5_baselines::obs_strategy_q2(
                        manifest,
                        &q1_slices,
                        &manifest.work_queue,
                        &episode.policy_card,
                        2,
                    )
                    .expect("phase 2 Q2 selection");
                    let selected: BTreeSet<_> = q1.iter().chain(&q2).cloned().collect();
                    let projected: Map<String, Value> = selected
                        .into_iter()
                        .map(|id| {
                            (
                                id.clone(),
                                serde_json::to_value(slices.get(&id).expect("selected slice"))
                                    .expect("serialize selected slice"),
                            )
                        })
                        .collect();
                    public.insert(
                        "phase2_base_manifest".to_owned(),
                        serde_json::to_value(&episode.phase2_base_manifest)
                            .expect("serialize phase 2 manifest"),
                    );
                    public.insert("phase2_slices".to_owned(), Value::Object(projected));
                    public.insert("q1_slice_ids_p2".to_owned(), json!(q1));
                    public.insert("q2_slice_ids_p2".to_owned(), json!(q2));
                }
                row.insert("public_episode".to_owned(), Value::Object(public));
                row.insert(
                    "rendered_card".to_owned(),
                    Value::String(episode.policy_card.rendered_rules_markdown.clone()),
                );
                row.insert(
                    "schema_sha256".to_owned(),
                    Value::String(
                        ib_v5_conformance::shared::PINNED_PUBLIC_SCHEMA_SHA256.to_owned(),
                    ),
                );
                Value::Object(row)
            })
            .collect();
        phase_rows.sort_by_key(|row| row.get("phase_index").and_then(Value::as_i64).unwrap_or(0));
        let phase_raw = canonical_jsonl(&phase_rows);
        let episode_hashes: BTreeMap<String, String> = datasets
            .iter()
            .map(|(suite, dataset)| (suite.clone(), sha256_bytes(&dataset.episodes_bytes)))
            .collect();
        let family_suites: Map<String, Value> = datasets
            .iter()
            .map(|(suite, dataset)| {
                let digest = &episode_hashes[suite];
                let episodes: Vec<Value> = dataset
                    .episodes
                    .iter()
                    .map(|episode| {
                        json!({
                            "episode_id": episode.episode_id,
                            "family_id": episode.family_id,
                        })
                    })
                    .collect();
                (
                    suite.clone(),
                    json!({
                        "source_episode_sha256": digest,
                        "seal_episode_binding": {
                            "episode_count": dataset.episodes.len(),
                            "episodes_sha256": digest,
                        },
                        "episodes": episodes,
                    }),
                )
            })
            .collect();
        let family_raw = canonical_json_bytes(&json!({"suites": family_suites}))
            .expect("canonical in-memory family map");
        let (family_map, family_map_sha) =
            ib_v5_conformance::load_family_map(&family_raw, &episode_hashes)
                .expect("family map bound to real sealed suites");

        let mut phase_by_id = BTreeMap::<String, Value>::new();
        let mut cases = Vec::with_capacity(1702);
        for row in &phase_rows {
            let frozen = ib_v5_conformance::freeze_case(row).expect("valid phase case");
            phase_by_id.insert(
                frozen.case_id().to_owned(),
                frozen.public_case.to_json_value(),
            );
            cases.push(frozen);
        }
        for record in recipe_rows.iter().chain(&trace_rows) {
            let base_id = record
                .get("input_ref")
                .and_then(Value::as_str)
                .expect("recipe input_ref");
            let base = phase_by_id.get(base_id).expect("phase input reference");
            cases.push(
                ib_v5_conformance::serialize_recipe_case(base, record)
                    .expect("valid shared stimulus case"),
            );
        }
        assert_eq!(cases.len(), 1702);

        let mut errors = Vec::new();
        let mut gaps = Vec::new();
        let coverage = expected_coverage(&cases, &datasets, &family_map, &mut errors, &mut gaps)
            .expect("derive coverage from sealed corpus");
        assert!(
            errors.is_empty(),
            "coverage reconstruction errors: {errors:?}"
        );
        assert!(gaps.is_empty(), "coverage reconstruction gaps: {gaps:?}");

        let public_values: Vec<Value> = cases
            .iter()
            .map(|case| case.public_case.to_json_value())
            .collect();
        let public_raw = canonical_jsonl(&public_values);
        let index_values: Vec<Value> = cases
            .iter()
            .map(|case| json!({"case_id":case.case_id(),"input_sha256":case.input_sha256}))
            .collect();
        let index_raw = canonical_jsonl(&index_values);
        let index_metadata: Vec<Value> = cases
            .iter()
            .map(|case| {
                json!({
                    "case_id": case.case_id(),
                    "canonical_case_sha256": case.input_sha256,
                    "canonical_case_bytes": case.canonical_bytes.len(),
                    "suite_id": case.public_case.suite_id,
                    "episode_id": case.public_case.episode_id(),
                    "phase_index": case.public_case.phase_index,
                    "operation": case.public_case.operation,
                })
            })
            .collect();
        let manifest = json!({
            "corpus_version": "v2",
            "protocol": SHARED_PROTOCOL,
            "all_case_count": 1702,
            "all_case_index": index_metadata,
            "normative_inputs": {"spec_sha256": V5_BASE_SPEC_SHA256, "family_map_sha256": family_map_sha},
            "phase_input_artifact": {"row_count": 360, "sha256": sha256_bytes(&phase_raw)},
            "stimulus_artifacts": {
                "recipes": {"row_count": 1282, "sha256": sha256_bytes(&recipe_raw)},
                "traces": {"row_count": 60, "sha256": sha256_bytes(&trace_raw)},
            },
        });
        let manifest_raw = canonical_json_bytes(&manifest).expect("canonical manifest");
        let coverage_raw = canonical_json_bytes(
            &ib_v5_conformance::shared::coverage_requirement_json(&coverage),
        )
        .expect("canonical coverage");
        let files = BTreeMap::from([
            ("manifest".to_owned(), (manifest_raw, workspace.clone())),
            ("phase_inputs".to_owned(), (phase_raw, workspace.clone())),
            ("recipes".to_owned(), (recipe_raw, workspace.clone())),
            ("traces".to_owned(), (trace_raw, workspace.clone())),
            ("family_map".to_owned(), (family_raw, workspace.clone())),
            ("public_cases".to_owned(), (public_raw, workspace.clone())),
            ("case_index".to_owned(), (index_raw, workspace.clone())),
            ("coverage".to_owned(), (coverage_raw, workspace.clone())),
        ]);
        errors.clear();
        gaps.clear();
        let reconstructed =
            validate_shared_corpus(&files, &datasets, &workspace, &mut errors, &mut gaps)
                .expect("corpus validator");
        assert!(
            errors.is_empty(),
            "corpus reconstruction errors: {errors:?}"
        );
        assert!(gaps.is_empty(), "corpus reconstruction gaps: {gaps:?}");
        assert_eq!(reconstructed.expect("full corpus").cases.len(), 1702);
    }
}
