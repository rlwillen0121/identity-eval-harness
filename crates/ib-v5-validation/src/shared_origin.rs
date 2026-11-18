//! Shared source-qualification and origin-provenance validation.
//!
//! This module mirrors the fail-closed validation in `validation.py` for production readiness,
//! historical author-origin adjudications, and explicitly versioned continuation chains. It only
//! reads hash-bound public evidence artifacts; it never opens author session logs.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use ib_v5_conformance::process::{SourceProfile, parse_utc_timestamp_ns, resolve_source_profile};
use ib_v5_conformance::shared::{EVALUATOR_WIRE_ID_BY_SLOT, canonical_json_bytes, sha256_bytes};
use ib_v5_schemas::V5_BASE_SPEC_SHA256;
use regex::Regex;
use serde_json::{Map, Value, json};

#[path = "shared_origin_chain_support.rs"]
mod chain_support;
use chain_support::{
    capture_cutoff_character, string_array, validate_baseline_adjudication,
    validate_chain_limitations, validate_chain_review, validate_predecessor,
    validate_reconstruction, validate_spawn_evidence, validate_successor_audit,
    validate_successor_root_attestation,
};

const AUDIT_FULL_V7: &str = "bounded fresh full-spec author-session source-origin metadata audit";
const AUDIT_CARD_V7: &str = "bounded card-v7 author-session suffix provenance metadata";
const FULL_SPEC_NORMATIVE_USE: &str =
    "Hash metadata only; original SPEC content is not asserted as an author input.";
const FULL_SPEC_AUTHORIZED_SCOPE: &str = "This excerpt was the only authorized SPEC input; original SPEC §0 was not authorized as author input.";
const FULL_SPEC_EXECUTED_NOTE: &str = "Parent-confirmed immutable executed-extractor archive; bind this archive rather than the formatted .py.";
const CARD_EXECUTED_NOTE: &str =
    "Read-only archive of the executed extractor; do not substitute the later formatted .py file.";
const CARD_EXECUTED_SHA256: &str =
    "0c479172206a4612c3d3c9970d521fe5825620830618fbc339c14a5d9c8a758c";
const CARD_HISTORICAL_NOTE: &str = "Binds the earlier 1304-line prefix only. The current suffix and capture were assessed independently; this historical record does not qualify the new source.";
const CANONICAL_CAPTURE_HASH: &str =
    "canonical_sorted_compact_ensure_ascii_false_allow_nan_false_after_removing_manifest_sha256";

static SHA256_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[a-f0-9]{64}$").expect("static SHA regex"));
static UTC_TIMESTAMP_RE: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$")
        .expect("static timestamp regex")
});

/// Validate evaluator source qualification and attach provenance only after every check passes.
#[allow(clippy::too_many_arguments)]
pub(crate) fn validate_shared_qualification(
    evaluator_id: &str,
    qualification: &Value,
    source_profile: &str,
    author_origin: &str,
    closure_sha: &str,
    capture_raw_sha: &str,
    capture_logical_sha: &str,
    capture_manifest_path: Option<&str>,
    captured_source_files_count: Option<usize>,
    runtime_manifest: &Map<String, Value>,
    repository_root: &Path,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
    provenance: &mut Map<String, Value>,
) -> bool {
    let Some(qualification) = qualification.as_object() else {
        gaps.push(format!(
            "{evaluator_id} qualification evidence is unavailable"
        ));
        return false;
    };

    if evaluator_id == "production" {
        if !exact_keys(qualification, &["kind", "evidence"])
            || qualification.get("kind").and_then(Value::as_str) != Some("production_readiness")
        {
            gaps.push(
                "production qualification is not a distinct production-readiness record".into(),
            );
            return false;
        }
        let Some((raw, _)) = read_bound(
            repository_root,
            qualification.get("evidence"),
            "production readiness qualification",
            errors,
            gaps,
        ) else {
            return false;
        };
        let Some(readiness) = read_object(&raw, "production readiness qualification", errors, gaps)
        else {
            return false;
        };
        if readiness.get("record_type").and_then(Value::as_str)
            != Some("production_adapter_readiness")
            || readiness.get("status").and_then(Value::as_str) != Some("QUALIFIED_ELIGIBLE")
            || readiness
                .get("source_closure_sha256")
                .and_then(Value::as_str)
                != Some(closure_sha)
            || readiness
                .get("runtime_manifest_sha256")
                .and_then(Value::as_str)
                != runtime_manifest
                    .get("manifest_sha256")
                    .or_else(|| runtime_manifest.get("source_closure_sha256"))
                    .and_then(Value::as_str)
        {
            gaps.push("production readiness does not bind the exact production closure".into());
            return false;
        }
        return true;
    }

    if qualification.get("kind").and_then(Value::as_str) == Some("author_origin_chain_v1") {
        return validate_origin_chain_v1(
            evaluator_id,
            qualification,
            source_profile,
            author_origin,
            capture_raw_sha,
            capture_logical_sha,
            capture_manifest_path,
            captured_source_files_count,
            repository_root,
            errors,
            gaps,
            provenance,
        );
    }

    validate_author_origin(
        evaluator_id,
        qualification,
        source_profile,
        author_origin,
        capture_raw_sha,
        capture_logical_sha,
        capture_manifest_path,
        captured_source_files_count,
        repository_root,
        errors,
        gaps,
        provenance,
    )
}

#[allow(clippy::too_many_arguments)]
fn validate_author_origin(
    evaluator_id: &str,
    qualification: &Map<String, Value>,
    source_profile: &str,
    author_origin: &str,
    capture_raw_sha: &str,
    capture_logical_sha: &str,
    capture_manifest_path: Option<&str>,
    captured_source_files_count: Option<usize>,
    root: &Path,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
    provenance: &mut Map<String, Value>,
) -> bool {
    if !exact_keys(qualification, &["kind", "adjudication", "origin_audit"])
        || qualification.get("kind").and_then(Value::as_str) != Some("author_origin")
    {
        gaps.push(format!(
            "{evaluator_id} author-origin qualification has an unsupported shape"
        ));
        return false;
    }
    let Some((adjudication_raw, _)) = read_bound(
        root,
        qualification.get("adjudication"),
        &format!("{evaluator_id} origin adjudication"),
        errors,
        gaps,
    ) else {
        return false;
    };
    let Some((audit_raw, _)) = read_bound(
        root,
        qualification.get("origin_audit"),
        &format!("{evaluator_id} native origin audit"),
        errors,
        gaps,
    ) else {
        return false;
    };
    let Some(mut adjudication) = read_object(
        &adjudication_raw,
        &format!("{evaluator_id} origin adjudication"),
        errors,
        gaps,
    ) else {
        return false;
    };
    if let Some(reference) = qualification.get("adjudication") {
        adjudication.insert("_bound_adjudication_reference".into(), reference.clone());
    }
    let Some(audit) = read_object(
        &audit_raw,
        &format!("{evaluator_id} native origin audit"),
        errors,
        gaps,
    ) else {
        return false;
    };
    if adjudication.get("record_type").and_then(Value::as_str) != Some("source_origin_adjudication")
    {
        errors.push(format!(
            "{evaluator_id} origin adjudication has an unsupported record type"
        ));
        return false;
    }
    if adjudication.get("record_version").and_then(Value::as_i64) != Some(2) {
        gaps.push(format!(
            "{evaluator_id} historical source-origin record cannot qualify this capture"
        ));
        return false;
    }
    let Some(scope) = adjudication.get("scope").and_then(Value::as_object) else {
        gaps.push(format!(
            "{evaluator_id} source-origin adjudication has no prefix scope"
        ));
        return false;
    };
    let audit_kind = audit
        .get("audit_kind")
        .and_then(Value::as_str)
        .unwrap_or("");
    let v7 = matches!(audit_kind, AUDIT_FULL_V7 | AUDIT_CARD_V7);
    let root_attestation = if v7 {
        let attestation = validate_v7_root_attestation(
            &adjudication,
            evaluator_id,
            source_profile,
            author_origin,
            errors,
            gaps,
        );
        if attestation.is_none() {
            return false;
        }
        Some(attestation.expect("checked above"))
    } else {
        None
    };
    if v7 && !provenance.is_empty() {
        // The caller's map is intentionally allowed to be pre-populated, but no partial claim is
        // written until all qualification evidence is verified below.
    }

    let normative = adjudication
        .get("normative_spec")
        .and_then(Value::as_object);
    let normative_keys =
        normative.map_or_else(BTreeSet::new, |value| value.keys().cloned().collect());
    if v7 && evaluator_id == "full_spec_blind" {
        if normative.is_none()
            || normative_keys
                != BTreeSet::from(["path".to_owned(), "sha256".to_owned(), "use".to_owned()])
            || normative
                .and_then(|value| value.get("use"))
                .and_then(Value::as_str)
                != Some(FULL_SPEC_NORMATIVE_USE)
        {
            gaps.push(format!(
                "{evaluator_id} source-origin adjudication lacks its exact v7 SPEC note"
            ));
            return false;
        }
        let authorized = adjudication
            .get("authorized_spec_input")
            .and_then(Value::as_object);
        if !authorized.is_some_and(|value| {
            exact_keys(value, &["path", "sha256", "scope"])
                && value.get("path").and_then(Value::as_str)
                    == Some(
                        "reports/v5/conformance/inputs/spec-sections-1-4/SPEC-v5-sections-1-4.md",
                    )
                && value
                    .get("sha256")
                    .and_then(Value::as_str)
                    .is_some_and(is_sha256)
                && value.get("scope").and_then(Value::as_str) == Some(FULL_SPEC_AUTHORIZED_SCOPE)
        }) {
            errors.push(format!(
                "{evaluator_id} authorized SPEC input binding is malformed"
            ));
            return false;
        }
    } else if normative.is_none()
        || !(normative_keys == BTreeSet::from(["sha256".to_owned()])
            || normative_keys == BTreeSet::from(["path".to_owned(), "sha256".to_owned()]))
    {
        gaps.push(format!(
            "{evaluator_id} source-origin adjudication lacks a supported SPEC binding"
        ));
        return false;
    }
    if normative
        .and_then(|value| value.get("path"))
        .and_then(Value::as_str)
        .unwrap_or("SPEC-v5.md")
        != "SPEC-v5.md"
    {
        errors.push(format!(
            "{evaluator_id} source-origin record binds an unexpected normative file"
        ));
    }
    if normative
        .and_then(|value| value.get("sha256"))
        .and_then(Value::as_str)
        != Some(V5_BASE_SPEC_SHA256)
    {
        errors.push(format!(
            "{evaluator_id} source-origin record binds an unexpected normative digest"
        ));
    }

    let Some(evidence_rows) = adjudication.get("evidence").and_then(Value::as_array) else {
        errors.push(format!(
            "{evaluator_id} adjudication does not bind its native audit"
        ));
        return false;
    };
    let Some(audit_ref) = qualification.get("origin_audit").and_then(Value::as_object) else {
        errors.push(format!(
            "{evaluator_id} adjudication does not bind its native audit"
        ));
        return false;
    };
    let audit_path = audit_ref.get("path").and_then(Value::as_str).unwrap_or("");
    let audit_sha = audit_ref
        .get("sha256")
        .and_then(Value::as_str)
        .unwrap_or("");
    let mut evidence_by_path = BTreeMap::<String, String>::new();
    let mut evidence_payloads = BTreeMap::<String, Vec<u8>>::new();
    for (index, row) in evidence_rows.iter().enumerate() {
        let Some(row) = row.as_object() else {
            errors.push(format!(
                "{evaluator_id} evidence row {index} has an invalid shape"
            ));
            continue;
        };
        let path = row.get("path").and_then(Value::as_str).unwrap_or("");
        let digest = row.get("raw_sha256").and_then(Value::as_str).unwrap_or("");
        let fields = row.keys().map(String::as_str).collect::<BTreeSet<_>>();
        let has_note = fields.contains("note");
        let expected = if has_note {
            ["path", "raw_sha256", "note"].as_slice()
        } else {
            ["path", "raw_sha256"].as_slice()
        };
        if !exact_keys(row, expected) {
            errors.push(format!(
                "{evaluator_id} evidence row {index} has an invalid shape"
            ));
            continue;
        }
        if has_note
            && (!v7
                || !v7_note_allowed(
                    evaluator_id,
                    path,
                    row.get("note").and_then(Value::as_str).unwrap_or(""),
                ))
        {
            errors.push(format!(
                "{evaluator_id} evidence row {index} has an unsupported note"
            ));
            continue;
        }
        if path.is_empty() || !is_sha256(digest) || evidence_by_path.contains_key(path) {
            errors.push(format!(
                "{evaluator_id} evidence row {index} is malformed or duplicated"
            ));
            continue;
        }
        evidence_by_path.insert(path.to_owned(), digest.to_owned());
        if let Some((bytes, _)) = read_bound(
            root,
            Some(&json!({"path": path, "sha256": digest})),
            &format!("{evaluator_id} qualification evidence {index}"),
            errors,
            gaps,
        ) {
            evidence_payloads.insert(path.to_owned(), bytes);
        }
    }
    if evidence_rows
        .iter()
        .filter(|row| {
            row.get("path").and_then(Value::as_str) == Some(audit_path)
                && row.get("raw_sha256").and_then(Value::as_str) == Some(audit_sha)
        })
        .count()
        != 1
    {
        errors.push(format!(
            "{evaluator_id} adjudication evidence does not uniquely bind native audit bytes"
        ));
    }

    if v7 && evaluator_id == "full_spec_blind" {
        let authorized = adjudication
            .get("authorized_spec_input")
            .and_then(Value::as_object);
        if let Some(authorized) = authorized
            && evidence_by_path
                .get(authorized.get("path").and_then(Value::as_str).unwrap_or(""))
                .map(String::as_str)
                != authorized.get("sha256").and_then(Value::as_str)
        {
            errors.push(format!(
                "{evaluator_id} authorized SPEC input is not hash-bound in evidence"
            ));
        }
    }

    validate_scope(
        evaluator_id,
        scope,
        &adjudication,
        &evidence_by_path,
        &evidence_payloads,
        source_profile,
        author_origin,
        capture_raw_sha,
        capture_logical_sha,
        capture_manifest_path,
        captured_source_files_count,
        v7,
        root,
        errors,
        gaps,
    );

    let attested_subject = if v7 {
        adjudication.get("subject").and_then(Value::as_object)
    } else {
        None
    };
    let native = native_origin_claims(
        &audit,
        evaluator_id,
        root,
        errors,
        gaps,
        attested_subject,
        if v7 { Some(scope) } else { None },
    );
    let Some(native) = native else {
        return false;
    };
    validate_native_bindings(
        evaluator_id,
        &adjudication,
        scope,
        &native,
        source_profile,
        author_origin,
        capture_raw_sha,
        capture_logical_sha,
        capture_manifest_path,
        captured_source_files_count,
        &evidence_by_path,
        v7,
        errors,
        gaps,
    );
    validate_independent_review(evaluator_id, &adjudication, &native, errors, gaps);

    if v7 && errors.is_empty() && gaps.is_empty() {
        provenance.extend(v7_provenance(
            evaluator_id,
            source_profile,
            author_origin,
            audit_kind,
            audit_sha,
            scope,
            &adjudication,
            &audit,
            &native,
            root_attestation.as_ref(),
            capture_raw_sha,
            capture_logical_sha,
        ));
    }
    errors.is_empty() && gaps.is_empty()
}

