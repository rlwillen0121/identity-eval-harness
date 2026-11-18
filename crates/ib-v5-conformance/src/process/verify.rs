//! Blind-output freeze receipt, sidecar, archive, and audit verification.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use ib_v5_schemas::normative_contract::{
    V5_APPROVED_ADDENDUM_PATH, V5_APPROVED_CONFORMANCE_CONTRACT_ID, V5_APPROVED_DIRECTIVE_PATH,
    V5_APPROVED_FULL_SPEC_PROFILE,
};
use serde_json::{Map, Number, Value};

use super::SharedRuntimeError;
use super::paths::{
    parse_utc_timestamp_ns, require_readonly_file, resolve_qualification_artifact,
    resolve_result_artifact, resolve_shared_input_artifact, v3_relative_input_path,
    verify_artifact_chronology, verify_readonly_digest,
};
use super::profiles::{
    APPROVED_RESULT_ROOT, FULL_SHARED_CONFORMANCE_OPERATIONS, PROTOCOL, SCHEMA_SHA256, SPEC_SHA256,
    SourceProfile, V1_RESULT_ROOT, V3_SHARED_MANIFEST_FIELDS, diagnostic_phase_batch_scope,
    namespace_packages_for_profile, profile_normative_contract, profile_pinned_source_hashes,
    resolve_source_profile, runtime_inventory_for_profile, validate_execution_classification,
    validate_relative_source, validate_v4_normative_contract,
};
use crate::shared::{
    MATERIALIZATION_CONTRACT_ID, SCORE_METRIC_NULLABILITY_CONTRACT_ID, base64_decode_strict,
    canonical_json_bytes, freeze_case, is_sha256, serialize_recipe_case, sha256_bytes,
    strict_json_loads, strict_jsonl_loads, validate_public_case,
};

pub type VerifiedSidecar = (Map<String, Value>, Vec<(PathBuf, String)>);

#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct VerifiedBlindFreeze {
    pub receipt_path: PathBuf,
    pub repository_root: PathBuf,
    pub receipt_sha256: String,
    pub proof_scope: String,
    pub scope_id: String,
    pub case_index_sha256: String,
    pub case_manifest_sha256: String,
    pub case_inventory: Vec<(String, String)>,
    pub source_closure_sha256: Vec<(String, String)>,
    pub normative_contract_id: Option<String>,
    pub bound_artifacts: Vec<(PathBuf, String)>,
}

impl VerifiedBlindFreeze {
    pub fn assert_current(&self) -> Result<(), SharedRuntimeError> {
        for (path, expected_sha256) in &self.bound_artifacts {
            require_readonly_file(path)?;
            let bytes = std::fs::read(path).map_err(|exc| {
                SharedRuntimeError(format!("failed reading {}: {exc}", path.display()))
            })?;
            if &sha256_bytes(&bytes) != expected_sha256 {
                let fname = path
                    .file_name()
                    .map(|s| s.to_string_lossy().into_owned())
                    .unwrap_or_default();
                return Err(SharedRuntimeError(format!(
                    "verified blind artifact changed: {fname}"
                )));
            }
        }
        Ok(())
    }
}

fn exact_int(value: &Value) -> Option<i64> {
    if value.is_boolean() {
        return None;
    }
    value.as_i64()
}

fn split_lines_keepends(raw: &[u8]) -> Vec<&[u8]> {
    let mut out = Vec::new();
    let mut start = 0;
    let mut i = 0;
    while i < raw.len() {
        if raw[i] == b'\r' {
            if i + 1 < raw.len() && raw[i + 1] == b'\n' {
                out.push(&raw[start..i + 2]);
                i += 2;
            } else {
                out.push(&raw[start..i + 1]);
                i += 1;
            }
            start = i;
        } else if raw[i] == b'\n' {
            out.push(&raw[start..i + 1]);
            i += 1;
            start = i;
        } else {
            i += 1;
        }
    }
    if start < raw.len() {
        out.push(&raw[start..]);
    }
    out
}

pub fn decode_canonical_jsonl(raw: &[u8], label: &str) -> Result<Vec<Value>, SharedRuntimeError> {
    let rows = strict_jsonl_loads(raw)?;
    let lines = split_lines_keepends(raw);
    if lines.len() != rows.len() {
        return Err(SharedRuntimeError(format!(
            "v3 {label} JSONL has blank or malformed physical lines"
        )));
    }
    for (index, (line, row)) in lines.iter().zip(rows.iter()).enumerate() {
        if !line.ends_with(b"\n")
            || line.ends_with(b"\r\n")
            || &line[..line.len() - 1] != canonical_json_bytes(row)?.as_slice()
        {
            return Err(SharedRuntimeError(format!(
                "v3 {label} row {index} is not canonical JSONL"
            )));
        }
    }
    Ok(rows)
}

pub fn case_index_sha256(
    case_inventory: &[(String, String)],
) -> Result<String, SharedRuntimeError> {
    let index: Vec<Value> = case_inventory
        .iter()
        .map(|(case_id, input_sha)| {
            let mut m = Map::new();
            m.insert("case_id".to_owned(), Value::String(case_id.clone()));
            m.insert("input_sha256".to_owned(), Value::String(input_sha.clone()));
            Value::Object(m)
        })
        .collect();
    let encoded = canonical_json_bytes(&Value::Array(index))?;
    Ok(sha256_bytes(&encoded))
}

pub fn validate_case_inventory(value: &Value) -> Result<Vec<(String, String)>, SharedRuntimeError> {
    let arr = value
        .as_array()
        .filter(|a| !a.is_empty())
        .ok_or_else(|| SharedRuntimeError("case_inventory must be a nonempty array".to_owned()))?;
    let mut rows = Vec::with_capacity(arr.len());
    let mut seen = BTreeSet::new();
    for item in arr {
        let obj = item
            .as_object()
            .filter(|m| m.len() == 2 && m.contains_key("case_id") && m.contains_key("input_sha256"))
            .ok_or_else(|| {
                SharedRuntimeError("case_inventory row fields are invalid".to_owned())
            })?;
        let case_id = obj["case_id"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                SharedRuntimeError("case_inventory IDs must be nonempty and unique".to_owned())
            })?;
        if !seen.insert(case_id.to_owned()) {
            return Err(SharedRuntimeError(
                "case_inventory IDs must be nonempty and unique".to_owned(),
            ));
        }
        let digest = obj["input_sha256"].as_str().unwrap_or("");
        if !is_sha256(digest) {
            return Err(SharedRuntimeError(format!(
                "case input digest is invalid: {case_id}"
            )));
        }
        rows.push((case_id.to_owned(), digest.to_owned()));
    }
    Ok(rows)
}

pub fn validate_case_index_binding(
    declared_count: &Value,
    declared_index_sha256: &Value,
    case_inventory: &[(String, String)],
    input_inventory: &[(String, String)],
) -> Result<String, SharedRuntimeError> {
    let Some(count) = exact_int(declared_count).filter(|&c| c > 0) else {
        return Err(SharedRuntimeError(
            "declared case count must be a positive integer".to_owned(),
        ));
    };
    if count as usize != case_inventory.len() || case_inventory.len() != input_inventory.len() {
        return Err(SharedRuntimeError(
            "declared case count differs from the frozen inventory".to_owned(),
        ));
    }
    let inv_ids: BTreeSet<&str> = case_inventory.iter().map(|(c, _)| c.as_str()).collect();
    let inp_ids: BTreeSet<&str> = input_inventory.iter().map(|(c, _)| c.as_str()).collect();
    if inv_ids.len() != case_inventory.len() || inp_ids.len() != input_inventory.len() {
        return Err(SharedRuntimeError(
            "case index contains duplicate case IDs".to_owned(),
        ));
    }
    if case_inventory != input_inventory {
        return Err(SharedRuntimeError(
            "case index does not exactly cover public inputs".to_owned(),
        ));
    }
    let expected = case_index_sha256(case_inventory)?;
    if declared_index_sha256.as_str() != Some(expected.as_str()) {
        return Err(SharedRuntimeError(
            "case index digest differs from the complete inventory".to_owned(),
        ));
    }
    Ok(expected)
}

fn validate_v1_proof_scope_count(
    proof_scope: &str,
    case_count: usize,
) -> Result<(), SharedRuntimeError> {
    if proof_scope == "reviewed_shared_proof" && case_count != 360 {
        return Err(SharedRuntimeError(
            "reviewed_shared_proof requires all 360 phase cases".to_owned(),
        ));
    }
    if proof_scope == "diagnostic_phase_checkpoint" && case_count != 2 {
        return Err(SharedRuntimeError(
            "diagnostic phase checkpoint requires exactly two frozen cases".to_owned(),
        ));
    }
    Ok(())
}

pub fn validate_diagnostic_phase_batch_scope(
    value: &Value,
    case_inventory: &[(String, String)],
    input_inventory: &[(String, String)],
    public_inputs_ref: &Map<String, Value>,
) -> Result<String, SharedRuntimeError> {
    let required: BTreeSet<&str> = [
        "scope_id",
        "case_count",
        "case_index_sha256",
        "operation",
        "phase_indices",
        "turn_context",
        "steering",
    ]
    .into_iter()
    .collect();
    let obj = value.as_object().ok_or_else(|| {
        SharedRuntimeError("diagnostic batch scope fields differ from the v2 contract".to_owned())
    })?;
    let actual: BTreeSet<&str> = obj.keys().map(String::as_str).collect();
    if actual != required {
        return Err(SharedRuntimeError(
            "diagnostic batch scope fields differ from the v2 contract".to_owned(),
        ));
    }
    let scope_id = obj["scope_id"].as_str().ok_or_else(|| {
        SharedRuntimeError("diagnostic phase batch scope ID must be a string".to_owned())
    })?;
    let binding = diagnostic_phase_batch_scope(scope_id)
        .ok_or_else(|| SharedRuntimeError("unknown diagnostic phase batch scope".to_owned()))?;
    validate_case_index_binding(
        &obj["case_count"],
        &obj["case_index_sha256"],
        case_inventory,
        input_inventory,
    )?;
    let phase_indices = obj["phase_indices"].as_array().ok_or_else(|| {
        SharedRuntimeError("diagnostic batch operation or phase scope is invalid".to_owned())
    })?;
    let parsed_phases: Option<Vec<i64>> = phase_indices.iter().map(exact_int).collect();
    if obj["operation"].as_str() != Some("derive_phase")
        || parsed_phases.as_deref() != Some(&[1, 2])
    {
        return Err(SharedRuntimeError(
            "diagnostic batch operation or phase scope is invalid".to_owned(),
        ));
    }
    if obj["turn_context"].as_str() != Some("T0") || obj["steering"].as_str() != Some("UNSTEERED") {
        return Err(SharedRuntimeError(
            "diagnostic batch context or steering scope is invalid".to_owned(),
        ));
    }
    if public_inputs_ref.get("path").and_then(Value::as_str) != Some(binding.public_inputs_path)
        || public_inputs_ref.get("sha256").and_then(Value::as_str)
            != Some(binding.public_inputs_sha256)
    {
        return Err(SharedRuntimeError(
            "diagnostic batch public-input binding differs from its scope".to_owned(),
        ));
    }
    Ok(scope_id.to_owned())
}

pub fn validate_audit_receipt(audit: &Map<String, Value>) -> Result<(), SharedRuntimeError> {
    let required: BTreeSet<&str> = [
        "audit_protocol",
        "evaluator_id",
        "read_paths",
        "import_names",
        "import_paths",
        "write_attempts",
        "blocked_events",
        "network_attempts",
        "author_stdout_bytes",
        "author_stderr_bytes",
    ]
    .into_iter()
    .collect();
    let actual: BTreeSet<&str> = audit.keys().map(String::as_str).collect();
    if actual != required || audit["audit_protocol"].as_str() != Some("shared-audit-v1") {
        return Err(SharedRuntimeError(
            "child audit fields differ from shared-audit-v1".to_owned(),
        ));
    }
    let non_empty_list = |v: &Value| match v {
        Value::Array(a) => !a.is_empty(),
        Value::Bool(b) => *b,
        _ => false,
    };
    if non_empty_list(&audit["write_attempts"])
        || non_empty_list(&audit["blocked_events"])
        || non_empty_list(&audit["network_attempts"])
    {
        return Err(SharedRuntimeError(
            "worker attempted a blocked filesystem, process, or network action".to_owned(),
        ));
    }
    let non_zero_bytes = |v: &Value| match v {
        Value::Number(n) => n.as_i64().unwrap_or(1) != 0,
        Value::Bool(b) => *b,
        _ => true,
    };
    if non_zero_bytes(&audit["author_stdout_bytes"])
        || non_zero_bytes(&audit["author_stderr_bytes"])
    {
        return Err(SharedRuntimeError(
            "worker emitted non-result stdout/stderr".to_owned(),
        ));
    }
    Ok(())
}

