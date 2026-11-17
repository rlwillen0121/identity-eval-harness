//! Source closure capture, frozen-case manifests, and diagnostic comparison contracts.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use ib_v5_schemas::normative_contract::{
    V5_APPROVED_CONFORMANCE_CONTRACT_ID, V5_APPROVED_FULL_SPEC_AUTHOR_ORIGIN,
    V5_APPROVED_FULL_SPEC_PROFILE,
};
use serde_json::{Map, Value};

use super::SharedRuntimeError;
use super::paths::{make_readonly, remove_readonly_tree};
use super::profiles::{
    APPROVED_RESULT_ROOT, PROTOCOL, SCHEMA_SHA256, SPEC_SHA256, SourceProfile,
    current_v5_normative_contract, profile_normative_contract, profile_pinned_source_hashes,
    resolve_source_profile, validate_relative_source,
};
use super::verify::validate_audit_receipt;
use crate::shared::{
    FrozenCase, base64_decode_strict, base64_encode, canonical_json_bytes, freeze_case, is_sha256,
    json_diff_paths, materialization_hash, sha256_bytes, sha256_json, strict_json_loads,
    validate_evaluator_result, validate_public_case,
};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FrozenCaseInventory {
    pub cases_by_id: BTreeMap<String, Vec<u8>>,
    pub hashes: BTreeMap<String, String>,
    pub ordered_ids: Vec<String>,
    pub case_manifest_sha256: String,
}

pub fn freeze_cases(public_cases: &[Value]) -> Result<FrozenCaseInventory, SharedRuntimeError> {
    let mut cases_by_id = BTreeMap::new();
    let mut hashes = BTreeMap::new();
    let mut ordered_ids = Vec::with_capacity(public_cases.len());
    let mut ordered_cases = Vec::with_capacity(public_cases.len());

    for case_obj in public_cases {
        let case = validate_public_case(case_obj)?;
        let case_id = case.case_id.clone();
        if cases_by_id.contains_key(&case_id) {
            return Err(SharedRuntimeError(format!(
                "duplicate public case id: {case_id}"
            )));
        }
        if case.schema_sha256 != SCHEMA_SHA256 {
            return Err(SharedRuntimeError(
                "public-case schema_sha256 is not the frozen schema".to_owned(),
            ));
        }
        let raw = canonical_json_bytes(&case.to_json_value())?;
        let digest = sha256_bytes(&raw);
        cases_by_id.insert(case_id.clone(), raw);
        hashes.insert(case_id.clone(), digest.clone());
        ordered_ids.push(case_id.clone());
        let mut entry = Map::new();
        entry.insert("case_id".to_owned(), Value::String(case_id));
        entry.insert("input_sha256".to_owned(), Value::String(digest));
        ordered_cases.push(Value::Object(entry));
    }
    if cases_by_id.is_empty() {
        return Err(SharedRuntimeError(
            "the frozen public-case corpus must not be empty".to_owned(),
        ));
    }
    let mut manifest = Map::new();
    manifest.insert("protocol".to_owned(), Value::String(PROTOCOL.to_owned()));
    manifest.insert(
        "canonicalization".to_owned(),
        Value::String("canonical-json-v1".to_owned()),
    );
    manifest.insert("cases".to_owned(), Value::Array(ordered_cases));
    let manifest_sha = sha256_bytes(&canonical_json_bytes(&Value::Object(manifest))?);
    Ok(FrozenCaseInventory {
        cases_by_id,
        hashes,
        ordered_ids,
        case_manifest_sha256: manifest_sha,
    })
}