fn validate_scope(
    evaluator_id: &str,
    scope: &Map<String, Value>,
    adjudication: &Map<String, Value>,
    evidence: &BTreeMap<String, String>,
    evidence_payloads: &BTreeMap<String, Vec<u8>>,
    source_profile: &str,
    author_origin: &str,
    capture_raw_sha: &str,
    capture_logical_sha: &str,
    capture_manifest_path: Option<&str>,
    captured_source_files_count: Option<usize>,
    v7: bool,
    root: &Path,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) {
    let required = [
        "capture_manifest_raw_sha256",
        "capture_manifest_logical_sha256",
        "session_id",
        "prefix_bytes",
        "prefix_sha256",
        "cutoff_line_inclusive",
        "cutoff_timestamp_utc",
    ];
    let mut fields = required
        .iter()
        .map(|field| (*field).to_owned())
        .collect::<BTreeSet<_>>();
    let mut ext = fields.clone();
    ext.extend([
        "capture_manifest_path".into(),
        "captured_source_files_count".into(),
    ]);
    let mut full_origin = fields.clone();
    full_origin.insert("capture_path".into());
    let mut v7_fields = fields.clone();
    v7_fields.extend([
        "capture_manifest_path".into(),
        "capture_cutoff_timestamp_utc".into(),
        "capture_cutoff_timestamp_character".into(),
        "excluded_lookahead".into(),
    ]);
    fields.insert("capture_manifest_path".into());

    if v7 {
        if !has_exact_key_set(scope, &v7_fields) {
            errors.push(format!(
                "{evaluator_id} v7 source-origin scope has an unsupported exact field set"
            ));
            return;
        }
        let excluded = scope.get("excluded_lookahead").and_then(Value::as_object);
        let expected_excluded = if evaluator_id == "full_spec_blind" {
            ["line", "timestamp_utc", "payload"].as_slice()
        } else {
            ["line", "payload"].as_slice()
        };
        let cutoff_char = if evaluator_id == "full_spec_blind" {
            "Weak filesystem mtime only; not cryptographic chronology."
        } else if evaluator_id == "agent_view_only" {
            "Filesystem mtime only; it is not cryptographic chronology."
        } else {
            ""
        };
        if scope.get("capture_manifest_path").and_then(Value::as_str) != capture_manifest_path
            || !is_valid_utc(
                scope
                    .get("capture_cutoff_timestamp_utc")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
            )
            || scope
                .get("capture_cutoff_timestamp_character")
                .and_then(Value::as_str)
                != Some(cutoff_char)
            || !excluded.is_some_and(|value| {
                exact_keys(value, expected_excluded)
                    && value.get("line").and_then(Value::as_u64)
                        == scope
                            .get("cutoff_line_inclusive")
                            .and_then(Value::as_u64)
                            .map(|line| line + 1)
                    && value
                        .get("payload")
                        .and_then(Value::as_str)
                        .is_some_and(|text| text.to_lowercase().contains("excluded"))
                    && (evaluator_id != "full_spec_blind"
                        || is_valid_utc(
                            value
                                .get("timestamp_utc")
                                .and_then(Value::as_str)
                                .unwrap_or(""),
                        ))
            })
        {
            errors.push(format!(
                "{evaluator_id} v7 capture/limit scope is malformed"
            ));
        }
        let path = scope
            .get("capture_manifest_path")
            .and_then(Value::as_str)
            .unwrap_or("");
        let matching = evidence
            .iter()
            .filter(|(p, digest)| p.as_str() == path && digest.as_str() == capture_raw_sha)
            .count();
        if matching != 1 {
            errors.push(format!(
                "{evaluator_id} v7 scope does not uniquely bind its captured source manifest"
            ));
        }
        validate_captured_manifest(
            evaluator_id,
            path,
            source_profile,
            author_origin,
            capture_raw_sha,
            capture_logical_sha,
            captured_source_files_count,
            root,
            errors,
            gaps,
        );
        return;
    }

    let set = scope.keys().cloned().collect::<BTreeSet<_>>();
    let required_set = required
        .iter()
        .map(|field| (*field).to_owned())
        .collect::<BTreeSet<_>>();
    if set == required_set || set == ext {
        let matching = evidence
            .iter()
            .filter(|(_, digest)| digest.as_str() == capture_raw_sha)
            .count();
        if matching != 1 {
            errors.push(format!(
                "{evaluator_id} adjudication evidence does not uniquely bind captured source bytes"
            ));
        }
    } else if set == full_origin {
        let run_paths = evidence_payloads
            .keys()
            .filter(|path| path.ends_with("/source-capture-run-manifest.json"))
            .collect::<Vec<_>>();
        if run_paths.len() != 1 {
            errors.push(format!(
                "{evaluator_id} full-spec origin record does not uniquely bind capture run metadata"
            ));
        } else if let Some(run) = evidence_payloads
            .get(run_paths[0])
            .and_then(|raw| read_object(raw, "source capture run manifest", errors, gaps))
        {
            let expected_slot = if evaluator_id == "full_spec_blind" {
                "full_spec"
            } else {
                "agent_view"
            };
            let captures = run.get("captures").and_then(Value::as_array);
            let matches = captures
                .into_iter()
                .flatten()
                .filter(|row| {
                    row.get("evaluator_slot").and_then(Value::as_str) == Some(expected_slot)
                })
                .collect::<Vec<_>>();
            if matches.len() != 1 {
                errors.push(format!(
                    "{evaluator_id} capture run manifest has no unique source capture"
                ));
            } else {
                let row = matches[0];
                let expected_path = capture_manifest_path.map(|path| {
                    Path::new(path)
                        .parent()
                        .unwrap_or(Path::new(""))
                        .to_string_lossy()
                        .into_owned()
                });
                if row.get("author_origin").and_then(Value::as_str) != Some(author_origin)
                    || row.get("source_profile").and_then(Value::as_str) != Some(source_profile)
                    || row
                        .get("capture_manifest_raw_sha256")
                        .and_then(Value::as_str)
                        != Some(capture_raw_sha)
                    || row
                        .get("capture_manifest_logical_sha256")
                        .and_then(Value::as_str)
                        != Some(capture_logical_sha)
                    || row.get("capture_path").and_then(Value::as_str) != expected_path.as_deref()
                    || row.get("source_files_count").and_then(Value::as_u64)
                        != captured_source_files_count.map(|n| n as u64)
                    || scope.get("capture_path").and_then(Value::as_str) != expected_path.as_deref()
                {
                    errors.push(format!(
                        "{evaluator_id} capture run manifest differs from exact source capture"
                    ));
                }
            }
        }
    } else {
        errors.push(format!(
            "{evaluator_id} source-origin scope has an unsupported exact field set"
        ));
    }
    let _ = adjudication;
}

fn validate_captured_manifest(
    evaluator_id: &str,
    path: &str,
    source_profile: &str,
    author_origin: &str,
    capture_raw_sha: &str,
    capture_logical_sha: &str,
    expected_count: Option<usize>,
    root: &Path,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) {
    let Some((raw, _)) = read_bound(
        root,
        Some(&json!({"path": path, "sha256": capture_raw_sha})),
        &format!("{evaluator_id} captured source manifest"),
        errors,
        gaps,
    ) else {
        return;
    };
    let Some(manifest) = read_object(
        &raw,
        &format!("{evaluator_id} captured source manifest"),
        errors,
        gaps,
    ) else {
        return;
    };
    let rows = manifest.get("source_files").and_then(Value::as_array);
    let unsigned = without_key(&manifest, "manifest_sha256");
    let logical = canonical_sha(&Value::Object(unsigned));
    if manifest.get("manifest_sha256").and_then(Value::as_str) != Some(capture_logical_sha)
        || logical.as_deref() != Some(capture_logical_sha)
        || manifest.get("source_profile").and_then(Value::as_str) != Some(source_profile)
        || manifest.get("author_origin").and_then(Value::as_str) != Some(author_origin)
        || rows.is_none_or(|rows| expected_count != Some(rows.len()))
    {
        errors.push(format!(
            "{evaluator_id} captured source manifest identity or file count differs"
        ));
    }
}

fn validate_native_bindings(
    evaluator_id: &str,
    adjudication: &Map<String, Value>,
    scope: &Map<String, Value>,
    native: &Map<String, Value>,
    source_profile: &str,
    author_origin: &str,
    capture_raw_sha: &str,
    capture_logical_sha: &str,
    capture_manifest_path: Option<&str>,
    captured_source_files_count: Option<usize>,
    evidence: &BTreeMap<String, String>,
    v7: bool,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) {
    if native.get("audit_layout").and_then(Value::as_str) == Some("bounded_full_spec_v7") {
        let archive = evidence
            .iter()
            .filter(|(path, _)| {
                path.ends_with("/bounded-fullspec-prefix-audit.executed.py.txt")
                    && path_note(adjudication, path, FULL_SPEC_EXECUTED_NOTE)
            })
            .collect::<Vec<_>>();
        if archive.len() != 1
            || archive[0].1
                != native
                    .get("approved_auditor_script_sha256")
                    .and_then(Value::as_str)
                    .unwrap_or("")
        {
            errors.push(format!("{evaluator_id} full-spec v7 evidence does not uniquely bind the executed audit archive"));
        }
    } else if native.get("audit_layout").and_then(Value::as_str) == Some("bounded_card_v7") {
        let archive = evidence
            .iter()
            .filter(|(path, _)| {
                path.ends_with("/card-v7-prefix-audit.executed.py.txt")
                    && path_note(adjudication, path, CARD_EXECUTED_NOTE)
            })
            .collect::<Vec<_>>();
        if archive.len() != 1 || archive[0].1 != CARD_EXECUTED_SHA256 {
            errors.push(format!(
                "{evaluator_id} card-v7 evidence does not uniquely bind the executed audit archive"
            ));
        }
    }
    let expected_subject = json!({
        "author_origin": author_origin,
        "author_task": native.get("author_task"),
        "source_profile": source_profile,
        "evaluator_id": evaluator_id,
    });
    if adjudication.get("subject") != Some(&expected_subject) {
        errors.push(format!(
            "{evaluator_id} adjudication subject does not bind its capture and author"
        ));
    }
    let expected = [
        ("capture_manifest_raw_sha256", json!(capture_raw_sha)),
        (
            "capture_manifest_logical_sha256",
            json!(capture_logical_sha),
        ),
        (
            "session_id",
            native.get("session_id").cloned().unwrap_or(Value::Null),
        ),
        (
            "prefix_bytes",
            native.get("prefix_bytes").cloned().unwrap_or(Value::Null),
        ),
        (
            "prefix_sha256",
            native.get("prefix_sha256").cloned().unwrap_or(Value::Null),
        ),
        (
            "cutoff_line_inclusive",
            native
                .get("cutoff_line_inclusive")
                .cloned()
                .unwrap_or(Value::Null),
        ),
    ];
    let mut mismatch = expected
        .iter()
        .any(|(key, value)| scope.get(*key) != Some(value));
    let scope_time = scope
        .get("cutoff_timestamp_utc")
        .and_then(Value::as_str)
        .unwrap_or("");
    let native_time = native
        .get("cutoff_timestamp_utc")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !same_utc(scope_time, native_time) {
        mismatch = true;
    }
    if mismatch {
        errors.push(format!(
            "{evaluator_id} adjudication scope differs from the native capture claim"
        ));
    }
    if v7 {
        if native.get("capture_manifest_path").and_then(Value::as_str)
            != scope.get("capture_manifest_path").and_then(Value::as_str)
            || native
                .get("capture_manifest_raw_sha256")
                .and_then(Value::as_str)
                != Some(capture_raw_sha)
            || native
                .get("capture_manifest_logical_sha256")
                .and_then(Value::as_str)
                != Some(capture_logical_sha)
            || !same_utc(
                native
                    .get("capture_cutoff_timestamp_utc")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
                scope
                    .get("capture_cutoff_timestamp_utc")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
            )
        {
            errors.push(format!(
                "{evaluator_id} v7 native audit capture binding differs from scope"
            ));
        }
        if let Some(descriptor) = native.get("capture_descriptor").and_then(Value::as_object)
            && evidence
                .get(descriptor.get("path").and_then(Value::as_str).unwrap_or(""))
                .map(String::as_str)
                != descriptor.get("sha256").and_then(Value::as_str)
        {
            errors.push(format!(
                "{evaluator_id} v7 capture descriptor is not hash-bound in evidence"
            ));
        }
        if native.get("audit_layout").and_then(Value::as_str) == Some("bounded_card_v7")
            && adjudication
                .get("basis")
                .and_then(Value::as_object)
                .and_then(|basis| basis.get("historical_prefix_binding"))
                != native.get("old_prefix")
        {
            errors.push(format!(
                "{evaluator_id} historical card prefix binding differs"
            ));
        }
    }
    if let Some(path) = capture_manifest_path {
        if scope
            .get("capture_manifest_path")
            .and_then(Value::as_str)
            .is_some_and(|p| p != path)
        {
            errors.push(format!(
                "{evaluator_id} adjudication capture path differs from source descriptor"
            ));
        }
    }
    if let Some(count) = captured_source_files_count {
        if let Some(scope_count) = scope
            .get("captured_source_files_count")
            .and_then(Value::as_u64)
            && scope_count != count as u64
        {
            errors.push(format!(
                "{evaluator_id} captured source file count differs from scope"
            ));
        }
    }
    if author_origin == "production_adapter" || source_profile == "production_v1" {
        errors.push(format!(
            "{evaluator_id} source claims a production origin as a blind author"
        ));
    }
    let _ = gaps;
}