pub fn verify_evaluator_audits(
    audit_rows: &[Value],
    receipt_rows: &[Value],
    evaluator_id: &str,
) -> Result<(), SharedRuntimeError> {
    if audit_rows.len() != receipt_rows.len() {
        return Err(SharedRuntimeError(format!(
            "raw audit count differs for {evaluator_id}"
        )));
    }
    let required: BTreeSet<&str> = [
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
    for (audit_val, receipt_val) in audit_rows.iter().zip(receipt_rows.iter()) {
        let audit_obj = audit_val.as_object().ok_or_else(|| {
            SharedRuntimeError(format!("raw audit fields differ for {evaluator_id}"))
        })?;
        let receipt_obj = receipt_val.as_object().ok_or_else(|| {
            SharedRuntimeError(format!("raw audit binding differs for {evaluator_id}"))
        })?;
        let actual: BTreeSet<&str> = audit_obj.keys().map(String::as_str).collect();
        if actual != required {
            return Err(SharedRuntimeError(format!(
                "raw audit fields differ for {evaluator_id}"
            )));
        }
        for key in &required {
            if audit_obj.get(*key) != receipt_obj.get(*key) {
                return Err(SharedRuntimeError(format!(
                    "raw audit binding differs for {evaluator_id}"
                )));
            }
        }
    }
    Ok(())
}

fn decode_raw_output(
    value: Option<&Value>,
    stream: &str,
    evaluator_id: &str,
) -> Result<Vec<u8>, SharedRuntimeError> {
    let text = value
        .and_then(Value::as_str)
        .ok_or_else(|| SharedRuntimeError(format!("raw {stream} is missing for {evaluator_id}")))?;
    base64_decode_strict(text).map_err(|_| {
        SharedRuntimeError(format!(
            "raw {stream} is not valid base64 for {evaluator_id}"
        ))
    })
}

#[allow(clippy::too_many_arguments)]
pub fn verify_evaluator_receipts(
    rows: &[Value],
    evaluator_id: &str,
    source_closure_sha256: &str,
    case_inventory: &[(String, String)],
    case_manifest_sha256: &str,
    wire_evaluator_id: &str,
    proof_scope: &str,
    profile: &SourceProfile,
    require_ordered_inventory: bool,
) -> Result<(), SharedRuntimeError> {
    if rows.len() != case_inventory.len() {
        return Err(SharedRuntimeError(format!(
            "raw receipt count differs for {evaluator_id}"
        )));
    }
    let inventory_by_id: BTreeMap<&str, &str> = case_inventory
        .iter()
        .map(|(cid, sha)| (cid.as_str(), sha.as_str()))
        .collect();
    let mut seen = BTreeSet::new();
    for (index, row_val) in rows.iter().enumerate() {
        let row = row_val.as_object().ok_or_else(|| {
            SharedRuntimeError(format!("raw receipt is not an object for {evaluator_id}"))
        })?;
        if row.get("protocol").and_then(Value::as_str) != Some(PROTOCOL) {
            return Err(SharedRuntimeError(format!(
                "raw receipt protocol differs for {evaluator_id}"
            )));
        }
        let case_id = row.get("case_id").and_then(Value::as_str);
        let input_sha = row.get("input_sha256").and_then(Value::as_str);
        let Some((cid, isha)) = case_id.zip(input_sha) else {
            return Err(SharedRuntimeError(format!(
                "raw receipt case binding differs for {evaluator_id}"
            )));
        };
        if seen.contains(cid) || inventory_by_id.get(cid) != Some(&isha) {
            return Err(SharedRuntimeError(format!(
                "raw receipt case binding differs for {evaluator_id}"
            )));
        }
        if require_ordered_inventory
            && (cid, isha)
                != (
                    case_inventory[index].0.as_str(),
                    case_inventory[index].1.as_str(),
                )
        {
            return Err(SharedRuntimeError(format!(
                "raw receipt order differs from the full frozen inventory for {evaluator_id}"
            )));
        }
        seen.insert(cid);
        if row.get("evaluator_id").and_then(Value::as_str) != Some(evaluator_id) {
            return Err(SharedRuntimeError(format!(
                "outer evaluator slot differs for {evaluator_id}"
            )));
        }
        let profile_required = row.contains_key("source_profile")
            || matches!(
                proof_scope,
                "reviewed_shared_proof" | "diagnostic_phase_batch" | "full_shared_conformance"
            );
        let profile_mismatch = row.get("source_profile").and_then(Value::as_str)
            != Some(profile.profile_id)
            || row.get("author_origin").and_then(Value::as_str) != Some(profile.author_origin);
        if profile_required && profile_mismatch {
            return Err(SharedRuntimeError(format!(
                "outer source-profile identity differs for {evaluator_id}"
            )));
        }
        if row.get("source_closure_sha256").and_then(Value::as_str) != Some(source_closure_sha256) {
            return Err(SharedRuntimeError(format!(
                "source closure hash differs for {evaluator_id}"
            )));
        }
        if row.get("case_manifest_sha256").and_then(Value::as_str) != Some(case_manifest_sha256) {
            return Err(SharedRuntimeError(format!(
                "case manifest hash differs for {evaluator_id}"
            )));
        }
        let stdout = decode_raw_output(row.get("raw_stdout_b64"), "stdout", evaluator_id)?;
        let stderr = decode_raw_output(row.get("raw_stderr_b64"), "stderr", evaluator_id)?;
        if Some(sha256_bytes(&stdout).as_str()) != row.get("stdout_sha256").and_then(Value::as_str)
        {
            return Err(SharedRuntimeError(format!(
                "raw stdout digest differs for {evaluator_id}"
            )));
        }
        if Some(sha256_bytes(&stderr).as_str()) != row.get("stderr_sha256").and_then(Value::as_str)
        {
            return Err(SharedRuntimeError(format!(
                "raw stderr digest differs for {evaluator_id}"
            )));
        }
        let status = row
            .get("execution_status")
            .and_then(Value::as_str)
            .unwrap_or("");
        if status != "ok" && status != "failed" {
            return Err(SharedRuntimeError(format!(
                "raw receipt execution status is invalid for {evaluator_id}"
            )));
        }
        if status == "ok" {
            if Some(sha256_bytes(&stdout).as_str())
                != row.get("result_sha256").and_then(Value::as_str)
            {
                return Err(SharedRuntimeError(format!(
                    "canonical result digest differs for {evaluator_id}"
                )));
            }
            let raw_result = strict_json_loads(&stdout)?;
            let raw_audit = strict_json_loads(&stderr)?;
            if !raw_result.is_object() || row.get("result") != Some(&raw_result) {
                return Err(SharedRuntimeError(format!(
                    "parsed result differs from raw stdout for {evaluator_id}"
                )));
            }
            if raw_result.get("protocol").and_then(Value::as_str) != Some(PROTOCOL) {
                return Err(SharedRuntimeError(format!(
                    "raw result protocol differs for {evaluator_id}"
                )));
            }
            if raw_result.get("case_id").and_then(Value::as_str) != Some(cid)
                || raw_result.get("input_sha256").and_then(Value::as_str) != Some(isha)
            {
                return Err(SharedRuntimeError(format!(
                    "raw result case binding differs for {evaluator_id}"
                )));
            }
            let Some(raw_audit_obj) = raw_audit.as_object() else {
                return Err(SharedRuntimeError(format!(
                    "parsed audit differs from raw stderr for {evaluator_id}"
                )));
            };
            if row.get("audit") != Some(&raw_audit) {
                return Err(SharedRuntimeError(format!(
                    "parsed audit differs from raw stderr for {evaluator_id}"
                )));
            }
            if matches!(
                proof_scope,
                "reviewed_shared_proof" | "diagnostic_phase_batch" | "full_shared_conformance"
            ) {
                if raw_result.get("evaluator_id").and_then(Value::as_str) != Some(wire_evaluator_id)
                {
                    return Err(SharedRuntimeError(format!(
                        "raw wire evaluator ID differs for {evaluator_id}"
                    )));
                }
                validate_audit_receipt(raw_audit_obj)?;
            }
        } else if proof_scope == "reviewed_shared_proof" {
            return Err(SharedRuntimeError(format!(
                "reviewed blind output has a failed execution: {evaluator_id}"
            )));
        }
    }
    let expected_keys: BTreeSet<&str> = inventory_by_id.keys().copied().collect();
    if seen != expected_keys {
        return Err(SharedRuntimeError(format!(
            "raw receipt case set differs for {evaluator_id}"
        )));
    }
    Ok(())
}

pub fn verify_capture_manifest(
    evaluator_id: &str,
    snapshot_root: &Path,
    expected_paths: &[String],
    profile: Option<&SourceProfile>,
) -> Result<(), SharedRuntimeError> {
    let manifest_path = snapshot_root.join("capture-manifest.json");
    let meta = std::fs::symlink_metadata(&manifest_path).map_err(|_| {
        SharedRuntimeError("source snapshot has no capture-manifest.json".to_owned())
    })?;
    if meta.file_type().is_symlink() || !meta.file_type().is_file() {
        return Err(SharedRuntimeError(
            "source snapshot has no capture-manifest.json".to_owned(),
        ));
    }
    let raw_bytes = std::fs::read(&manifest_path).map_err(|exc| {
        SharedRuntimeError(format!("failed reading capture-manifest.json: {exc}"))
    })?;
    let manifest_val = strict_json_loads(&raw_bytes)?;
    let mut manifest = manifest_val
        .as_object()
        .ok_or_else(|| SharedRuntimeError("source capture manifest is not an object".to_owned()))?
        .clone();
    if manifest.get("evaluator_id").and_then(Value::as_str) != Some(evaluator_id) {
        return Err(SharedRuntimeError(
            "source capture evaluator identity mismatch".to_owned(),
        ));
    }
    let fallback_profile = resolve_source_profile(evaluator_id, Some("legacy_diagnostic"))?;
    let prof = profile.unwrap_or(&fallback_profile);
    if prof.source_files != expected_paths {
        return Err(SharedRuntimeError(
            "source capture allowlist differs from the pinned profile".to_owned(),
        ));
    }
    let has_capture_profile =
        manifest.contains_key("source_profile") || manifest.contains_key("author_origin");
    let capture_profile_mismatch = manifest.get("source_profile").and_then(Value::as_str)
        != Some(prof.profile_id)
        || manifest.get("author_origin").and_then(Value::as_str) != Some(prof.author_origin);
    if prof.profile_id == "legacy_diagnostic" && has_capture_profile && capture_profile_mismatch {
        return Err(SharedRuntimeError(
            "legacy capture source-profile identity mismatch".to_owned(),
        ));
    }
    if prof.profile_id != "legacy_diagnostic" && capture_profile_mismatch {
        return Err(SharedRuntimeError(
            "source capture profile or author origin mismatch".to_owned(),
        ));
    }
    if manifest.get("capture_status").and_then(Value::as_str) != Some("verified_source_copy") {
        return Err(SharedRuntimeError(
            "source capture has no verified-copy status".to_owned(),
        ));
    }
    let manifest_sha = manifest.remove("manifest_sha256");
    let expected_sha = sha256_bytes(&canonical_json_bytes(&Value::Object(manifest.clone()))?);
    if manifest_sha.as_ref().and_then(Value::as_str) != Some(expected_sha.as_str()) {
        return Err(SharedRuntimeError(
            "source capture manifest hash mismatch".to_owned(),
        ));
    }
    let rows = manifest
        .get("source_files")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            SharedRuntimeError(
                "source capture path list differs from the exact allowlist".to_owned(),
            )
        })?;
    let row_paths: Vec<&str> = rows
        .iter()
        .filter_map(Value::as_object)
        .filter_map(|r| r.get("path").and_then(Value::as_str))
        .collect();
    let expected_refs: Vec<&str> = expected_paths.iter().map(String::as_str).collect();
    if row_paths != expected_refs {
        return Err(SharedRuntimeError(
            "source capture path list differs from the exact allowlist".to_owned(),
        ));
    }
    if manifest.get("entrypoint").and_then(Value::as_str) != Some(prof.entrypoint) {
        return Err(SharedRuntimeError(
            "source capture entrypoint mismatch".to_owned(),
        ));
    }
    if let Some(ep_mod) = manifest.get("entrypoint_module") {
        if ep_mod.as_str() != prof.entrypoint_module {
            return Err(SharedRuntimeError(
                "source capture entrypoint module mismatch".to_owned(),
            ));
        }
    } else if prof.entrypoint_module.is_some() {
        return Err(SharedRuntimeError(
            "source capture is missing its pinned package entrypoint".to_owned(),
        ));
    }
    let mut captured_hashes: BTreeMap<&str, &str> = BTreeMap::new();
    for row_val in rows {
        let row = row_val.as_object().ok_or_else(|| {
            SharedRuntimeError("source capture record has unexpected fields".to_owned())
        })?;
        let exp: BTreeSet<&str> = ["path", "sha256", "size_bytes"].into_iter().collect();
        let act: BTreeSet<&str> = row.keys().map(String::as_str).collect();
        if act != exp {
            return Err(SharedRuntimeError(
                "source capture record has unexpected fields".to_owned(),
            ));
        }
        let relative = row["path"].as_str().ok_or_else(|| {
            SharedRuntimeError("source capture record has unexpected fields".to_owned())
        })?;
        validate_relative_source(relative, Some(evaluator_id))?;
        let path = snapshot_root.join(relative);
        let pmeta = std::fs::symlink_metadata(&path).map_err(|_| {
            SharedRuntimeError(format!("captured source missing or symlinked: {relative}"))
        })?;
        if pmeta.file_type().is_symlink() || !pmeta.file_type().is_file() {
            return Err(SharedRuntimeError(format!(
                "captured source missing or symlinked: {relative}"
            )));
        }
        let raw = std::fs::read(&path).map_err(|_| {
            SharedRuntimeError(format!("captured source hash mismatch: {relative}"))
        })?;
        let row_size = exact_int(&row["size_bytes"]);
        let row_sha = row["sha256"].as_str().unwrap_or("");
        if i64::try_from(raw.len()).ok() != row_size || sha256_bytes(&raw) != row_sha {
            return Err(SharedRuntimeError(format!(
                "captured source hash mismatch: {relative}"
            )));
        }
        captured_hashes.insert(relative, row_sha);
    }
    for (path, digest) in profile_pinned_source_hashes(prof)? {
        if captured_hashes.get(path.as_str()) != Some(&digest.as_str()) {
            return Err(SharedRuntimeError(
                "captured source differs from its approved profile pin".to_owned(),
            ));
        }
    }
    let expected_schema_sha = captured_hashes
        .get("crates/ib-v5-schemas/src/lib.rs")
        .copied()
        .unwrap_or(SCHEMA_SHA256);
    if manifest.get("schema_sha256").and_then(Value::as_str) != Some(expected_schema_sha) {
        return Err(SharedRuntimeError(
            "captured schema hash differs from the frozen schema".to_owned(),
        ));
    }
    if let Some(contract) = profile_normative_contract(prof)? {
        for (key, item) in &contract {
            if key == "contract_id" {
                continue;
            }
            if let Some(obj) = item.as_object() {
                let p = obj.get("path").and_then(Value::as_str).unwrap_or("");
                let d = obj.get("sha256").and_then(Value::as_str).unwrap_or("");
                if captured_hashes.get(p) != Some(&d) {
                    return Err(SharedRuntimeError(
                        "captured normative source differs from the approved contract".to_owned(),
                    ));
                }
            }
        }
    }
    Ok(())
}

pub fn verify_capture_reference(
    root: &Path,
    value: &Value,
    evaluator_id: &str,
    profile: Option<&SourceProfile>,
) -> Result<Vec<(PathBuf, String)>, SharedRuntimeError> {
    let ref_obj = value
        .as_object()
        .filter(|m| m.len() == 2 && m.contains_key("path") && m.contains_key("sha256"))
        .ok_or_else(|| {
            SharedRuntimeError(format!(
                "source capture reference is invalid for {evaluator_id}"
            ))
        })?;
    let rel_path = ref_obj["path"].as_str().unwrap_or("");
    let exp_sha = ref_obj["sha256"].as_str().unwrap_or("");
    let path = resolve_result_artifact(root, rel_path)?;
    let raw = verify_readonly_digest(&path, exp_sha)?;
    if path.file_name().and_then(|s| s.to_str()) != Some("capture-manifest.json") {
        return Err(SharedRuntimeError(format!(
            "source capture reference path is invalid for {evaluator_id}"
        )));
    }
    let source_root = path.parent().ok_or_else(|| {
        SharedRuntimeError(format!(
            "source capture reference path is invalid for {evaluator_id}"
        ))
    })?;
    let fallback = resolve_source_profile(evaluator_id, Some("legacy_diagnostic"))?;
    let prof = profile.unwrap_or(&fallback);
    verify_capture_manifest(evaluator_id, source_root, &prof.source_files, Some(prof))?;
    let manifest_val = strict_json_loads(&raw)?;
    let manifest = manifest_val
        .as_object()
        .ok_or_else(|| SharedRuntimeError("source capture manifest is not an object".to_owned()))?;
    let rows = manifest
        .get("source_files")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            SharedRuntimeError("source capture manifest has no source file list".to_owned())
        })?;
    let mut artifacts = vec![(path.clone(), exp_sha.to_owned())];
    for row_val in rows {
        let row = row_val
            .as_object()
            .filter(|m| {
                m.len() == 3
                    && m.contains_key("path")
                    && m.contains_key("sha256")
                    && m.contains_key("size_bytes")
            })
            .ok_or_else(|| {
                SharedRuntimeError("source capture file record is invalid".to_owned())
            })?;
        let rel = row["path"].as_str().ok_or_else(|| {
            SharedRuntimeError("source capture file record is invalid".to_owned())
        })?;
        let src = source_root.join(rel);
        require_readonly_file(&src)?;
        let sha = row["sha256"].as_str().unwrap_or("").to_owned();
        artifacts.push((src, sha));
    }
    Ok(artifacts)
}