pub fn capture_source_closure(
    evaluator_id: &str,
    source_profile: Option<&str>,
    approved_paths: Option<&[String]>,
    repository_root: &Path,
    result_root: Option<&Path>,
) -> Result<Value, SharedRuntimeError> {
    let profile = resolve_source_profile(evaluator_id, source_profile)?;
    if let Some(paths) = approved_paths
        && paths != profile.source_files.as_slice()
    {
        return Err(SharedRuntimeError(
            "capture paths must match the exact evaluator allowlist".to_owned(),
        ));
    }
    let root = repository_root
        .canonicalize()
        .map_err(|exc| SharedRuntimeError(format!("repository root invalid: {exc}")))?;
    let target_root = match result_root {
        Some(p) => p.to_path_buf(),
        None => {
            if profile.profile_id == "legacy_diagnostic" {
                root.join("reports/v5/conformance/shared_results/v1/phase-checkpoint")
                    .join(evaluator_id)
            } else {
                let results_root = if profile.normative_contract_id.is_some()
                    || profile.profile_id == "production_v4"
                {
                    APPROVED_RESULT_ROOT
                } else {
                    "reports/v5/conformance/shared_results/v1"
                };
                root.join(results_root)
                    .join("phase-checkpoint/source-capture")
                    .join(evaluator_id)
                    .join(profile.profile_id)
            }
        }
    };

    let mut before: BTreeMap<String, Vec<u8>> = BTreeMap::new();
    for relative in &profile.source_files {
        validate_relative_source(relative, Some(evaluator_id))?;
        let source = root.join(relative);
        let meta = std::fs::symlink_metadata(&source).map_err(|_| {
            SharedRuntimeError(format!(
                "approved source is missing or symlinked: {relative}"
            ))
        })?;
        if meta.file_type().is_symlink() || !meta.file_type().is_file() {
            return Err(SharedRuntimeError(format!(
                "approved source is missing or symlinked: {relative}"
            )));
        }
        let bytes = std::fs::read(&source).map_err(|_| {
            SharedRuntimeError(format!(
                "approved source is missing or symlinked: {relative}"
            ))
        })?;
        before.insert(relative.clone(), bytes);
    }

    if evaluator_id == "full_spec"
        && let Some(spec_bytes) = before.get("SPEC-v5.md")
        && sha256_bytes(spec_bytes) != SPEC_SHA256
    {
        return Err(SharedRuntimeError(
            "SPEC-v5.md changed from its approved full-spec hash".to_owned(),
        ));
    }
    if let Some(contract) = profile_normative_contract(&profile)? {
        for (key, item) in &contract {
            if key == "contract_id" {
                continue;
            }
            if let Some(obj) = item.as_object() {
                let path = obj.get("path").and_then(Value::as_str).unwrap_or("");
                let digest = obj.get("sha256").and_then(Value::as_str).unwrap_or("");
                let actual = before.get(path).map(|b| sha256_bytes(b));
                if actual.as_deref() != Some(digest) {
                    return Err(SharedRuntimeError(
                        "captured normative source differs from the approved contract".to_owned(),
                    ));
                }
            }
        }
    }
    for (path, digest) in profile_pinned_source_hashes(&profile)? {
        if before.get(&path).map(|b| sha256_bytes(b)).as_deref() != Some(digest.as_str()) {
            return Err(SharedRuntimeError(
                "captured source differs from its approved profile pin".to_owned(),
            ));
        }
    }
    let schema_path = "src/identitybench/v5/schemas.py";
    let schema_bytes = before.get(schema_path).ok_or_else(|| {
        SharedRuntimeError(
            "source closure does not capture the public schemas.py dependency".to_owned(),
        )
    })?;
    let schema_hash = sha256_bytes(schema_bytes);
    if schema_hash != SCHEMA_SHA256 {
        return Err(SharedRuntimeError(
            "schemas.py changed from the public-case schema hash".to_owned(),
        ));
    }

    if target_root.exists() {
        return Err(SharedRuntimeError(format!(
            "source checkpoint already exists: {}",
            target_root.display()
        )));
    }
    std::fs::create_dir_all(&target_root)
        .map_err(|exc| SharedRuntimeError(format!("failed creating source capture dir: {exc}")))?;

    let write_res = (|| -> Result<Value, SharedRuntimeError> {
        let mut records = Vec::with_capacity(profile.source_files.len());
        for relative in &profile.source_files {
            let raw = &before[relative];
            let dest = target_root.join(relative);
            if let Some(parent) = dest.parent() {
                std::fs::create_dir_all(parent)
                    .map_err(|exc| SharedRuntimeError(format!("mkdir failed: {exc}")))?;
            }
            std::fs::write(&dest, raw)
                .map_err(|exc| SharedRuntimeError(format!("write failed: {exc}")))?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = std::fs::set_permissions(&dest, std::fs::Permissions::from_mode(0o444));
            }
            let staged = std::fs::read(&dest)
                .map_err(|exc| SharedRuntimeError(format!("readback failed: {exc}")))?;
            let digest = sha256_bytes(raw);
            if sha256_bytes(&staged) != digest {
                return Err(SharedRuntimeError(format!(
                    "copied source differs from capture bytes: {relative}"
                )));
            }
            let mut rec = Map::new();
            rec.insert("path".to_owned(), Value::String(relative.clone()));
            rec.insert("sha256".to_owned(), Value::String(digest));
            rec.insert(
                "size_bytes".to_owned(),
                Value::Number((raw.len() as u64).into()),
            );
            records.push(Value::Object(rec));
        }
        for relative in &profile.source_files {
            let raw = &before[relative];
            let source = root.join(relative);
            let meta = std::fs::symlink_metadata(&source).ok();
            let current = std::fs::read(&source).ok();
            if meta.is_none_or(|m| m.file_type().is_symlink() || !m.file_type().is_file())
                || current.as_ref() != Some(raw)
            {
                return Err(SharedRuntimeError(format!(
                    "source changed during checkpoint capture: {relative}"
                )));
            }
        }
        let mut manifest = Map::new();
        manifest.insert(
            "format".to_owned(),
            Value::String("identitybench.v5.shared-conformance-source-checkpoint.v1".to_owned()),
        );
        manifest.insert(
            "evaluator_id".to_owned(),
            Value::String(evaluator_id.to_owned()),
        );
        manifest.insert(
            "source_profile".to_owned(),
            Value::String(profile.profile_id.to_owned()),
        );
        manifest.insert(
            "author_origin".to_owned(),
            Value::String(profile.author_origin.to_owned()),
        );
        manifest.insert(
            "entrypoint".to_owned(),
            Value::String(profile.entrypoint.to_owned()),
        );
        manifest.insert(
            "entrypoint_module".to_owned(),
            match profile.entrypoint_module {
                Some(m) => Value::String(m.to_owned()),
                None => Value::Null,
            },
        );
        manifest.insert("source_files".to_owned(), Value::Array(records));
        manifest.insert("schema_sha256".to_owned(), Value::String(schema_hash));
        manifest.insert(
            "capture_status".to_owned(),
            Value::String("verified_source_copy".to_owned()),
        );
        let manifest_sha = sha256_bytes(&canonical_json_bytes(&Value::Object(manifest.clone()))?);
        manifest.insert("manifest_sha256".to_owned(), Value::String(manifest_sha));
        let mut out_bytes = canonical_json_bytes(&Value::Object(manifest.clone()))?;
        out_bytes.push(b'\n');
        let manifest_path = target_root.join("capture-manifest.json");
        std::fs::write(&manifest_path, &out_bytes)
            .map_err(|exc| SharedRuntimeError(format!("manifest write failed: {exc}")))?;
        make_readonly(&target_root)?;
        manifest.insert(
            "capture_path".to_owned(),
            Value::String(target_root.to_string_lossy().into_owned()),
        );
        Ok(Value::Object(manifest))
    })();

    if write_res.is_err() {
        remove_readonly_tree(&target_root);
    }
    write_res
}