fn validate_independent_review(
    evaluator_id: &str,
    adjudication: &Map<String, Value>,
    native: &Map<String, Value>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) {
    let status = adjudication
        .get("status")
        .and_then(Value::as_str)
        .unwrap_or("");
    let Some(independent) = adjudication
        .get("independent_review")
        .and_then(Value::as_object)
    else {
        gaps.push(format!(
            "{evaluator_id} source-origin record lacks independent review evidence"
        ));
        return;
    };
    let author_task = native
        .get("author_task")
        .and_then(Value::as_str)
        .unwrap_or("");
    if status == "INELIGIBLE" {
        if !independent
            .get("controlling_disposition")
            .and_then(Value::as_str)
            .is_some_and(|v| !v.is_empty())
        {
            gaps.push(format!(
                "{evaluator_id} ineligible origin record lacks a concrete reason"
            ));
        }
        let assessments = independent
            .get("initial_lane_assessments")
            .and_then(Value::as_array);
        if assessments.is_none_or(Vec::is_empty) {
            gaps.push(format!(
                "{evaluator_id} ineligible origin record lacks review assessments"
            ));
        } else if let Some(assessments) = assessments {
            let reviewer_ids = assessments
                .iter()
                .filter_map(|row| {
                    row.as_object()
                        .and_then(|row| row.get("reviewer"))
                        .and_then(Value::as_str)
                })
                .collect::<Vec<_>>();
            if reviewer_ids.len() != assessments.len()
                || reviewer_ids.iter().collect::<BTreeSet<_>>().len() != reviewer_ids.len()
            {
                errors.push(format!(
                    "{evaluator_id} ineligible review identities are malformed"
                ));
            }
            if reviewer_ids.contains(&author_task) {
                errors.push(format!(
                    "{evaluator_id} author reviewed its own origin disposition"
                ));
            }
        }
        if independent
            .get("controlling_disposition")
            .and_then(Value::as_str)
            .is_some_and(|v| !v.is_empty())
        {
            errors.push(format!(
                "{evaluator_id} author origin is explicitly INELIGIBLE: {}",
                independent["controlling_disposition"]
                    .as_str()
                    .unwrap_or("")
            ));
        }
        return;
    }
    if status != "QUALIFIED_ELIGIBLE" {
        gaps.push(format!(
            "{evaluator_id} source-origin record is not eligible for strict conformance"
        ));
    }
    let decision = independent
        .get("decision")
        .or_else(|| independent.get("status"))
        .and_then(Value::as_str);
    let Some(reviewers) = independent.get("reviewers").and_then(Value::as_array) else {
        gaps.push(format!(
            "{evaluator_id} source-origin record lacks reviewer identities"
        ));
        return;
    };
    if reviewers.is_empty() {
        gaps.push(format!(
            "{evaluator_id} source-origin record lacks reviewer identities"
        ));
        return;
    }
    let mut ids = Vec::new();
    for reviewer in reviewers {
        if let Some(id) = reviewer.as_str().filter(|id| !id.is_empty()) {
            ids.push(id.to_owned());
            if decision.is_none_or(|verdict| !matches!(verdict, "PASS" | "QUALIFIED_ELIGIBLE")) {
                gaps.push(format!(
                    "{evaluator_id} source-origin record lacks positive reviewer verdicts"
                ));
            }
        } else if let Some(row) = reviewer.as_object() {
            let id = row.get("reviewer").and_then(Value::as_str).unwrap_or("");
            let verdict = row.get("decision").and_then(Value::as_str).unwrap_or("");
            if id.is_empty() || verdict.is_empty() {
                errors.push(format!(
                    "{evaluator_id} source-origin reviewer identity is malformed"
                ));
            } else {
                ids.push(id.to_owned());
                if !matches!(verdict, "PASS" | "QUALIFIED_ELIGIBLE") {
                    gaps.push(format!(
                        "{evaluator_id} source-origin record lacks positive reviewer verdicts"
                    ));
                }
            }
        } else {
            errors.push(format!(
                "{evaluator_id} source-origin reviewer identity is malformed"
            ));
        }
    }
    if ids.iter().collect::<BTreeSet<_>>().len() != ids.len()
        || ids.iter().any(|id| id == author_task)
    {
        errors.push(format!(
            "{evaluator_id} author reviewed its own source-origin record"
        ));
    }
    if decision.is_some_and(|value| !matches!(value, "PASS" | "QUALIFIED_ELIGIBLE")) {
        gaps.push(format!(
            "{evaluator_id} source-origin record lacks a positive review verdict"
        ));
    }
    if independent.contains_key("agreement")
        && !independent
            .get("agreement")
            .and_then(Value::as_str)
            .is_some_and(|v| !v.is_empty())
    {
        errors.push(format!(
            "{evaluator_id} source-origin review agreement is malformed"
        ));
    }
}