#[allow(clippy::too_many_arguments)]
pub fn verify_runtime_reference(
    root: &Path,
    value: &Value,
    evaluator_id: &str,
    expected_closure_sha256: &str,
    capture_reference: &Value,
    expected_case_count: usize,
    profile: Option<&SourceProfile>,
    direct_manifest: bool,
) -> Result<Vec<(PathBuf, String)>, SharedRuntimeError> {
    let ref_obj = value
        .as_object()
        .filter(|m| m.len() == 2 && m.contains_key("path") && m.contains_key("sha256"))
        .ok_or_else(|| {
            SharedRuntimeError(format!(
                "runtime manifest reference is invalid for {evaluator_id}"
            ))
        })?;
    let path = resolve_result_artifact(root, ref_obj["path"].as_str().unwrap_or(""))?;
    let exp_sha = ref_obj["sha256"].as_str().unwrap_or("");
    let raw = verify_readonly_digest(&path, exp_sha)?;
    let wrapper_val = strict_json_loads(&raw)?;
    let wrapper = wrapper_val.as_object().ok_or_else(|| {
        SharedRuntimeError(format!(
            "runtime manifest wrapper is invalid for {evaluator_id}"
        ))
    })?;
    let (freeze, wrapper_case_count) = if direct_manifest {
        if wrapper.contains_key("freeze") {
            return Err(SharedRuntimeError(format!(
                "runtime manifest must be direct for {evaluator_id}"
            )));
        }
        (wrapper, wrapper.get("case_count").and_then(exact_int))
    } else {
        let inner = wrapper
            .get("freeze")
            .and_then(Value::as_object)
            .ok_or_else(|| {
                SharedRuntimeError(format!(
                    "runtime manifest wrapper is invalid for {evaluator_id}"
                ))
            })?;
        (inner, wrapper.get("case_count").and_then(exact_int))
    };

    let fallback = resolve_source_profile(evaluator_id, Some("legacy_diagnostic"))?;
    let prof = profile.unwrap_or(&fallback);
    let normative_contract = profile_normative_contract(prof)?;

    let required: BTreeSet<&str> = [
        "format",
        "evaluator_id",
        "entrypoint",
        "approved_source_files",
        "runtime_inventory",
        "dependency_files",
        "python_runtime",
        "source_closure_sha256",
        "case_count",
        "dependency_snapshot",
    ]
    .into_iter()
    .collect();
    let profile_keys: BTreeSet<&str> = ["source_profile", "author_origin", "entrypoint_module"]
        .into_iter()
        .collect();
    let mut allowed_sets: Vec<BTreeSet<&str>> = vec![
        required.clone(),
        required
            .iter()
            .copied()
            .chain(["namespace_packages"])
            .collect(),
        required.union(&profile_keys).copied().collect(),
        required
            .union(&profile_keys)
            .copied()
            .chain(["namespace_packages"])
            .collect(),
    ];
    if normative_contract.is_some() {
        allowed_sets.push(
            required
                .union(&profile_keys)
                .copied()
                .chain(["normative_contract"])
                .collect(),
        );
        allowed_sets.push(
            required
                .union(&profile_keys)
                .copied()
                .chain(["normative_contract", "namespace_packages"])
                .collect(),
        );
    }
    let freeze_keys: BTreeSet<&str> = freeze.keys().map(String::as_str).collect();
    if !allowed_sets.contains(&freeze_keys) {
        return Err(SharedRuntimeError(format!(
            "runtime manifest fields differ for {evaluator_id}"
        )));
    }
    let expected_format = if normative_contract.is_some() {
        "identitybench.v5.evaluator-closure.v2"
    } else {
        "identitybench.v5.evaluator-closure.v1"
    };
    if freeze["format"].as_str() != Some(expected_format)
        || freeze["evaluator_id"].as_str() != Some(evaluator_id)
        || freeze["entrypoint"].as_str() != Some(prof.entrypoint)
        || exact_int(&freeze["case_count"]) != i64::try_from(expected_case_count).ok()
        || wrapper_case_count != i64::try_from(expected_case_count).ok()
    {
        return Err(SharedRuntimeError(format!(
            "runtime manifest identity/count mismatch for {evaluator_id}"
        )));
    }
    let has_profile = profile_keys.is_subset(&freeze_keys);
    if prof.profile_id == "legacy_diagnostic" && !has_profile {
        // allowed
    } else if !has_profile
        || freeze.get("source_profile").and_then(Value::as_str) != Some(prof.profile_id)
        || freeze.get("author_origin").and_then(Value::as_str) != Some(prof.author_origin)
        || freeze.get("entrypoint_module").and_then(Value::as_str) != prof.entrypoint_module
    {
        return Err(SharedRuntimeError(format!(
            "runtime profile identity mismatch for {evaluator_id}"
        )));
    }
    if let Some(contract) = &normative_contract
        && freeze.get("normative_contract").and_then(Value::as_object) != Some(contract)
    {
        return Err(SharedRuntimeError(format!(
            "runtime normative contract mismatch for {evaluator_id}"
        )));
    }

    let mut closure_payload = Map::new();
    for key in [
        "format",
        "evaluator_id",
        "entrypoint",
        "approved_source_files",
        "runtime_inventory",
        "dependency_files",
        "python_runtime",
    ] {
        closure_payload.insert(key.to_owned(), freeze[key].clone());
    }
    if has_profile {
        for key in ["source_profile", "author_origin", "entrypoint_module"] {
            closure_payload.insert(key.to_owned(), freeze[key].clone());
        }
    }
    if normative_contract.is_some() {
        closure_payload.insert(
            "normative_contract".to_owned(),
            freeze["normative_contract"].clone(),
        );
    }
    let reconstructed = sha256_bytes(&canonical_json_bytes(&Value::Object(closure_payload))?);
    if reconstructed != expected_closure_sha256
        || freeze["source_closure_sha256"].as_str() != Some(reconstructed.as_str())
    {
        return Err(SharedRuntimeError(format!(
            "runtime source-closure digest mismatch for {evaluator_id}"
        )));
    }

    let cap_path_str = capture_reference
        .as_object()
        .and_then(|m| m.get("path"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let capture_path = resolve_result_artifact(root, cap_path_str)?;
    let cap_raw = std::fs::read(&capture_path)
        .map_err(|exc| SharedRuntimeError(format!("failed reading capture manifest: {exc}")))?;
    let cap_val = strict_json_loads(&cap_raw)?;
    let cap_manifest = cap_val.as_object().ok_or_else(|| {
        SharedRuntimeError(format!(
            "source capture manifest is invalid for {evaluator_id}"
        ))
    })?;
    let captured_rows = cap_manifest.get("source_files").and_then(Value::as_array);
    let approved_rows = freeze["approved_source_files"].as_array();
    if captured_rows.is_none() || approved_rows != captured_rows {
        return Err(SharedRuntimeError(format!(
            "runtime closure differs from captured source for {evaluator_id}"
        )));
    }
    let approved = approved_rows.expect("checked");
    let approved_paths: Vec<&str> = approved
        .iter()
        .filter_map(Value::as_object)
        .filter_map(|r| r.get("path").and_then(Value::as_str))
        .collect();
    let expected_src_paths: Vec<&str> = prof.source_files.iter().map(String::as_str).collect();
    if approved_paths != expected_src_paths {
        return Err(SharedRuntimeError(format!(
            "runtime source list differs from approved closure for {evaluator_id}"
        )));
    }
    let inventory = freeze["runtime_inventory"].as_array().ok_or_else(|| {
        SharedRuntimeError(format!("runtime inventory list differs for {evaluator_id}"))
    })?;
    let inv_paths: Vec<&str> = inventory
        .iter()
        .filter_map(Value::as_object)
        .filter_map(|r| r.get("path").and_then(Value::as_str))
        .collect();
    let expected_inv: Vec<&str> = runtime_inventory_for_profile(prof).to_vec();
    if inv_paths != expected_inv {
        return Err(SharedRuntimeError(format!(
            "runtime inventory list differs for {evaluator_id}"
        )));
    }
    let dependencies = freeze["dependency_files"]
        .as_array()
        .filter(|a| !a.is_empty())
        .ok_or_else(|| {
            SharedRuntimeError(format!(
                "runtime dependency manifest is empty for {evaluator_id}"
            ))
        })?;
    for record_val in approved
        .iter()
        .chain(inventory.iter())
        .chain(dependencies.iter())
    {
        let rec = record_val
            .as_object()
            .filter(|m| {
                m.len() == 3
                    && m.contains_key("path")
                    && m.contains_key("sha256")
                    && m.contains_key("size_bytes")
            })
            .ok_or_else(|| {
                SharedRuntimeError(format!("runtime file record is invalid for {evaluator_id}"))
            })?;
        if !rec["path"].is_string()
            || !rec["sha256"].as_str().is_some_and(is_sha256)
            || exact_int(&rec["size_bytes"]).is_none_or(|s| s < 0)
        {
            return Err(SharedRuntimeError(format!(
                "runtime file record is invalid for {evaluator_id}"
            )));
        }
    }
    let dep_snap = freeze["dependency_snapshot"]
        .as_object()
        .filter(|m| {
            m.len() == 4
                && m.contains_key("file_count")
                && m.contains_key("files_sha256")
                && m.contains_key("packages")
                && m.contains_key("staged_readonly")
        })
        .ok_or_else(|| {
            SharedRuntimeError(format!(
                "runtime dependency summary is invalid for {evaluator_id}"
            ))
        })?;
    let deps_sha = sha256_bytes(&canonical_json_bytes(&freeze["dependency_files"])?);
    if exact_int(&dep_snap["file_count"]) != i64::try_from(dependencies.len()).ok()
        || dep_snap["staged_readonly"].as_bool() != Some(true)
        || dep_snap["files_sha256"].as_str() != Some(deps_sha.as_str())
    {
        return Err(SharedRuntimeError(format!(
            "runtime dependency summary mismatch for {evaluator_id}"
        )));
    }
    if let Some(expected_ns) = namespace_packages_for_profile(prof) {
        let actual_ns: Option<Vec<String>> = freeze
            .get("namespace_packages")
            .and_then(Value::as_array)
            .and_then(|arr| arr.iter().map(|v| v.as_str().map(str::to_owned)).collect());
        if actual_ns.as_ref() != Some(&expected_ns) {
            return Err(SharedRuntimeError(
                "production runtime namespace package declaration differs".to_owned(),
            ));
        }
    } else if freeze.contains_key("namespace_packages") {
        return Err(SharedRuntimeError(format!(
            "blind runtime declares an unexpected namespace package: {evaluator_id}"
        )));
    }
    Ok(vec![(path, exp_sha.to_owned())])
}

pub fn verify_archive_records(
    archive_root: &Path,
    records: &Value,
    label: &str,
) -> Result<Vec<(PathBuf, String)>, SharedRuntimeError> {
    let rows = records
        .as_array()
        .filter(|a| !a.is_empty())
        .ok_or_else(|| SharedRuntimeError(format!("{label} archive file list is empty")))?;
    let base = archive_root
        .canonicalize()
        .map_err(|exc| SharedRuntimeError(format!("{label} archive root unavailable: {exc}")))?;
    let archive_parent = archive_root.parent();
    let mut artifacts = Vec::with_capacity(rows.len());
    let mut seen = BTreeSet::new();
    for rec_val in rows {
        let rec = rec_val
            .as_object()
            .filter(|m| {
                m.len() == 3
                    && m.contains_key("path")
                    && m.contains_key("sha256")
                    && m.contains_key("size_bytes")
            })
            .ok_or_else(|| SharedRuntimeError(format!("{label} archive file record is invalid")))?;
        let rel_path = rec["path"]
            .as_str()
            .ok_or_else(|| SharedRuntimeError(format!("{label} archive file record is invalid")))?;
        let sha = rec["sha256"]
            .as_str()
            .filter(|s| is_sha256(s))
            .ok_or_else(|| SharedRuntimeError(format!("{label} archive file record is invalid")))?;
        let size = exact_int(&rec["size_bytes"])
            .filter(|&s| s >= 0)
            .ok_or_else(|| SharedRuntimeError(format!("{label} archive file record is invalid")))?;
        if rel_path.is_empty()
            || rel_path.starts_with('/')
            || rel_path.split('/').any(|p| matches!(p, "" | "." | ".."))
        {
            return Err(SharedRuntimeError(format!(
                "{label} archive path is unsafe"
            )));
        }
        if !seen.insert(rel_path.to_owned()) {
            return Err(SharedRuntimeError(format!(
                "{label} archive contains duplicate paths"
            )));
        }
        let mut candidate = archive_root.to_path_buf();
        for part in rel_path.split('/') {
            candidate.push(part);
        }
        let resolved = candidate
            .canonicalize()
            .map_err(|exc| SharedRuntimeError(format!("{label} archive path missing: {exc}")))?;
        if !resolved.starts_with(&base) {
            return Err(SharedRuntimeError(format!(
                "{label} archive path escaped its root"
            )));
        }
        let mut cur = candidate.parent();
        while let Some(parent) = cur {
            if Some(parent) == archive_parent {
                break;
            }
            let pmeta = std::fs::symlink_metadata(parent).map_err(|exc| {
                SharedRuntimeError(format!("{label} archive parent missing: {exc}"))
            })?;
            if pmeta.file_type().is_symlink() {
                return Err(SharedRuntimeError(format!(
                    "{label} archive traverses a symlink"
                )));
            }
            cur = parent.parent();
        }
        verify_readonly_digest(&candidate, sha)?;
        let meta = std::fs::metadata(&candidate)
            .map_err(|exc| SharedRuntimeError(format!("{label} archive stat failed: {exc}")))?;
        if i64::try_from(meta.len()).ok() != Some(size) {
            return Err(SharedRuntimeError(format!(
                "{label} archive file size differs"
            )));
        }
        artifacts.push((candidate, sha.to_owned()));
    }
    Ok(artifacts)
}

pub fn verify_runtime_archive_reference(
    root: &Path,
    value: &Value,
    evaluator_id: &str,
    freeze: &Map<String, Value>,
    source_closure_sha256: &str,
    case_manifest_sha256: &str,
) -> Result<Vec<(PathBuf, String)>, SharedRuntimeError> {
    let ref_obj = value
        .as_object()
        .filter(|m| m.len() == 2 && m.contains_key("path") && m.contains_key("sha256"))
        .ok_or_else(|| {
            SharedRuntimeError(format!(
                "runtime archive reference is invalid for {evaluator_id}"
            ))
        })?;
    let path = resolve_result_artifact(root, ref_obj["path"].as_str().unwrap_or(""))?;
    let exp_sha = ref_obj["sha256"].as_str().unwrap_or("");
    let raw = verify_readonly_digest(&path, exp_sha)?;
    let archive_val = strict_json_loads(&raw)?;
    let mut archive = archive_val
        .as_object()
        .ok_or_else(|| {
            SharedRuntimeError(format!("runtime archive fields differ for {evaluator_id}"))
        })?
        .clone();
    let fields: BTreeSet<&str> = [
        "format",
        "evaluator_id",
        "source_profile",
        "author_origin",
        "source_closure_sha256",
        "case_manifest_sha256",
        "source_files",
        "dependency_snapshot",
        "python_runtime",
        "runtime_inventory",
        "archive_manifest_sha256",
    ]
    .into_iter()
    .collect();
    let act: BTreeSet<&str> = archive.keys().map(String::as_str).collect();
    if act != fields {
        return Err(SharedRuntimeError(format!(
            "runtime archive fields differ for {evaluator_id}"
        )));
    }
    let archive_sha = archive.remove("archive_manifest_sha256");
    let computed_sha = sha256_bytes(&canonical_json_bytes(&Value::Object(archive.clone()))?);
    if archive_sha.as_ref().and_then(Value::as_str) != Some(computed_sha.as_str()) {
        return Err(SharedRuntimeError(format!(
            "runtime archive manifest digest differs for {evaluator_id}"
        )));
    }
    if archive["format"].as_str()
        != Some("identitybench.v5.shared-conformance-runtime-source-archive.v1")
        || archive["evaluator_id"].as_str() != Some(evaluator_id)
        || archive.get("source_profile") != freeze.get("source_profile")
        || archive.get("author_origin") != freeze.get("author_origin")
        || archive["source_closure_sha256"].as_str() != Some(source_closure_sha256)
        || archive["case_manifest_sha256"].as_str() != Some(case_manifest_sha256)
        || archive.get("dependency_snapshot") != freeze.get("dependency_snapshot")
        || archive.get("python_runtime") != freeze.get("python_runtime")
        || archive.get("runtime_inventory") != freeze.get("runtime_inventory")
    {
        return Err(SharedRuntimeError(format!(
            "runtime archive identity differs for {evaluator_id}"
        )));
    }
    let parent = path.parent().ok_or_else(|| {
        SharedRuntimeError(format!("runtime archive parent missing for {evaluator_id}"))
    })?;
    let mut artifacts = vec![(path.clone(), exp_sha.to_owned())];
    artifacts.extend(verify_archive_records(
        parent,
        &archive["source_files"],
        "source",
    )?);

    let mut expected_records: BTreeMap<&str, &Value> = BTreeMap::new();
    for list_key in ["approved_source_files", "runtime_inventory"] {
        if let Some(arr) = freeze.get(list_key).and_then(Value::as_array) {
            for rec in arr {
                if let Some(p) = rec
                    .as_object()
                    .and_then(|o| o.get("path"))
                    .and_then(Value::as_str)
                {
                    expected_records.insert(p, rec);
                }
            }
        }
    }
    let mut actual_records: BTreeMap<&str, &Value> = BTreeMap::new();
    if let Some(arr) = archive["source_files"].as_array() {
        for rec in arr {
            if let Some(p) = rec
                .as_object()
                .and_then(|o| o.get("path"))
                .and_then(Value::as_str)
            {
                actual_records.insert(p, rec);
            }
        }
    }
    if actual_records != expected_records {
        return Err(SharedRuntimeError(format!(
            "runtime archive inventory differs for {evaluator_id}"
        )));
    }
    Ok(artifacts)
}

pub fn verify_dependency_archive_reference(
    root: &Path,
    value: &Value,
    evaluator_id: &str,
    freeze: &Map<String, Value>,
    profile: &SourceProfile,
) -> Result<Vec<(PathBuf, String)>, SharedRuntimeError> {
    let ref_obj = value
        .as_object()
        .filter(|m| m.len() == 2 && m.contains_key("path") && m.contains_key("sha256"))
        .ok_or_else(|| {
            SharedRuntimeError(format!(
                "dependency archive reference is invalid for {evaluator_id}"
            ))
        })?;
    let path = resolve_result_artifact(root, ref_obj["path"].as_str().unwrap_or(""))?;
    let exp_sha = ref_obj["sha256"].as_str().unwrap_or("");
    let raw = verify_readonly_digest(&path, exp_sha)?;
    let manifest_val = strict_json_loads(&raw)?;
    let mut manifest = manifest_val
        .as_object()
        .ok_or_else(|| {
            SharedRuntimeError(format!(
                "dependency archive fields differ for {evaluator_id}"
            ))
        })?
        .clone();
    let fields: BTreeSet<&str> = [
        "format",
        "evaluator_slot",
        "source_profile",
        "author_origin",
        "source_closure_sha256",
        "dependency_snapshot",
        "dependency_files",
        "manifest_sha256",
    ]
    .into_iter()
    .collect();
    let act: BTreeSet<&str> = manifest.keys().map(String::as_str).collect();
    if act != fields {
        return Err(SharedRuntimeError(format!(
            "dependency archive fields differ for {evaluator_id}"
        )));
    }
    let manifest_sha = manifest.remove("manifest_sha256");
    let computed_sha = sha256_bytes(&canonical_json_bytes(&Value::Object(manifest.clone()))?);
    if manifest_sha.as_ref().and_then(Value::as_str) != Some(computed_sha.as_str()) {
        return Err(SharedRuntimeError(format!(
            "dependency archive manifest digest differs for {evaluator_id}"
        )));
    }
    if manifest["format"].as_str() != Some("identitybench.v5.phase-only-frozen-dependencies.v1")
        || manifest["evaluator_slot"].as_str() != Some(evaluator_id)
        || manifest["source_profile"].as_str() != Some(profile.profile_id)
        || manifest["author_origin"].as_str() != Some(profile.author_origin)
        || manifest.get("source_closure_sha256") != freeze.get("source_closure_sha256")
        || manifest.get("dependency_snapshot") != freeze.get("dependency_snapshot")
        || manifest.get("dependency_files") != freeze.get("dependency_files")
    {
        return Err(SharedRuntimeError(format!(
            "dependency archive identity differs for {evaluator_id}"
        )));
    }
    let dep_root = path
        .parent()
        .ok_or_else(|| {
            SharedRuntimeError(format!(
                "dependency archive parent missing for {evaluator_id}"
            ))
        })?
        .join("frozen-dependencies");
    let mut artifacts = vec![(path.clone(), exp_sha.to_owned())];
    artifacts.extend(verify_archive_records(
        &dep_root,
        &manifest["dependency_files"],
        "dependency",
    )?);
    Ok(artifacts)
}

pub fn verify_full_shared_v3_protocol_sidecar(
    root: &Path,
    value: &Value,
    scope_id: &str,
) -> Result<VerifiedSidecar, SharedRuntimeError> {
    let ref_obj = value
        .as_object()
        .filter(|m| m.len() == 2 && m.contains_key("path") && m.contains_key("sha256"))
        .ok_or_else(|| {
            SharedRuntimeError("full conformance sidecar reference is invalid".to_owned())
        })?;
    let path = resolve_result_artifact(root, ref_obj["path"].as_str().unwrap_or(""))?;
    let exp_sha = ref_obj["sha256"].as_str().unwrap_or("");
    let raw = verify_readonly_digest(&path, exp_sha)?;
    let sidecar_val = strict_json_loads(&raw)?;
    let sidecar = sidecar_val.as_object().ok_or_else(|| {
        SharedRuntimeError("full conformance sidecar fields differ from the v3 contract".to_owned())
    })?;
    let required: BTreeSet<&str> = [
        "protocol",
        "materialization_contract_id",
        "score_metric_nullability_contract_id",
        "scope_id",
    ]
    .into_iter()
    .collect();
    let actual: BTreeSet<&str> = sidecar.keys().map(String::as_str).collect();
    if actual != required {
        return Err(SharedRuntimeError(
            "full conformance sidecar fields differ from the v3 contract".to_owned(),
        ));
    }
    if sidecar["protocol"].as_str() != Some(PROTOCOL)
        || sidecar["materialization_contract_id"].as_str() != Some(MATERIALIZATION_CONTRACT_ID)
        || sidecar["score_metric_nullability_contract_id"].as_str()
            != Some(SCORE_METRIC_NULLABILITY_CONTRACT_ID)
        || sidecar["scope_id"].as_str() != Some(scope_id)
    {
        return Err(SharedRuntimeError(
            "full conformance sidecar identity differs".to_owned(),
        ));
    }
    Ok((sidecar.clone(), vec![(path, exp_sha.to_owned())]))
}

#[allow(clippy::too_many_arguments)]
pub fn verify_full_shared_v3_run_manifest(
    root: &Path,
    value: &Value,
    evaluator_id: &str,
    source_closure_sha256: &str,
    case_manifest_sha256: &str,
    raw_receipts_ref: &Map<String, Value>,
    raw_audits_ref: &Map<String, Value>,
    case_count: usize,
) -> Result<Vec<(PathBuf, String)>, SharedRuntimeError> {
    let ref_obj = value
        .as_object()
        .filter(|m| m.len() == 2 && m.contains_key("path") && m.contains_key("sha256"))
        .ok_or_else(|| {
            SharedRuntimeError(format!(
                "run manifest reference is invalid for {evaluator_id}"
            ))
        })?;
    let path = resolve_result_artifact(root, ref_obj["path"].as_str().unwrap_or(""))?;
    let exp_sha = ref_obj["sha256"].as_str().unwrap_or("");
    let raw = verify_readonly_digest(&path, exp_sha)?;
    let run_val = strict_json_loads(&raw)?;
    let run = run_val.as_object().ok_or_else(|| {
        SharedRuntimeError(format!(
            "run manifest header is incomplete for {evaluator_id}"
        ))
    })?;
    let required = [
        "source_closure_sha256",
        "case_manifest_sha256",
        "raw_receipts",
        "raw_audits",
    ];
    if !required.iter().all(|k| run.contains_key(*k)) {
        return Err(SharedRuntimeError(format!(
            "run manifest header is incomplete for {evaluator_id}"
        )));
    }
    if run["source_closure_sha256"].as_str() != Some(source_closure_sha256)
        || run["case_manifest_sha256"].as_str() != Some(case_manifest_sha256)
        || run["raw_receipts"].as_object() != Some(raw_receipts_ref)
        || run["raw_audits"].as_object() != Some(raw_audits_ref)
        || raw_receipts_ref.get("row_count").and_then(exact_int) != i64::try_from(case_count).ok()
        || raw_audits_ref.get("row_count").and_then(exact_int) != i64::try_from(case_count).ok()
    {
        return Err(SharedRuntimeError(format!(
            "run manifest inventory or stream binding differs for {evaluator_id}"
        )));
    }
    for key in ["raw_receipts", "raw_audits"] {
        let stream_ref = run[key].as_object().ok_or_else(|| {
            SharedRuntimeError(format!(
                "run manifest stream reference is invalid for {evaluator_id}"
            ))
        })?;
        let exp: BTreeSet<&str> = ["path", "sha256", "row_count"].into_iter().collect();
        let act: BTreeSet<&str> = stream_ref.keys().map(String::as_str).collect();
        if act != exp
            || exact_int(&stream_ref["row_count"]).is_none()
            || !stream_ref["sha256"].as_str().is_some_and(is_sha256)
            || !stream_ref["path"].is_string()
        {
            return Err(SharedRuntimeError(format!(
                "run manifest stream reference is invalid for {evaluator_id}"
            )));
        }
    }
    Ok(vec![(path, exp_sha.to_owned())])
}

pub fn verify_opaque_metadata_reference(
    root: &Path,
    value: &Value,
    label: &str,
) -> Result<(PathBuf, String), SharedRuntimeError> {
    let ref_obj = value
        .as_object()
        .filter(|m| m.len() == 2 && m.contains_key("path") && m.contains_key("sha256"))
        .ok_or_else(|| SharedRuntimeError(format!("{label} reference is invalid")))?;
    let relative = ref_obj["path"].as_str().unwrap_or("");
    let exp_sha = ref_obj["sha256"].as_str().unwrap_or("");
    let path = if label.starts_with("qualification:") {
        resolve_qualification_artifact(root, relative)?
    } else {
        resolve_result_artifact(root, relative)?
    };
    verify_readonly_digest(&path, exp_sha)?;
    Ok((path, exp_sha.to_owned()))
}

#[allow(clippy::too_many_arguments)]
pub fn verify_full_shared_v3_inputs(
    root: &Path,
    scope_val: &Value,
    input_artifacts_val: &Value,
    public_path: &Path,
    input_rows: &[Value],
    case_inventory: &[(String, String)],
    case_manifest_sha256: &str,
    case_index_sha256: &str,
) -> Result<(String, Vec<(PathBuf, String)>), SharedRuntimeError> {
    let scope_fields: BTreeSet<&str> = [
        "scope_id",
        "case_count",
        "case_index_sha256",
        "operations",
        "phase_indices",
        "turn_context",
        "steering",
    ]
    .into_iter()
    .collect();
    let scope = scope_val.as_object().ok_or_else(|| {
        SharedRuntimeError("full conformance scope fields differ from the v3 contract".to_owned())
    })?;
    let actual_scope_fields: BTreeSet<&str> = scope.keys().map(String::as_str).collect();
    if actual_scope_fields != scope_fields {
        return Err(SharedRuntimeError(
            "full conformance scope fields differ from the v3 contract".to_owned(),
        ));
    }
    let scope_id = scope["scope_id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            SharedRuntimeError("full conformance scope_id must be a nonempty string".to_owned())
        })?;
    let ops_ok = scope["operations"]
        .as_array()
        .and_then(|arr| arr.iter().map(Value::as_str).collect::<Option<Vec<_>>>())
        .is_some_and(|v| v == FULL_SHARED_CONFORMANCE_OPERATIONS);
    let phases_ok = scope["phase_indices"]
        .as_array()
        .and_then(|arr| arr.iter().map(exact_int).collect::<Option<Vec<_>>>())
        .is_some_and(|v| v == [1, 2]);
    if exact_int(&scope["case_count"]) != i64::try_from(case_inventory.len()).ok()
        || case_inventory.is_empty()
        || !scope["case_index_sha256"].as_str().is_some_and(is_sha256)
        || scope["case_index_sha256"].as_str() != Some(case_index_sha256)
        || !ops_ok
        || !phases_ok
        || scope["turn_context"].as_str() != Some("T0")
        || scope["steering"].as_str() != Some("UNSTEERED")
    {
        return Err(SharedRuntimeError(
            "full conformance scope differs from the complete v3 contract".to_owned(),
        ));
    }

    let required_artifacts: BTreeSet<&str> = [
        "manifest",
        "phase_inputs",
        "phase_checkpoint",
        "recipes",
        "traces",
        "family_map",
    ]
    .into_iter()
    .collect();
    let input_artifacts = input_artifacts_val.as_object().ok_or_else(|| {
        SharedRuntimeError(
            "full conformance input_artifacts differ from the v3 contract".to_owned(),
        )
    })?;
    let act_arts: BTreeSet<&str> = input_artifacts.keys().map(String::as_str).collect();
    if act_arts != required_artifacts {
        return Err(SharedRuntimeError(
            "full conformance input_artifacts differ from the v3 contract".to_owned(),
        ));
    }
    for (name, reference) in input_artifacts {
        let ref_obj = reference
            .as_object()
            .filter(|m| m.len() == 2 && m.contains_key("path") && m.contains_key("sha256"))
            .ok_or_else(|| {
                SharedRuntimeError(format!("v3 corpus artifact reference is invalid: {name}"))
            })?;
        if !ref_obj["sha256"].as_str().is_some_and(is_sha256) {
            return Err(SharedRuntimeError(format!(
                "v3 corpus artifact digest is invalid: {name}"
            )));
        }
    }

    let manifest_ref = input_artifacts["manifest"].as_object().expect("checked");
    let manifest_path =
        resolve_shared_input_artifact(root, manifest_ref["path"].as_str().unwrap_or(""))?;
    let manifest_sha = manifest_ref["sha256"].as_str().unwrap_or("");
    let manifest_raw = verify_readonly_digest(&manifest_path, manifest_sha)?;
    let manifest_val = strict_json_loads(&manifest_raw)?;
    let manifest_value = manifest_val.as_object().ok_or_else(|| {
        SharedRuntimeError(
            "full shared corpus manifest fields differ from the v3 contract".to_owned(),
        )
    })?;
    let exp_mfields: BTreeSet<&str> = V3_SHARED_MANIFEST_FIELDS.into_iter().collect();
    let act_mfields: BTreeSet<&str> = manifest_value.keys().map(String::as_str).collect();
    if act_mfields != exp_mfields {
        return Err(SharedRuntimeError(
            "full shared corpus manifest fields differ from the v3 contract".to_owned(),
        ));
    }
    if manifest_value.get("corpus_version").and_then(Value::as_str) != Some("v3")
        || manifest_value.get("protocol").and_then(Value::as_str) != Some(PROTOCOL)
    {
        return Err(SharedRuntimeError(
            "full shared corpus manifest version or protocol differs".to_owned(),
        ));
    }
    let normative = manifest_value
        .get("normative_inputs")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            SharedRuntimeError("v3 normative input bindings are incomplete".to_owned())
        })?;
    let exp_norm_keys: BTreeSet<&str> =
        ["schemas_path", "schemas_sha256", "spec_path", "spec_sha256"]
            .into_iter()
            .collect();
    let act_norm_keys: BTreeSet<&str> = normative.keys().map(String::as_str).collect();
    if act_norm_keys != exp_norm_keys {
        return Err(SharedRuntimeError(
            "v3 normative input bindings are incomplete".to_owned(),
        ));
    }
    if normative["schemas_path"].as_str() != Some("src/identitybench/v5/schemas.py")
        || normative["schemas_sha256"].as_str() != Some(SCHEMA_SHA256)
        || normative["spec_path"].as_str() != Some("SPEC-v5.md")
        || normative["spec_sha256"].as_str() != Some(SPEC_SHA256)
    {
        return Err(SharedRuntimeError(
            "v3 normative input bindings differ from the pinned sources".to_owned(),
        ));
    }

    let mut bound: Vec<(PathBuf, String)> = vec![(manifest_path.clone(), manifest_sha.to_owned())];
    let mut bind_declared_artifact =
        |name: &str, relative_path: &str, digest: &str| -> Result<Vec<u8>, SharedRuntimeError> {
            let expected_path = v3_relative_input_path(root, &manifest_path, relative_path)?;
            let reference = input_artifacts[name].as_object().expect("checked");
            if reference["path"].as_str() != Some(expected_path.as_str())
                || reference["sha256"].as_str() != Some(digest)
            {
                return Err(SharedRuntimeError(format!(
                    "v3 input artifact differs from manifest: {name}"
                )));
            }
            let path = resolve_shared_input_artifact(root, &expected_path)?;
            let raw = verify_readonly_digest(&path, digest)?;
            bound.push((path, digest.to_owned()));
            Ok(raw)
        };

    let phase_meta = manifest_value
        .get("phase_checkpoint")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            SharedRuntimeError("v3 phase checkpoint descriptor is incomplete".to_owned())
        })?;
    let exp_pmeta: BTreeSet<&str> = [
        "path",
        "phase_case_count",
        "phase_inputs_path",
        "phase_inputs_sha256",
        "preserved_checkpoint_bytes",
        "sha256",
    ]
    .into_iter()
    .collect();
    if phase_meta
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>()
        != exp_pmeta
    {
        return Err(SharedRuntimeError(
            "v3 phase checkpoint descriptor is incomplete".to_owned(),
        ));
    }
    let phase_case_count = exact_int(&phase_meta["phase_case_count"]);
    if phase_meta["path"].as_str().is_none_or(str::is_empty)
        || phase_case_count.is_none_or(|c| c <= 0)
        || phase_meta["preserved_checkpoint_bytes"].as_bool() != Some(true)
        || !phase_meta["sha256"].as_str().is_some_and(is_sha256)
        || !phase_meta["phase_inputs_sha256"]
            .as_str()
            .is_some_and(is_sha256)
    {
        return Err(SharedRuntimeError(
            "v3 phase checkpoint descriptor is malformed".to_owned(),
        ));
    }
    let phase_raw = bind_declared_artifact(
        "phase_inputs",
        phase_meta["phase_inputs_path"].as_str().unwrap_or(""),
        phase_meta["phase_inputs_sha256"].as_str().unwrap_or(""),
    )?;
    let checkpoint_raw = bind_declared_artifact(
        "phase_checkpoint",
        phase_meta["path"].as_str().unwrap_or(""),
        phase_meta["sha256"].as_str().unwrap_or(""),
    )?;

    let stimuli = manifest_value
        .get("stimulus_sources")
        .and_then(Value::as_array)
        .filter(|a| a.len() == 2)
        .ok_or_else(|| {
            SharedRuntimeError(
                "v3 manifest must declare recipe and trace stimulus sources".to_owned(),
            )
        })?;
    let mut stimulus_descriptors: BTreeMap<String, &Map<String, Value>> = BTreeMap::new();
    for desc_val in stimuli {
        let desc = desc_val.as_object().ok_or_else(|| {
            SharedRuntimeError("v3 stimulus source descriptor fields are invalid".to_owned())
        })?;
        let exp_d: BTreeSet<&str> = ["copied_byte_exact_from", "path", "record_count", "sha256"]
            .into_iter()
            .collect();
        if desc.keys().map(String::as_str).collect::<BTreeSet<_>>() != exp_d {
            return Err(SharedRuntimeError(
                "v3 stimulus source descriptor fields are invalid".to_owned(),
            ));
        }
        if desc["copied_byte_exact_from"]
            .as_str()
            .is_none_or(str::is_empty)
        {
            return Err(SharedRuntimeError(
                "v3 stimulus provenance descriptor is invalid".to_owned(),
            ));
        }
        let label = desc["path"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| SharedRuntimeError("v3 stimulus source path is invalid".to_owned()))?;
        let file_name = label.rsplit('/').next().unwrap_or(label);
        let name = file_name.strip_suffix(".jsonl").unwrap_or("");
        if !matches!(name, "recipes" | "traces") || !label.ends_with(".jsonl") {
            return Err(SharedRuntimeError(
                "v3 stimulus source path is unsupported".to_owned(),
            ));
        }
        if stimulus_descriptors.contains_key(name) {
            return Err(SharedRuntimeError(
                "v3 stimulus source is duplicated".to_owned(),
            ));
        }
        if exact_int(&desc["record_count"]).is_none_or(|c| c <= 0) {
            return Err(SharedRuntimeError(
                "v3 stimulus record count is invalid".to_owned(),
            ));
        }
        if !desc["sha256"].as_str().is_some_and(is_sha256) {
            return Err(SharedRuntimeError(
                "v3 stimulus source digest is invalid".to_owned(),
            ));
        }
        stimulus_descriptors.insert(name.to_owned(), desc);
    }
    if stimulus_descriptors.len() != 2 {
        return Err(SharedRuntimeError(
            "v3 manifest must bind both recipes and traces".to_owned(),
        ));
    }
    let recipes_raw = bind_declared_artifact(
        "recipes",
        stimulus_descriptors["recipes"]["path"]
            .as_str()
            .unwrap_or(""),
        stimulus_descriptors["recipes"]["sha256"]
            .as_str()
            .unwrap_or(""),
    )?;
    let traces_raw = bind_declared_artifact(
        "traces",
        stimulus_descriptors["traces"]["path"]
            .as_str()
            .unwrap_or(""),
        stimulus_descriptors["traces"]["sha256"]
            .as_str()
            .unwrap_or(""),
    )?;

    let family_meta = manifest_value
        .get("family_metadata")
        .and_then(Value::as_object)
        .filter(|m| m.contains_key("path") && m.contains_key("sha256"))
        .ok_or_else(|| SharedRuntimeError("v3 family map descriptor is incomplete".to_owned()))?;
    if !family_meta["sha256"].as_str().is_some_and(is_sha256) {
        return Err(SharedRuntimeError(
            "v3 family map digest is invalid".to_owned(),
        ));
    }
    let family_raw = bind_declared_artifact(
        "family_map",
        family_meta["path"].as_str().unwrap_or(""),
        family_meta["sha256"].as_str().unwrap_or(""),
    )?;
    let family_val = strict_json_loads(&family_raw)?;
    if !family_val.is_object() {
        return Err(SharedRuntimeError(
            "v3 family map must be a JSON object".to_owned(),
        ));
    }

    let phase_rows = decode_canonical_jsonl(&phase_raw, "phase_inputs")?;
    let recipe_rows = decode_canonical_jsonl(&recipes_raw, "recipes")?;
    let trace_rows = decode_canonical_jsonl(&traces_raw, "traces")?;
    let exp_recipes = exact_int(&stimulus_descriptors["recipes"]["record_count"]).unwrap_or(-1);
    let exp_traces = exact_int(&stimulus_descriptors["traces"]["record_count"]).unwrap_or(-1);
    if i64::try_from(phase_rows.len()).ok() != phase_case_count
        || i64::try_from(recipe_rows.len()).ok() != Some(exp_recipes)
        || i64::try_from(trace_rows.len()).ok() != Some(exp_traces)
        || phase_rows.is_empty()
        || recipe_rows.is_empty()
        || trace_rows.is_empty()
    {
        return Err(SharedRuntimeError(
            "v3 source artifact row counts differ from the complete corpus".to_owned(),
        ));
    }

    let mut phase_cases: BTreeMap<String, Value> = BTreeMap::new();
    for val in &phase_rows {
        if !val.is_object() {
            return Err(SharedRuntimeError(
                "v3 phase input is not an object".to_owned(),
            ));
        }
        let case = validate_public_case(val)?;
        if case.operation != "derive_phase" || phase_cases.contains_key(&case.case_id) {
            return Err(SharedRuntimeError(
                "v3 phase input has a duplicate ID or wrong operation".to_owned(),
            ));
        }
        phase_cases.insert(case.case_id, val.clone());
    }

    let manifest_index = manifest_value
        .get("case_index")
        .and_then(Value::as_array)
        .filter(|arr| arr.len() == phase_rows.len() + recipe_rows.len() + trace_rows.len())
        .ok_or_else(|| {
            SharedRuntimeError(
                "v3 manifest case_index count differs from its source rows".to_owned(),
            )
        })?;
    let common_fields: BTreeSet<&str> = [
        "canonical_case_bytes",
        "canonical_case_sha256",
        "case_id",
        "episode_id",
        "operation",
        "phase_index",
        "suite_id",
    ]
    .into_iter()
    .collect();
    let mut used_phase: BTreeSet<String> = BTreeSet::new();
    let mut used_recipes: BTreeSet<usize> = BTreeSet::new();
    let mut used_traces: BTreeSet<usize> = BTreeSet::new();
    let mut cases = Vec::with_capacity(manifest_index.len());
    let mut derived_inventory: Vec<(String, String)> = Vec::with_capacity(manifest_index.len());
    let mut operation_counts: BTreeMap<String, i64> = BTreeMap::new();
    let mut phase_episode_ids: BTreeSet<(String, String)> = BTreeSet::new();
    let mut derived_phase_counts: BTreeMap<i64, i64> = BTreeMap::new();

    for (index, meta_val) in manifest_index.iter().enumerate() {
        let metadata = meta_val.as_object().ok_or_else(|| {
            SharedRuntimeError(format!(
                "v3 manifest case_index row {index} is not an object"
            ))
        })?;
        let operation = metadata
            .get("operation")
            .and_then(Value::as_str)
            .unwrap_or("");
        let mut expected_keys = common_fields.clone();
        if operation == "derive_phase" {
            expected_keys.insert("source_file");
        } else {
            expected_keys.extend([
                "input_ref",
                "stimulus_record_id",
                "stimulus_record_index",
                "stimulus_source_file",
            ]);
        }
        let act_keys: BTreeSet<&str> = metadata.keys().map(String::as_str).collect();
        if act_keys != expected_keys {
            return Err(SharedRuntimeError(format!(
                "v3 manifest case_index row {index} has invalid fields"
            )));
        }
        let case_id = metadata
            .get("case_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let episode_id = metadata
            .get("episode_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let suite_id = metadata
            .get("suite_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let phase_index = metadata.get("phase_index").and_then(exact_int);
        let digest = metadata
            .get("canonical_case_sha256")
            .and_then(Value::as_str)
            .unwrap_or("");
        let byte_count = metadata.get("canonical_case_bytes").and_then(exact_int);
        if case_id.is_empty()
            || episode_id.is_empty()
            || !matches!(suite_id, "v5_dev" | "v5_lite" | "v5_core" | "v5_full")
            || !matches!(phase_index, Some(1 | 2))
            || !FULL_SHARED_CONFORMANCE_OPERATIONS.contains(&operation)
            || !is_sha256(digest)
            || byte_count.is_none_or(|b| b <= 0)
        {
            return Err(SharedRuntimeError(format!(
                "v3 manifest case_index row {index} has invalid identity"
            )));
        }
        let phase_idx = phase_index.unwrap();
        let frozen = if operation == "derive_phase" {
            if metadata["source_file"].as_str() != phase_meta["phase_inputs_path"].as_str() {
                return Err(SharedRuntimeError(format!(
                    "v3 derive row {index} references an unknown source"
                )));
            }
            let Some(case_value) = phase_cases.get(case_id) else {
                return Err(SharedRuntimeError(format!(
                    "v3 derive row {index} is missing or duplicated"
                )));
            };
            if !used_phase.insert(case_id.to_owned()) {
                return Err(SharedRuntimeError(format!(
                    "v3 derive row {index} is missing or duplicated"
                )));
            }
            freeze_case(case_value)?
        } else {
            let source_name = if operation == "score_trace" {
                "traces"
            } else {
                "recipes"
            };
            let source_desc = stimulus_descriptors[source_name];
            if metadata["stimulus_source_file"].as_str() != source_desc["path"].as_str() {
                return Err(SharedRuntimeError(format!(
                    "v3 stimulus row {index} references an unknown source"
                )));
            }
            let records = if source_name == "traces" {
                &trace_rows
            } else {
                &recipe_rows
            };
            let used_set = if source_name == "traces" {
                &mut used_traces
            } else {
                &mut used_recipes
            };
            let rec_idx_i64 = exact_int(&metadata["stimulus_record_index"]).ok_or_else(|| {
                SharedRuntimeError(format!(
                    "v3 stimulus row {index} has an invalid record index"
                ))
            })?;
            if rec_idx_i64 < 0 || (rec_idx_i64 as usize) >= records.len() {
                return Err(SharedRuntimeError(format!(
                    "v3 stimulus row {index} has an invalid record index"
                )));
            }
            let rec_idx = rec_idx_i64 as usize;
            if !used_set.insert(rec_idx) {
                return Err(SharedRuntimeError(format!(
                    "v3 stimulus record is referenced more than once: {index}"
                )));
            }
            let record = &records[rec_idx];
            let rec_obj = record.as_object().ok_or_else(|| {
                SharedRuntimeError(format!(
                    "v3 stimulus row {index} differs from its source record"
                ))
            })?;
            let rec_id = rec_obj.get("recipe_id").and_then(Value::as_str);
            let rec_input_ref = rec_obj.get("input_ref").and_then(Value::as_str);
            if rec_id.is_none()
                || rec_input_ref.is_none()
                || metadata["stimulus_record_id"].as_str() != rec_id
                || metadata["input_ref"].as_str() != rec_input_ref
                || Some(operation) != rec_obj.get("operation").and_then(Value::as_str)
                || Some(case_id) != rec_id
            {
                return Err(SharedRuntimeError(format!(
                    "v3 stimulus row {index} differs from its source record"
                )));
            }
            let base = phase_cases.get(rec_input_ref.unwrap()).ok_or_else(|| {
                SharedRuntimeError(format!(
                    "v3 stimulus row {index} references an unknown phase case"
                ))
            })?;
            serialize_recipe_case(base, record)?
        };

        if frozen.case_id() != case_id
            || frozen.input_sha256 != digest
            || i64::try_from(frozen.canonical_bytes.len()).ok() != byte_count
            || frozen.public_case.operation != operation
            || frozen.public_case.suite_id != suite_id
            || frozen.public_case.phase_index != phase_idx
            || frozen.public_case.episode_id() != episode_id
        {
            return Err(SharedRuntimeError(format!(
                "v3 manifest case_index differs from reconstructed row {index}"
            )));
        }
        *operation_counts.entry(operation.to_owned()).or_insert(0) += 1;
        if operation == "derive_phase" {
            phase_episode_ids.insert((suite_id.to_owned(), episode_id.to_owned()));
            *derived_phase_counts.entry(phase_idx).or_insert(0) += 1;
        }
        derived_inventory.push((frozen.case_id().to_owned(), frozen.input_sha256.clone()));
        cases.push(frozen);
    }

    if used_phase.len() != phase_cases.len()
        || used_recipes.len() != recipe_rows.len()
        || used_traces.len() != trace_rows.len()
        || !derived_phase_counts.contains_key(&1)
        || !derived_phase_counts.contains_key(&2)
        || derived_phase_counts.len() != 2
    {
        return Err(SharedRuntimeError(
            "v3 manifest case index does not consume every source row exactly once".to_owned(),
        ));
    }
    if derived_inventory != case_inventory {
        return Err(SharedRuntimeError(
            "v3 manifest, receipt, and reconstructed case inventories differ".to_owned(),
        ));
    }
    let mut public_inventory = Vec::with_capacity(input_rows.len());
    for val in input_rows {
        let _ = validate_public_case(val)?;
        let f = freeze_case(val)?;
        public_inventory.push((f.case_id().to_owned(), f.input_sha256));
    }
    if public_inventory != derived_inventory {
        return Err(SharedRuntimeError(
            "full public input order or digest differs from the v3 manifest".to_owned(),
        ));
    }
    let public_raw = std::fs::read(public_path)
        .map_err(|exc| SharedRuntimeError(format!("failed reading public inputs: {exc}")))?;
    let mut expected_public_raw = Vec::new();
    for c in &cases {
        expected_public_raw.extend_from_slice(&c.canonical_bytes);
        expected_public_raw.push(b'\n');
    }
    if public_raw != expected_public_raw {
        return Err(SharedRuntimeError(
            "full public input bytes differ from reconstructed v3 cases".to_owned(),
        ));
    }

    let checkpoint_val = strict_json_loads(&checkpoint_raw)?;
    let checkpoint = checkpoint_val.as_object().ok_or_else(|| {
        SharedRuntimeError("v3 phase checkpoint must be a JSON object".to_owned())
    })?;
    if checkpoint.get("protocol").and_then(Value::as_str) != Some(PROTOCOL)
        || checkpoint.get("corpus_version").and_then(Value::as_str) != Some("v3")
        || checkpoint.get("normative_inputs") != manifest_value.get("normative_inputs")
    {
        return Err(SharedRuntimeError(
            "v3 phase checkpoint protocol or normative bindings differ".to_owned(),
        ));
    }
    let phase_descriptor = checkpoint
        .get("phase_input_artifact")
        .and_then(Value::as_object)
        .ok_or_else(|| {
            SharedRuntimeError("v3 phase checkpoint input descriptor is invalid".to_owned())
        })?;
    let exp_pdesc: BTreeSet<&str> = [
        "byte_length",
        "canonicalization",
        "case_digest",
        "path",
        "row_count",
        "sha256",
    ]
    .into_iter()
    .collect();
    if phase_descriptor
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>()
        != exp_pdesc
    {
        return Err(SharedRuntimeError(
            "v3 phase checkpoint input descriptor is invalid".to_owned(),
        ));
    }
    if phase_descriptor.get("path") != phase_meta.get("phase_inputs_path")
        || phase_descriptor.get("sha256") != phase_meta.get("phase_inputs_sha256")
        || exact_int(&phase_descriptor["row_count"]) != i64::try_from(phase_rows.len()).ok()
        || exact_int(&phase_descriptor["byte_length"]) != i64::try_from(phase_raw.len()).ok()
        || phase_descriptor["canonicalization"].as_str()
            != Some("UTF-8 JSON; sorted keys; compact separators; trailing LF per record")
        || phase_descriptor["case_digest"].as_str()
            != Some("SHA-256 over each full canonical envelope without trailing LF")
    {
        return Err(SharedRuntimeError(
            "v3 phase checkpoint input binding differs".to_owned(),
        ));
    }
    let expected_phase_index: Vec<Value> = manifest_index[..phase_rows.len()]
        .iter()
        .map(|row| {
            let mut m = Map::new();
            for k in [
                "case_id",
                "canonical_case_sha256",
                "canonical_case_bytes",
                "episode_id",
                "phase_index",
                "suite_id",
            ] {
                m.insert(k.to_owned(), row[k].clone());
            }
            Value::Object(m)
        })
        .collect();
    if checkpoint.get("phase_case_index").and_then(Value::as_array) != Some(&expected_phase_index) {
        return Err(SharedRuntimeError(
            "v3 phase checkpoint index differs from the corpus prefix".to_owned(),
        ));
    }

    let counts = manifest_value
        .get("counts")
        .and_then(Value::as_object)
        .ok_or_else(|| SharedRuntimeError("v3 manifest counts fields differ".to_owned()))?;
    let count_fields: BTreeSet<&str> = [
        "episodes",
        "operations",
        "phase_cases",
        "phase_target_tuples",
        "recipe_cases",
        "total_cases",
        "trace_cases",
    ]
    .into_iter()
    .collect();
    if counts.keys().map(String::as_str).collect::<BTreeSet<_>>() != count_fields {
        return Err(SharedRuntimeError(
            "v3 manifest counts fields differ".to_owned(),
        ));
    }
    let declared_ops = counts["operations"].as_object().ok_or_else(|| {
        SharedRuntimeError("v3 manifest counts differ from the reconstructed sources".to_owned())
    })?;
    let expected_ops_keys: BTreeSet<&str> =
        FULL_SHARED_CONFORMANCE_OPERATIONS.into_iter().collect();
    let act_ops_keys: BTreeSet<&str> = declared_ops.keys().map(String::as_str).collect();
    let ops_match = act_ops_keys == expected_ops_keys
        && FULL_SHARED_CONFORMANCE_OPERATIONS.iter().all(|name| {
            let expected = operation_counts.get(*name).copied().unwrap_or(0);
            expected > 0 && exact_int(&declared_ops[*name]) == Some(expected)
        });
    if exact_int(&counts["total_cases"]) != i64::try_from(cases.len()).ok()
        || !ops_match
        || exact_int(&counts["episodes"]) != i64::try_from(phase_episode_ids.len()).ok()
        || exact_int(&counts["phase_cases"]) != i64::try_from(phase_rows.len()).ok()
        || exact_int(&counts["recipe_cases"]) != i64::try_from(recipe_rows.len()).ok()
        || exact_int(&counts["trace_cases"]) != i64::try_from(trace_rows.len()).ok()
        || exact_int(&counts["phase_target_tuples"]).is_none_or(|c| c < 0)
    {
        return Err(SharedRuntimeError(
            "v3 manifest counts differ from the reconstructed sources".to_owned(),
        ));
    }
    let phase_targets = checkpoint
        .get("phase_target_index")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            SharedRuntimeError("v3 phase target index shape or count differs".to_owned())
        })?;
    let target_keys: BTreeSet<&str> = [
        "episode_id",
        "mutation_kind",
        "phase_index",
        "queue_field",
        "suite_id",
        "target_entity_id",
    ]
    .into_iter()
    .collect();
    if i64::try_from(phase_targets.len()).ok() != exact_int(&counts["phase_target_tuples"])
        || phase_targets.iter().any(|r| {
            r.as_object().is_none_or(|m| {
                m.keys().map(String::as_str).collect::<BTreeSet<_>>() != target_keys
            })
        })
    {
        return Err(SharedRuntimeError(
            "v3 phase target index shape or count differs".to_owned(),
        ));
    }
    if cases.len() != case_inventory.len() || !is_sha256(case_manifest_sha256) {
        return Err(SharedRuntimeError(
            "full v3 corpus is incomplete".to_owned(),
        ));
    }
    if case_index_sha256 != self::case_index_sha256(&derived_inventory)? {
        return Err(SharedRuntimeError(
            "full v3 case-index digest differs from the complete inventory".to_owned(),
        ));
    }
    let mut deduped = Vec::new();
    let mut seen_bound = BTreeSet::new();
    for item in bound {
        if seen_bound.insert(item.0.clone()) {
            deduped.push(item);
        }
    }
    Ok((scope_id.to_owned(), deduped))
}