#[derive(Debug, Clone, PartialEq)]
pub struct DiagnosticComparison {
    pub case_id: String,
    pub input_sha256: String,
    pub status: String,
    pub diagnostic_only: bool,
    pub g_spec_01: String,
    pub agent_view: String,
    pub author_independence: String,
    pub normative_contract_id: String,
    pub normative_contract_sha256: String,
    pub case_manifest_sha256: String,
    pub diagnostic_freeze_manifest_sha256: String,
    pub source_closure_sha256_by_evaluator: BTreeMap<String, String>,
    pub wire_evaluator_id_by_slot: BTreeMap<String, String>,
    pub execution_status_by_evaluator: BTreeMap<String, String>,
    pub payload_sha256_by_evaluator: BTreeMap<String, Option<String>>,
    pub materialization_sha256_by_evaluator: BTreeMap<String, Option<String>>,
    pub mismatch_paths: Vec<String>,
    pub gaps: Vec<Map<String, Value>>,
}

impl DiagnosticComparison {
    #[must_use]
    pub fn to_record(&self) -> Value {
        let mut map = Map::new();
        map.insert(
            "schema_version".to_owned(),
            Value::String(
                "identitybench.v5.full-spec-production-diagnostic-comparison.v1".to_owned(),
            ),
        );
        map.insert(
            "diagnostic_only".to_owned(),
            Value::Bool(self.diagnostic_only),
        );
        map.insert(
            "g_spec_01".to_owned(),
            Value::String(self.g_spec_01.clone()),
        );
        map.insert(
            "agent_view".to_owned(),
            Value::String(self.agent_view.clone()),
        );
        map.insert(
            "author_independence".to_owned(),
            Value::String(self.author_independence.clone()),
        );
        map.insert("case_id".to_owned(), Value::String(self.case_id.clone()));
        map.insert(
            "input_sha256".to_owned(),
            Value::String(self.input_sha256.clone()),
        );
        map.insert("status".to_owned(), Value::String(self.status.clone()));
        map.insert(
            "normative_contract_id".to_owned(),
            Value::String(self.normative_contract_id.clone()),
        );
        map.insert(
            "normative_contract_sha256".to_owned(),
            Value::String(self.normative_contract_sha256.clone()),
        );
        map.insert(
            "case_manifest_sha256".to_owned(),
            Value::String(self.case_manifest_sha256.clone()),
        );
        map.insert(
            "diagnostic_freeze_manifest_sha256".to_owned(),
            Value::String(self.diagnostic_freeze_manifest_sha256.clone()),
        );
        map.insert(
            "source_closure_sha256_by_evaluator".to_owned(),
            str_map_to_value(&self.source_closure_sha256_by_evaluator),
        );
        map.insert(
            "wire_evaluator_id_by_slot".to_owned(),
            str_map_to_value(&self.wire_evaluator_id_by_slot),
        );
        map.insert(
            "execution_status_by_evaluator".to_owned(),
            str_map_to_value(&self.execution_status_by_evaluator),
        );
        map.insert(
            "payload_sha256_by_evaluator".to_owned(),
            opt_str_map_to_value(&self.payload_sha256_by_evaluator),
        );
        map.insert(
            "materialization_sha256_by_evaluator".to_owned(),
            opt_str_map_to_value(&self.materialization_sha256_by_evaluator),
        );
        map.insert(
            "mismatch_paths".to_owned(),
            Value::Array(
                self.mismatch_paths
                    .iter()
                    .map(|s| Value::String(s.clone()))
                    .collect(),
            ),
        );
        map.insert(
            "gaps".to_owned(),
            Value::Array(self.gaps.iter().cloned().map(Value::Object).collect()),
        );
        Value::Object(map)
    }
}