fn read_bound(
    root: &Path,
    reference: Option<&Value>,
    label: &str,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Option<(Vec<u8>, PathBuf)> {
    crate::blind_receipt::shared_bound_bytes(root, reference, label, errors, gaps, false, true)
}

fn read_object(
    raw: &[u8],
    label: &str,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Option<Map<String, Value>> {
    crate::blind_receipt::shared_json_object(raw, label, errors, gaps)
}

fn exact_keys(object: &Map<String, Value>, fields: &[&str]) -> bool {
    object.len() == fields.len() && fields.iter().all(|field| object.contains_key(*field))
}

fn has_exact_key_set(object: &Map<String, Value>, fields: &BTreeSet<String>) -> bool {
    object.keys().cloned().collect::<BTreeSet<_>>() == *fields
}

fn without_key(object: &Map<String, Value>, key: &str) -> Map<String, Value> {
    object
        .iter()
        .filter(|(field, _)| field.as_str() != key)
        .map(|(field, value)| (field.clone(), value.clone()))
        .collect()
}

fn canonical_sha(value: &Value) -> Option<String> {
    canonical_json_bytes(value)
        .ok()
        .map(|bytes| sha256_bytes(&bytes))
}

fn is_sha256(value: &str) -> bool {
    SHA256_RE.is_match(value)
}

fn is_valid_utc(value: &str) -> bool {
    if !UTC_TIMESTAMP_RE.is_match(value) || parse_utc_timestamp_ns(value).is_err() {
        return false;
    }
    let Some((date, _time)) = value.split_once('T') else {
        return false;
    };
    let mut parts = date.split('-');
    let (Some(year), Some(month), Some(day)) = (parts.next(), parts.next(), parts.next()) else {
        return false;
    };
    if parts.next().is_some() {
        return false;
    }
    let (Ok(year), Ok(month), Ok(day)) = (
        year.parse::<i32>(),
        month.parse::<u32>(),
        day.parse::<u32>(),
    ) else {
        return false;
    };
    if !(1..=9999).contains(&year) || !(1..=12).contains(&month) {
        return false;
    }
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let last_day = match month {
        2 if leap => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    (1..=last_day).contains(&day)
}

fn same_utc(left: &str, right: &str) -> bool {
    fn key(value: &str) -> Option<(String, String)> {
        if !is_valid_utc(value) {
            return None;
        }
        let value = value
            .strip_suffix('Z')
            .or_else(|| value.strip_suffix("+00:00"))?;
        let (seconds, fraction) = value
            .split_once('.')
            .map_or((value, ""), |(seconds, fraction)| (seconds, fraction));
        Some((
            seconds.to_owned(),
            fraction.trim_end_matches('0').to_owned(),
        ))
    }
    key(left).is_some() && key(left) == key(right)
}

fn v7_note_allowed(evaluator_id: &str, path: &str, note: &str) -> bool {
    match evaluator_id {
        "full_spec_blind" => {
            path.ends_with("/bounded-fullspec-prefix-audit.executed.py.txt")
                && note == FULL_SPEC_EXECUTED_NOTE
        }
        "agent_view_only" => {
            (path.ends_with("/card-v7-prefix-audit.executed.py.txt") && note == CARD_EXECUTED_NOTE)
                || (path
                    .ends_with("/fresh-card-only-v2-v6-source-origin-adjudication-2026-11-12.json")
                    && note == CARD_HISTORICAL_NOTE)
        }
        _ => false,
    }
}

fn path_note(adjudication: &Map<String, Value>, path: &str, expected: &str) -> bool {
    adjudication
        .get("evidence")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .any(|row| {
            row.get("path").and_then(Value::as_str) == Some(path)
                && row.get("note").and_then(Value::as_str) == Some(expected)
        })
}

fn validate_v7_root_attestation(
    adjudication: &Map<String, Value>,
    evaluator_id: &str,
    source_profile: &str,
    author_origin: &str,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Option<Map<String, Value>> {
    let (
        expected_subject,
        expected_attestation,
        expected_limits,
        expected_not_claimed,
        audit_fields,
    ) = match evaluator_id {
        "full_spec_blind" => (
            json!({
                "author_origin":"fresh_bounded_full_spec_author_v1",
                "author_task":"/root/bounded_full_spec_reference",
                "source_profile":"bounded_full_spec_v1",
                "evaluator_id":"full_spec_blind"
            }),
            json!({
                "fork_mode":"fork_none",
                "steering_attested":[
                    "public normative material",
                    "neutral wire protocol",
                    "task ownership",
                    "explicit user Gate3 permanent consumption override",
                    "auxiliary P1 snapshot contract"
                ],
                "not_supplied_attested":[
                    "private gold",
                    "production implementation or private production results",
                    "results",
                    "comparison answers",
                    "other references",
                    "proposed ETag formula",
                    "chosen first-stage headline predicate"
                ],
                "evidence_character":"Supplied trusted root attestation; inbound message semantics are encrypted/opaque and independently unverifiable. Eligibility depends on the attestation, not plaintext inspection."
            }),
            json!({
                "evidence":"Unsigned local evidence and supplied root attestation; no OS-level completeness, log authenticity, or global absence claim is made.",
                "chronology":"The capture cutoff filesystem mtime is weak metadata, not cryptographic chronology; line 2062 remains excluded.",
                "fast_tier":"Requested, not independently verified.",
                "contrary_evidence":"Evidence of actual forbidden exposure overrides this bounded qualification."
            }),
            json!([
                "source semantic correctness",
                "runtime or dependency behavior",
                "execution result",
                "semantic conformance or G-SPEC result",
                "M5.0 milestone completion",
                "publication readiness"
            ]),
            BTreeSet::from([
                "allowed_path_events".to_owned(),
                "unknown_operations".to_owned(),
                "sole_nonallowed_literal".to_owned(),
                "interpretation".to_owned(),
            ]),
        ),
        "agent_view_only" => (
            json!({
                "author_origin":"fresh_independent_card_only_author_v2",
                "author_task":"/root/fresh_card_only_v2",
                "source_profile":"fresh_card_only_v2",
                "evaluator_id":"agent_view_only"
            }),
            json!({
                "statement":"Through capture, steering was limited to transport/gap protocol, public correction topics, and the public v7 release; no case IDs, comparison results, expected answers, private gold, or new ETag formulas were supplied.",
                "evidence_character":"Encrypted inbound-message semantics are independently unverifiable. This qualification relies on the trusted root attestation, not plaintext inspection."
            }),
            json!({
                "evidence":"Unsigned local evidence. No OS-level completeness or global absence claim is made; post-cutoff activity is excluded.",
                "chronology":"The capture cutoff timestamp is filesystem mtime only, not cryptographic chronology.",
                "contrary_evidence":"Evidence of actual forbidden exposure overrides this bounded qualification."
            }),
            json!([
                "source semantic correctness",
                "runtime or dependency behavior",
                "execution result",
                "G-SPEC or semantic conformance",
                "M5.0 milestone completion",
                "publication readiness"
            ]),
            BTreeSet::from([
                "allowed_path_events".to_owned(),
                "unique_paths".to_owned(),
                "established_outside_allowed_paths".to_owned(),
                "result".to_owned(),
                "coverage_limits".to_owned(),
            ]),
        ),
        _ => {
            gaps.push(format!("{evaluator_id} has no v7 bounded-origin adapter"));
            return None;
        }
    };
    if source_profile != expected_subject["source_profile"].as_str().unwrap_or("")
        || author_origin != expected_subject["author_origin"].as_str().unwrap_or("")
        || adjudication.get("subject") != Some(&expected_subject)
    {
        errors.push(format!(
            "{evaluator_id} v7 origin subject differs from its exact adapter identity"
        ));
        return None;
    }
    let Some(basis) = adjudication.get("basis").and_then(Value::as_object) else {
        gaps.push(format!(
            "{evaluator_id} v7 origin record has no bounded-review basis"
        ));
        return None;
    };
    let Some(attestation) = basis
        .get("trusted_root_orchestration_attestation")
        .and_then(Value::as_object)
    else {
        gaps.push(format!(
            "{evaluator_id} v7 origin record lacks explicit root attestation"
        ));
        return None;
    };
    if Value::Object(attestation.clone()) != expected_attestation {
        errors.push(format!(
            "{evaluator_id} v7 root attestation differs from the reviewed contract"
        ));
        return None;
    }
    if adjudication.get("limitations") != Some(&expected_limits) {
        errors.push(format!(
            "{evaluator_id} v7 audit limitations differ from the reviewed contract"
        ));
        return None;
    }
    if adjudication.get("not_claimed") != Some(&expected_not_claimed) {
        errors.push(format!(
            "{evaluator_id} v7 origin record changes its non-claims"
        ));
        return None;
    }
    let bounded = basis.get("bounded_native_audit").and_then(Value::as_object);
    let interpretation = bounded
        .and_then(|value| value.get("interpretation").or_else(|| value.get("result")))
        .and_then(Value::as_str)
        .unwrap_or("");
    if !bounded
        .is_some_and(|value| has_exact_key_set(value, &audit_fields) && !interpretation.is_empty())
    {
        gaps.push(format!(
            "{evaluator_id} v7 bounded-audit review metadata is incomplete"
        ));
        return None;
    }
    Some(attestation.clone())
}

fn native_origin_claims(
    audit: &Map<String, Value>,
    evaluator_id: &str,
    repository_root: &Path,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
    attested_subject: Option<&Map<String, Value>>,
    attested_scope: Option<&Map<String, Value>>,
) -> Option<Map<String, Value>> {
    if matches!(
        audit.get("audit_kind").and_then(Value::as_str),
        Some(AUDIT_FULL_V7 | AUDIT_CARD_V7)
    ) {
        return v7_native_origin_claims(
            audit,
            evaluator_id,
            repository_root,
            errors,
            gaps,
            attested_subject,
            attested_scope,
        );
    }
    let Some(audit_scope) = audit.get("scope").and_then(Value::as_object) else {
        gaps.push(format!("{evaluator_id} native audit has no author scope"));
        return None;
    };
    let repository_root_text = repository_root
        .canonicalize()
        .map(|path| path.to_string_lossy().into_owned())
        .unwrap_or_default();
    let (
        layout,
        session,
        parent,
        history,
        prefix_bytes,
        prefix_sha,
        line,
        cutoff,
        matches_capture,
        task,
    ) = if let Some(provenance) = audit.get("native_provenance").and_then(Value::as_object) {
        let Some(prefix) = provenance
            .get("child_prefix_capture")
            .and_then(Value::as_object)
        else {
            gaps.push(format!(
                "{evaluator_id} native audit lacks full-spec prefix/session metadata"
            ));
            return None;
        };
        let Some(session_meta) = provenance.get("child_session").and_then(Value::as_object) else {
            gaps.push(format!(
                "{evaluator_id} native audit lacks full-spec prefix/session metadata"
            ));
            return None;
        };
        let task = session_meta
            .get("agent_path")
            .cloned()
            .unwrap_or(Value::Null);
        if audit_scope.get("author_path") != Some(&task) {
            errors.push(format!(
                "{evaluator_id} audit author path differs from full-spec session"
            ));
        }
        if session_meta.get("cwd_matches_repo") != Some(&Value::Bool(true)) {
            errors.push(format!(
                "{evaluator_id} full-spec session cwd is not bound to repository"
            ));
        }
        if prefix.get("prefix_matches_initial_capture") != Some(&Value::Bool(true)) {
            errors.push(format!(
                "{evaluator_id} full-spec prefix does not match initial capture"
            ));
        }
        (
            "native_provenance",
            session_meta
                .get("session_id")
                .cloned()
                .unwrap_or(Value::Null),
            session_meta
                .get("parent_thread_id")
                .cloned()
                .unwrap_or(Value::Null),
            session_meta
                .get("history_mode")
                .cloned()
                .unwrap_or(Value::Null),
            prefix.get("prefix_bytes").cloned().unwrap_or(Value::Null),
            prefix.get("prefix_sha256").cloned().unwrap_or(Value::Null),
            prefix
                .get("cutoff_source_line")
                .cloned()
                .unwrap_or(Value::Null),
            prefix
                .get("cutoff_timestamp_utc")
                .cloned()
                .unwrap_or(Value::Null),
            prefix
                .get("prefix_matches_initial_capture")
                .cloned()
                .unwrap_or(Value::Null),
            task,
        )
    } else if let Some(child) = audit.get("child_session").and_then(Value::as_object) {
        let Some(prefix) = child.get("prefix").and_then(Value::as_object) else {
            gaps.push(format!(
                "{evaluator_id} native audit lacks card-only prefix/session metadata"
            ));
            return None;
        };
        let Some(session_meta) = child.get("session_meta").and_then(Value::as_object) else {
            gaps.push(format!(
                "{evaluator_id} native audit lacks card-only prefix/session metadata"
            ));
            return None;
        };
        let Some(last) = prefix.get("last_record").and_then(Value::as_object) else {
            gaps.push(format!(
                "{evaluator_id} card-only audit lacks its bound cutoff record"
            ));
            return None;
        };
        let task = session_meta
            .get("agent_path")
            .cloned()
            .unwrap_or(Value::Null);
        if audit_scope.get("author_path") != Some(&task) {
            errors.push(format!(
                "{evaluator_id} audit author path differs from card-only session"
            ));
        }
        if session_meta
            .get("cwd")
            .and_then(Value::as_str)
            .and_then(|path| Path::new(path).canonicalize().ok())
            != repository_root.canonicalize().ok()
        {
            errors.push(format!(
                "{evaluator_id} card-only session cwd differs from repository"
            ));
        }
        if !audit.get("root_spawn_evidence").is_some_and(|spawn| {
            spawn.get("status").and_then(Value::as_str) == Some("completed")
                && spawn.get("matched_agent_path") == Some(&task)
                && spawn.get("same_cwd").and_then(Value::as_str)
                    == Some(repository_root_text.as_str())
        }) {
            gaps.push(format!(
                "{evaluator_id} card-only native audit lacks a matched root spawn binding"
            ));
        }
        if prefix.get("line_count").and_then(Value::as_i64)
            != last.get("line").and_then(Value::as_i64)
        {
            errors.push(format!(
                "{evaluator_id} card-only prefix line count differs from cutoff"
            ));
        }
        (
            "card_session_meta",
            session_meta.get("id").cloned().unwrap_or(Value::Null),
            session_meta
                .get("parent_thread_id")
                .cloned()
                .unwrap_or(Value::Null),
            session_meta
                .get("history_mode")
                .cloned()
                .unwrap_or(Value::Null),
            prefix.get("bytes").cloned().unwrap_or(Value::Null),
            prefix.get("sha256").cloned().unwrap_or(Value::Null),
            last.get("line").cloned().unwrap_or(Value::Null),
            last.get("timestamp").cloned().unwrap_or(Value::Null),
            prefix
                .get("matches_rechecked_capture")
                .cloned()
                .unwrap_or(Value::Null),
            task,
        )
    } else if let Some(prefix) = audit_scope.get("new_prefix").and_then(Value::as_object) {
        let Some(last) = prefix
            .get("last_included_record")
            .and_then(Value::as_object)
        else {
            gaps.push(format!(
                "{evaluator_id} suffix audit lacks its bound last record"
            ));
            return None;
        };
        if last.get("line") != prefix.get("through_line")
            || last
                .get("bytes")
                .and_then(Value::as_i64)
                .is_none_or(|bytes| bytes <= 0)
        {
            errors.push(format!(
                "{evaluator_id} suffix audit last record differs from prefix cutoff"
            ));
        }
        (
            "full_spec_suffix",
            audit_scope
                .get("session_id")
                .cloned()
                .unwrap_or(Value::Null),
            Value::Null,
            Value::Null,
            prefix.get("bytes").cloned().unwrap_or(Value::Null),
            prefix.get("sha256").cloned().unwrap_or(Value::Null),
            prefix.get("through_line").cloned().unwrap_or(Value::Null),
            last.get("timestamp").cloned().unwrap_or(Value::Null),
            Value::Null,
            audit_scope
                .get("author_path")
                .cloned()
                .unwrap_or(Value::Null),
        )
    } else {
        gaps.push(format!(
            "{evaluator_id} native audit has an unsupported source-origin layout"
        ));
        return None;
    };
    let suffix = layout == "full_spec_suffix";
    if !task
        .as_str()
        .is_some_and(|value| value.starts_with("/root/"))
        || !session.as_str().is_some_and(|value| !value.is_empty())
        || (!suffix
            && (!parent.as_str().is_some_and(|value| !value.is_empty())
                || !history.as_str().is_some_and(|value| !value.is_empty())))
        || prefix_bytes.as_i64().is_none_or(|value| value <= 0)
        || !prefix_sha.as_str().is_some_and(is_sha256)
        || line.as_i64().is_none_or(|value| value <= 0)
        || !cutoff.as_str().is_some_and(|value| is_valid_utc(value))
        || (!suffix && matches_capture != Value::Bool(true))
    {
        gaps.push(format!(
            "{evaluator_id} native session prefix binding is incomplete or malformed"
        ));
    }
    Some(Map::from_iter([
        ("audit_layout".into(), json!(layout)),
        ("author_task".into(), task),
        ("session_id".into(), session),
        ("parent_thread_id".into(), parent),
        ("history_mode".into(), history),
        ("prefix_bytes".into(), prefix_bytes),
        ("prefix_sha256".into(), prefix_sha),
        ("cutoff_line_inclusive".into(), line),
        ("cutoff_timestamp_utc".into(), cutoff),
        ("prefix_matches_capture".into(), matches_capture),
    ]))
}

#[allow(clippy::too_many_arguments)]
fn v7_native_origin_claims(
    audit: &Map<String, Value>,
    evaluator_id: &str,
    _repository_root: &Path,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
    attested_subject: Option<&Map<String, Value>>,
    attested_scope: Option<&Map<String, Value>>,
) -> Option<Map<String, Value>> {
    let (Some(subject), Some(scope)) = (attested_subject, attested_scope) else {
        gaps.push(format!(
            "{evaluator_id} v7 origin audit lacks its attested subject or scope"
        ));
        return None;
    };
    if audit.contains_key("scope") {
        errors.push(format!(
            "{evaluator_id} v7 audit unexpectedly contains a historical scope field"
        ));
        return None;
    }
    if audit.get("audit_kind").and_then(Value::as_str) == Some(AUDIT_FULL_V7) {
        if evaluator_id != "full_spec_blind" {
            errors.push("full-spec v7 audit is bound to the wrong evaluator slot".into());
            return None;
        }
        let required = [
            "ambiguity_record",
            "approved_auditor_script_sha256",
            "audit_identity",
            "audit_kind",
            "author_content_allowlist",
            "author_self_audit",
            "capture_binding",
            "capture_descriptor",
            "child_log_prefix",
            "cutoff_basis",
            "cutoff_utc",
            "extractor_current_sha256",
            "limitations",
            "runtime_capture_attestation",
        ];
        if !exact_keys(audit, &required) {
            errors
                .push("full-spec v7 audit fields differ from the bounded metadata contract".into());
            return None;
        }
        let Some(identity) = audit.get("audit_identity").and_then(Value::as_object) else {
            errors.push("full-spec v7 audit identity fields differ".into());
            return None;
        };
        let expected_identity = [
            "author_origin",
            "child_session_id",
            "lane",
            "source_profile",
            "task",
            "wire_profile",
        ];
        if !exact_keys(identity, &expected_identity)
            || identity.get("author_origin") != subject.get("author_origin")
            || identity.get("source_profile") != subject.get("source_profile")
            || identity.get("wire_profile").and_then(Value::as_str) != Some(evaluator_id)
            || identity.get("lane").and_then(Value::as_str)
                != Some("fresh isolated full-spec blind reference")
            || identity.get("task").and_then(Value::as_str)
                != subject
                    .get("author_task")
                    .and_then(Value::as_str)
                    .map(|task| format!("{task} (fresh fork_none)"))
                    .as_deref()
        {
            errors
                .push("full-spec v7 audit identity differs from the root-attested subject".into());
            return None;
        }
        let Some(capture) = audit.get("capture_binding").and_then(Value::as_object) else {
            errors.push("full-spec v7 audit capture binding fields differ".into());
            return None;
        };
        let capture_fields = [
            "logical_sha256_verification",
            "manifest_bytes",
            "manifest_canonical_json_sha256_observed",
            "manifest_filesystem_mtime_ns",
            "manifest_filesystem_mtime_utc",
            "manifest_logical_sha256",
            "manifest_only_read",
            "manifest_path",
            "manifest_raw_sha256",
            "mtime_precision_comparison",
            "referenced_paths_followed",
        ];
        if !exact_keys(capture, &capture_fields) {
            errors.push("full-spec v7 audit capture binding fields differ".into());
            return None;
        }
        let descriptor = audit.get("capture_descriptor").and_then(Value::as_object);
        if !descriptor.is_some_and(|value| exact_keys(value, &["origin", "path", "sha256"])
            && value.get("origin").and_then(Value::as_str) == Some("root-supplied and independently verified metadata; not opened by this script")
            && value.get("path").and_then(Value::as_str).is_some_and(|s| !s.is_empty())
            && value.get("sha256").and_then(Value::as_str).is_some_and(is_sha256)) {
            errors.push("full-spec v7 capture descriptor is malformed".into());
            return None;
        }
        let Some(container) = audit.get("child_log_prefix").and_then(Value::as_object) else {
            errors.push("full-spec v7 bounded prefix or excluded lookahead is malformed".into());
            return None;
        };
        let prefix_container_fields = [
            "bytes_read_beyond_included_prefix",
            "captured_log_size_bytes_from_fstat",
            "concrete_unresolved_items",
            "excluded_lookahead",
            "log_path",
            "outer_and_payload_type_counts",
            "prefix",
            "record_bound_bytes",
            "resolved_metadata_events",
            "unread_bytes_after_stop",
        ];
        let prefix_fields = [
            "bytes",
            "last_included_outer_timestamp",
            "line_count",
            "sha256",
            "start_offset_bytes",
        ];
        let excluded_fields = [
            "bytes",
            "excluded",
            "included_prefix_hash_updated",
            "line",
            "outer_type",
            "payload_inspected",
            "timestamp",
        ];
        let prefix = container.get("prefix").and_then(Value::as_object);
        let excluded = container
            .get("excluded_lookahead")
            .and_then(Value::as_object);
        if !exact_keys(container, &prefix_container_fields)
            || !prefix.is_some_and(|p| {
                exact_keys(p, &prefix_fields)
                    && p.get("bytes")
                        .and_then(Value::as_i64)
                        .is_some_and(|n| n > 0)
                    && p.get("line_count")
                        .and_then(Value::as_i64)
                        .is_some_and(|n| n > 0)
                    && p.get("start_offset_bytes").and_then(Value::as_i64) == Some(0)
                    && p.get("sha256")
                        .and_then(Value::as_str)
                        .is_some_and(is_sha256)
                    && p.get("last_included_outer_timestamp")
                        .and_then(Value::as_str)
                        .is_some_and(is_valid_utc)
            })
            || !excluded.is_some_and(|e| {
                exact_keys(e, &excluded_fields)
                    && e.get("line").and_then(Value::as_i64)
                        == prefix
                            .and_then(|p| p.get("line_count"))
                            .and_then(Value::as_i64)
                            .map(|n| n + 1)
                    && e.get("excluded") == Some(&Value::Bool(true))
                    && e.get("included_prefix_hash_updated") == Some(&Value::Bool(false))
                    && e.get("payload_inspected") == Some(&Value::Bool(false))
                    && e.get("bytes")
                        .and_then(Value::as_i64)
                        .is_some_and(|n| n > 0)
                    && e.get("timestamp")
                        .and_then(Value::as_str)
                        .is_some_and(is_valid_utc)
            })
        {
            errors.push("full-spec v7 bounded prefix or excluded lookahead is malformed".into());
            return None;
        }
        let capture_mtime = capture
            .get("manifest_filesystem_mtime_utc")
            .and_then(Value::as_str)
            .unwrap_or("");
        let cutoff = audit
            .get("cutoff_utc")
            .and_then(Value::as_str)
            .unwrap_or("");
        if capture.get("manifest_path") != scope.get("capture_manifest_path")
            || capture.get("manifest_raw_sha256") != scope.get("capture_manifest_raw_sha256")
            || capture.get("manifest_logical_sha256")
                != scope.get("capture_manifest_logical_sha256")
            || capture.get("manifest_canonical_json_sha256_observed")
                != scope.get("capture_manifest_logical_sha256")
            || capture
                .get("manifest_bytes")
                .and_then(Value::as_i64)
                .is_none_or(|n| n <= 0)
            || capture
                .get("manifest_filesystem_mtime_ns")
                .and_then(Value::as_i64)
                .is_none()
            || capture.get("manifest_only_read") != Some(&Value::Bool(true))
            || capture.get("referenced_paths_followed") != Some(&Value::Bool(false))
            || capture
                .get("logical_sha256_verification")
                .and_then(Value::as_str)
                != Some(CANONICAL_CAPTURE_HASH)
            || !is_valid_utc(capture_mtime)
            || !same_utc(capture_mtime, cutoff)
            || audit.get("cutoff_basis").and_then(Value::as_str)
                != Some(
                    "explicit root-supplied capture-manifest filesystem mtime; weak chronology metadata, not cryptographic chronology proof",
                )
        {
            errors.push("full-spec v7 audit capture identity or cutoff differs".into());
            return None;
        }
        let runtime = audit
            .get("runtime_capture_attestation")
            .and_then(Value::as_object);
        if !runtime.is_some_and(|value| {
            exact_keys(
                value,
                &[
                    "files_opened_by_this_script",
                    "read_mode",
                    "root_verified_state",
                    "runtime_file_count",
                ],
            ) && value
                .get("files_opened_by_this_script")
                .and_then(Value::as_i64)
                == Some(0)
                && value.get("read_mode").and_then(Value::as_str) == Some("read-only")
                && value.get("root_verified_state").and_then(Value::as_str)
                    == Some("current_equals_pre_equals_post")
                && value
                    .get("runtime_file_count")
                    .and_then(Value::as_i64)
                    .is_some_and(|n| n > 0)
        }) || !audit
            .get("approved_auditor_script_sha256")
            .and_then(Value::as_str)
            .is_some_and(is_sha256)
            || !audit
                .get("extractor_current_sha256")
                .and_then(Value::as_str)
                .is_some_and(is_sha256)
            || audit
                .get("author_content_allowlist")
                .and_then(Value::as_array)
                .is_none()
            || audit
                .get("author_self_audit")
                .and_then(Value::as_object)
                .is_none()
            || audit
                .get("ambiguity_record")
                .and_then(Value::as_object)
                .is_none()
            || !nonempty_string_array(audit.get("limitations"))
        {
            errors
                .push("full-spec v7 bounded audit summary or script identity is malformed".into());
            return None;
        }
        let prefix = prefix.expect("validated prefix");
        return Some(Map::from_iter([
            ("audit_layout".into(), json!("bounded_full_spec_v7")),
            (
                "author_task".into(),
                subject.get("author_task").cloned().unwrap_or(Value::Null),
            ),
            (
                "session_id".into(),
                identity
                    .get("child_session_id")
                    .cloned()
                    .unwrap_or(Value::Null),
            ),
            ("parent_thread_id".into(), Value::Null),
            ("history_mode".into(), Value::Null),
            (
                "prefix_bytes".into(),
                prefix.get("bytes").cloned().unwrap_or(Value::Null),
            ),
            (
                "prefix_sha256".into(),
                prefix.get("sha256").cloned().unwrap_or(Value::Null),
            ),
            (
                "cutoff_line_inclusive".into(),
                prefix.get("line_count").cloned().unwrap_or(Value::Null),
            ),
            (
                "cutoff_timestamp_utc".into(),
                prefix
                    .get("last_included_outer_timestamp")
                    .cloned()
                    .unwrap_or(Value::Null),
            ),
            ("prefix_matches_capture".into(), Value::Null),
            (
                "capture_manifest_path".into(),
                capture.get("manifest_path").cloned().unwrap_or(Value::Null),
            ),
            (
                "capture_manifest_raw_sha256".into(),
                capture
                    .get("manifest_raw_sha256")
                    .cloned()
                    .unwrap_or(Value::Null),
            ),
            (
                "capture_manifest_logical_sha256".into(),
                capture
                    .get("manifest_logical_sha256")
                    .cloned()
                    .unwrap_or(Value::Null),
            ),
            ("capture_cutoff_timestamp_utc".into(), json!(capture_mtime)),
            (
                "capture_descriptor".into(),
                descriptor
                    .map(|value| Value::Object(value.clone()))
                    .unwrap_or(Value::Null),
            ),
            (
                "approved_auditor_script_sha256".into(),
                audit
                    .get("approved_auditor_script_sha256")
                    .cloned()
                    .unwrap_or(Value::Null),
            ),
            (
                "unknown_raw_claims".into(),
                json!([
                    "parent_thread_id",
                    "history_mode",
                    "root_spawn_evidence",
                    "operating_system_read_completeness",
                    "log_authenticity",
                    "cryptographic_chronology"
                ]),
            ),
            (
                "audit_limitations".into(),
                audit.get("limitations").cloned().unwrap_or(Value::Null),
            ),
        ]));
    }
    if evaluator_id != "agent_view_only" {
        errors.push("card-v7 audit is bound to the wrong evaluator slot".into());
        return None;
    }
    let required = [
        "audit_kind",
        "child_session",
        "concrete_unresolved_items",
        "cutoff",
        "cutoff_basis",
        "declared_author_content_allowlist",
        "known_source_and_capture_hashes",
        "limitations",
        "outer_and_payload_type_counts",
        "resolved_metadata_events",
    ];
    if !exact_keys(audit, &required) {
        errors.push("card-v7 audit fields differ from the bounded metadata contract".into());
        return None;
    }
    let child = audit.get("child_session").and_then(Value::as_object);
    if !child.is_some_and(|value| {
        exact_keys(
            value,
            &[
                "audited_prefix_binding",
                "excluded_lookahead",
                "id",
                "included_suffix",
                "log_path",
                "old_prefix",
            ],
        )
    }) {
        errors.push("card-v7 child-session metadata fields differ".into());
        return None;
    }
    let child = child.expect("checked above");
    let prefix = child
        .get("audited_prefix_binding")
        .and_then(Value::as_object);
    let suffix = child.get("included_suffix").and_then(Value::as_object);
    let old = child.get("old_prefix").and_then(Value::as_object);
    let lookahead = child.get("excluded_lookahead").and_then(Value::as_object);
    let hash_fields = ["bytes", "line_count", "sha256"];
    if !prefix.is_some_and(|value| exact_keys(value, &hash_fields))
        || !suffix.is_some_and(|value| {
            exact_keys(
                value,
                &[
                    "bytes",
                    "last_included_outer_timestamp",
                    "line_count",
                    "sha256",
                ],
            )
        })
        || !old.is_some_and(|value| exact_keys(value, &hash_fields))
        || !lookahead.is_some_and(|value| {
            exact_keys(
                value,
                &[
                    "bytes",
                    "excluded",
                    "line",
                    "payload_inspected",
                    "timestamp",
                ],
            )
        })
    {
        errors.push("card-v7 bounded prefix/suffix or excluded lookahead is malformed".into());
        return None;
    }
    let prefix = prefix.expect("validated prefix");
    let suffix = suffix.expect("validated suffix");
    let old = old.expect("validated old prefix");
    let lookahead = lookahead.expect("validated lookahead");
    let positive_hash = |row: &Map<String, Value>| {
        row.get("bytes")
            .and_then(Value::as_i64)
            .is_some_and(|n| n > 0)
            && row
                .get("line_count")
                .and_then(Value::as_i64)
                .is_some_and(|n| n > 0)
            && row
                .get("sha256")
                .and_then(Value::as_str)
                .is_some_and(is_sha256)
    };
    if !positive_hash(prefix)
        || !positive_hash(suffix)
        || !positive_hash(old)
        || prefix.get("bytes").and_then(Value::as_i64)
            != old
                .get("bytes")
                .and_then(Value::as_i64)
                .zip(suffix.get("bytes").and_then(Value::as_i64))
                .map(|(a, b)| a + b)
        || prefix.get("line_count").and_then(Value::as_i64)
            != old
                .get("line_count")
                .and_then(Value::as_i64)
                .zip(suffix.get("line_count").and_then(Value::as_i64))
                .map(|(a, b)| a + b)
        || lookahead.get("line").and_then(Value::as_i64)
            != prefix
                .get("line_count")
                .and_then(Value::as_i64)
                .map(|n| n + 1)
        || lookahead.get("excluded") != Some(&Value::Bool(true))
        || lookahead.get("payload_inspected") != Some(&Value::Bool(false))
        || lookahead
            .get("bytes")
            .and_then(Value::as_i64)
            .is_none_or(|n| n <= 0)
        || !lookahead
            .get("timestamp")
            .and_then(Value::as_str)
            .is_some_and(is_valid_utc)
        || !suffix
            .get("last_included_outer_timestamp")
            .and_then(Value::as_str)
            .is_some_and(is_valid_utc)
        || child.get("id") != scope.get("session_id")
        || !child
            .get("log_path")
            .and_then(Value::as_str)
            .is_some_and(|s| !s.is_empty())
    {
        errors.push("card-v7 bounded prefix/suffix or excluded lookahead is malformed".into());
        return None;
    }
    let known = audit
        .get("known_source_and_capture_hashes")
        .and_then(Value::as_object);
    let known_fields = [
        "author_self_audit_sha256",
        "manifest_logical_sha256",
        "manifest_raw_sha256",
        "old_audit_sha256",
        "schema_sha256",
        "worker_sha256",
    ];
    let cutoff = audit.get("cutoff").and_then(Value::as_str).unwrap_or("");
    if !known.is_some_and(|value| {
        exact_keys(value, &known_fields)
            && value
                .values()
                .all(|digest| digest.as_str().is_some_and(is_sha256))
            && value.get("manifest_raw_sha256") == scope.get("capture_manifest_raw_sha256")
            && value.get("manifest_logical_sha256") == scope.get("capture_manifest_logical_sha256")
    }) || !is_valid_utc(cutoff)
        || audit.get("cutoff_basis").and_then(Value::as_str)
            != Some(
                "captured-manifest filesystem timestamp; binds an audited prefix but is not cryptographic chronology proof",
            )
        || !nonempty_string_array(audit.get("limitations"))
    {
        errors.push("card-v7 source/capture hashes or cutoff metadata differ".into());
        return None;
    }
    let known = known.expect("validated source hash set");
    Some(Map::from_iter([
        ("audit_layout".into(), json!("bounded_card_v7")),
        (
            "author_task".into(),
            subject.get("author_task").cloned().unwrap_or(Value::Null),
        ),
        (
            "session_id".into(),
            child.get("id").cloned().unwrap_or(Value::Null),
        ),
        ("parent_thread_id".into(), Value::Null),
        ("history_mode".into(), Value::Null),
        (
            "prefix_bytes".into(),
            prefix.get("bytes").cloned().unwrap_or(Value::Null),
        ),
        (
            "prefix_sha256".into(),
            prefix.get("sha256").cloned().unwrap_or(Value::Null),
        ),
        (
            "cutoff_line_inclusive".into(),
            prefix.get("line_count").cloned().unwrap_or(Value::Null),
        ),
        (
            "cutoff_timestamp_utc".into(),
            suffix
                .get("last_included_outer_timestamp")
                .cloned()
                .unwrap_or(Value::Null),
        ),
        ("prefix_matches_capture".into(), Value::Null),
        (
            "capture_manifest_path".into(),
            scope
                .get("capture_manifest_path")
                .cloned()
                .unwrap_or(Value::Null),
        ),
        (
            "capture_manifest_raw_sha256".into(),
            known
                .get("manifest_raw_sha256")
                .cloned()
                .unwrap_or(Value::Null),
        ),
        (
            "capture_manifest_logical_sha256".into(),
            known
                .get("manifest_logical_sha256")
                .cloned()
                .unwrap_or(Value::Null),
        ),
        ("capture_cutoff_timestamp_utc".into(), json!(cutoff)),
        ("capture_descriptor".into(), Value::Null),
        (
            "old_prefix".into(),
            json!({"prefix_bytes": old.get("bytes"), "prefix_sha256": old.get("sha256"), "cutoff_line": old.get("line_count")}),
        ),
        (
            "unknown_raw_claims".into(),
            json!([
                "raw_author_task_in_native_audit",
                "parent_thread_id",
                "history_mode",
                "root_spawn_evidence",
                "operating_system_read_completeness",
                "log_authenticity",
                "cryptographic_chronology"
            ]),
        ),
        (
            "audit_limitations".into(),
            audit.get("limitations").cloned().unwrap_or(Value::Null),
        ),
    ]))
}

fn nonempty_string_array(value: Option<&Value>) -> bool {
    value.and_then(Value::as_array).is_some_and(|items| {
        !items.is_empty()
            && items
                .iter()
                .all(|item| item.as_str().is_some_and(|text| !text.is_empty()))
    })
}

fn v7_provenance(
    evaluator_id: &str,
    source_profile: &str,
    author_origin: &str,
    audit_kind: &str,
    audit_sha: &str,
    scope: &Map<String, Value>,
    adjudication: &Map<String, Value>,
    audit: &Map<String, Value>,
    native: &Map<String, Value>,
    root_attestation: Option<&Map<String, Value>>,
    capture_raw_sha: &str,
    capture_logical_sha: &str,
) -> Map<String, Value> {
    let limitations = adjudication
        .get("limitations")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let bounded_audit = adjudication
        .get("basis")
        .and_then(Value::as_object)
        .and_then(|basis| basis.get("bounded_native_audit"))
        .cloned()
        .unwrap_or(Value::Null);
    let scope_fields = [
        "capture_manifest_path",
        "capture_manifest_raw_sha256",
        "capture_manifest_logical_sha256",
        "session_id",
        "prefix_bytes",
        "prefix_sha256",
        "cutoff_line_inclusive",
        "cutoff_timestamp_utc",
        "capture_cutoff_timestamp_utc",
    ];
    let scope_value = scope_fields
        .iter()
        .filter_map(|field| {
            scope
                .get(*field)
                .map(|value| ((*field).to_owned(), value.clone()))
        })
        .collect::<Map<_, _>>();
    Map::from_iter([
        (
            "provenance_mode".into(),
            json!("conditional_root_attestation_with_bounded_metadata_audit"),
        ),
        ("qualification_status".into(), json!("QUALIFIED_ELIGIBLE")),
        ("source_isolation".into(), json!("UNKNOWN")),
        ("evaluator_id".into(), json!(evaluator_id)),
        ("source_profile".into(), json!(source_profile)),
        ("author_origin".into(), json!(author_origin)),
        ("audit_kind".into(), json!(audit_kind)),
        ("audit_sha256".into(), json!(audit_sha)),
        (
            "adjudication_sha256".into(),
            adjudication_ref_sha(adjudication).map_or(Value::Null, |sha| json!(sha)),
        ),
        (
            "scope".into(),
            Value::Object({
                let mut map = scope_value;
                map.insert("capture_manifest_raw_sha256".into(), json!(capture_raw_sha));
                map.insert(
                    "capture_manifest_logical_sha256".into(),
                    json!(capture_logical_sha),
                );
                map
            }),
        ),
        (
            "root_attestation".into(),
            root_attestation
                .cloned()
                .map(Value::Object)
                .unwrap_or(Value::Null),
        ),
        (
            "not_claimed".into(),
            adjudication
                .get("not_claimed")
                .cloned()
                .unwrap_or(Value::Null),
        ),
        (
            "limitations".into(),
            json!({
                "adjudication": limitations,
                "bounded_audit_review": bounded_audit,
                "native_audit": audit.get("limitations").cloned().unwrap_or(Value::Null),
            }),
        ),
        (
            "unknown_raw_claims".into(),
            native
                .get("unknown_raw_claims")
                .cloned()
                .unwrap_or(Value::Null),
        ),
    ])
}

fn adjudication_ref_sha(adjudication: &Map<String, Value>) -> Option<&str> {
    adjudication
        .get("_bound_adjudication_reference")
        .and_then(|value| value.get("sha256"))
        .and_then(Value::as_str)
}

#[derive(Clone)]
pub(super) struct ChainCapture {
    pub(super) wrapper: Map<String, Value>,
    pub(super) source_map: Map<String, Value>,
    pub(super) manifest_path: PathBuf,
}

pub(super) struct ChainArtifact {
    pub(super) raw: Vec<u8>,
    pub(super) path: PathBuf,
    pub(super) reference: Map<String, Value>,
}

#[allow(clippy::too_many_arguments)]
fn validate_origin_chain_v1(
    evaluator_id: &str,
    qualification: &Map<String, Value>,
    source_profile: &str,
    author_origin: &str,
    capture_raw_sha: &str,
    capture_logical_sha: &str,
    capture_manifest_path: Option<&str>,
    captured_source_files_count: Option<usize>,
    repository_root: &Path,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
    provenance: &mut Map<String, Value>,
) -> bool {
    let Some(profile_slot) = EVALUATOR_WIRE_ID_BY_SLOT
        .iter()
        .find(|(_, wire)| *wire == evaluator_id)
        .map(|(slot, _)| *slot)
        .filter(|slot| *slot != "production")
    else {
        errors.push(format!("{evaluator_id} has no source-origin chain adapter"));
        return false;
    };
    if !exact_keys(qualification, &["kind", "chain", "chain_review"]) {
        errors.push("source-origin chain qualification fields differ".into());
        return false;
    }
    let mut referenced = BTreeSet::<(String, String)>::new();
    let chain_ref = read_chain_ref(
        repository_root,
        qualification.get("chain"),
        "chain record",
        &mut referenced,
        errors,
        gaps,
    );
    let review_ref = read_chain_ref(
        repository_root,
        qualification.get("chain_review"),
        "chain review",
        &mut referenced,
        errors,
        gaps,
    );
    let (Some(chain_artifact), Some(chain_reference)) =
        (chain_ref, reference_object(qualification.get("chain")))
    else {
        return false;
    };
    let (Some(review_artifact), Some(review_reference)) = (
        review_ref,
        reference_object(qualification.get("chain_review")),
    ) else {
        return false;
    };
    let chain_digest = chain_reference
        .get("sha256")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    let review_digest = review_reference
        .get("sha256")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();
    referenced.remove(&(
        chain_reference
            .get("path")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        chain_digest.clone(),
    ));
    referenced.remove(&(
        review_reference
            .get("path")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_owned(),
        review_digest.clone(),
    ));
    let review_raw = review_artifact.raw;
    let Some(chain) = read_object(
        &chain_artifact.raw,
        "source-origin chain record",
        errors,
        gaps,
    ) else {
        return false;
    };
    let required_chain = [
        "format",
        "record_version",
        "evaluator_id",
        "historical_baseline",
        "successor_input",
        "successor_author",
        "successor_audit",
        "successor_output",
        "limitations",
    ];
    let optional_chain = ["predecessor_continuation"];
    if !exact_keys_with_optional(&chain, &required_chain, &optional_chain) {
        errors.push("source-origin chain record fields differ".into());
        return false;
    }
    if chain.get("format").and_then(Value::as_str)
        != Some("identitybench.v5.source-origin-chain.v1")
        || chain.get("record_version").and_then(Value::as_i64) != Some(1)
        || chain.get("evaluator_id").and_then(Value::as_str) != Some(evaluator_id)
    {
        errors.push("source-origin chain record version or evaluator differs".into());
    }

    let Some(hist) = chain.get("historical_baseline").and_then(Value::as_object) else {
        errors.push("source-origin chain historical baseline must be an object".into());
        return false;
    };
    let required_hist = [
        "adjudication",
        "audit",
        "executed_extractor",
        "capture",
        "author",
        "prefix",
    ];
    let optional_hist = ["baseline_adjudication", "reconstructed_inventory"];
    if !exact_keys_with_optional(hist, &required_hist, &optional_hist) {
        errors.push("source-origin chain historical baseline fields differ".into());
        return false;
    }
    let hist_author = parse_chain_author(hist.get("author"), "historical", profile_slot, errors);
    let hist_prefix = parse_chain_prefix(hist.get("prefix"), "historical", errors);
    let hist_capture = parse_chain_capture(
        hist.get("capture"),
        "historical",
        profile_slot,
        evaluator_id,
        repository_root,
        &mut referenced,
        errors,
        gaps,
    );
    let hist_adjudication = read_chain_json_ref(
        repository_root,
        hist.get("adjudication"),
        "historical adjudication",
        &mut referenced,
        errors,
        gaps,
    );
    let hist_audit = read_chain_json_ref(
        repository_root,
        hist.get("audit"),
        "historical audit",
        &mut referenced,
        errors,
        gaps,
    );
    let hist_extractor = read_chain_ref(
        repository_root,
        hist.get("executed_extractor"),
        "historical executed extractor",
        &mut referenced,
        errors,
        gaps,
    );
    let (
        Some(hist_author),
        Some(hist_prefix),
        Some(hist_capture),
        Some(hist_adjudication),
        Some(hist_audit),
        Some(hist_extractor),
    ) = (
        hist_author,
        hist_prefix,
        hist_capture,
        hist_adjudication,
        hist_audit,
        hist_extractor,
    )
    else {
        gaps.push("source-origin chain historical core evidence is incomplete".into());
        return false;
    };
    if hist_prefix
        .get("start_offset_bytes")
        .and_then(Value::as_i64)
        != Some(0)
    {
        errors.push("source-origin chain historical prefix must start at zero".into());
    }
    if hist_prefix.get("session_id") != hist_author.get("session_id") {
        errors.push("source-origin chain historical prefix session differs from its author".into());
    }
    if hist_capture.source_map.get("source_profile") != hist_author.get("source_profile")
        || hist_capture.source_map.get("author_origin") != hist_author.get("author_origin")
    {
        errors.push("source-origin chain historical capture and author differ".into());
    }
    let hist_adjudication_map = &hist_adjudication.0;
    let hist_audit_map = &hist_audit.0;
    let audit_kind = hist_audit_map
        .get("audit_kind")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !matches!(audit_kind, AUDIT_FULL_V7 | AUDIT_CARD_V7) {
        gaps.push(
            "source-origin chain historical baseline uses an unsupported v7 audit layout".into(),
        );
    }
    if hist_adjudication_map
        .get("record_type")
        .and_then(Value::as_str)
        != Some("source_origin_adjudication")
        || hist_adjudication_map
            .get("record_version")
            .and_then(Value::as_i64)
            != Some(2)
    {
        gaps.push("source-origin chain historical adjudication has an unsupported schema".into());
    }
    if hist_adjudication_map.get("subject")
        != Some(&json!({
            "author_origin": hist_author.get("author_origin"),
            "author_task": hist_author.get("task"),
            "source_profile": hist_author.get("source_profile"),
            "evaluator_id": evaluator_id,
        }))
    {
        errors.push("source-origin chain historical adjudication subject differs".into());
    }
    let hist_scope = hist_adjudication_map
        .get("scope")
        .and_then(Value::as_object);
    if !hist_scope.is_some_and(|scope| {
        scope.get("session_id") == hist_author.get("session_id")
            && scope.get("prefix_bytes") == hist_prefix.get("bytes")
            && scope.get("prefix_sha256") == hist_prefix.get("sha256")
            && scope.get("capture_manifest_raw_sha256")
                == hist_capture
                    .wrapper
                    .get("manifest")
                    .and_then(|m| m.get("sha256"))
            && scope.get("capture_manifest_logical_sha256")
                == hist_capture.wrapper.get("logical_sha256")
    }) {
        errors.push(
            "source-origin chain historical adjudication does not bind core capture and prefix"
                .into(),
        );
    }
    if hist_adjudication_map.get("status").and_then(Value::as_str) != Some("QUALIFIED_ELIGIBLE") {
        gaps.push("source-origin chain historical baseline is not positively adjudicated".into());
    }
    validate_historical_normative(evaluator_id, hist_adjudication_map, errors);
    let hist_scope_map = hist_scope;
    if let Some(scope) = hist_scope_map {
        let expected_scope = [
            "capture_manifest_raw_sha256",
            "capture_manifest_logical_sha256",
            "session_id",
            "prefix_bytes",
            "prefix_sha256",
            "cutoff_line_inclusive",
            "cutoff_timestamp_utc",
            "capture_manifest_path",
            "capture_cutoff_timestamp_utc",
            "capture_cutoff_timestamp_character",
            "excluded_lookahead",
        ];
        if !exact_keys(scope, &expected_scope) {
            errors.push("source-origin chain historical scope fields differ from v7".into());
        }
        let excluded = scope.get("excluded_lookahead").and_then(Value::as_object);
        let expected_excluded: &[&str] = if evaluator_id == "full_spec_blind" {
            &["line", "timestamp_utc", "payload"]
        } else {
            &["line", "payload"]
        };
        if scope.get("capture_manifest_path")
            != hist_capture
                .wrapper
                .get("manifest")
                .and_then(|m| m.get("path"))
            || scope
                .get("capture_cutoff_timestamp_character")
                .and_then(Value::as_str)
                != Some(capture_cutoff_character(evaluator_id))
            || scope.get("cutoff_line_inclusive") != hist_prefix.get("line_count")
            || !excluded.is_some_and(|value| {
                exact_keys(value, expected_excluded)
                    && value.get("line").and_then(Value::as_i64)
                        == scope
                            .get("cutoff_line_inclusive")
                            .and_then(Value::as_i64)
                            .map(|n| n + 1)
                    && value.get("payload").and_then(Value::as_str).is_some()
                    && (evaluator_id != "full_spec_blind"
                        || value
                            .get("timestamp_utc")
                            .and_then(Value::as_str)
                            .is_some_and(is_valid_utc))
            })
        {
            errors.push("source-origin chain historical scope metadata differs".into());
        }
    }
    let hist_native = native_origin_claims(
        hist_audit_map,
        evaluator_id,
        repository_root,
        errors,
        gaps,
        hist_adjudication_map
            .get("subject")
            .and_then(Value::as_object),
        hist_scope_map,
    );
    let Some(hist_native) = hist_native else {
        gaps.push("source-origin chain historical baseline audit claims are unsupported".into());
        return false;
    };
    compare_historical_claims(
        &hist_native,
        hist_audit_map,
        &hist_author,
        &hist_prefix,
        &hist_capture,
        &hist_extractor.reference,
        evaluator_id,
        hist_scope_map,
        errors,
    );
    validate_historical_evidence(
        hist_adjudication_map,
        hist_audit_map,
        hist.get("audit").and_then(Value::as_object),
        &hist_extractor.reference,
        hist_scope_map,
        &hist_prefix,
        &hist_capture,
        evaluator_id,
        errors,
        gaps,
    );

    // Re-run the legacy source-origin checks over the historical author record. This rejects stale
    // or ineligible historical evidence before it can be used as a continuation base.
    let mut legacy_qualification = Map::new();
    legacy_qualification.insert("kind".into(), json!("author_origin"));
    legacy_qualification.insert(
        "adjudication".into(),
        hist.get("adjudication").cloned().unwrap_or(Value::Null),
    );
    legacy_qualification.insert(
        "origin_audit".into(),
        hist.get("audit").cloned().unwrap_or(Value::Null),
    );
    let mut legacy_adjudication = hist_adjudication_map.clone();
    legacy_adjudication.insert(
        "_bound_adjudication_reference".into(),
        hist.get("adjudication").cloned().unwrap_or(Value::Null),
    );
    let legacy_ok = validate_author_origin(
        evaluator_id,
        &legacy_qualification,
        hist_author
            .get("source_profile")
            .and_then(Value::as_str)
            .unwrap_or(""),
        hist_author
            .get("author_origin")
            .and_then(Value::as_str)
            .unwrap_or(""),
        "0".repeat(64).as_str(),
        hist_capture
            .wrapper
            .get("logical_sha256")
            .and_then(Value::as_str)
            .unwrap_or(""),
        hist_capture
            .wrapper
            .get("manifest")
            .and_then(|m| m.get("path"))
            .and_then(Value::as_str),
        hist_capture
            .source_map
            .get("files")
            .and_then(Value::as_array)
            .map(Vec::len),
        repository_root,
        errors,
        gaps,
        &mut Map::new(),
    );
    let _ = legacy_adjudication;
    if !legacy_ok {
        gaps.push("source-origin chain historical legacy binding is not eligible".into());
    }

    let mut handoff = hist_capture.clone();
    let mut completeness_reviewer: Option<String> = None;
    if let Some(pred_value) = chain.get("predecessor_continuation") {
        if let Some(predecessor_handoff) = validate_predecessor(
            pred_value,
            &hist_author,
            &hist_prefix,
            &hist_capture,
            evaluator_id,
            profile_slot,
            repository_root,
            &mut referenced,
            errors,
            gaps,
        ) {
            handoff = predecessor_handoff;
        }
    }
    let reconstructed_ref = if let Some(value) = hist.get("reconstructed_inventory") {
        let Some((inventory, reference)) = read_chain_json_ref(
            repository_root,
            Some(value),
            "reconstructed inventory",
            &mut referenced,
            errors,
            gaps,
        ) else {
            return false;
        };
        validate_reconstruction(
            &inventory,
            evaluator_id,
            profile_slot,
            &hist_author,
            &handoff,
            repository_root,
            &mut referenced,
            &mut completeness_reviewer,
            errors,
            gaps,
        );
        if let Some(current) = inventory.get("current_capture") {
            if let Some(parsed) = parse_chain_capture(
                Some(current),
                "reconstructed current",
                profile_slot,
                evaluator_id,
                repository_root,
                &mut referenced,
                errors,
                gaps,
            ) {
                handoff = parsed;
            }
        }
        Some(reference)
    } else {
        None
    };

    let baseline_ref = if let Some(value) = hist.get("baseline_adjudication") {
        let Some((baseline, reference)) = read_chain_json_ref(
            repository_root,
            Some(value),
            "baseline adjudication",
            &mut referenced,
            errors,
            gaps,
        ) else {
            return false;
        };
        validate_baseline_adjudication(
            &baseline,
            &hist,
            &hist_author,
            chain.get("predecessor_continuation"),
            &handoff,
            evaluator_id,
            repository_root,
            &mut referenced,
            errors,
            gaps,
        );
        Some(reference)
    } else {
        None
    };
    if reconstructed_ref.is_some() && baseline_ref.is_none() {
        errors
            .push("source-origin chain reconstruction requires a new baseline adjudication".into());
    }
    let _ = (baseline_ref, reconstructed_ref);

    let Some(successor_input) = chain.get("successor_input").and_then(Value::as_object) else {
        errors.push("source-origin chain successor input must be an object".into());
        return false;
    };
    if !exact_keys(successor_input, &["capture", "source_map"]) {
        errors.push("source-origin chain successor input fields differ".into());
    }
    let input_capture = parse_chain_capture(
        successor_input.get("capture"),
        "successor input",
        profile_slot,
        evaluator_id,
        repository_root,
        &mut referenced,
        errors,
        gaps,
    );
    let input_map = parse_source_map(
        successor_input.get("source_map"),
        "successor input",
        evaluator_id,
        profile_slot,
        errors,
    );
    if let (Some(input_capture), Some(input_map)) = (&input_capture, &input_map) {
        if input_capture.wrapper != handoff.wrapper
            || input_capture.source_map != handoff.source_map
            || input_map != &input_capture.source_map
        {
            errors
                .push("source-origin chain successor input differs from immediate handoff".into());
        }
    }

    let Some(successor) = chain.get("successor_author").and_then(Value::as_object) else {
        errors.push("source-origin chain successor author must be an object".into());
        return false;
    };
    let successor_fields = [
        "task",
        "session_id",
        "source_profile",
        "author_origin",
        "spawn_evidence",
        "root_attestation",
    ];
    if !exact_keys(successor, &successor_fields) {
        errors.push("source-origin chain successor author fields differ".into());
    }
    let successor_task = successor.get("task").and_then(Value::as_str).unwrap_or("");
    let successor_session = successor
        .get("session_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    if successor_task.is_empty() || successor_session.is_empty() {
        errors.push("source-origin chain successor task/session is malformed".into());
    }
    let successor_profile = match profile_for(
        profile_slot,
        successor.get("source_profile"),
        successor.get("author_origin"),
        "successor",
        errors,
    ) {
        Some(profile) => profile,
        None => return false,
    };
    if successor_task
        == hist_author
            .get("task")
            .and_then(Value::as_str)
            .unwrap_or("")
        || successor_session
            == hist_author
                .get("session_id")
                .and_then(Value::as_str)
                .unwrap_or("")
        || completeness_reviewer.as_deref() == Some(successor_task)
    {
        errors.push(
            "source-origin chain successor author overlaps a historical author or reviewer".into(),
        );
    }
    let input_map_sha =
        canonical_sha(&json!(successor_input.get("source_map"))).unwrap_or_default();
    validate_spawn_evidence(
        successor,
        successor_task,
        successor_session,
        evaluator_id,
        repository_root,
        &input_map_sha,
        &mut referenced,
        errors,
        gaps,
    );
    validate_successor_root_attestation(
        successor,
        evaluator_id,
        successor_task,
        successor_session,
        &input_map_sha,
        errors,
        gaps,
    );

    let Some(successor_audit) = chain.get("successor_audit").and_then(Value::as_object) else {
        errors.push("source-origin chain successor audit must be an object".into());
        return false;
    };
    if !exact_keys(
        successor_audit,
        &[
            "report",
            "executed_extractor",
            "prefix",
            "cutoff",
            "disclosed_unknowns",
        ],
    ) {
        errors.push("source-origin chain successor audit fields differ".into());
    }
    let Some((report, _report_ref)) = read_chain_json_ref(
        repository_root,
        successor_audit.get("report"),
        "successor audit report",
        &mut referenced,
        errors,
        gaps,
    ) else {
        return false;
    };
    let Some(extractor) = read_chain_ref(
        repository_root,
        successor_audit.get("executed_extractor"),
        "successor executed extractor",
        &mut referenced,
        errors,
        gaps,
    ) else {
        return false;
    };
    let output_capture = parse_chain_capture(
        chain.get("successor_output"),
        "successor output",
        profile_slot,
        evaluator_id,
        repository_root,
        &mut referenced,
        errors,
        gaps,
    );
    if !validate_successor_audit(
        &report,
        successor_audit,
        successor,
        successor_task,
        successor_session,
        evaluator_id,
        &extractor,
        output_capture.as_ref(),
        input_capture.as_ref(),
        errors,
        gaps,
    ) {
        gaps.push("source-origin chain successor audit evidence is incomplete".into());
    }
    let Some(output_capture) = output_capture else {
        return false;
    };
    if output_capture
        .source_map
        .get("evaluator_id")
        .and_then(Value::as_str)
        != Some(evaluator_id)
        || output_capture
            .source_map
            .get("source_profile")
            .and_then(Value::as_str)
            != Some(source_profile)
        || output_capture
            .source_map
            .get("author_origin")
            .and_then(Value::as_str)
            != Some(author_origin)
        || output_capture
            .wrapper
            .get("manifest")
            .and_then(|m| m.get("path"))
            .and_then(Value::as_str)
            != capture_manifest_path
        || output_capture
            .wrapper
            .get("manifest")
            .and_then(|m| m.get("sha256"))
            .and_then(Value::as_str)
            != Some(capture_raw_sha)
        || output_capture
            .wrapper
            .get("logical_sha256")
            .and_then(Value::as_str)
            != Some(capture_logical_sha)
        || output_capture
            .source_map
            .get("files")
            .and_then(Value::as_array)
            .map(Vec::len)
            != captured_source_files_count
    {
        errors.push(
            "source-origin chain successor output differs from current evaluator capture".into(),
        );
    }
    if output_capture.source_map.get("source_profile") != successor.get("source_profile")
        || output_capture.source_map.get("author_origin") != successor.get("author_origin")
    {
        errors.push(
            "source-origin chain successor output profile differs from successor author".into(),
        );
    }

    validate_chain_limitations(chain.get("limitations"), errors);

    validate_chain_review(
        &review_raw,
        &review_reference,
        &chain_reference,
        &referenced,
        hist_author
            .get("task")
            .and_then(Value::as_str)
            .unwrap_or(""),
        successor_task,
        evaluator_id,
        errors,
        gaps,
    );
    let mut coverage_limits = string_array(chain.get("limitations"));
    coverage_limits.extend(string_array(
        successor
            .get("root_attestation")
            .and_then(Value::as_object)
            .and_then(|attestation| attestation.get("limitations")),
    ));
    if let Some(review) = read_object(&review_raw, "source-origin chain review", errors, gaps) {
        coverage_limits.extend(string_array(review.get("limitations")));
    }
    if errors.is_empty() && gaps.is_empty() {
        let input_map_json = successor_input
            .get("source_map")
            .cloned()
            .unwrap_or(Value::Null);
        provenance.extend(Map::from_iter([
            ("provenance_mode".into(), json!("conditional_root_attestation_with_bounded_metadata_chain")),
            ("qualification_status".into(), json!("QUALIFIED_ELIGIBLE")),
            ("source_isolation".into(), json!("UNKNOWN")),
            ("evaluator_id".into(), json!(evaluator_id)),
            ("source_profile".into(), json!(source_profile)),
            ("author_origin".into(), json!(author_origin)),
            ("chain_sha256".into(), json!(chain_digest)),
            ("chain_review_sha256".into(), json!(review_digest)),
            ("current_capture_manifest_path".into(), json!(capture_manifest_path)),
            ("current_capture_manifest_sha256".into(), json!(capture_raw_sha)),
            ("current_capture_logical_sha256".into(), json!(capture_logical_sha)),
            ("current_source_map_sha256".into(), json!(canonical_sha(&json!(output_capture.source_map)))),
            ("input_source_map_sha256".into(), json!(canonical_sha(&input_map_json))),
            ("authors".into(), json!([hist_author, {"task":successor_task,"session_id":successor_session,"source_profile":successor_profile.profile_id,"author_origin":successor_profile.author_origin}])),
            ("prefix_session_ids".into(), json!([hist_prefix.get("session_id"), successor_audit.get("prefix").and_then(|p|p.get("session_id"))])),
            ("coverage_limits".into(), json!(coverage_limits)),
            ("historical_unknown_claims".into(), hist_native.get("unknown_raw_claims").cloned().unwrap_or(Value::Null)),
            ("disclosed_unknowns".into(), successor_audit.get("disclosed_unknowns").cloned().unwrap_or(Value::Null)),
            ("chronology".into(), json!("filesystem_mtime_weak")),
            ("not_claimed".into(), json!(["OS-level isolation", "log authenticity", "cryptographic chronology", "global absence", "semantic conformance"])),
        ]));
    }
    errors.is_empty() && gaps.is_empty()
}

fn exact_keys_with_optional(
    object: &Map<String, Value>,
    required: &[&str],
    optional: &[&str],
) -> bool {
    required.iter().all(|field| object.contains_key(*field))
        && object
            .keys()
            .all(|key| required.contains(&key.as_str()) || optional.contains(&key.as_str()))
}

fn reference_object(value: Option<&Value>) -> Option<Map<String, Value>> {
    value.and_then(Value::as_object).cloned()
}

fn read_chain_ref(
    root: &Path,
    reference: Option<&Value>,
    label: &str,
    referenced: &mut BTreeSet<(String, String)>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Option<ChainArtifact> {
    let Some(reference) = reference.and_then(Value::as_object) else {
        errors.push(format!(
            "source-origin chain {label} artifact reference must be an object"
        ));
        return None;
    };
    if !exact_keys(reference, &["path", "sha256"]) {
        errors.push(format!(
            "source-origin chain {label} artifact reference fields differ"
        ));
        return None;
    }
    let path = reference.get("path").and_then(Value::as_str).unwrap_or("");
    let digest = reference
        .get("sha256")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !path.starts_with("reports/v5/conformance/") || !is_sha256(digest) {
        errors.push(format!(
            "source-origin chain {label} path or digest is outside conformance evidence"
        ));
        return None;
    }
    let normalized = json!({"path":path,"sha256":digest});
    let Some((raw, full_path)) = read_bound(
        root,
        Some(&normalized),
        &format!("source-origin chain {label}"),
        errors,
        gaps,
    ) else {
        return None;
    };
    referenced.insert((path.to_owned(), digest.to_owned()));
    Some(ChainArtifact {
        raw,
        path: full_path,
        reference: reference.clone(),
    })
}

fn read_chain_json_ref(
    root: &Path,
    reference: Option<&Value>,
    label: &str,
    referenced: &mut BTreeSet<(String, String)>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Option<(Map<String, Value>, Map<String, Value>)> {
    let artifact = read_chain_ref(root, reference, label, referenced, errors, gaps)?;
    let object = read_object(
        &artifact.raw,
        &format!("source-origin chain {label}"),
        errors,
        gaps,
    )?;
    Some((object, artifact.reference))
}

fn profile_for(
    slot: &str,
    profile_id: Option<&Value>,
    origin_id: Option<&Value>,
    label: &str,
    errors: &mut Vec<String>,
) -> Option<SourceProfile> {
    let (Some(profile_id), Some(origin_id)) = (
        profile_id.and_then(Value::as_str),
        origin_id.and_then(Value::as_str),
    ) else {
        errors.push(format!(
            "source-origin chain {label} profile identity is malformed"
        ));
        return None;
    };
    if profile_id.is_empty() || origin_id.is_empty() {
        errors.push(format!(
            "source-origin chain {label} profile identity is malformed"
        ));
        return None;
    }
    let Ok(profile) = resolve_source_profile(slot, Some(profile_id)) else {
        errors.push(format!(
            "source-origin chain {label} uses an unregistered source profile"
        ));
        return None;
    };
    if profile.evaluator_id != slot || profile.author_origin != origin_id {
        errors.push(format!(
            "source-origin chain {label} profile/origin registration differs"
        ));
        return None;
    }
    Some(profile)
}

fn parse_source_map(
    value: Option<&Value>,
    label: &str,
    evaluator_id: &str,
    profile_slot: &str,
    errors: &mut Vec<String>,
) -> Option<Map<String, Value>> {
    let Some(map) = value.and_then(Value::as_object) else {
        errors.push(format!(
            "source-origin chain {label} source map must be an object"
        ));
        return None;
    };
    if !exact_keys(
        map,
        &["evaluator_id", "source_profile", "author_origin", "files"],
    ) {
        errors.push(format!(
            "source-origin chain {label} source map fields differ"
        ));
        return None;
    }
    if map.get("evaluator_id").and_then(Value::as_str) != Some(evaluator_id) {
        errors.push(format!(
            "source-origin chain {label} wire evaluator ID differs"
        ));
        return None;
    }
    let profile = profile_for(
        profile_slot,
        map.get("source_profile"),
        map.get("author_origin"),
        label,
        errors,
    )?;
    let Some(files) = map.get("files").and_then(Value::as_array) else {
        errors.push(format!(
            "source-origin chain {label} source map files are malformed"
        ));
        return None;
    };
    let mut normalized = Vec::new();
    let mut seen = BTreeSet::new();
    for (index, row) in files.iter().enumerate() {
        let Some(row) = row.as_object() else {
            errors.push(format!(
                "source-origin chain {label} source file {index} must be an object"
            ));
            continue;
        };
        if !exact_keys(row, &["path", "sha256"]) {
            errors.push(format!(
                "source-origin chain {label} source file {index} fields differ"
            ));
            continue;
        }
        let path = row.get("path").and_then(Value::as_str).unwrap_or("");
        let digest = row.get("sha256").and_then(Value::as_str).unwrap_or("");
        if path.is_empty()
            || path.starts_with('/')
            || path.contains('\\')
            || path
                .split('/')
                .any(|part| part.is_empty() || part == "." || part == "..")
            || !seen.insert(path.to_owned())
        {
            errors.push(format!(
                "source-origin chain {label} source file path is unsafe or duplicated"
            ));
            continue;
        }
        if !is_sha256(digest) {
            errors.push(format!(
                "source-origin chain {label} source file {index} digest is malformed"
            ));
            continue;
        }
        normalized.push(json!({"path":path,"sha256":digest}));
    }
    let registered = profile.source_files;
    let provided = normalized
        .iter()
        .filter_map(|row| row.get("path").and_then(Value::as_str))
        .collect::<Vec<_>>();
    if provided != registered.iter().map(String::as_str).collect::<Vec<_>>() {
        errors.push(format!(
            "source-origin chain {label} does not bind the registered complete closure"
        ));
    }
    Some(Map::from_iter([
        ("evaluator_id".into(), json!(evaluator_id)),
        (
            "source_profile".into(),
            map.get("source_profile").cloned().unwrap_or(Value::Null),
        ),
        (
            "author_origin".into(),
            map.get("author_origin").cloned().unwrap_or(Value::Null),
        ),
        ("files".into(), Value::Array(normalized)),
    ]))
}

fn parse_chain_capture(
    value: Option<&Value>,
    label: &str,
    profile_slot: &str,
    evaluator_id: &str,
    repository_root: &Path,
    referenced: &mut BTreeSet<(String, String)>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Option<ChainCapture> {
    let Some(wrapper) = value.and_then(Value::as_object) else {
        errors.push(format!(
            "source-origin chain {label} capture must be an object"
        ));
        return None;
    };
    if !exact_keys(wrapper, &["manifest", "logical_sha256", "source_map"]) {
        errors.push(format!("source-origin chain {label} capture fields differ"));
        return None;
    }
    let logical_sha = wrapper
        .get("logical_sha256")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !is_sha256(logical_sha) {
        errors.push(format!(
            "source-origin chain {label} logical digest must be lowercase SHA-256"
        ));
    }
    let source_map = parse_source_map(
        wrapper.get("source_map"),
        label,
        evaluator_id,
        profile_slot,
        errors,
    )?;
    let Some(manifest_artifact) = read_chain_ref(
        repository_root,
        wrapper.get("manifest"),
        &format!("{label} capture manifest"),
        referenced,
        errors,
        gaps,
    ) else {
        return None;
    };
    let Some(manifest) = read_object(
        &manifest_artifact.raw,
        &format!("source-origin chain {label} capture manifest"),
        errors,
        gaps,
    ) else {
        return None;
    };
    let required = [
        "source_profile",
        "author_origin",
        "source_files",
        "manifest_sha256",
    ];
    let legacy = [
        "source_profile",
        "author_origin",
        "source_files",
        "manifest_sha256",
        "evaluator_id",
    ];
    let checkpoint = [
        "format",
        "evaluator_id",
        "source_profile",
        "author_origin",
        "entrypoint",
        "entrypoint_module",
        "schema_sha256",
        "capture_status",
        "source_files",
        "manifest_sha256",
    ];
    if !exact_keys(&manifest, &required)
        && !exact_keys(&manifest, &legacy)
        && !exact_keys(&manifest, &checkpoint)
    {
        errors.push(format!(
            "source-origin chain {label} capture manifest has unsupported fields"
        ));
        return None;
    }
    let unsigned = without_key(&manifest, "manifest_sha256");
    if manifest.get("manifest_sha256").and_then(Value::as_str) != Some(logical_sha)
        || canonical_sha(&Value::Object(unsigned)).as_deref() != Some(logical_sha)
    {
        errors.push(format!(
            "source-origin chain {label} capture logical digest differs"
        ));
    }
    if manifest.get("source_profile") != source_map.get("source_profile")
        || manifest.get("author_origin") != source_map.get("author_origin")
        || (manifest.contains_key("evaluator_id")
            && manifest.get("evaluator_id").and_then(Value::as_str) != Some(profile_slot))
    {
        errors.push(format!(
            "source-origin chain {label} capture manifest identity differs"
        ));
    }
    if exact_keys(&manifest, &checkpoint) {
        let profile_id = source_map
            .get("source_profile")
            .and_then(Value::as_str)
            .unwrap_or("");
        let profile = resolve_source_profile(profile_slot, Some(profile_id)).ok();
        if manifest.get("format").and_then(Value::as_str)
            != Some("identitybench.v5.shared-conformance-source-checkpoint.v1")
            || manifest.get("capture_status").and_then(Value::as_str)
                != Some("verified_source_copy")
            || profile.as_ref().is_none_or(|profile| {
                manifest.get("entrypoint").and_then(Value::as_str) != Some(profile.entrypoint)
                    || manifest.get("entrypoint_module").and_then(Value::as_str)
                        != profile.entrypoint_module
            })
            || !manifest
                .get("schema_sha256")
                .and_then(Value::as_str)
                .is_some_and(is_sha256)
        {
            errors.push(format!(
                "source-origin chain {label} source checkpoint metadata differs"
            ));
        }
    }
    let manifest_rows = manifest.get("source_files").and_then(Value::as_array);
    let map_rows = source_map
        .get("files")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if manifest_rows.is_none_or(|rows| rows.len() != map_rows.len()) {
        errors.push(format!(
            "source-origin chain {label} capture source row count differs"
        ));
        return None;
    }
    let root = repository_root
        .canonicalize()
        .unwrap_or_else(|_| repository_root.to_path_buf());
    for (index, (row, map_row)) in manifest_rows
        .unwrap()
        .iter()
        .zip(map_rows.iter())
        .enumerate()
    {
        let Some(row) = row.as_object() else {
            errors.push(format!(
                "source-origin chain {label} capture source row {index} must be an object"
            ));
            continue;
        };
        let keys = ["path", "sha256"];
        let optional = ["size_bytes"];
        if !exact_keys(row, &keys) && !exact_keys_with_optional(row, &keys, &optional) {
            errors.push(format!(
                "source-origin chain {label} capture source row {index} fields differ"
            ));
            continue;
        }
        if row.get("path") != map_row.get("path") || row.get("sha256") != map_row.get("sha256") {
            errors.push(format!(
                "source-origin chain {label} capture source row differs from source map"
            ));
            continue;
        }
        if row
            .get("size_bytes")
            .is_some_and(|size| size.as_u64().is_none())
        {
            errors.push(format!(
                "source-origin chain {label} capture source size is malformed"
            ));
            continue;
        }
        let relative = row.get("path").and_then(Value::as_str).unwrap_or("");
        match bound_source_bytes(&root, &manifest_artifact.path, relative) {
            Ok(raw) => {
                if sha256_bytes(&raw) != row.get("sha256").and_then(Value::as_str).unwrap_or("") {
                    errors.push(format!(
                        "source-origin chain {label} captured source file digest differs"
                    ));
                }
                if row
                    .get("size_bytes")
                    .and_then(Value::as_u64)
                    .is_some_and(|size| size != raw.len() as u64)
                {
                    errors.push(format!(
                        "source-origin chain {label} captured source size differs"
                    ));
                }
            }
            Err(reason) => gaps.push(format!(
                "source-origin chain {label} source bytes unavailable: {reason}"
            )),
        }
    }
    if exact_keys(&manifest, &checkpoint) {
        let schema = map_rows.iter().find(|row| {
            row.get("path").and_then(Value::as_str) == Some("src/identitybench/v5/schemas.py")
        });
        if schema.is_none_or(|row| row.get("sha256") != manifest.get("schema_sha256")) {
            errors.push(format!(
                "source-origin chain {label} checkpoint schema digest differs"
            ));
        }
    }
    Some(ChainCapture {
        wrapper: wrapper.clone(),
        source_map,
        manifest_path: manifest_artifact.path,
    })
}

fn bound_source_bytes(
    root: &Path,
    manifest_path: &Path,
    relative: &str,
) -> Result<Vec<u8>, String> {
    let relative_path = Path::new(relative);
    if relative.is_empty()
        || relative_path.is_absolute()
        || relative.contains('\\')
        || relative
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return Err("source path is unsafe".into());
    }
    let root = root.canonicalize().map_err(|error| error.to_string())?;
    let candidate = manifest_path.parent().unwrap_or(&root).join(relative_path);
    if !candidate.starts_with(&root) {
        return Err("source path escapes repository root".into());
    }
    let mut current = root.clone();
    let lexical = candidate
        .strip_prefix(&root)
        .map_err(|error| error.to_string())?;
    for component in lexical.components() {
        current.push(component.as_os_str());
        if std::fs::symlink_metadata(&current)
            .is_ok_and(|metadata| metadata.file_type().is_symlink())
        {
            return Err("source path contains a symlink".into());
        }
    }
    let resolved = candidate
        .canonicalize()
        .map_err(|error| error.to_string())?;
    if !resolved.starts_with(root) || !resolved.is_file() {
        return Err("source is not a regular repository file".into());
    }
    std::fs::read(resolved).map_err(|error| error.to_string())
}

fn parse_chain_author(
    value: Option<&Value>,
    label: &str,
    profile_slot: &str,
    errors: &mut Vec<String>,
) -> Option<Map<String, Value>> {
    let Some(author) = value.and_then(Value::as_object) else {
        errors.push(format!(
            "source-origin chain {label} author must be an object"
        ));
        return None;
    };
    if !exact_keys(
        author,
        &["task", "session_id", "source_profile", "author_origin"],
    ) {
        errors.push(format!("source-origin chain {label} author fields differ"));
        return None;
    }
    if ["task", "session_id"].iter().any(|field| {
        !author
            .get(*field)
            .and_then(Value::as_str)
            .is_some_and(|v| !v.is_empty())
    }) {
        errors.push(format!(
            "source-origin chain {label} author identity is malformed"
        ));
        return None;
    }
    profile_for(
        profile_slot,
        author.get("source_profile"),
        author.get("author_origin"),
        label,
        errors,
    )?;
    Some(author.clone())
}

fn parse_chain_prefix(
    value: Option<&Value>,
    label: &str,
    errors: &mut Vec<String>,
) -> Option<Map<String, Value>> {
    let Some(prefix) = value.and_then(Value::as_object) else {
        errors.push(format!(
            "source-origin chain {label} prefix must be an object"
        ));
        return None;
    };
    if !exact_keys(
        prefix,
        &[
            "session_id",
            "start_offset_bytes",
            "bytes",
            "line_count",
            "sha256",
            "last_included_utc",
        ],
    ) {
        errors.push(format!("source-origin chain {label} prefix fields differ"));
        return None;
    }
    if !prefix
        .get("session_id")
        .and_then(Value::as_str)
        .is_some_and(|v| !v.is_empty())
        || prefix
            .get("start_offset_bytes")
            .and_then(Value::as_i64)
            .is_none_or(|n| n < 0)
        || ["bytes", "line_count"].iter().any(|key| {
            prefix
                .get(*key)
                .and_then(Value::as_i64)
                .is_none_or(|n| n <= 0)
        })
        || !prefix
            .get("sha256")
            .and_then(Value::as_str)
            .is_some_and(is_sha256)
        || !prefix
            .get("last_included_utc")
            .and_then(Value::as_str)
            .is_some_and(is_valid_utc)
    {
        errors.push(format!(
            "source-origin chain {label} prefix metadata is malformed"
        ));
    }
    Some(prefix.clone())
}

fn validate_historical_normative(
    evaluator_id: &str,
    adjudication: &Map<String, Value>,
    errors: &mut Vec<String>,
) {
    let normative = adjudication
        .get("normative_spec")
        .and_then(Value::as_object);
    let fields = normative
        .map(|value| value.keys().map(String::as_str).collect::<BTreeSet<_>>())
        .unwrap_or_default();
    if evaluator_id == "full_spec_blind" {
        if !normative.is_some_and(|record| {
            fields == BTreeSet::from(["path", "sha256", "use"])
                && record.get("path").and_then(Value::as_str) == Some("SPEC-v5.md")
                && record.get("sha256").and_then(Value::as_str) == Some(V5_BASE_SPEC_SHA256)
                && record.get("use").and_then(Value::as_str) == Some(FULL_SPEC_NORMATIVE_USE)
        }) {
            errors.push("source-origin chain historical full-spec binding differs".into());
        }
        let authorized = adjudication
            .get("authorized_spec_input")
            .and_then(Value::as_object);
        if !authorized.is_some_and(|record| {
            exact_keys(record, &["path", "sha256", "scope"])
                && record.get("path").and_then(Value::as_str)
                    == Some(
                        "reports/v5/conformance/inputs/spec-sections-1-4/SPEC-v5-sections-1-4.md",
                    )
                && record
                    .get("sha256")
                    .and_then(Value::as_str)
                    .is_some_and(is_sha256)
                && record.get("scope").and_then(Value::as_str) == Some(FULL_SPEC_AUTHORIZED_SCOPE)
        }) {
            errors.push("source-origin chain historical authorized SPEC input is malformed".into());
        }
    } else if !normative.is_some_and(|record| {
        (fields == BTreeSet::from(["sha256"]) || fields == BTreeSet::from(["path", "sha256"]))
            && record.get("sha256").and_then(Value::as_str) == Some(V5_BASE_SPEC_SHA256)
            && record
                .get("path")
                .and_then(Value::as_str)
                .unwrap_or("SPEC-v5.md")
                == "SPEC-v5.md"
    }) {
        errors.push("source-origin chain historical normative binding differs".into());
    }
}

fn compare_historical_claims(
    native: &Map<String, Value>,
    audit: &Map<String, Value>,
    author: &Map<String, Value>,
    prefix: &Map<String, Value>,
    capture: &ChainCapture,
    extractor: &Map<String, Value>,
    evaluator_id: &str,
    scope: Option<&Map<String, Value>>,
    errors: &mut Vec<String>,
) {
    let audit_identity = audit.get("audit_identity").and_then(Value::as_object);
    let task = audit_identity
        .and_then(|identity| identity.get("task"))
        .and_then(Value::as_str)
        .map(task_identity_base);
    if task.as_deref() != author.get("task").and_then(Value::as_str)
        || audit_identity.and_then(|identity| identity.get("child_session_id"))
            != author.get("session_id")
        || audit_identity.and_then(|identity| identity.get("source_profile"))
            != author.get("source_profile")
        || audit_identity.and_then(|identity| identity.get("author_origin"))
            != author.get("author_origin")
        || audit_identity
            .and_then(|identity| identity.get("wire_profile"))
            .and_then(Value::as_str)
            != Some(evaluator_id)
    {
        errors.push("source-origin chain historical audit identity differs".into());
    }
    let expected = [
        ("author_task", author.get("task")),
        ("session_id", author.get("session_id")),
        ("session_id", prefix.get("session_id")),
        ("prefix_bytes", prefix.get("bytes")),
        ("prefix_sha256", prefix.get("sha256")),
        (
            "capture_manifest_raw_sha256",
            capture
                .wrapper
                .get("manifest")
                .and_then(|value| value.get("sha256")),
        ),
        (
            "capture_manifest_logical_sha256",
            capture.wrapper.get("logical_sha256"),
        ),
        ("approved_auditor_script_sha256", extractor.get("sha256")),
        ("cutoff_timestamp_utc", prefix.get("last_included_utc")),
    ];
    if expected
        .iter()
        .any(|(field, value)| native.get(*field) != *value)
    {
        errors.push(
            "source-origin chain historical audit claims do not bind its core evidence".into(),
        );
    }
    if let Some(scope) = scope {
        let expected_capture_cutoff = scope
            .get("capture_cutoff_timestamp_utc")
            .and_then(Value::as_str)
            .unwrap_or("");
        let native_cutoff = native
            .get("capture_cutoff_timestamp_utc")
            .and_then(Value::as_str)
            .unwrap_or("");
        let path_matches = native.get("capture_manifest_path").and_then(Value::as_str)
            == capture
                .wrapper
                .get("manifest")
                .and_then(|value| value.get("path"))
                .and_then(Value::as_str);
        if !same_utc(expected_capture_cutoff, native_cutoff)
            || !path_matches
            || !mtime_matches(&capture.manifest_path, expected_capture_cutoff)
        {
            errors.push(
                "source-origin chain historical audit does not bind capture mtime cutoff".into(),
            );
        }
    }
}

fn task_identity_base(task: &str) -> String {
    task.split_once(" (")
        .map_or(task, |(base, _)| base)
        .to_owned()
}

fn mtime_matches(path: &Path, timestamp: &str) -> bool {
    if !is_valid_utc(timestamp) || path.is_symlink() {
        return false;
    }
    let Ok(metadata) = std::fs::symlink_metadata(path) else {
        return false;
    };
    let Ok(modified) = metadata.modified() else {
        return false;
    };
    let Ok(duration) = modified.duration_since(std::time::UNIX_EPOCH) else {
        return false;
    };
    let Some(target_ns) = parse_utc_timestamp_ns(timestamp).ok() else {
        return false;
    };
    (duration.as_nanos() as i128) / 1_000 == target_ns / 1_000
}

#[allow(clippy::too_many_arguments)]
fn validate_historical_evidence(
    adjudication: &Map<String, Value>,
    audit: &Map<String, Value>,
    audit_ref: Option<&Map<String, Value>>,
    extractor: &Map<String, Value>,
    scope: Option<&Map<String, Value>>,
    prefix: &Map<String, Value>,
    capture: &ChainCapture,
    evaluator_id: &str,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) {
    let Some(rows) = adjudication.get("evidence").and_then(Value::as_array) else {
        errors.push("source-origin chain historical adjudication has no evidence inventory".into());
        return;
    };
    let Some(audit_ref) = audit_ref else {
        return;
    };
    let audit_path = audit_ref.get("path").and_then(Value::as_str).unwrap_or("");
    let audit_sha = audit_ref
        .get("sha256")
        .and_then(Value::as_str)
        .unwrap_or("");
    for (label, reference) in [("audit", audit_ref), ("executed extractor", extractor)] {
        let path = reference.get("path").and_then(Value::as_str).unwrap_or("");
        let digest = reference
            .get("sha256")
            .and_then(Value::as_str)
            .unwrap_or("");
        let matches = rows
            .iter()
            .filter(|row| {
                row.get("path").and_then(Value::as_str) == Some(path)
                    && row.get("raw_sha256").and_then(Value::as_str) == Some(digest)
            })
            .collect::<Vec<_>>();
        if matches.len() != 1 {
            errors.push(format!(
                "source-origin chain historical adjudication does not uniquely bind {label}"
            ));
        }
        if label == "executed extractor" && matches.len() == 1 {
            let note = matches[0].get("note").and_then(Value::as_str).unwrap_or("");
            if !v7_note_allowed(evaluator_id, path, note) {
                errors.push("source-origin chain historical extractor note is unsupported".into());
            }
        }
    }
    if rows
        .iter()
        .filter(|row| {
            row.get("path").and_then(Value::as_str) == Some(audit_path)
                && row.get("raw_sha256").and_then(Value::as_str) == Some(audit_sha)
        })
        .count()
        != 1
    {
        errors.push(
            "source-origin chain historical adjudication does not uniquely bind audit".into(),
        );
    }
    let identity = audit.get("audit_identity").and_then(Value::as_object);
    if !identity.is_some_and(|identity| {
        identity
            .get("task")
            .and_then(Value::as_str)
            .map(task_identity_base)
            == adjudication
                .get("subject")
                .and_then(|subject| subject.get("author_task"))
                .and_then(Value::as_str)
                .map(str::to_owned)
            && identity.get("child_session_id")
                == adjudication
                    .get("subject")
                    .and_then(|subject| subject.get("session_id"))
            && identity.get("source_profile")
                == adjudication
                    .get("subject")
                    .and_then(|subject| subject.get("source_profile"))
            && identity.get("author_origin")
                == adjudication
                    .get("subject")
                    .and_then(|subject| subject.get("author_origin"))
            && identity.get("wire_profile").and_then(Value::as_str) == Some(evaluator_id)
    }) {
        errors.push("source-origin chain historical audit identity differs".into());
    }
    if audit
        .get("approved_auditor_script_sha256")
        .and_then(Value::as_str)
        != extractor.get("sha256").and_then(Value::as_str)
    {
        errors.push(
            "source-origin chain historical executed extractor is not bound by its audit".into(),
        );
    }
    if !scope.is_some_and(|scope| {
        scope
            .get("cutoff_timestamp_utc")
            .and_then(Value::as_str)
            .is_some_and(is_valid_utc)
            && scope.get("cutoff_line_inclusive") == prefix.get("line_count")
    }) {
        errors.push("source-origin chain historical scope cutoff is invalid".into());
    }
    if audit
        .get("limitations")
        .and_then(Value::as_array)
        .is_some_and(Vec::is_empty)
    {
        errors.push("source-origin chain historical audit limitations are empty".into());
    }
    let cutoff = scope
        .and_then(|scope| scope.get("capture_cutoff_timestamp_utc"))
        .and_then(Value::as_str);
    let path_matches = scope
        .and_then(|scope| scope.get("capture_manifest_path"))
        .and_then(Value::as_str)
        == capture
            .wrapper
            .get("manifest")
            .and_then(|manifest| manifest.get("path"))
            .and_then(Value::as_str);
    if cutoff.is_none_or(|value| !mtime_matches(&capture.manifest_path, value)) || !path_matches {
        errors
            .push("source-origin chain historical audit does not bind capture mtime cutoff".into());
    }
    let _ = gaps;
}