#[allow(clippy::too_many_arguments)]
pub fn verify_diagnostic_phase_sidecar(
    root: &Path,
    value: &Value,
    scope: &Map<String, Value>,
    public_inputs_ref: &Map<String, Value>,
    input_artifacts: &Map<String, Value>,
    case_inventory: &[(String, String)],
    case_manifest_sha256: &str,
    input_rows: &[Value],
) -> Result<VerifiedSidecar, SharedRuntimeError> {
    let ref_obj = value
        .as_object()
        .filter(|m| m.len() == 2 && m.contains_key("path") && m.contains_key("sha256"))
        .ok_or_else(|| {
            SharedRuntimeError("protocol sidecar reference differs from the v2 contract".to_owned())
        })?;
    let scope_id = scope["scope_id"].as_str().unwrap_or("");
    let binding = diagnostic_phase_batch_scope(scope_id)
        .ok_or_else(|| SharedRuntimeError("unknown diagnostic phase batch scope".to_owned()))?;
    if ref_obj["path"].as_str() != Some(binding.protocol_sidecar_path)
        || ref_obj["sha256"].as_str() != Some(binding.protocol_sidecar_sha256)
    {
        return Err(SharedRuntimeError(
            "protocol sidecar reference differs from the declared scope".to_owned(),
        ));
    }
    let sidecar_path = resolve_result_artifact(root, binding.protocol_sidecar_path)?;
    let raw = verify_readonly_digest(&sidecar_path, binding.protocol_sidecar_sha256)?;
    let sidecar_val = strict_json_loads(&raw)?;
    let sidecar = sidecar_val.as_object().ok_or_else(|| {
        SharedRuntimeError("protocol sidecar fields differ from the pinned contract".to_owned())
    })?;
    let sidecar_fields: BTreeSet<&str> = [
        "format",
        "materialization_contract_id",
        "protocol",
        "inputs",
        "execution_scope",
        "evaluator_captures",
        "protocol_review",
        "runtime_binding",
    ]
    .into_iter()
    .collect();
    if sidecar.keys().map(String::as_str).collect::<BTreeSet<_>>() != sidecar_fields {
        return Err(SharedRuntimeError(
            "protocol sidecar fields differ from the pinned contract".to_owned(),
        ));
    }
    if sidecar["format"].as_str() != Some("identitybench.v5.phase-only-neutral-protocol-sidecar.v1")
        || sidecar["materialization_contract_id"].as_str()
            != Some("identitybench.v5.materialization-contract.v2")
        || sidecar["protocol"].as_str() != Some(PROTOCOL)
    {
        return Err(SharedRuntimeError(
            "protocol sidecar identity differs".to_owned(),
        ));
    }
    let inputs = sidecar["inputs"]
        .as_object()
        .ok_or_else(|| SharedRuntimeError("protocol sidecar input binding differs".to_owned()))?;
    let input_fields: BTreeSet<&str> = [
        "case_inventory",
        "case_manifest_sha256",
        "manifest_path",
        "manifest_sha256",
        "phase_checkpoint_path",
        "phase_checkpoint_sha256",
        "public_inputs_path",
        "public_inputs_sha256",
    ]
    .into_iter()
    .collect();
    if inputs.keys().map(String::as_str).collect::<BTreeSet<_>>() != input_fields {
        return Err(SharedRuntimeError(
            "protocol sidecar input binding differs".to_owned(),
        ));
    }
    let inventory_json: Vec<Value> = case_inventory
        .iter()
        .map(|(cid, sha)| {
            let mut m = Map::new();
            m.insert("case_id".to_owned(), Value::String(cid.clone()));
            m.insert("input_sha256".to_owned(), Value::String(sha.clone()));
            Value::Object(m)
        })
        .collect();
    if inputs["case_inventory"].as_array() != Some(&inventory_json)
        || inputs["case_manifest_sha256"].as_str() != Some(case_manifest_sha256)
        || inputs.get("public_inputs_sha256") != public_inputs_ref.get("sha256")
    {
        return Err(SharedRuntimeError(
            "protocol sidecar case inventory differs".to_owned(),
        ));
    }
    let mut bound = vec![(sidecar_path, binding.protocol_sidecar_sha256.to_owned())];
    for (name, sidecar_sha, expected_sha) in [
        (
            "manifest",
            inputs["manifest_sha256"].as_str(),
            binding.input_manifest_sha256,
        ),
        (
            "phase_checkpoint",
            inputs["phase_checkpoint_sha256"].as_str(),
            binding.phase_checkpoint_sha256,
        ),
    ] {
        let r = input_artifacts
            .get(name)
            .and_then(Value::as_object)
            .filter(|m| m.len() == 2 && m.contains_key("path") && m.contains_key("sha256"))
            .ok_or_else(|| {
                SharedRuntimeError(format!("input artifact reference is invalid: {name}"))
            })?;
        let r_sha = r["sha256"].as_str();
        if r_sha != sidecar_sha || r_sha != Some(expected_sha) {
            return Err(SharedRuntimeError(format!(
                "protocol sidecar input digest differs: {name}"
            )));
        }
        let path = resolve_result_artifact(root, r["path"].as_str().unwrap_or(""))?;
        verify_readonly_digest(&path, expected_sha)?;
        bound.push((path, expected_sha.to_owned()));
    }

    let exec_scope = sidecar["execution_scope"].as_object().ok_or_else(|| {
        SharedRuntimeError("protocol sidecar execution scope fields differ".to_owned())
    })?;
    let exec_fields: BTreeSet<&str> = [
        "case_count",
        "evaluator_slots",
        "operation",
        "phase_counts",
        "phase_indices",
        "steering",
        "turn_context",
    ]
    .into_iter()
    .collect();
    if exec_scope
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>()
        != exec_fields
    {
        return Err(SharedRuntimeError(
            "protocol sidecar execution scope fields differ".to_owned(),
        ));
    }
    let mut phase_counts: BTreeMap<String, i64> = BTreeMap::new();
    for row in input_rows {
        let obj = row.as_object().ok_or_else(|| {
            SharedRuntimeError("diagnostic batch contains a non-derive input".to_owned())
        })?;
        if obj.get("operation").and_then(Value::as_str) != Some("derive_phase") {
            return Err(SharedRuntimeError(
                "diagnostic batch contains a non-derive input".to_owned(),
            ));
        }
        let p = obj.get("phase_index").and_then(exact_int);
        if p != Some(1) && p != Some(2) {
            return Err(SharedRuntimeError(
                "diagnostic batch contains an invalid phase index".to_owned(),
            ));
        }
        *phase_counts.entry(p.unwrap().to_string()).or_insert(0) += 1;
    }
    let mut expected_pc_map = Map::new();
    for (k, v) in phase_counts {
        expected_pc_map.insert(k, Value::Number(Number::from(v)));
    }
    let slots_ok = exec_scope["evaluator_slots"]
        .as_array()
        .and_then(|a| a.iter().map(Value::as_str).collect::<Option<Vec<_>>>())
        .is_some_and(|v| v == ["full_spec", "agent_view"]);
    let p_idx_ok = exec_scope["phase_indices"]
        .as_array()
        .and_then(|a| a.iter().map(exact_int).collect::<Option<Vec<_>>>())
        .is_some_and(|v| v == [1, 2]);
    if exact_int(&exec_scope["case_count"]) != i64::try_from(case_inventory.len()).ok()
        || !slots_ok
        || exec_scope["operation"].as_str() != Some("derive_phase")
        || exec_scope["phase_counts"].as_object() != Some(&expected_pc_map)
        || !p_idx_ok
        || exec_scope["steering"].as_str() != Some("UNSTEERED")
        || exec_scope["turn_context"].as_str() != Some("T0")
    {
        return Err(SharedRuntimeError(
            "protocol sidecar execution scope differs from public inputs".to_owned(),
        ));
    }
    for key in [
        "case_count",
        "operation",
        "phase_indices",
        "steering",
        "turn_context",
    ] {
        if exec_scope.get(key) != scope.get(key) {
            return Err(SharedRuntimeError(
                "declared diagnostic scope differs from protocol sidecar".to_owned(),
            ));
        }
    }

    let review = sidecar["protocol_review"]
        .as_object()
        .ok_or_else(|| SharedRuntimeError("protocol review sidecar fields differ".to_owned()))?;
    let rev_fields: BTreeSet<&str> = [
        "accepted_scope",
        "adapter_source_path",
        "adapter_source_sha256",
        "adapter_test_path",
        "adapter_test_sha256",
        "focused_test_count",
        "status",
    ]
    .into_iter()
    .collect();
    if review.keys().map(String::as_str).collect::<BTreeSet<_>>() != rev_fields {
        return Err(SharedRuntimeError(
            "protocol review sidecar fields differ".to_owned(),
        ));
    }
    if review["accepted_scope"].as_str() != Some("derive_phase_only")
        || review["adapter_test_path"].as_str() != Some("tests/v5/test_conformance_shared.py")
        || review["status"].as_str() != Some("accepted")
        || exact_int(&review["focused_test_count"]).is_none_or(|c| c <= 0)
        || !review["adapter_source_sha256"]
            .as_str()
            .is_some_and(is_sha256)
        || !review["adapter_test_sha256"]
            .as_str()
            .is_some_and(is_sha256)
    {
        return Err(SharedRuntimeError(
            "protocol review sidecar is not an accepted derive review".to_owned(),
        ));
    }
    let runtime_binding = sidecar["runtime_binding"]
        .as_object()
        .ok_or_else(|| SharedRuntimeError("protocol runtime binding fields differ".to_owned()))?;
    let rt_fields: BTreeSet<&str> = [
        "public_schema_path",
        "public_schema_sha256",
        "runtime_source_path",
        "runtime_source_sha256",
        "spec_path",
        "spec_sha256",
    ]
    .into_iter()
    .collect();
    if runtime_binding
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>()
        != rt_fields
    {
        return Err(SharedRuntimeError(
            "protocol runtime binding fields differ".to_owned(),
        ));
    }
    if !runtime_binding["public_schema_sha256"]
        .as_str()
        .is_some_and(is_sha256)
        || !runtime_binding["runtime_source_sha256"]
            .as_str()
            .is_some_and(is_sha256)
        || !runtime_binding["spec_sha256"]
            .as_str()
            .is_some_and(is_sha256)
    {
        return Err(SharedRuntimeError(
            "protocol runtime binding contains an invalid digest".to_owned(),
        ));
    }
    let captures = sidecar["evaluator_captures"]
        .as_object()
        .ok_or_else(|| SharedRuntimeError("protocol sidecar capture set differs".to_owned()))?;
    if captures.keys().map(String::as_str).collect::<BTreeSet<_>>()
        != BTreeSet::from(["full_spec", "agent_view"])
    {
        return Err(SharedRuntimeError(
            "protocol sidecar capture set differs".to_owned(),
        ));
    }
    Ok((sidecar.clone(), bound))
}