fn str_map_to_value(m: &BTreeMap<String, String>) -> Value {
    let mut obj = Map::new();
    for (k, v) in m {
        obj.insert(k.clone(), Value::String(v.clone()));
    }
    Value::Object(obj)
}

fn opt_str_map_to_value(m: &BTreeMap<String, Option<String>>) -> Value {
    let mut obj = Map::new();
    for (k, v) in m {
        obj.insert(
            k.clone(),
            match v {
                Some(s) => Value::String(s.clone()),
                None => Value::Null,
            },
        );
    }
    Value::Object(obj)
}

#[allow(clippy::too_many_arguments)]
pub fn validate_diagnostic_outer_receipt<'a>(
    receipt: &'a Map<String, Value>,
    expected_evaluator_id: &str,
    expected_profile: &str,
    expected_origin: &str,
    expected_case: &FrozenCase,
    expected_closure_sha256: &str,
    expected_case_manifest_sha256: &str,
    diagnostic_wrapper: bool,
    expected_normative_contract: Option<&Map<String, Value>>,
    expected_diagnostic_manifest_sha256: Option<&str>,
) -> Result<&'a Map<String, Value>, SharedRuntimeError> {
    let execution = if diagnostic_wrapper {
        let required_wrapper_keys: BTreeSet<&str> = [
            "schema_version",
            "diagnostic_only",
            "g_spec_01",
            "agent_view",
            "author_independence",
            "normative_contract",
            "normative_contract_sha256",
            "diagnostic_freeze_manifest_sha256",
            "execution",
        ]
        .into_iter()
        .collect();
        let actual: BTreeSet<&str> = receipt.keys().map(String::as_str).collect();
        let expected_contract_sha = expected_normative_contract
            .map(|c| canonical_json_bytes(&Value::Object(c.clone())).map(|b| sha256_bytes(&b)))
            .transpose()?
            .unwrap_or_default();
        if actual != required_wrapper_keys
            || receipt.get("schema_version").and_then(Value::as_str)
                != Some("identitybench.v5.production-diagnostic-run.v1")
            || receipt.get("diagnostic_only").and_then(Value::as_bool) != Some(true)
            || receipt.get("g_spec_01").and_then(Value::as_str) != Some("not_evaluated")
            || receipt.get("agent_view").and_then(Value::as_str) != Some("outstanding")
            || receipt.get("author_independence").and_then(Value::as_str) != Some("not_asserted")
            || receipt.get("normative_contract").and_then(Value::as_object)
                != expected_normative_contract
            || receipt
                .get("normative_contract_sha256")
                .and_then(Value::as_str)
                != Some(expected_contract_sha.as_str())
            || receipt
                .get("diagnostic_freeze_manifest_sha256")
                .and_then(Value::as_str)
                != expected_diagnostic_manifest_sha256
        {
            return Err(SharedRuntimeError(
                "production diagnostic receipt markers or contract are invalid".to_owned(),
            ));
        }
        receipt
            .get("execution")
            .and_then(Value::as_object)
            .ok_or_else(|| {
                SharedRuntimeError("diagnostic execution receipt is malformed".to_owned())
            })?
    } else {
        if [
            "schema_version",
            "diagnostic_only",
            "g_spec_01",
            "agent_view",
            "diagnostic_freeze_manifest_sha256",
        ]
        .iter()
        .any(|k| receipt.contains_key(*k))
        {
            return Err(SharedRuntimeError(
                "full-spec result is not an ordinary frozen evaluator receipt".to_owned(),
            ));
        }
        receipt
    };

    let status = execution
        .get("execution_status")
        .and_then(Value::as_str)
        .unwrap_or("");
    if status != "ok" && status != "failed" {
        return Err(SharedRuntimeError(
            "diagnostic execution status is invalid".to_owned(),
        ));
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
        "audit",
        "stdout_sha256",
        "stderr_sha256",
        "raw_stdout_b64",
        "raw_stderr_b64",
    ]
    .into_iter()
    .collect();
    if status == "ok" {
        expected_fields.extend(["result", "result_sha256", "exit_code"]);
    } else {
        expected_fields.insert("failure");
    }
    let act_fields: BTreeSet<&str> = execution.keys().map(String::as_str).collect();
    if act_fields != expected_fields {
        return Err(SharedRuntimeError(
            "diagnostic execution receipt fields differ from its contract".to_owned(),
        ));
    }
    if execution["protocol"].as_str() != Some(PROTOCOL)
        || execution["evaluator_id"].as_str() != Some(expected_evaluator_id)
        || execution["source_profile"].as_str() != Some(expected_profile)
        || execution["author_origin"].as_str() != Some(expected_origin)
        || execution["case_id"].as_str() != Some(expected_case.case_id())
        || execution["input_sha256"].as_str() != Some(expected_case.input_sha256.as_str())
        || execution["source_closure_sha256"].as_str() != Some(expected_closure_sha256)
        || execution["case_manifest_sha256"].as_str() != Some(expected_case_manifest_sha256)
        || execution.contains_key("blind_freeze_receipt_sha256")
        || execution.contains_key("blind_freeze_scope")
    {
        return Err(SharedRuntimeError(
            "diagnostic execution identity does not match its frozen inputs".to_owned(),
        ));
    }

    let stdout =
        base64_decode_strict(execution["raw_stdout_b64"].as_str().unwrap_or("")).map_err(|_| {
            SharedRuntimeError("diagnostic execution raw output encoding is invalid".to_owned())
        })?;
    let stderr =
        base64_decode_strict(execution["raw_stderr_b64"].as_str().unwrap_or("")).map_err(|_| {
            SharedRuntimeError("diagnostic execution raw output encoding is invalid".to_owned())
        })?;
    if Some(sha256_bytes(&stdout).as_str()) != execution["stdout_sha256"].as_str()
        || Some(sha256_bytes(&stderr).as_str()) != execution["stderr_sha256"].as_str()
    {
        return Err(SharedRuntimeError(
            "diagnostic execution raw output hashes differ".to_owned(),
        ));
    }
    if status == "failed" {
        if execution["failure"].as_str().is_none_or(str::is_empty) {
            return Err(SharedRuntimeError(
                "failed diagnostic execution lacks a failure reason".to_owned(),
            ));
        }
        return Ok(execution);
    }

    if execution["exit_code"].as_i64() != Some(0)
        || execution["result_sha256"].as_str() != Some(sha256_bytes(&stdout).as_str())
        || strict_json_loads(&stdout).ok().as_ref() != execution.get("result")
        || strict_json_loads(&stderr).ok().as_ref() != execution.get("audit")
    {
        return Err(SharedRuntimeError(
            "successful diagnostic raw output does not match its receipt".to_owned(),
        ));
    }
    let audit = execution["audit"].as_object().ok_or_else(|| {
        SharedRuntimeError("diagnostic audit evaluator identity is invalid".to_owned())
    })?;
    if audit.get("evaluator_id").and_then(Value::as_str) != Some(expected_evaluator_id) {
        return Err(SharedRuntimeError(
            "diagnostic audit evaluator identity is invalid".to_owned(),
        ));
    }
    validate_audit_receipt(audit)?;
    Ok(execution)
}