pub fn verify_sidecar_capture_binding(
    sidecar: &Map<String, Value>,
    evaluator_id: &str,
    capture_reference: &Map<String, Value>,
    capture_manifest: &Map<String, Value>,
    profile: &SourceProfile,
) -> Result<(), SharedRuntimeError> {
    let value = sidecar["evaluator_captures"]
        .as_object()
        .and_then(|c| c.get(evaluator_id))
        .and_then(Value::as_object)
        .ok_or_else(|| {
            SharedRuntimeError(format!("sidecar capture fields differ for {evaluator_id}"))
        })?;
    let required: BTreeSet<&str> = [
        "author_origin",
        "entrypoint",
        "entrypoint_module",
        "manifest_logical_sha256",
        "manifest_path",
        "manifest_raw_sha256",
        "source_files",
        "source_profile",
    ]
    .into_iter()
    .collect();
    if value.keys().map(String::as_str).collect::<BTreeSet<_>>() != required {
        return Err(SharedRuntimeError(format!(
            "sidecar capture fields differ for {evaluator_id}"
        )));
    }
    if value.get("manifest_path") != capture_reference.get("path")
        || value.get("manifest_raw_sha256") != capture_reference.get("sha256")
    {
        return Err(SharedRuntimeError(format!(
            "sidecar capture digest differs for {evaluator_id}"
        )));
    }
    if value.get("manifest_logical_sha256") != capture_manifest.get("manifest_sha256")
        || value["source_profile"].as_str() != Some(profile.profile_id)
        || value["author_origin"].as_str() != Some(profile.author_origin)
        || value["entrypoint"].as_str() != Some(profile.entrypoint)
        || value["entrypoint_module"].as_str() != profile.entrypoint_module
        || value.get("source_files") != capture_manifest.get("source_files")
    {
        return Err(SharedRuntimeError(format!(
            "sidecar capture identity differs for {evaluator_id}"
        )));
    }
    Ok(())
}

pub fn verify_sidecar_runtime_binding(
    sidecar: &Map<String, Value>,
    evaluator_id: &str,
    freeze: &Map<String, Value>,
    profile: &SourceProfile,
) -> Result<(), SharedRuntimeError> {
    let binding = sidecar["runtime_binding"].as_object().ok_or_else(|| {
        SharedRuntimeError(format!(
            "sidecar runtime binding differs for {evaluator_id}"
        ))
    })?;
    let mut files: BTreeMap<&str, &str> = BTreeMap::new();
    for key in ["approved_source_files", "runtime_inventory"] {
        if let Some(arr) = freeze.get(key).and_then(Value::as_array) {
            for rec in arr {
                if let Some(obj) = rec.as_object()
                    && let (Some(p), Some(s)) = (
                        obj.get("path").and_then(Value::as_str),
                        obj.get("sha256").and_then(Value::as_str),
                    )
                {
                    files.insert(p, s);
                }
            }
        }
    }
    for (path_key, sha_key, expected_path) in [
        (
            "public_schema_path",
            "public_schema_sha256",
            "src/identitybench/v5/schemas.py",
        ),
        (
            "runtime_source_path",
            "runtime_source_sha256",
            "src/identitybench/v5/conformance_process.py",
        ),
    ] {
        if binding.get(path_key).and_then(Value::as_str) != Some(expected_path)
            || files.get(expected_path).copied() != binding.get(sha_key).and_then(Value::as_str)
        {
            return Err(SharedRuntimeError(format!(
                "sidecar runtime binding differs for {evaluator_id}"
            )));
        }
    }
    if binding["spec_path"].as_str() != Some("SPEC-v5.md")
        || binding["spec_sha256"].as_str() != Some(SPEC_SHA256)
    {
        return Err(SharedRuntimeError(
            "sidecar normative-spec binding differs".to_owned(),
        ));
    }
    if profile.profile_id == "fresh_full_spec_v2"
        && files.get("SPEC-v5.md").copied() != binding["spec_sha256"].as_str()
    {
        return Err(SharedRuntimeError(
            "fresh full-spec archive differs from sidecar spec binding".to_owned(),
        ));
    }
    let review = sidecar["protocol_review"].as_object().ok_or_else(|| {
        SharedRuntimeError(format!(
            "sidecar adapter binding differs for {evaluator_id}"
        ))
    })?;
    let adapter_path = "src/identitybench/v5/conformance_shared.py";
    if review["adapter_source_path"].as_str() != Some(adapter_path)
        || files.get(adapter_path).copied() != review["adapter_source_sha256"].as_str()
    {
        return Err(SharedRuntimeError(format!(
            "sidecar adapter binding differs for {evaluator_id}"
        )));
    }
    Ok(())
}