#[allow(clippy::too_many_arguments)]
pub fn compare_diagnostic_receipt_pair(
    case: &FrozenCase,
    full_spec_closure_sha256: &str,
    production_closure_sha256: &str,
    case_manifest_sha256: &str,
    diagnostic_freeze_manifest_sha256: &str,
    full_spec_receipt: &Map<String, Value>,
    production_receipt: &Map<String, Value>,
) -> Result<DiagnosticComparison, SharedRuntimeError> {
    let verified_case = freeze_case(&case.public_case.to_json_value())?;
    if verified_case.canonical_bytes != case.canonical_bytes
        || verified_case.input_sha256 != case.input_sha256
    {
        return Err(SharedRuntimeError(
            "diagnostic comparison case bytes or digest are stale".to_owned(),
        ));
    }
    let expected_contract = current_v5_normative_contract()?;
    let contract_sha = sha256_bytes(&canonical_json_bytes(&Value::Object(
        expected_contract.clone(),
    ))?);
    if !is_sha256(full_spec_closure_sha256) || !is_sha256(production_closure_sha256) {
        return Err(SharedRuntimeError(
            "diagnostic source closure hash is malformed".to_owned(),
        ));
    }

    let full_exec = validate_diagnostic_outer_receipt(
        full_spec_receipt,
        "full_spec",
        V5_APPROVED_FULL_SPEC_PROFILE,
        V5_APPROVED_FULL_SPEC_AUTHOR_ORIGIN,
        &verified_case,
        full_spec_closure_sha256,
        case_manifest_sha256,
        false,
        None,
        None,
    )?;
    let prod_exec = validate_diagnostic_outer_receipt(
        production_receipt,
        "production",
        "production_v4",
        "production_adapter",
        &verified_case,
        production_closure_sha256,
        case_manifest_sha256,
        true,
        Some(&expected_contract),
        Some(diagnostic_freeze_manifest_sha256),
    )?;

    let mut source_hashes = BTreeMap::new();
    source_hashes.insert("full_spec".to_owned(), full_spec_closure_sha256.to_owned());
    source_hashes.insert(
        "production".to_owned(),
        production_closure_sha256.to_owned(),
    );

    let full_status = full_exec["execution_status"].as_str().unwrap_or("");
    let prod_status = prod_exec["execution_status"].as_str().unwrap_or("");
    let mut execution_statuses = BTreeMap::new();
    execution_statuses.insert("full_spec".to_owned(), full_status.to_owned());
    execution_statuses.insert("production".to_owned(), prod_status.to_owned());

    if full_status == "failed" || prod_status == "failed" {
        let mut gaps = Vec::new();
        for (slot, exec) in [("full_spec", full_exec), ("production", prod_exec)] {
            if exec["execution_status"].as_str() != Some("ok") {
                let mut g = Map::new();
                g.insert("evaluator_slot".to_owned(), Value::String(slot.to_owned()));
                g.insert(
                    "evaluator_id".to_owned(),
                    Value::String(if slot == "full_spec" {
                        "full_spec_blind".to_owned()
                    } else {
                        "production".to_owned()
                    }),
                );
                g.insert(
                    "code".to_owned(),
                    Value::String("EXECUTION_FAILED".to_owned()),
                );
                g.insert(
                    "detail".to_owned(),
                    Value::String(
                        exec.get("failure")
                            .and_then(Value::as_str)
                            .unwrap_or("execution did not complete")
                            .to_owned(),
                    ),
                );
                gaps.push(g);
            }
        }
        return Ok(DiagnosticComparison {
            case_id: verified_case.case_id().to_owned(),
            input_sha256: verified_case.input_sha256,
            status: "INCOMPLETE".to_owned(),
            diagnostic_only: true,
            g_spec_01: "not_evaluated".to_owned(),
            agent_view: "outstanding".to_owned(),
            author_independence: "not_asserted".to_owned(),
            normative_contract_id: V5_APPROVED_CONFORMANCE_CONTRACT_ID.to_owned(),
            normative_contract_sha256: contract_sha,
            case_manifest_sha256: case_manifest_sha256.to_owned(),
            diagnostic_freeze_manifest_sha256: diagnostic_freeze_manifest_sha256.to_owned(),
            source_closure_sha256_by_evaluator: source_hashes,
            wire_evaluator_id_by_slot: BTreeMap::from([
                ("full_spec".to_owned(), "full_spec_blind".to_owned()),
                ("production".to_owned(), "production".to_owned()),
            ]),
            execution_status_by_evaluator: execution_statuses,
            payload_sha256_by_evaluator: BTreeMap::from([
                ("full_spec".to_owned(), None),
                ("production".to_owned(), None),
            ]),
            materialization_sha256_by_evaluator: BTreeMap::from([
                ("full_spec".to_owned(), None),
                ("production".to_owned(), None),
            ]),
            mismatch_paths: Vec::new(),
            gaps,
        });
    }

    let full_result = full_exec
        .get("result")
        .filter(|v| v.is_object())
        .ok_or_else(|| {
            SharedRuntimeError(
                "successful diagnostic execution omitted an evaluator result".to_owned(),
            )
        })?;
    let prod_result = prod_exec
        .get("result")
        .filter(|v| v.is_object())
        .ok_or_else(|| {
            SharedRuntimeError(
                "successful diagnostic execution omitted an evaluator result".to_owned(),
            )
        })?;
    let left =
        validate_evaluator_result(full_result, &verified_case, "full_spec").map_err(|e| {
            SharedRuntimeError(format!(
                "diagnostic result failed the shared protocol contract: {e}"
            ))
        })?;
    let right =
        validate_evaluator_result(prod_result, &verified_case, "production").map_err(|e| {
            SharedRuntimeError(format!(
                "diagnostic result failed the shared protocol contract: {e}"
            ))
        })?;

    let mut mismatch_paths = Vec::new();
    if left.status != right.status {
        mismatch_paths.push("$.status".to_owned());
    }
    if canonical_json_bytes(&left.materialization)? != canonical_json_bytes(&right.materialization)?
    {
        mismatch_paths.push("$.materialization".to_owned());
    }
    mismatch_paths.extend(json_diff_paths(
        &left.raw_value["payload"],
        &right.raw_value["payload"],
        "$.payload",
    ));
    mismatch_paths.extend(json_diff_paths(
        &left.raw_value["coverage"],
        &right.raw_value["coverage"],
        "$.coverage",
    ));
    let mismatch_paths: Vec<String> = mismatch_paths
        .into_iter()
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let status = if !mismatch_paths.is_empty() {
        "MISMATCH"
    } else if left.status != "ok" || right.status != "ok" {
        "INCOMPLETE"
    } else {
        "MATCH"
    };

    let mut gaps = Vec::new();
    for (slot, ev_id, ev_gaps) in [
        ("full_spec", left.evaluator_id.as_str(), &left.gaps),
        ("production", right.evaluator_id.as_str(), &right.gaps),
    ] {
        for g in ev_gaps {
            let mut m = Map::new();
            m.insert("evaluator_slot".to_owned(), Value::String(slot.to_owned()));
            m.insert("evaluator_id".to_owned(), Value::String(ev_id.to_owned()));
            m.insert("code".to_owned(), Value::String(g.code.clone()));
            m.insert("operation".to_owned(), Value::String(g.operation.clone()));
            m.insert("case_id".to_owned(), Value::String(g.case_id.clone()));
            m.insert("path".to_owned(), Value::String(g.path.clone()));
            m.insert("detail".to_owned(), Value::String(g.detail.clone()));
            gaps.push(m);
        }
    }

    Ok(DiagnosticComparison {
        case_id: verified_case.case_id().to_owned(),
        input_sha256: verified_case.input_sha256,
        status: status.to_owned(),
        diagnostic_only: true,
        g_spec_01: "not_evaluated".to_owned(),
        agent_view: "outstanding".to_owned(),
        author_independence: "not_asserted".to_owned(),
        normative_contract_id: V5_APPROVED_CONFORMANCE_CONTRACT_ID.to_owned(),
        normative_contract_sha256: contract_sha,
        case_manifest_sha256: case_manifest_sha256.to_owned(),
        diagnostic_freeze_manifest_sha256: diagnostic_freeze_manifest_sha256.to_owned(),
        source_closure_sha256_by_evaluator: source_hashes,
        wire_evaluator_id_by_slot: BTreeMap::from([
            ("full_spec".to_owned(), left.evaluator_id.clone()),
            ("production".to_owned(), right.evaluator_id.clone()),
        ]),
        execution_status_by_evaluator: execution_statuses,
        payload_sha256_by_evaluator: BTreeMap::from([
            (
                "full_spec".to_owned(),
                Some(sha256_json(&Value::Object(left.payload))?),
            ),
            (
                "production".to_owned(),
                Some(sha256_json(&Value::Object(right.payload))?),
            ),
        ]),
        materialization_sha256_by_evaluator: BTreeMap::from([
            (
                "full_spec".to_owned(),
                materialization_hash(&left.materialization)?,
            ),
            (
                "production".to_owned(),
                materialization_hash(&right.materialization)?,
            ),
        ]),
        mismatch_paths,
        gaps,
    })
}