pub fn verify_blind_freeze_receipt(
    receipt_path: &Path,
    expected_sha256: &str,
    repository_root: &Path,
) -> Result<VerifiedBlindFreeze, SharedRuntimeError> {
    let root = repository_root
        .canonicalize()
        .map_err(|exc| SharedRuntimeError(format!("repository root invalid: {exc}")))?;
    let rel_str = receipt_path
        .to_str()
        .ok_or_else(|| SharedRuntimeError("unsafe artifact path".to_owned()))?;
    let receipt_file = resolve_result_artifact(&root, rel_str)?;
    require_readonly_file(&receipt_file)?;
    let receipt_raw = std::fs::read(&receipt_file)
        .map_err(|exc| SharedRuntimeError(format!("failed reading receipt: {exc}")))?;
    let receipt_sha = sha256_bytes(&receipt_raw);
    if receipt_sha != expected_sha256 {
        return Err(SharedRuntimeError(
            "blind freeze receipt hash differs from the supplied reference".to_owned(),
        ));
    }
    let receipt_val = strict_json_loads(&receipt_raw)?;
    let receipt_value = receipt_val.as_object().ok_or_else(|| {
        SharedRuntimeError("blind freeze receipt must be a JSON object".to_owned())
    })?;
    let schema_version = receipt_value
        .get("schema_version")
        .and_then(Value::as_str)
        .unwrap_or("");
    let v1_required: BTreeSet<&str> = [
        "schema_version",
        "proof_scope",
        "case_inventory",
        "public_inputs",
        "evaluators",
        "runtime_attestation",
        "author_independence",
        "frozen_at_utc",
    ]
    .into_iter()
    .collect();
    let v2_required: BTreeSet<&str> = [
        "schema_version",
        "proof_scope",
        "scope",
        "case_inventory",
        "public_inputs",
        "input_artifacts",
        "protocol_sidecar",
        "evaluators",
        "runtime_attestation",
        "author_independence",
        "execution_classification",
        "frozen_at_utc",
    ]
    .into_iter()
    .collect();
    let v4_required: BTreeSet<&str> = v2_required
        .iter()
        .copied()
        .chain(["normative_contract"])
        .collect();

    let is_v1 = schema_version == "identitybench.v5.blind-output-freeze.v1";
    let is_v2 = schema_version == "identitybench.v5.blind-output-freeze.v2";
    let is_v3 = schema_version == "identitybench.v5.blind-output-freeze.v3";
    let is_v4 = schema_version == "identitybench.v5.blind-output-freeze.v4";
    let actual_keys: BTreeSet<&str> = receipt_value.keys().map(String::as_str).collect();
    if is_v1 && actual_keys != v1_required {
        return Err(SharedRuntimeError(
            "blind freeze receipt fields differ from the v1 contract".to_owned(),
        ));
    }
    if is_v2 && actual_keys != v2_required {
        return Err(SharedRuntimeError(
            "blind freeze receipt fields differ from the v2 contract".to_owned(),
        ));
    }
    if is_v3 && actual_keys != v2_required {
        return Err(SharedRuntimeError(
            "blind freeze receipt fields differ from the v3 contract".to_owned(),
        ));
    }
    if is_v4 && actual_keys != v4_required {
        return Err(SharedRuntimeError(
            "blind freeze receipt fields differ from the v4 contract".to_owned(),
        ));
    }
    if !is_v1 && !is_v2 && !is_v3 && !is_v4 {
        return Err(SharedRuntimeError(
            "unsupported blind freeze receipt schema".to_owned(),
        ));
    }
    if is_v4 {
        validate_v4_normative_contract(&receipt_value["normative_contract"])?;
    }
    let expected_result_root = if is_v4 {
        APPROVED_RESULT_ROOT
    } else {
        V1_RESULT_ROOT
    };
    let expected_root_path = root
        .join(expected_result_root)
        .canonicalize()
        .map_err(|_| {
            SharedRuntimeError(
                "blind freeze receipt is stored under the wrong versioned root".to_owned(),
            )
        })?;
    if !receipt_file.starts_with(&expected_root_path) {
        return Err(SharedRuntimeError(
            "blind freeze receipt is stored under the wrong versioned root".to_owned(),
        ));
    }
    let proof_scope = receipt_value["proof_scope"]
        .as_str()
        .ok_or_else(|| SharedRuntimeError("unknown blind freeze proof scope".to_owned()))?;
    let scope_allowed = if is_v1 {
        matches!(
            proof_scope,
            "diagnostic_phase_checkpoint" | "reviewed_shared_proof"
        )
    } else if is_v2 {
        proof_scope == "diagnostic_phase_batch"
    } else {
        proof_scope == "full_shared_conformance"
    };
    if !scope_allowed {
        return Err(SharedRuntimeError(
            "unknown blind freeze proof scope".to_owned(),
        ));
    }
    let frozen_at_utc = receipt_value["frozen_at_utc"]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            SharedRuntimeError("blind freeze receipt requires frozen_at_utc".to_owned())
        })?;
    let frozen_at_ns = if is_v2 || is_v3 || is_v4 {
        Some(parse_utc_timestamp_ns(frozen_at_utc)?)
    } else {
        None
    };

    let case_inventory = validate_case_inventory(&receipt_value["case_inventory"])?;
    let public_ref = receipt_value["public_inputs"]
        .as_object()
        .filter(|m| m.len() == 2 && m.contains_key("path") && m.contains_key("sha256"))
        .ok_or_else(|| {
            SharedRuntimeError("public_inputs must identify one hash-bound file".to_owned())
        })?;
    let public_path = resolve_result_artifact(&root, public_ref["path"].as_str().unwrap_or(""))?;
    let public_sha = public_ref["sha256"].as_str().unwrap_or("");
    let public_bytes = verify_readonly_digest(&public_path, public_sha)?;
    let input_rows = strict_jsonl_loads(&public_bytes)?;

    let mut input_by_id: BTreeMap<String, String> = BTreeMap::new();
    let mut input_cases: BTreeMap<String, Value> = BTreeMap::new();
    let mut input_inventory: Vec<(String, String)> = Vec::new();
    for val in &input_rows {
        if !val.is_object() {
            return Err(SharedRuntimeError(
                "public input JSONL row is not an object".to_owned(),
            ));
        }
        let case = validate_public_case(val)?;
        if input_by_id.contains_key(&case.case_id) {
            return Err(SharedRuntimeError(
                "public input file contains duplicate case IDs".to_owned(),
            ));
        }
        let digest = sha256_bytes(&canonical_json_bytes(&case.to_json_value())?);
        input_inventory.push((case.case_id.clone(), digest.clone()));
        input_by_id.insert(case.case_id.clone(), digest);
        input_cases.insert(case.case_id, val.clone());
    }

    let case_manifest_rows: Vec<Value> = case_inventory
        .iter()
        .map(|(cid, dig)| {
            let mut m = Map::new();
            m.insert("case_id".to_owned(), Value::String(cid.clone()));
            m.insert("input_sha256".to_owned(), Value::String(dig.clone()));
            Value::Object(m)
        })
        .collect();
    let mut case_manifest = Map::new();
    case_manifest.insert("protocol".to_owned(), Value::String(PROTOCOL.to_owned()));
    case_manifest.insert(
        "canonicalization".to_owned(),
        Value::String("canonical-json-v1".to_owned()),
    );
    case_manifest.insert("cases".to_owned(), Value::Array(case_manifest_rows));
    let case_manifest_sha = sha256_bytes(&canonical_json_bytes(&Value::Object(case_manifest))?);
    let case_index_sha = case_index_sha256(&case_inventory)?;

    let mut v3_input_artifacts: Vec<(PathBuf, String)> = Vec::new();
    let scope_id = if is_v1 {
        for (cid, dig) in &case_inventory {
            if input_by_id.get(cid) != Some(dig) {
                return Err(SharedRuntimeError(
                    "frozen case inventory differs from the public input file".to_owned(),
                ));
            }
        }
        validate_v1_proof_scope_count(proof_scope, case_inventory.len())?;
        format!("legacy-v1:{proof_scope}")
    } else if is_v2 {
        validate_diagnostic_phase_batch_scope(
            &receipt_value["scope"],
            &case_inventory,
            &input_inventory,
            public_ref,
        )?
    } else {
        let (sid, arts) = verify_full_shared_v3_inputs(
            &root,
            &receipt_value["scope"],
            &receipt_value["input_artifacts"],
            &public_path,
            &input_rows,
            &case_inventory,
            &case_manifest_sha,
            &case_index_sha,
        )?;
        v3_input_artifacts = arts;
        sid
    };

    if proof_scope == "diagnostic_phase_checkpoint" {
        let mut phases = BTreeSet::new();
        for (cid, _) in &case_inventory {
            let c = input_cases.get(cid).ok_or_else(|| {
                SharedRuntimeError(
                    "diagnostic checkpoint must contain one derive case per phase".to_owned(),
                )
            })?;
            if c.get("operation").and_then(Value::as_str) != Some("derive_phase") {
                return Err(SharedRuntimeError(
                    "diagnostic checkpoint must contain one derive case per phase".to_owned(),
                ));
            }
            if let Some(p) = c.get("phase_index").and_then(exact_int) {
                phases.insert(p);
            }
        }
        if phases != BTreeSet::from([1, 2]) {
            return Err(SharedRuntimeError(
                "diagnostic checkpoint must contain one derive case per phase".to_owned(),
            ));
        }
    }
    if proof_scope == "reviewed_shared_proof" {
        let inv_ids: BTreeSet<&str> = case_inventory.iter().map(|(c, _)| c.as_str()).collect();
        let inp_ids: BTreeSet<&str> = input_by_id.keys().map(String::as_str).collect();
        if inp_ids != inv_ids {
            return Err(SharedRuntimeError(
                "reviewed case inventory must cover the exact public input file".to_owned(),
            ));
        }
    }
    if proof_scope == "diagnostic_phase_batch" && case_inventory != input_inventory {
        return Err(SharedRuntimeError(
            "diagnostic batch inventory must exactly cover public inputs".to_owned(),
        ));
    }
    if proof_scope == "full_shared_conformance" && case_inventory != input_inventory {
        return Err(SharedRuntimeError(
            "full conformance inventory must exactly cover public inputs".to_owned(),
        ));
    }

    let mut bound_artifacts: Vec<(PathBuf, String)> = vec![(public_path, public_sha.to_owned())];
    bound_artifacts.extend(v3_input_artifacts);

    let mut sidecar_value: Option<Map<String, Value>> = None;
    if is_v2 {
        let input_artifacts = receipt_value["input_artifacts"]
            .as_object()
            .filter(|m| {
                m.len() == 2 && m.contains_key("manifest") && m.contains_key("phase_checkpoint")
            })
            .ok_or_else(|| {
                SharedRuntimeError("input_artifacts fields differ from the v2 contract".to_owned())
            })?;
        let binding = diagnostic_phase_batch_scope(&scope_id)
            .ok_or_else(|| SharedRuntimeError("unknown diagnostic phase batch scope".to_owned()))?;
        for name in ["manifest", "phase_checkpoint"] {
            let r = input_artifacts[name]
                .as_object()
                .filter(|m| m.len() == 2 && m.contains_key("path") && m.contains_key("sha256"))
                .ok_or_else(|| {
                    SharedRuntimeError(format!("input artifact reference is invalid: {name}"))
                })?;
            let (exp_path, exp_sha) = if name == "manifest" {
                (binding.input_manifest_path, binding.input_manifest_sha256)
            } else {
                (
                    binding.phase_checkpoint_path,
                    binding.phase_checkpoint_sha256,
                )
            };
            if r["path"].as_str() != Some(exp_path) || r["sha256"].as_str() != Some(exp_sha) {
                return Err(SharedRuntimeError(format!(
                    "input artifact binding differs from diagnostic scope: {name}"
                )));
            }
            let art_path = resolve_result_artifact(&root, exp_path)?;
            verify_readonly_digest(&art_path, exp_sha)?;
            bound_artifacts.push((art_path, exp_sha.to_owned()));
        }
        let scope_obj = receipt_value["scope"].as_object().expect("validated");
        let (sc, sc_arts) = verify_diagnostic_phase_sidecar(
            &root,
            &receipt_value["protocol_sidecar"],
            scope_obj,
            public_ref,
            input_artifacts,
            &case_inventory,
            &case_manifest_sha,
            &input_rows,
        )?;
        sidecar_value = Some(sc);
        bound_artifacts.extend(sc_arts);
        validate_execution_classification(proof_scope, &receipt_value["execution_classification"])?;
        if receipt_value["runtime_attestation"].as_str() != Some("diagnostic_unaccepted") {
            return Err(SharedRuntimeError(
                "diagnostic batch runtime attestation must remain unaccepted".to_owned(),
            ));
        }
        if receipt_value["author_independence"].as_str() != Some("pending_qualified_adjudication") {
            return Err(SharedRuntimeError(
                "diagnostic batch cannot claim author qualification".to_owned(),
            ));
        }
    } else if is_v3 || is_v4 {
        let (sc, sc_arts) = verify_full_shared_v3_protocol_sidecar(
            &root,
            &receipt_value["protocol_sidecar"],
            &scope_id,
        )?;
        sidecar_value = Some(sc);
        bound_artifacts.extend(sc_arts);
        validate_execution_classification(proof_scope, &receipt_value["execution_classification"])?;
        if receipt_value["runtime_attestation"].as_str() != Some("diagnostic_unaccepted") {
            return Err(SharedRuntimeError(
                "full conformance freeze runtime is not accepted".to_owned(),
            ));
        }
        if receipt_value["author_independence"].as_str() != Some("pending_qualified_adjudication") {
            return Err(SharedRuntimeError(
                "full conformance freeze cannot claim author qualification".to_owned(),
            ));
        }
    }

    let evaluator_values = receipt_value["evaluators"].as_object().ok_or_else(|| {
        SharedRuntimeError("blind freeze must bind both independent evaluators".to_owned())
    })?;
    if evaluator_values
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>()
        != BTreeSet::from(["full_spec", "agent_view"])
    {
        return Err(SharedRuntimeError(
            "blind freeze must bind both independent evaluators".to_owned(),
        ));
    }

    let mut closure_hashes: Vec<(String, String)> = Vec::new();
    for (evaluator_id, expected_wire_id) in [
        ("full_spec", "full_spec_blind"),
        ("agent_view", "agent_view_only"),
    ] {
        let item = evaluator_values[evaluator_id].as_object().ok_or_else(|| {
            SharedRuntimeError(format!(
                "blind freeze record fields differ for {evaluator_id}"
            ))
        })?;
        let required_item_keys: BTreeSet<&str> = [
            "wire_evaluator_id",
            "source_closure_sha256",
            "raw_receipts",
            "source_capture_manifest",
            "runtime_manifest",
        ]
        .into_iter()
        .collect();
        let v2_item_keys: BTreeSet<&str> = required_item_keys
            .iter()
            .copied()
            .chain([
                "source_profile",
                "author_origin",
                "raw_audits",
                "runtime_archive",
                "dependency_manifest",
            ])
            .collect();
        let v3_item_keys: BTreeSet<&str> = v2_item_keys
            .iter()
            .copied()
            .chain(["run_manifest", "qualification"])
            .collect();
        let item_keys: BTreeSet<&str> = item.keys().map(String::as_str).collect();
        let keys_ok = if is_v1 {
            item_keys == required_item_keys
                || item_keys
                    == required_item_keys
                        .iter()
                        .copied()
                        .chain(["source_profile", "author_origin"])
                        .collect()
        } else if is_v3 || is_v4 {
            item_keys == v3_item_keys
        } else {
            item_keys == v2_item_keys
        };
        if !keys_ok {
            return Err(SharedRuntimeError(format!(
                "blind freeze record fields differ for {evaluator_id}"
            )));
        }

        let source_profile = item
            .get("source_profile")
            .and_then(Value::as_str)
            .unwrap_or("legacy_diagnostic");
        let profile = resolve_source_profile(evaluator_id, Some(source_profile))?;
        if is_v4 && evaluator_id == "full_spec" && source_profile != V5_APPROVED_FULL_SPEC_PROFILE {
            return Err(SharedRuntimeError(
                "v4 freeze requires the approved addendum full-spec profile".to_owned(),
            ));
        }
        if is_v4
            && evaluator_id == "agent_view"
            && profile
                .source_files
                .iter()
                .any(|p| p == V5_APPROVED_ADDENDUM_PATH || p == V5_APPROVED_DIRECTIVE_PATH)
        {
            return Err(SharedRuntimeError(
                "agent-view source closure includes normative-only files".to_owned(),
            ));
        }
        if is_v3 && source_profile == V5_APPROVED_FULL_SPEC_PROFILE {
            return Err(SharedRuntimeError(
                "approved addendum profile requires a v4 freeze receipt".to_owned(),
            ));
        }
        let author_origin = item
            .get("author_origin")
            .and_then(Value::as_str)
            .unwrap_or(profile.author_origin);
        if author_origin != profile.author_origin {
            return Err(SharedRuntimeError(format!(
                "author origin differs from the pinned profile for {evaluator_id}"
            )));
        }
        if proof_scope == "reviewed_shared_proof"
            && evaluator_id == "full_spec"
            && source_profile != "fresh_full_spec_v2"
        {
            return Err(SharedRuntimeError(
                "reviewed full-spec proof requires the fresh author profile".to_owned(),
            ));
        }
        if item["wire_evaluator_id"].as_str() != Some(expected_wire_id) {
            return Err(SharedRuntimeError(format!(
                "raw wire identity mismatch for {evaluator_id}"
            )));
        }
        let closure_sha = item["source_closure_sha256"].as_str().unwrap_or("");
        if !is_sha256(closure_sha) {
            return Err(SharedRuntimeError(format!(
                "source closure digest is invalid for {evaluator_id}"
            )));
        }
        closure_hashes.push((evaluator_id.to_owned(), closure_sha.to_owned()));

        let capture_artifacts = verify_capture_reference(
            &root,
            &item["source_capture_manifest"],
            evaluator_id,
            Some(&profile),
        )?;
        if is_v2 {
            let sc = sidecar_value.as_ref().ok_or_else(|| {
                SharedRuntimeError("diagnostic protocol sidecar was not verified".to_owned())
            })?;
            let cap_ref = item["source_capture_manifest"]
                .as_object()
                .expect("verified");
            let cap_path = resolve_result_artifact(&root, cap_ref["path"].as_str().unwrap_or(""))?;
            let cap_manifest_val = strict_json_loads(
                &std::fs::read(&cap_path).map_err(|e| SharedRuntimeError(e.to_string()))?,
            )?;
            let cap_manifest = cap_manifest_val.as_object().ok_or_else(|| {
                SharedRuntimeError("source capture manifest is not an object".to_owned())
            })?;
            verify_sidecar_capture_binding(sc, evaluator_id, cap_ref, cap_manifest, &profile)?;
        }

        let mut runtime_artifacts = verify_runtime_reference(
            &root,
            &item["runtime_manifest"],
            evaluator_id,
            closure_sha,
            &item["source_capture_manifest"],
            case_inventory.len(),
            Some(&profile),
            is_v2 || is_v3 || is_v4,
        )?;
        if is_v2 || is_v3 || is_v4 {
            let rt_ref = item["runtime_manifest"].as_object().expect("verified");
            let rt_path = resolve_result_artifact(&root, rt_ref["path"].as_str().unwrap_or(""))?;
            let rt_val = strict_json_loads(
                &std::fs::read(&rt_path).map_err(|e| SharedRuntimeError(e.to_string()))?,
            )?;
            let freeze = rt_val.as_object().ok_or_else(|| {
                SharedRuntimeError(format!("runtime manifest is invalid for {evaluator_id}"))
            })?;
            if is_v2 {
                let sc = sidecar_value.as_ref().expect("checked");
                verify_sidecar_runtime_binding(sc, evaluator_id, freeze, &profile)?;
            }
            let rt_archive_arts = verify_runtime_archive_reference(
                &root,
                &item["runtime_archive"],
                evaluator_id,
                freeze,
                closure_sha,
                &case_manifest_sha,
            )?;
            let dep_arts = verify_dependency_archive_reference(
                &root,
                &item["dependency_manifest"],
                evaluator_id,
                freeze,
                &profile,
            )?;
            runtime_artifacts.extend(rt_archive_arts);
            runtime_artifacts.extend(dep_arts);
        }

        let raw_ref = item["raw_receipts"]
            .as_object()
            .filter(|m| {
                m.len() == 3
                    && m.contains_key("path")
                    && m.contains_key("sha256")
                    && m.contains_key("row_count")
            })
            .ok_or_else(|| {
                SharedRuntimeError(format!(
                    "raw receipt reference is invalid for {evaluator_id}"
                ))
            })?;
        let row_count = exact_int(&raw_ref["row_count"]);
        if (is_v3 || is_v4) && row_count.is_none() {
            return Err(SharedRuntimeError(format!(
                "full conformance receipt count is invalid for {evaluator_id}"
            )));
        }
        let raw_path = resolve_result_artifact(&root, raw_ref["path"].as_str().unwrap_or(""))?;
        let raw_sha = raw_ref["sha256"].as_str().unwrap_or("");
        let raw_bytes = verify_readonly_digest(&raw_path, raw_sha)?;
        let rows = strict_jsonl_loads(&raw_bytes)?;
        if i64::try_from(rows.len()).ok() != row_count || rows.len() != case_inventory.len() {
            return Err(SharedRuntimeError(format!(
                "raw receipt count differs for {evaluator_id}"
            )));
        }
        verify_evaluator_receipts(
            &rows,
            evaluator_id,
            closure_sha,
            &case_inventory,
            &case_manifest_sha,
            expected_wire_id,
            proof_scope,
            &profile,
            is_v3 || is_v4,
        )?;

        if is_v2 || is_v3 || is_v4 {
            let audit_ref = item["raw_audits"]
                .as_object()
                .filter(|m| {
                    m.len() == 3
                        && m.contains_key("path")
                        && m.contains_key("sha256")
                        && m.contains_key("row_count")
                })
                .ok_or_else(|| {
                    SharedRuntimeError(format!("raw audit reference is invalid for {evaluator_id}"))
                })?;
            let audit_count = exact_int(&audit_ref["row_count"]);
            if (is_v3 || is_v4) && audit_count.is_none() {
                return Err(SharedRuntimeError(format!(
                    "full conformance audit count is invalid for {evaluator_id}"
                )));
            }
            let audit_path =
                resolve_result_artifact(&root, audit_ref["path"].as_str().unwrap_or(""))?;
            let audit_sha = audit_ref["sha256"].as_str().unwrap_or("");
            let audit_bytes = verify_readonly_digest(&audit_path, audit_sha)?;
            let audit_rows = strict_jsonl_loads(&audit_bytes)?;
            if i64::try_from(audit_rows.len()).ok() != audit_count {
                return Err(SharedRuntimeError(format!(
                    "raw audit count differs for {evaluator_id}"
                )));
            }
            verify_evaluator_audits(&audit_rows, &rows, evaluator_id)?;
            bound_artifacts.push((audit_path, audit_sha.to_owned()));

            if is_v3 || is_v4 {
                let run_arts = verify_full_shared_v3_run_manifest(
                    &root,
                    &item["run_manifest"],
                    evaluator_id,
                    closure_sha,
                    &case_manifest_sha,
                    raw_ref,
                    audit_ref,
                    case_inventory.len(),
                )?;
                let qual_art = verify_opaque_metadata_reference(
                    &root,
                    &item["qualification"],
                    &format!("qualification:{evaluator_id}"),
                )?;
                bound_artifacts.extend(run_arts);
                bound_artifacts.push(qual_art);
            }
        }
        bound_artifacts.extend(capture_artifacts);
        bound_artifacts.extend(runtime_artifacts);
        bound_artifacts.push((raw_path, raw_sha.to_owned()));
    }

    let rt_att = receipt_value["runtime_attestation"].as_str().unwrap_or("");
    let auth_ind = receipt_value["author_independence"].as_str().unwrap_or("");
    if proof_scope == "reviewed_shared_proof" {
        if rt_att != "reviewed" {
            return Err(SharedRuntimeError(
                "reviewed proof scope requires reviewed runtime attestation".to_owned(),
            ));
        }
        if auth_ind != "qualified_reviewed" {
            return Err(SharedRuntimeError(
                "reviewed proof scope requires qualified author-independence review".to_owned(),
            ));
        }
    } else if rt_att != "diagnostic_unaccepted" {
        return Err(SharedRuntimeError(
            "diagnostic scope must remain explicitly unaccepted".to_owned(),
        ));
    }
    if auth_ind != "pending_qualified_adjudication" && auth_ind != "qualified_reviewed" {
        return Err(SharedRuntimeError(
            "blind freeze author-independence state is invalid".to_owned(),
        ));
    }
    if (proof_scope == "diagnostic_phase_checkpoint" || proof_scope == "diagnostic_phase_batch")
        && auth_ind != "pending_qualified_adjudication"
    {
        let msg = if proof_scope == "diagnostic_phase_batch" {
            "diagnostic batch cannot claim qualified author independence"
        } else {
            "diagnostic scope cannot claim qualified author independence"
        };
        return Err(SharedRuntimeError(msg.to_owned()));
    }

    let v1_root = root.join(V1_RESULT_ROOT).canonicalize().ok();
    let approved_root = root.join(APPROVED_RESULT_ROOT).canonicalize().ok();
    for (p, _) in &bound_artifacts {
        let resolved_p = p.canonicalize().unwrap_or_else(|_| p.clone());
        let in_versioned = v1_root.as_ref().is_some_and(|r| resolved_p.starts_with(r))
            || approved_root
                .as_ref()
                .is_some_and(|r| resolved_p.starts_with(r));
        if in_versioned && !resolved_p.starts_with(&expected_root_path) {
            return Err(SharedRuntimeError(
                "blind freeze references artifacts from another versioned root".to_owned(),
            ));
        }
    }
    verify_readonly_digest(&receipt_file, &receipt_sha)?;
    if let Some(ns) = frozen_at_ns {
        verify_artifact_chronology(ns, &bound_artifacts)?;
    }
    bound_artifacts.push((receipt_file.clone(), receipt_sha.clone()));

    let mut seen_art = BTreeSet::new();
    let mut dedup_arts = Vec::with_capacity(bound_artifacts.len());
    for pair in bound_artifacts {
        if seen_art.insert(pair.clone()) {
            dedup_arts.push(pair);
        }
    }

    Ok(VerifiedBlindFreeze {
        receipt_path: receipt_file,
        repository_root: root,
        receipt_sha256: receipt_sha,
        proof_scope: proof_scope.to_owned(),
        scope_id,
        case_index_sha256: case_index_sha,
        case_manifest_sha256: case_manifest_sha,
        case_inventory,
        source_closure_sha256: closure_hashes,
        normative_contract_id: if is_v4 {
            Some(V5_APPROVED_CONFORMANCE_CONTRACT_ID.to_owned())
        } else {
            None
        },
        bound_artifacts: dedup_arts,
    })
}