#[allow(clippy::too_many_arguments)]
pub fn failed_receipt(
    evaluator_id: &str,
    source_profile: &str,
    author_origin: &str,
    source_closure_sha256: &str,
    case_manifest_sha256: &str,
    case_id: &str,
    input_sha: &str,
    reason: &str,
    stdout: &[u8],
    stderr: &[u8],
    audit: Option<&Map<String, Value>>,
) -> Value {
    let mut map = Map::new();
    map.insert("protocol".to_owned(), Value::String(PROTOCOL.to_owned()));
    map.insert(
        "evaluator_id".to_owned(),
        Value::String(evaluator_id.to_owned()),
    );
    map.insert(
        "source_profile".to_owned(),
        Value::String(source_profile.to_owned()),
    );
    map.insert(
        "author_origin".to_owned(),
        Value::String(author_origin.to_owned()),
    );
    map.insert("case_id".to_owned(), Value::String(case_id.to_owned()));
    map.insert(
        "input_sha256".to_owned(),
        Value::String(input_sha.to_owned()),
    );
    map.insert(
        "source_closure_sha256".to_owned(),
        Value::String(source_closure_sha256.to_owned()),
    );
    map.insert(
        "case_manifest_sha256".to_owned(),
        Value::String(case_manifest_sha256.to_owned()),
    );
    map.insert(
        "execution_status".to_owned(),
        Value::String("failed".to_owned()),
    );
    map.insert("failure".to_owned(), Value::String(reason.to_owned()));
    map.insert(
        "audit".to_owned(),
        match audit {
            Some(a) => Value::Object(a.clone()),
            None => Value::Null,
        },
    );
    map.insert(
        "stdout_sha256".to_owned(),
        Value::String(sha256_bytes(stdout)),
    );
    map.insert(
        "stderr_sha256".to_owned(),
        Value::String(sha256_bytes(stderr)),
    );
    map.insert(
        "raw_stdout_b64".to_owned(),
        Value::String(base64_encode(stdout)),
    );
    map.insert(
        "raw_stderr_b64".to_owned(),
        Value::String(base64_encode(stderr)),
    );
    Value::Object(map)
}

#[must_use]
pub fn clean_child_environment() -> BTreeMap<String, String> {
    let mut result = BTreeMap::new();
    for key in ["PATH", "LANG", "LC_ALL", "TZ"] {
        if let Ok(val) = std::env::var(key) {
            result.insert(key.to_owned(), val);
        }
    }
    result.insert("PYTHONDONTWRITEBYTECODE".to_owned(), "1".to_owned());
    result.insert("PYTHONNOUSERSITE".to_owned(), "1".to_owned());
    result.insert("PYTHONHASHSEED".to_owned(), "0".to_owned());
    result
}

pub fn verify_source_profile_files_against_profile(
    profile: &SourceProfile,
    files: &[String],
) -> Result<(), SharedRuntimeError> {
    if files != profile.source_files.as_slice() {
        return Err(SharedRuntimeError(
            "source closure file list differs from pinned profile".to_owned(),
        ));
    }
    Ok(())
}
