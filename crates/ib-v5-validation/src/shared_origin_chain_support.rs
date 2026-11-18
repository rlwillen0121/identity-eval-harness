//! Validation helpers for the bounded source-origin continuation chain.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;

use serde_json::{Map, Value, json};

use super::{ChainArtifact, ChainCapture};

pub(super) fn capture_cutoff_character(evaluator_id: &str) -> &'static str {
    match evaluator_id {
        "full_spec_blind" => "Weak filesystem mtime only; not cryptographic chronology.",
        "agent_view_only" => "Filesystem mtime only; it is not cryptographic chronology.",
        _ => "",
    }
}

pub(super) fn string_array(value: Option<&Value>) -> Vec<String> {
    value
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_owned)
        .collect()
}

fn exact_object<'a>(
    value: Option<&'a Value>,
    required: &[&str],
    optional: &[&str],
    label: &str,
    errors: &mut Vec<String>,
) -> Option<&'a Map<String, Value>> {
    let Some(object) = value.and_then(Value::as_object) else {
        errors.push(format!("source-origin chain {label} must be an object"));
        return None;
    };
    if !required.iter().all(|field| object.contains_key(*field))
        || object
            .keys()
            .any(|key| !required.contains(&key.as_str()) && !optional.contains(&key.as_str()))
    {
        errors.push(format!("source-origin chain {label} fields differ"));
        return None;
    }
    Some(object)
}

fn strict_int(value: Option<&Value>) -> Option<i128> {
    match value? {
        Value::Number(number) => number
            .as_i64()
            .map(i128::from)
            .or_else(|| number.as_u64().map(i128::from)),
        _ => None,
    }
}

fn chain_string<'a>(
    value: Option<&'a Value>,
    label: &str,
    errors: &mut Vec<String>,
) -> Option<&'a str> {
    let result = value
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty());
    if result.is_none() {
        errors.push(format!(
            "source-origin chain {label} must be a nonempty string"
        ));
    }
    result
}

fn timestamp(value: Option<&Value>, label: &str, errors: &mut Vec<String>) -> Option<String> {
    let Some(value) = value.and_then(Value::as_str) else {
        errors.push(format!(
            "source-origin chain {label} is not an explicit UTC RFC3339 timestamp"
        ));
        return None;
    };
    if !super::is_valid_utc(value) {
        errors.push(format!(
            "source-origin chain {label} is not an explicit UTC RFC3339 timestamp"
        ));
        return None;
    }
    Some(value.to_owned())
}

fn limitations(
    value: Option<&Value>,
    label: &str,
    nonempty: bool,
    errors: &mut Vec<String>,
) -> Option<Vec<String>> {
    let Some(items) = value.and_then(Value::as_array) else {
        errors.push(format!(
            "source-origin chain {label} must be a string array"
        ));
        return None;
    };
    let mut result = Vec::with_capacity(items.len());
    for item in items {
        let Some(value) = item.as_str().filter(|value| !value.is_empty()) else {
            errors.push(format!(
                "source-origin chain {label} must be sorted unique nonempty strings"
            ));
            return None;
        };
        result.push(value.to_owned());
    }
    let mut sorted = result.clone();
    sorted.sort();
    sorted.dedup();
    if result != sorted || (nonempty && result.is_empty()) {
        errors.push(format!(
            "source-origin chain {label} must be sorted unique nonempty strings"
        ));
        return None;
    }
    Some(result)
}

fn validate_route(value: Option<&Value>, label: &str, errors: &mut Vec<String>) {
    let Some(route) = exact_object(
        value,
        &["model", "reasoning_effort", "provider", "service_tier"],
        &[],
        &format!("{label} route"),
        errors,
    ) else {
        return;
    };
    if route
        .values()
        .any(|value| !value.as_str().is_some_and(|value| !value.is_empty()))
    {
        errors.push(format!(
            "source-origin chain {label} route values must be nonempty strings"
        ));
    }
}

fn read_artifact(
    root: &Path,
    reference: Option<&Value>,
    label: &str,
    referenced: &mut BTreeSet<(String, String)>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Option<Map<String, Value>> {
    let artifact = super::read_chain_ref(root, reference, label, referenced, errors, gaps)?;
    Some(artifact.reference)
}

fn artifact_is_available(root: &Path, reference: &Map<String, Value>, label: &str) -> bool {
    let mut ignored_refs = BTreeSet::new();
    let mut errors = Vec::new();
    let mut gaps = Vec::new();
    super::read_chain_ref(
        root,
        Some(&Value::Object(reference.clone())),
        label,
        &mut ignored_refs,
        &mut errors,
        &mut gaps,
    )
    .is_some()
}

pub(super) fn validate_reconstruction(
    inventory: &Map<String, Value>,
    evaluator_id: &str,
    profile_slot: &str,
    hist_author: &Map<String, Value>,
    handoff: &ChainCapture,
    root: &Path,
    referenced: &mut BTreeSet<(String, String)>,
    completeness_reviewer: &mut Option<String>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) {
    let inventory_value = Value::Object(inventory.clone());
    const REQUIRED: &[&str] = &[
        "format",
        "record_version",
        "reconstructed_at_utc",
        "current_capture",
        "files",
        "completeness_decision",
        "limitations",
    ];
    let Some(inventory_ref_fields) = exact_object(
        Some(&inventory_value),
        REQUIRED,
        &[],
        "reconstructed inventory",
        errors,
    ) else {
        return;
    };
    if inventory_ref_fields.get("format").and_then(Value::as_str)
        != Some("identitybench.v5.source-origin-reconstructed-inventory.v1")
        || strict_int(inventory_ref_fields.get("record_version")) != Some(1)
    {
        errors.push("source-origin chain reconstructed inventory version differs".into());
    }
    timestamp(
        inventory_ref_fields.get("reconstructed_at_utc"),
        "reconstruction time",
        errors,
    );
    limitations(
        inventory_ref_fields.get("limitations"),
        "reconstruction limitations",
        true,
        errors,
    );

    let capture = super::parse_chain_capture(
        inventory_ref_fields.get("current_capture"),
        "reconstructed current",
        profile_slot,
        evaluator_id,
        root,
        referenced,
        errors,
        gaps,
    );
    let file_rows = inventory_ref_fields.get("files").and_then(Value::as_array);
    let Some(capture) = capture else {
        gaps.push("source-origin chain reconstruction is incomplete".into());
        return;
    };
    let Some(file_rows) = file_rows else {
        gaps.push("source-origin chain reconstruction is incomplete".into());
        return;
    };
    let map_rows = capture
        .source_map
        .get("files")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if file_rows.len() != map_rows.len() {
        errors.push("source-origin chain reconstruction inventory is incomplete".into());
    }
    for (index, (row, map_row)) in file_rows.iter().zip(&map_rows).enumerate() {
        let Some(row) = exact_object(
            Some(row),
            &["path", "sha256", "provenance"],
            &[],
            &format!("reconstruction component {index}"),
            errors,
        ) else {
            continue;
        };
        if row.get("path") != map_row.get("path") || row.get("sha256") != map_row.get("sha256") {
            errors.push("source-origin chain reconstruction component map differs".into());
            continue;
        }
        let provenance = read_artifact(
            root,
            row.get("provenance"),
            &format!("reconstruction component {index} provenance"),
            referenced,
            errors,
            gaps,
        );
        if provenance
            .as_ref()
            .and_then(|reference| reference.get("sha256"))
            != map_row.get("sha256")
        {
            errors.push(
                "source-origin chain reconstruction component provenance digest differs".into(),
            );
        }
    }

    let decision = exact_object(
        inventory_ref_fields.get("completeness_decision"),
        &[
            "decision",
            "reviewer_task",
            "route",
            "evidence",
            "reviewed_at_utc",
        ],
        &[],
        "reconstruction completeness decision",
        errors,
    );
    if let Some(decision) = decision {
        if decision.get("decision").and_then(Value::as_str) != Some("PASS") {
            gaps.push("source-origin chain reconstruction completeness is not PASS".into());
        }
        *completeness_reviewer = chain_string(
            decision.get("reviewer_task"),
            "completeness reviewer",
            errors,
        )
        .map(str::to_owned);
        if completeness_reviewer.as_deref() == hist_author.get("task").and_then(Value::as_str) {
            errors.push("source-origin chain author reviewed reconstruction completeness".into());
        }
        validate_route(decision.get("route"), "completeness reviewer", errors);
        timestamp(
            decision.get("reviewed_at_utc"),
            "completeness review time",
            errors,
        );
        read_artifact(
            root,
            decision.get("evidence"),
            "reconstruction completeness evidence",
            referenced,
            errors,
            gaps,
        );
    }

    let handoff_files = handoff
        .source_map
        .get("files")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    if capture.source_map.get("files").and_then(Value::as_array) != Some(&handoff_files) {
        errors.push(
            "source-origin chain reconstruction map differs from immediate predecessor".into(),
        );
    }
    if capture
        .source_map
        .get("evaluator_id")
        .and_then(Value::as_str)
        != Some(evaluator_id)
    {
        errors.push("source-origin chain reconstructed evaluator differs".into());
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn validate_baseline_adjudication(
    baseline: &Map<String, Value>,
    hist: &Map<String, Value>,
    hist_author: &Map<String, Value>,
    predecessor_continuation: Option<&Value>,
    handoff: &ChainCapture,
    evaluator_id: &str,
    root: &Path,
    referenced: &mut BTreeSet<(String, String)>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) {
    let baseline_value = Value::Object(baseline.clone());
    let Some(baseline) = exact_object(
        Some(&baseline_value),
        &[
            "record_type",
            "record_version",
            "adjudicator",
            "decision",
            "core_bindings",
            "omitted_optional_corroboration",
            "root_baseline_attestation",
            "limitations",
        ],
        &[],
        "baseline adjudication",
        errors,
    ) else {
        return;
    };
    if baseline.get("record_type").and_then(Value::as_str)
        != Some("source_origin_baseline_adjudication")
        || strict_int(baseline.get("record_version")) != Some(1)
        || baseline.get("decision").and_then(Value::as_str) != Some("QUALIFIED_ELIGIBLE")
    {
        gaps.push("source-origin chain baseline adjudication is not positively eligible".into());
    }
    limitations(
        baseline.get("limitations"),
        "baseline adjudication limitations",
        true,
        errors,
    );

    let adjudicator = exact_object(
        baseline.get("adjudicator"),
        &["task", "route", "reviewed_at_utc"],
        &[],
        "baseline adjudicator",
        errors,
    );
    if let Some(adjudicator) = adjudicator {
        let adjudicator_task =
            chain_string(adjudicator.get("task"), "baseline adjudicator task", errors);
        validate_route(adjudicator.get("route"), "baseline adjudicator", errors);
        timestamp(
            adjudicator.get("reviewed_at_utc"),
            "baseline adjudication time",
            errors,
        );
        if adjudicator_task == hist_author.get("task").and_then(Value::as_str) {
            errors
                .push("source-origin chain baseline adjudicator overlaps historical author".into());
        }
    }

    let bindings = exact_object(
        baseline.get("core_bindings"),
        &[
            "audit",
            "executed_extractor",
            "capture",
            "author",
            "prefix",
            "handoff",
        ],
        &[],
        "baseline core bindings",
        errors,
    );
    let mut handoff_capture = None;
    if let Some(bindings) = bindings {
        for field in ["audit", "executed_extractor", "capture", "author", "prefix"] {
            if bindings.get(field) != hist.get(field) {
                errors
                    .push("source-origin chain baseline adjudication core bindings differ".into());
                break;
            }
        }
        let profile_slot = super::EVALUATOR_WIRE_ID_BY_SLOT
            .iter()
            .find_map(|(slot, wire)| (*wire == evaluator_id).then_some(*slot))
            .unwrap_or("");
        handoff_capture = super::parse_chain_capture(
            bindings.get("handoff"),
            "baseline handoff",
            profile_slot,
            evaluator_id,
            root,
            referenced,
            errors,
            gaps,
        );
        if handoff_capture
            .as_ref()
            .is_none_or(|capture| capture.wrapper != handoff.wrapper)
        {
            errors.push(
                "source-origin chain baseline adjudication does not bind exact handoff".into(),
            );
        }
    }

    let Some((historical_adjudication, _historical_ref)) = super::read_chain_json_ref(
        root,
        hist.get("adjudication"),
        "historical adjudication",
        referenced,
        errors,
        gaps,
    ) else {
        gaps.push("source-origin chain historical evidence inventory is unavailable".into());
        return;
    };
    let historical_evidence = historical_adjudication
        .get("evidence")
        .and_then(Value::as_array);
    let evidence_map: BTreeMap<&str, &Value> = historical_evidence
        .into_iter()
        .flatten()
        .filter_map(|row| {
            let row = row.as_object()?;
            Some((row.get("path")?.as_str()?, row.get("raw_sha256")?))
        })
        .collect();
    let Some(omissions) = baseline
        .get("omitted_optional_corroboration")
        .and_then(Value::as_array)
    else {
        errors.push("source-origin chain optional corroboration omissions are malformed".into());
        return;
    };
    let core_paths: BTreeSet<&str> = ["adjudication", "audit", "executed_extractor", "capture"]
        .into_iter()
        .filter_map(|field| hist.get(field)?.get("path")?.as_str())
        .collect();
    let mut omitted_paths = Vec::new();
    for (index, row) in omissions.iter().enumerate() {
        let Some(row) = exact_object(
            Some(row),
            &["path", "old_sha256", "reason"],
            &[],
            &format!("optional omission {index}"),
            errors,
        ) else {
            continue;
        };
        let path = row.get("path").and_then(Value::as_str);
        let old_sha = row.get("old_sha256").and_then(Value::as_str);
        let reason = row.get("reason").and_then(Value::as_str);
        let valid = path.is_some_and(|path| path.starts_with("reports/v5/conformance/"))
            && old_sha.is_some_and(super::is_sha256)
            && old_sha.is_some_and(|sha| {
                evidence_map
                    .get(path.unwrap_or_default())
                    .is_some_and(|expected| expected.as_str() == Some(sha))
            })
            && reason.is_some_and(|reason| !reason.is_empty())
            && path.is_some_and(|path| !core_paths.contains(path));
        if !valid {
            errors
                .push("source-origin chain optional omission does not bind the old record".into());
        } else if let Some(path) = path {
            omitted_paths.push(path.to_owned());
        }
    }
    let mut sorted_omissions = omitted_paths.clone();
    sorted_omissions.sort();
    sorted_omissions.dedup();
    if sorted_omissions != omitted_paths {
        errors.push("source-origin chain optional omissions are not sorted and unique".into());
    }

    if evaluator_id == "full_spec_blind"
        && let Some(authorized) = historical_adjudication
            .get("authorized_spec_input")
            .and_then(Value::as_object)
        && let (Some(path), Some(sha)) = (
            authorized.get("path").and_then(Value::as_str),
            authorized.get("sha256").and_then(Value::as_str),
        )
        && evidence_map
            .get(path)
            .is_none_or(|expected| expected.as_str() != Some(sha))
    {
        errors
            .push("source-origin chain historical authorized SPEC input is not hash-bound".into());
    }

    if let Some(rows) = historical_evidence {
        let normative_path = historical_adjudication
            .get("normative_spec")
            .and_then(Value::as_object)
            .and_then(|item| item.get("path"))
            .and_then(Value::as_str)
            .unwrap_or("SPEC-v5.md");
        let authorized_path = historical_adjudication
            .get("authorized_spec_input")
            .and_then(Value::as_object)
            .and_then(|item| item.get("path"))
            .and_then(Value::as_str);
        let mut seen_paths = BTreeSet::new();
        for (index, row) in rows.iter().enumerate() {
            let Some(row) = row.as_object() else {
                errors.push(format!(
                    "source-origin chain historical evidence row {index} is malformed"
                ));
                continue;
            };
            let path = row.get("path").and_then(Value::as_str);
            let sha = row.get("raw_sha256").and_then(Value::as_str);
            let note = row.get("note");
            let expected_len = if note.is_some() { 3 } else { 2 };
            if row.len() != expected_len
                || !path.is_some_and(|path| !path.is_empty())
                || !sha.is_some_and(super::is_sha256)
                || path.is_none_or(|path| !seen_paths.insert(path.to_owned()))
            {
                errors.push(format!(
                    "source-origin chain historical evidence row {index} has invalid fields"
                ));
                continue;
            }
            if let Some(note_value) = note {
                if !note_value
                    .as_str()
                    .zip(path)
                    .is_some_and(|(note, path)| super::v7_note_allowed(evaluator_id, path, note))
                {
                    errors.push(format!(
                        "source-origin chain historical evidence row {index} has unsupported note"
                    ));
                    continue;
                }
            }
            let path = path.unwrap_or_default();
            let sha = sha.unwrap_or_default();
            if omitted_paths.iter().any(|item| item == path) {
                if core_paths.contains(path)
                    || path == normative_path
                    || Some(path) == authorized_path
                {
                    errors.push("source-origin chain cannot omit historical core evidence".into());
                    continue;
                }
                let reference = Map::from_iter([
                    ("path".to_owned(), Value::String(path.to_owned())),
                    ("sha256".to_owned(), Value::String(sha.to_owned())),
                ]);
                if artifact_is_available(root, &reference, "optional historical corroboration") {
                    errors.push(
                        "source-origin chain omits optional corroboration whose old bytes remain available"
                            .into(),
                    );
                }
            } else {
                let reference = Map::from_iter([
                    ("path".to_owned(), Value::String(path.to_owned())),
                    ("sha256".to_owned(), Value::String(sha.to_owned())),
                ]);
                super::read_chain_ref(
                    root,
                    Some(&Value::Object(reference)),
                    &format!("historical evidence {index}"),
                    referenced,
                    errors,
                    gaps,
                );
            }
        }
    } else {
        errors.push("source-origin chain historical evidence inventory is malformed".into());
    }

    let Some(attestation) = exact_object(
        baseline.get("root_baseline_attestation"),
        &[
            "subject",
            "source_map_sha256",
            "basis_artifacts",
            "issued_at_utc",
            "statement",
            "evidence_character",
            "limitations",
        ],
        &[],
        "root baseline attestation",
        errors,
    ) else {
        return;
    };
    let expected_subject = json!({
        "task": hist_author.get("task"),
        "session_id": hist_author.get("session_id"),
        "evaluator_id": evaluator_id,
        "source_profile": hist_author.get("source_profile"),
        "author_origin": hist_author.get("author_origin"),
    });
    if attestation.get("subject") != Some(&expected_subject) {
        errors.push("source-origin chain root baseline subject differs".into());
    }
    let handoff_map = handoff_capture
        .as_ref()
        .map(|capture| Value::Object(capture.source_map.clone()));
    if attestation.get("source_map_sha256").and_then(Value::as_str)
        != handoff_map
            .as_ref()
            .and_then(super::canonical_sha)
            .as_deref()
    {
        errors.push("source-origin chain root baseline source map digest differs".into());
    }
    if !attestation
        .get("statement")
        .and_then(Value::as_str)
        .is_some_and(|value| !value.is_empty())
        || !attestation
            .get("evidence_character")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty())
    {
        errors.push("source-origin chain root baseline attestation text is incomplete".into());
    }
    timestamp(
        attestation.get("issued_at_utc"),
        "root baseline attestation time",
        errors,
    );
    limitations(
        attestation.get("limitations"),
        "root baseline limitations",
        true,
        errors,
    );
    let Some(basis) = attestation.get("basis_artifacts").and_then(Value::as_array) else {
        errors.push("source-origin chain root baseline artifact basis is malformed".into());
        return;
    };
    let mut basis_refs = Vec::new();
    for (index, item) in basis.iter().enumerate() {
        if let Some(reference) = read_artifact(
            root,
            Some(item),
            &format!("baseline basis artifact {index}"),
            referenced,
            errors,
            gaps,
        ) {
            if let (Some(path), Some(sha)) = (
                reference.get("path").and_then(Value::as_str),
                reference.get("sha256").and_then(Value::as_str),
            ) {
                basis_refs.push((path.to_owned(), sha.to_owned()));
            }
        }
    }
    let mut sorted_basis = basis_refs.clone();
    sorted_basis.sort();
    sorted_basis.dedup();
    if basis_refs != sorted_basis {
        errors.push(
            "source-origin chain root baseline artifact basis is not sorted and unique".into(),
        );
    }
    let mut required_basis = BTreeSet::new();
    for field in ["adjudication", "audit", "executed_extractor", "capture"] {
        if let Some(reference) = hist.get(field).and_then(Value::as_object)
            && let (Some(path), Some(sha)) = (
                reference.get("path").and_then(Value::as_str),
                reference.get("sha256").and_then(Value::as_str),
            )
        {
            required_basis.insert((path.to_owned(), sha.to_owned()));
        }
    }
    let predecessor_manifest = predecessor_continuation
        .and_then(Value::as_object)
        .and_then(|value| value.get("output_capture"))
        .and_then(Value::as_object)
        .and_then(|value| value.get("manifest"));
    for candidate in [predecessor_manifest, hist.get("reconstructed_inventory")] {
        if let Some(reference) = candidate.and_then(Value::as_object)
            && let (Some(path), Some(sha)) = (
                reference.get("path").and_then(Value::as_str),
                reference.get("sha256").and_then(Value::as_str),
            )
        {
            required_basis.insert((path.to_owned(), sha.to_owned()));
        }
    }
    let actual_basis: BTreeSet<_> = basis_refs.into_iter().collect();
    if !required_basis.is_subset(&actual_basis) {
        errors.push(
            "source-origin chain root baseline attestation omits core basis artifacts".into(),
        );
    }
    if hist.contains_key("reconstructed_inventory")
        && !hist
            .get("reconstructed_inventory")
            .is_some_and(Value::is_object)
    {
        errors
            .push("source-origin chain reconstruction requires a new baseline adjudication".into());
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn validate_spawn_evidence(
    successor: &Map<String, Value>,
    task: &str,
    session: &str,
    evaluator_id: &str,
    root: &Path,
    input_map_sha: &str,
    referenced: &mut BTreeSet<(String, String)>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) {
    let Some((spawn, _reference)) = super::read_chain_json_ref(
        root,
        successor.get("spawn_evidence"),
        "successor spawn evidence",
        referenced,
        errors,
        gaps,
    ) else {
        return;
    };
    let spawn_value = Value::Object(spawn);
    let Some(spawn) = exact_object(
        Some(&spawn_value),
        &[
            "format",
            "record_version",
            "task",
            "session_id",
            "parent_task",
            "fork_mode",
            "repository_root",
            "input_source_map_sha256",
            "spawned_at_utc",
        ],
        &[],
        "spawn evidence",
        errors,
    ) else {
        return;
    };
    let root_path = root
        .canonicalize()
        .unwrap_or_else(|_| root.to_path_buf())
        .to_string_lossy()
        .into_owned();
    if spawn.get("format").and_then(Value::as_str)
        != Some("identitybench.v5.author-spawn-evidence.v1")
        || strict_int(spawn.get("record_version")) != Some(1)
        || spawn.get("task").and_then(Value::as_str) != Some(task)
        || spawn.get("session_id").and_then(Value::as_str) != Some(session)
        || spawn.get("fork_mode").and_then(Value::as_str) != Some("fork_none")
        || spawn.get("repository_root").and_then(Value::as_str) != Some(root_path.as_str())
        || spawn.get("input_source_map_sha256").and_then(Value::as_str) != Some(input_map_sha)
        || !spawn
            .get("parent_task")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty())
    {
        errors.push("source-origin chain spawn evidence does not bind successor task/input".into());
    }
    timestamp(spawn.get("spawned_at_utc"), "successor spawn time", errors);
    let _ = evaluator_id;
}

pub(super) fn validate_successor_root_attestation(
    successor: &Map<String, Value>,
    evaluator_id: &str,
    task: &str,
    session: &str,
    input_map_sha: &str,
    errors: &mut Vec<String>,
    _gaps: &mut Vec<String>,
) {
    let Some(attestation) = exact_object(
        successor.get("root_attestation"),
        &[
            "subject",
            "fork_mode",
            "input_source_map_sha256",
            "issued_at_utc",
            "allowed_inputs",
            "not_supplied",
            "statement",
            "evidence_character",
            "limitations",
        ],
        &[],
        "successor root attestation",
        errors,
    ) else {
        return;
    };
    let expected_subject = json!({
        "task": task,
        "session_id": session,
        "source_profile": successor.get("source_profile"),
        "author_origin": successor.get("author_origin"),
        "evaluator_id": evaluator_id,
    });
    if attestation.get("subject") != Some(&expected_subject)
        || attestation.get("fork_mode").and_then(Value::as_str) != Some("fork_none")
        || attestation
            .get("input_source_map_sha256")
            .and_then(Value::as_str)
            != Some(input_map_sha)
    {
        errors.push("source-origin chain root attestation subject/input binding differs".into());
    }
    timestamp(
        attestation.get("issued_at_utc"),
        "successor root attestation time",
        errors,
    );
    for field in ["statement", "evidence_character"] {
        if !attestation
            .get(field)
            .and_then(Value::as_str)
            .is_some_and(|value| !value.is_empty())
        {
            errors.push(format!(
                "source-origin chain root attestation {field} is empty"
            ));
        }
    }
    limitations(
        attestation.get("limitations"),
        "successor root limitations",
        true,
        errors,
    );
    let Some(allowed_inputs) = attestation.get("allowed_inputs").and_then(Value::as_array) else {
        errors.push("source-origin chain allowed input list is malformed".into());
        return;
    };
    let mut allowed_rows = Vec::new();
    for (index, row) in allowed_inputs.iter().enumerate() {
        let Some(row) = exact_object(
            Some(row),
            &["path", "sha256", "scope"],
            &[],
            &format!("allowed input {index}"),
            errors,
        ) else {
            continue;
        };
        let path = row.get("path").and_then(Value::as_str);
        let digest = row.get("sha256").and_then(Value::as_str);
        let scope = row.get("scope").and_then(Value::as_str);
        if path.is_none_or(|path| {
            path.is_empty()
                || path.starts_with('/')
                || path.contains('\\')
                || path.split('/').any(|part| matches!(part, "" | "." | ".."))
        }) || !digest.is_some_and(super::is_sha256)
        {
            errors.push("source-origin chain allowed input path or digest is malformed".into());
        }
        if scope.is_none_or(str::is_empty) {
            errors.push("source-origin chain allowed input scope is malformed".into());
        }
        if let (Some(path), Some(digest), Some(scope)) = (path, digest, scope) {
            allowed_rows.push((path.to_owned(), digest.to_owned(), scope.to_owned()));
        }
    }
    let mut sorted_allowed = allowed_rows.clone();
    sorted_allowed.sort();
    sorted_allowed.dedup();
    if sorted_allowed != allowed_rows {
        errors.push("source-origin chain allowed inputs are not sorted and unique".into());
    }

    const FORBIDDEN_CATEGORIES: &[&str] = &[
        "private gold",
        "production implementation",
        "private production results",
        "results",
        "comparison answers",
        "other references",
        "proposed ETag formula",
        "chosen first-stage headline predicate",
        "oracle",
        "generator",
        "verifier",
        "baselines",
        "sealedoracles",
        "comparisons",
        "reference semantic source",
        "reference semantic results",
    ];
    let Some(not_supplied) = attestation.get("not_supplied").and_then(Value::as_array) else {
        errors.push(
            "source-origin chain not_supplied categories are unsupported or unordered".into(),
        );
        return;
    };
    let mut categories = Vec::new();
    for value in not_supplied {
        let Some(value) = value.as_str() else {
            errors.push(
                "source-origin chain not_supplied categories are unsupported or unordered".into(),
            );
            return;
        };
        if !FORBIDDEN_CATEGORIES.contains(&value) {
            errors.push(
                "source-origin chain not_supplied categories are unsupported or unordered".into(),
            );
            return;
        }
        categories.push(value.to_owned());
    }
    let mut sorted_categories = categories.clone();
    sorted_categories.sort();
    sorted_categories.dedup();
    if sorted_categories != categories {
        errors.push(
            "source-origin chain not_supplied categories are unsupported or unordered".into(),
        );
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn validate_chain_review(
    raw: &[u8],
    review_ref: &Map<String, Value>,
    chain_ref: &Map<String, Value>,
    referenced: &BTreeSet<(String, String)>,
    hist_task: &str,
    successor_task: &str,
    _evaluator_id: &str,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) {
    let Some(review_value) = super::read_object(raw, "source-origin chain review", errors, gaps)
    else {
        return;
    };
    let review_value = Value::Object(review_value);
    let Some(review) = exact_object(
        Some(&review_value),
        &[
            "format",
            "record_version",
            "chain",
            "referenced_artifacts",
            "checks",
            "decision",
            "reviewers",
            "limitations",
        ],
        &[],
        "chain review",
        errors,
    ) else {
        return;
    };
    if review.get("chain") != Some(&Value::Object(chain_ref.clone())) {
        errors.push("source-origin chain review does not bind exact chain bytes".into());
    }
    if review.get("format").and_then(Value::as_str)
        != Some("identitybench.v5.source-origin-chain-review.v1")
        || strict_int(review.get("record_version")) != Some(1)
    {
        errors.push("source-origin chain review schema differs".into());
    }
    let expected_references: Vec<Value> = referenced
        .iter()
        .map(|(path, digest)| json!({"path":path,"sha256":digest}))
        .collect();
    if review.get("referenced_artifacts") != Some(&Value::Array(expected_references)) {
        errors.push("source-origin chain review referenced artifact set differs".into());
    }
    let Some(checks) = exact_object(
        review.get("checks"),
        &[
            "baseline_core",
            "transitions",
            "author_separation",
            "artifact_hashes",
            "forbidden_exposure",
            "omissions",
            "coverage_limits",
        ],
        &[],
        "chain review checks",
        errors,
    ) else {
        return;
    };
    for field in [
        "baseline_core",
        "transitions",
        "author_separation",
        "artifact_hashes",
        "forbidden_exposure",
        "omissions",
    ] {
        if checks.get(field).and_then(Value::as_str) != Some("PASS") {
            gaps.push(format!(
                "source-origin chain review check {field} is not PASS"
            ));
        }
    }
    if checks.get("coverage_limits").and_then(Value::as_str) != Some("ACKNOWLEDGED_UNKNOWN") {
        gaps.push("source-origin chain review does not acknowledge coverage limits".into());
    }
    if checks.get("forbidden_exposure").and_then(Value::as_str) == Some("FAIL") {
        errors.push("source-origin chain review confirms forbidden exposure".into());
    }
    match review.get("decision").and_then(Value::as_str) {
        Some("QUALIFIED_ELIGIBLE") => {}
        Some("INELIGIBLE") => {
            errors.push("source-origin chain review declares the chain ineligible".into());
        }
        _ => gaps.push("source-origin chain review decision is not qualified".into()),
    }
    let Some(reviewers) = review.get("reviewers").and_then(Value::as_array) else {
        gaps.push("source-origin chain review has no independent reviewer".into());
        return;
    };
    if reviewers.is_empty() {
        gaps.push("source-origin chain review has no independent reviewer".into());
        return;
    }
    let mut reviewer_tasks = Vec::new();
    for (index, reviewer_value) in reviewers.iter().enumerate() {
        let Some(reviewer) = exact_object(
            Some(reviewer_value),
            &["task", "decision", "route", "reviewed_at_utc"],
            &[],
            &format!("chain reviewer {index}"),
            errors,
        ) else {
            continue;
        };
        if let Some(task) = reviewer
            .get("task")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
        {
            reviewer_tasks.push(task.to_owned());
        } else {
            errors.push("source-origin chain reviewer task is malformed".into());
        }
        if reviewer.get("decision").and_then(Value::as_str) != Some("PASS") {
            gaps.push("source-origin chain reviewer has no positive verdict".into());
        }
        validate_route(reviewer.get("route"), "chain reviewer", errors);
        timestamp(reviewer.get("reviewed_at_utc"), "chain review time", errors);
    }
    let mut unique_reviewers = reviewer_tasks.clone();
    unique_reviewers.sort();
    unique_reviewers.dedup();
    if unique_reviewers.len() != reviewer_tasks.len()
        || reviewer_tasks
            .iter()
            .any(|task| task == hist_task || task == successor_task)
    {
        errors.push("source-origin chain reviewer overlaps an author or reviewer".into());
    }
    limitations(
        review.get("limitations"),
        "chain review limitations",
        true,
        errors,
    );
    let _ = review_ref;
}

#[allow(clippy::too_many_arguments)]
pub(super) fn validate_successor_audit(
    report: &Map<String, Value>,
    wrapper: &Map<String, Value>,
    successor: &Map<String, Value>,
    task: &str,
    session: &str,
    evaluator_id: &str,
    extractor: &ChainArtifact,
    output: Option<&ChainCapture>,
    input: Option<&ChainCapture>,
    errors: &mut Vec<String>,
    _gaps: &mut Vec<String>,
) -> bool {
    let wrapper_value = Value::Object(wrapper.clone());
    let Some(wrapper) = exact_object(
        Some(&wrapper_value),
        &[
            "report",
            "executed_extractor",
            "prefix",
            "cutoff",
            "disclosed_unknowns",
        ],
        &[],
        "successor audit",
        errors,
    ) else {
        return false;
    };
    let report_value = Value::Object(report.clone());
    let Some(report) = exact_object(
        Some(&report_value),
        &[
            "format",
            "record_version",
            "evaluator_id",
            "author",
            "prefix",
            "output_capture",
            "cutoff",
            "executed_extractor_sha256",
            "observed_path_events",
            "forbidden_exposure",
            "disclosed_unknowns",
        ],
        &[],
        "successor audit report",
        errors,
    ) else {
        return false;
    };
    if report.get("format").and_then(Value::as_str)
        != Some("identitybench.v5.successor-source-origin-audit.v1")
        || strict_int(report.get("record_version")) != Some(1)
        || report.get("evaluator_id").and_then(Value::as_str) != Some(evaluator_id)
    {
        errors.push("source-origin chain successor audit schema or evaluator differs".into());
    }
    let expected_author = json!({
        "task": task,
        "session_id": session,
        "source_profile": successor.get("source_profile"),
        "author_origin": successor.get("author_origin"),
    });
    if report.get("author") != Some(&expected_author) {
        errors.push("source-origin chain successor audit author binding differs".into());
    }
    let wrapper_prefix =
        super::parse_chain_prefix(wrapper.get("prefix"), "successor audit", errors);
    let report_prefix = report.get("prefix");
    if wrapper.get("prefix") != report_prefix {
        errors.push("source-origin chain successor wrapper/report prefix differs".into());
    }
    let Some(report_prefix) =
        super::parse_chain_prefix(report.get("prefix"), "successor report", errors)
    else {
        return false;
    };
    if wrapper_prefix.is_none() {
        return false;
    }
    if report_prefix.get("session_id").and_then(Value::as_str) != Some(session)
        || strict_int(report_prefix.get("start_offset_bytes")) != Some(0)
    {
        errors.push(
            "source-origin chain successor audit prefix is not the exact session prefix".into(),
        );
    }
    if let Some(output) = output {
        if wrapper.get("executed_extractor") != Some(&Value::Object(extractor.reference.clone())) {
            errors.push("source-origin chain successor extractor binding differs".into());
        }
        if report.get("output_capture") != Some(&Value::Object(output.wrapper.clone())) {
            errors.push("source-origin chain successor audit output capture differs".into());
        }
    } else {
        errors.push("source-origin chain successor audit output capture differs".into());
    }
    if wrapper.get("cutoff") != report.get("cutoff") {
        errors.push("source-origin chain successor wrapper/report cutoff differs".into());
    }
    let Some(cutoff) = exact_object(
        wrapper.get("cutoff"),
        &["timestamp_utc", "basis"],
        &[],
        "successor cutoff",
        errors,
    ) else {
        return false;
    };
    if cutoff.get("basis").and_then(Value::as_str) != Some("filesystem_mtime_weak") {
        errors.push("source-origin chain successor cutoff basis is unsupported".into());
    }
    let cutoff_time = timestamp(cutoff.get("timestamp_utc"), "successor cutoff", errors);
    if report
        .get("executed_extractor_sha256")
        .and_then(Value::as_str)
        != extractor.reference.get("sha256").and_then(Value::as_str)
    {
        errors.push("source-origin chain successor audit does not bind executed extractor".into());
    }

    let mut event_rows: Vec<(String, String, String)> = Vec::new();
    let mut forbidden_seen = false;
    let Some(events) = report.get("observed_path_events").and_then(Value::as_array) else {
        errors.push("source-origin chain observed path events are malformed".into());
        return false;
    };
    for (index, event_value) in events.iter().enumerate() {
        let Some(event) = exact_object(
            Some(event_value),
            &["classification", "path", "evidence_sha256"],
            &[],
            &format!("observed path event {index}"),
            errors,
        ) else {
            continue;
        };
        let classification = event.get("classification").and_then(Value::as_str);
        let path = event.get("path").and_then(Value::as_str);
        let digest = event.get("evidence_sha256").and_then(Value::as_str);
        if !matches!(classification, Some("ALLOWED" | "FORBIDDEN"))
            || path.is_none_or(str::is_empty)
            || !digest.is_some_and(super::is_sha256)
        {
            errors.push("source-origin chain observed path event is malformed".into());
            continue;
        }
        if classification == Some("FORBIDDEN") {
            forbidden_seen = true;
        }
        event_rows.push((
            path.unwrap_or_default().to_owned(),
            classification.unwrap_or_default().to_owned(),
            digest.unwrap_or_default().to_owned(),
        ));
    }
    let mut sorted_events = event_rows.clone();
    sorted_events.sort();
    sorted_events.dedup();
    if event_rows != sorted_events {
        errors.push("source-origin chain observed path events are not sorted and unique".into());
    }
    if let (Some(input), Some(output)) = (input, output) {
        let input_hashes = source_file_hashes(&input.source_map);
        let output_hashes = source_file_hashes(&output.source_map);
        let changed: BTreeSet<&str> = input_hashes
            .iter()
            .filter_map(|(path, digest)| {
                (output_hashes.get(path).map(String::as_str) != Some(digest.as_str()))
                    .then_some(path.as_str())
            })
            .collect();
        let allowed: BTreeSet<&str> = event_rows
            .iter()
            .filter_map(|(path, classification, _)| {
                (classification == "ALLOWED").then_some(path.as_str())
            })
            .collect();
        if !changed.is_subset(&allowed) {
            errors.push("source-origin chain source edits lack allowed audit events".into());
        }
        if input.source_map.get("files") != output.source_map.get("files")
            && input.source_map.get("source_profile") != output.source_map.get("source_profile")
        {
            let profile_slot = super::EVALUATOR_WIRE_ID_BY_SLOT
                .iter()
                .find_map(|(slot, wire)| (*wire == evaluator_id).then_some(*slot))
                .unwrap_or("");
            let input_profile = super::profile_for(
                profile_slot,
                input.source_map.get("source_profile"),
                input.source_map.get("author_origin"),
                "successor input",
                errors,
            );
            let output_profile = super::profile_for(
                profile_slot,
                output.source_map.get("source_profile"),
                output.source_map.get("author_origin"),
                "successor output",
                errors,
            );
            if input_profile
                .as_ref()
                .zip(output_profile.as_ref())
                .is_some_and(|(input, output)| input.source_files != output.source_files)
            {
                errors.push(
                    "source-origin chain successor input/output closures are not registered equivalent"
                        .into(),
                );
            } else if input_profile.is_none() || output_profile.is_none() {
                errors.push(
                    "source-origin chain successor input/output profile equivalence is unsupported"
                        .into(),
                );
            }
        }
    }
    let expected_disposition = if forbidden_seen {
        "CONFIRMED"
    } else {
        "NONE_OBSERVED"
    };
    if report.get("forbidden_exposure").and_then(Value::as_str) != Some(expected_disposition) {
        errors
            .push("source-origin chain forbidden exposure disposition differs from events".into());
    }
    if forbidden_seen {
        errors.push("source-origin chain confirms forbidden exposure".into());
    }

    let unknowns = report.get("disclosed_unknowns");
    if unknowns != wrapper.get("disclosed_unknowns") || unknowns.and_then(Value::as_array).is_none()
    {
        errors.push("source-origin chain disclosed unknowns differ from report".into());
    } else if let Some(unknowns) = unknowns.and_then(Value::as_array) {
        let mut normalized = Vec::new();
        for (index, row_value) in unknowns.iter().enumerate() {
            let Some(row) = exact_object(
                Some(row_value),
                &["code", "evidence_sha256", "reason"],
                &[],
                &format!("disclosed unknown {index}"),
                errors,
            ) else {
                continue;
            };
            let code = row.get("code").and_then(Value::as_str);
            let reason = row.get("reason").and_then(Value::as_str);
            let digest_value = row.get("evidence_sha256");
            let digest = match digest_value {
                Some(Value::Null) | None => None,
                Some(Value::String(value)) if super::is_sha256(value) => Some(value.as_str()),
                _ => {
                    errors.push("source-origin chain disclosed unknown row is malformed".into());
                    continue;
                }
            };
            if code.is_none_or(str::is_empty) || reason.is_none_or(str::is_empty) {
                errors.push("source-origin chain disclosed unknown row is malformed".into());
                continue;
            }
            normalized.push((
                code.unwrap_or_default().to_owned(),
                reason.unwrap_or_default().to_owned(),
                digest.map(str::to_owned),
            ));
        }
        let mut sorted = normalized.clone();
        sorted.sort_by(|left, right| {
            left.0
                .cmp(&right.0)
                .then(left.1.cmp(&right.1))
                .then(left.2.is_some().cmp(&right.2.is_some()))
                .then(left.2.cmp(&right.2))
        });
        sorted.dedup();
        if normalized != sorted {
            errors.push("source-origin chain disclosed unknowns are not sorted and unique".into());
        }
    }
    if let Some(output) = output {
        if !super::mtime_matches(&output.manifest_path, cutoff_time.as_deref().unwrap_or("")) {
            errors.push(
                "source-origin chain successor cutoff does not bind output capture mtime".into(),
            );
        }
    }
    true
}

fn source_file_hashes(source_map: &Map<String, Value>) -> BTreeMap<String, String> {
    source_map
        .get("files")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|row| {
            let row = row.as_object()?;
            Some((
                row.get("path")?.as_str()?.to_owned(),
                row.get("sha256")?.as_str()?.to_owned(),
            ))
        })
        .collect()
}

pub(super) fn validate_chain_limitations(value: Option<&Value>, errors: &mut Vec<String>) {
    limitations(value, "chain limitations", true, errors);
}

#[allow(clippy::too_many_arguments)]
pub(super) fn validate_predecessor(
    value: &Value,
    hist_author: &Map<String, Value>,
    hist_prefix: &Map<String, Value>,
    hist_capture: &ChainCapture,
    evaluator_id: &str,
    profile_slot: &str,
    root: &Path,
    referenced: &mut BTreeSet<(String, String)>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Option<ChainCapture> {
    let predecessor = exact_object(
        Some(value),
        &[
            "author",
            "input_prefix",
            "extension",
            "output_prefix",
            "audit",
            "executed_extractor",
            "output_capture",
        ],
        &[],
        "predecessor continuation",
        errors,
    )?;
    let pred_author = super::parse_chain_author(
        predecessor.get("author"),
        "predecessor",
        profile_slot,
        errors,
    );
    let input_prefix =
        super::parse_chain_prefix(predecessor.get("input_prefix"), "predecessor input", errors);
    let output_prefix = super::parse_chain_prefix(
        predecessor.get("output_prefix"),
        "predecessor output",
        errors,
    );
    let extension = exact_object(
        predecessor.get("extension"),
        &[
            "start_offset_bytes",
            "bytes",
            "line_count",
            "sha256",
            "last_included_utc",
        ],
        &[],
        "predecessor extension",
        errors,
    );
    let audit_result = super::read_chain_json_ref(
        root,
        predecessor.get("audit"),
        "predecessor audit",
        referenced,
        errors,
        gaps,
    );
    let extractor = super::read_chain_ref(
        root,
        predecessor.get("executed_extractor"),
        "predecessor executed extractor",
        referenced,
        errors,
        gaps,
    );
    let output = super::parse_chain_capture(
        predecessor.get("output_capture"),
        "predecessor output",
        profile_slot,
        evaluator_id,
        root,
        referenced,
        errors,
        gaps,
    );
    if pred_author.as_ref() != Some(hist_author) {
        errors.push("source-origin chain predecessor is not the historical author".into());
    }
    let (
        Some(input_prefix),
        Some(output_prefix),
        Some(extension),
        Some((report, _)),
        Some(extractor),
        Some(output),
    ) = (
        input_prefix,
        output_prefix,
        extension,
        audit_result,
        extractor,
        output,
    )
    else {
        gaps.push("source-origin chain predecessor transition evidence is incomplete".into());
        return None;
    };

    for field in [
        "session_id",
        "start_offset_bytes",
        "bytes",
        "line_count",
        "sha256",
        "last_included_utc",
    ] {
        if input_prefix.get(field) != hist_prefix.get(field) {
            errors.push(
                "source-origin chain predecessor input prefix differs from historical prefix"
                    .into(),
            );
            break;
        }
    }
    let ext_start = strict_int(extension.get("start_offset_bytes"));
    let ext_bytes = strict_int(extension.get("bytes"));
    let ext_lines = strict_int(extension.get("line_count"));
    if ext_start.is_none_or(|value| value < 0)
        || ext_start != strict_int(input_prefix.get("bytes"))
        || ext_bytes.is_none_or(|value| value <= 0)
        || ext_lines.is_none_or(|value| value <= 0)
    {
        errors.push("source-origin chain predecessor extension arithmetic is invalid".into());
    }
    let ext_digest = extension.get("sha256").and_then(Value::as_str);
    if !ext_digest.is_some_and(super::is_sha256) {
        errors.push("source-origin chain predecessor extension digest is malformed".into());
    }
    timestamp(
        extension.get("last_included_utc"),
        "predecessor extension cutoff",
        errors,
    );
    let expected_bytes =
        strict_int(input_prefix.get("bytes")).unwrap_or_default() + ext_bytes.unwrap_or_default();
    let expected_lines = strict_int(input_prefix.get("line_count")).unwrap_or_default()
        + ext_lines.unwrap_or_default();
    if output_prefix.get("session_id") != hist_author.get("session_id")
        || strict_int(output_prefix.get("start_offset_bytes")) != Some(0)
        || strict_int(output_prefix.get("bytes")) != Some(expected_bytes)
        || strict_int(output_prefix.get("line_count")) != Some(expected_lines)
        || output_prefix.get("last_included_utc") != extension.get("last_included_utc")
    {
        errors.push("source-origin chain predecessor output prefix does not extend input".into());
    }

    let report_identity = report.get("audit_identity").and_then(Value::as_object);
    if !report_identity.is_some_and(|identity| {
        identity
            .get("task")
            .and_then(Value::as_str)
            .is_some_and(|task| {
                super::task_identity_base(task)
                    == hist_author
                        .get("task")
                        .and_then(Value::as_str)
                        .unwrap_or_default()
            })
            && identity.get("child_session_id") == hist_author.get("session_id")
            && identity.get("source_profile") == hist_author.get("source_profile")
            && identity.get("author_origin") == hist_author.get("author_origin")
            && identity.get("wire_profile").and_then(Value::as_str) == Some(evaluator_id)
    }) {
        errors.push("source-origin chain predecessor audit author/session differs".into());
    }
    if report.get("approved_auditor_script_sha256") != extractor.reference.get("sha256") {
        errors.push("source-origin chain predecessor extractor is not bound by audit".into());
    }
    let full = evaluator_id == "full_spec_blind";
    let expected_report_fields: BTreeSet<&str> = if full {
        [
            "audit_kind",
            "audit_identity",
            "cutoff_utc",
            "cutoff_basis",
            "approved_auditor_script_sha256",
            "extractor_current_sha256",
            "audit_predecessor",
            "capture_binding",
            "capture_source_evidence",
            "prior_qualified_prefix",
            "child_log_extension",
            "author_content_allowlist",
            "optional_corroboration_paths",
            "root_approved_public_supplement",
            "limitations",
        ]
        .into_iter()
        .collect()
    } else {
        [
            "audit_kind",
            "audit_identity",
            "cutoff_utc",
            "cutoff_basis",
            "approved_auditor_script_sha256",
            "audit_predecessor",
            "capture_binding",
            "qualified_prefix_binding",
            "child_log_extension",
            "declared_author_content_allowlist",
            "optional_corroboration_path_status",
            "root_approved_public_supplement",
            "limitations",
        ]
        .into_iter()
        .collect()
    };
    if report.keys().map(String::as_str).collect::<BTreeSet<_>>() != expected_report_fields {
        errors.push("source-origin chain predecessor audit report fields differ".into());
    }
    let expected_kind = if full {
        "bounded fresh full-spec author-session source-origin metadata audit"
    } else {
        "bounded metadata-only continuation after a qualified card-author prefix"
    };
    if report.get("audit_kind").and_then(Value::as_str) != Some(expected_kind) {
        errors.push("source-origin chain predecessor audit report kind is unsupported".into());
    }
    let expected_basis = if full {
        "explicit root-supplied capture-manifest filesystem mtime; weak chronology metadata, not cryptographic chronology proof"
    } else {
        "root-bound capture-manifest filesystem mtime; weak chronology metadata, not cryptographic chronology proof"
    };
    if report.get("cutoff_basis").and_then(Value::as_str) != Some(expected_basis) {
        errors.push("source-origin chain predecessor cutoff basis differs".into());
    }
    if full && report.get("extractor_current_sha256") != extractor.reference.get("sha256") {
        errors.push("source-origin chain predecessor current extractor hash differs".into());
    }

    let capture_binding = report.get("capture_binding").and_then(Value::as_object);
    let expected_capture_fields: BTreeSet<&str> = if full {
        [
            "manifest_path",
            "manifest_raw_sha256",
            "manifest_logical_sha256",
            "manifest_canonical_json_sha256_observed",
            "logical_sha256_verification",
            "manifest_filesystem_mtime_utc",
            "manifest_filesystem_mtime_ns",
            "mtime_precision_comparison",
            "manifest_bytes",
            "manifest_only_read",
            "referenced_paths_followed",
        ]
        .into_iter()
        .collect()
    } else {
        [
            "manifest_path",
            "manifest_raw_sha256",
            "manifest_logical_sha256",
            "manifest_filesystem_mtime_utc",
            "manifest_filesystem_mtime_ns",
            "manifest_bytes",
            "manifest_only_read",
            "referenced_paths_followed",
        ]
        .into_iter()
        .collect()
    };
    if capture_binding.is_none_or(|binding| {
        binding.keys().map(String::as_str).collect::<BTreeSet<_>>() != expected_capture_fields
    }) {
        errors.push("source-origin chain predecessor capture binding fields differ".into());
    }

    let prefix_field = if full {
        "prior_qualified_prefix"
    } else {
        "qualified_prefix_binding"
    };
    let prefix_report = exact_object(
        report.get(prefix_field),
        &[
            "session_id",
            "bytes",
            "line_count",
            "sha256",
            "last_included_timestamp",
            "historical_bytes_payload_decoded",
        ],
        &[],
        "predecessor report prior prefix",
        errors,
    );
    let extension_report = exact_object(
        report.get("child_log_extension"),
        &[
            "log_path",
            "qualified_prefix",
            "included_extension",
            "combined_prefix",
            "excluded_lookahead",
            "outer_and_payload_type_counts",
            "resolved_metadata_events",
            "concrete_unresolved_items",
            "captured_log_size_bytes_from_fstat",
            "unread_bytes_after_stop",
            "bytes_read_beyond_included_prefix",
            "record_bound_bytes",
        ],
        &[],
        "predecessor child-log extension report",
        errors,
    );
    let mut cutoff = None;
    if let (Some(prefix_report), Some(extension_report)) = (prefix_report, extension_report) {
        if prefix_report.get("historical_bytes_payload_decoded") != Some(&Value::Bool(false))
            || !extension_report
                .get("log_path")
                .and_then(Value::as_str)
                .is_some_and(|path| !path.is_empty())
        {
            errors.push("source-origin chain predecessor prefix metadata is unsupported".into());
        }
        let counts_valid = extension_report
            .get("outer_and_payload_type_counts")
            .and_then(Value::as_object)
            .is_some_and(|counts| {
                counts
                    .values()
                    .all(|count| strict_int(Some(count)).is_some_and(|count| count >= 0))
            });
        if !counts_valid {
            errors.push("source-origin chain predecessor audit counts are malformed".into());
        }
        for field in [
            "captured_log_size_bytes_from_fstat",
            "unread_bytes_after_stop",
            "bytes_read_beyond_included_prefix",
            "record_bound_bytes",
        ] {
            if strict_int(extension_report.get(field)).is_none_or(|value| value < 0) {
                errors.push(format!(
                    "source-origin chain predecessor audit {field} is malformed"
                ));
            }
        }
        let events = extension_report.get("resolved_metadata_events");
        let unresolved = extension_report.get("concrete_unresolved_items");
        if events.and_then(Value::as_array).is_none()
            || unresolved.and_then(Value::as_array).is_none()
        {
            errors.push("source-origin chain predecessor event inventory is malformed".into());
        } else if contains_forbidden_event(events.unwrap_or(&Value::Null)) {
            errors.push("source-origin chain predecessor audit confirms forbidden exposure".into());
        }
        if let Some(lookahead) = extension_report.get("excluded_lookahead")
            && !lookahead.is_null()
        {
            if let Some(lookahead) = exact_object(
                Some(lookahead),
                &[
                    "line",
                    "timestamp",
                    "outer_type",
                    "bytes",
                    "excluded",
                    "payload_inspected",
                    "included_prefix_hash_updated",
                ],
                &[],
                "predecessor excluded lookahead",
                errors,
            ) {
                if strict_int(lookahead.get("line")).is_none_or(|value| value <= 0)
                    || timestamp(
                        lookahead.get("timestamp"),
                        "predecessor lookahead timestamp",
                        errors,
                    )
                    .is_none()
                    || lookahead
                        .get("outer_type")
                        .and_then(Value::as_str)
                        .is_none()
                    || strict_int(lookahead.get("bytes")).is_none_or(|value| value <= 0)
                    || lookahead.get("excluded") != Some(&Value::Bool(true))
                    || lookahead.get("payload_inspected") != Some(&Value::Bool(false))
                    || lookahead.get("included_prefix_hash_updated") != Some(&Value::Bool(false))
                {
                    errors.push("source-origin chain predecessor lookahead binding differs".into());
                }
            }
        }
        for (field, expected) in [
            ("session_id", input_prefix.get("session_id")),
            ("bytes", input_prefix.get("bytes")),
            ("line_count", input_prefix.get("line_count")),
            ("sha256", input_prefix.get("sha256")),
        ] {
            if prefix_report.get(field) != expected {
                errors.push("source-origin chain predecessor report input prefix differs".into());
                break;
            }
        }
        if prefix_report.get("last_included_timestamp") != input_prefix.get("last_included_utc")
            || prefix_report.get("start_offset_bytes").is_some()
        {
            errors.push("source-origin chain predecessor report input prefix differs".into());
        }
        let extension_binding = exact_object(
            extension_report.get("included_extension"),
            &[
                "start_offset_bytes",
                "bytes",
                "line_count",
                "sha256",
                "last_included_outer_timestamp",
            ],
            &[],
            "predecessor included extension",
            errors,
        );
        if extension_binding.is_none_or(|binding| {
            binding.get("start_offset_bytes") != extension.get("start_offset_bytes")
                || binding.get("bytes") != extension.get("bytes")
                || binding.get("line_count") != extension.get("line_count")
                || binding.get("sha256") != extension.get("sha256")
                || binding.get("last_included_outer_timestamp")
                    != extension.get("last_included_utc")
        }) {
            errors.push("source-origin chain predecessor report extension differs".into());
        }
        let combined = exact_object(
            extension_report.get("combined_prefix"),
            &["bytes", "line_count", "sha256"],
            &[],
            "predecessor combined prefix",
            errors,
        );
        if combined.is_none_or(|binding| {
            binding.get("bytes") != output_prefix.get("bytes")
                || binding.get("line_count") != output_prefix.get("line_count")
                || binding.get("sha256") != output_prefix.get("sha256")
        }) {
            errors.push("source-origin chain predecessor report combined prefix differs".into());
        }
        let log_prefix = exact_object(
            extension_report.get("qualified_prefix"),
            &[
                "start_offset_bytes",
                "bytes",
                "line_count",
                "sha256",
                "last_included_outer_timestamp",
                "payload_decoded",
            ],
            &[],
            "predecessor audit qualified prefix",
            errors,
        );
        if log_prefix.is_none_or(|prefix| {
            strict_int(prefix.get("start_offset_bytes")) != Some(0)
                || prefix.get("bytes") != input_prefix.get("bytes")
                || prefix.get("line_count") != input_prefix.get("line_count")
                || prefix.get("sha256") != input_prefix.get("sha256")
                || prefix.get("payload_decoded") != Some(&Value::Bool(false))
                || prefix
                    .get("last_included_outer_timestamp")
                    .and_then(Value::as_str)
                    .zip(
                        input_prefix
                            .get("last_included_utc")
                            .and_then(Value::as_str),
                    )
                    .is_none_or(|(left, right)| !super::same_utc(left, right))
        }) {
            errors.push(
                "source-origin chain predecessor report does not bind exact prior prefix".into(),
            );
        }
        cutoff = timestamp(
            report.get("cutoff_utc"),
            "predecessor report cutoff",
            errors,
        );
    } else {
        errors
            .push("source-origin chain predecessor audit has no supported prefix extension".into());
    }

    let binding = capture_binding;
    if let Some(binding) = binding {
        let capture_manifest = output.wrapper.get("manifest").and_then(Value::as_object);
        let mtime = std::fs::symlink_metadata(&output.manifest_path).ok();
        #[cfg(unix)]
        let (mtime_ns, size_bytes) = mtime.as_ref().map_or((None, None), |metadata| {
            use std::os::unix::fs::MetadataExt;
            (
                Some(
                    i128::from(metadata.mtime()) * 1_000_000_000
                        + i128::from(metadata.mtime_nsec()),
                ),
                Some(i128::from(metadata.len())),
            )
        });
        #[cfg(not(unix))]
        let (mtime_ns, size_bytes) = (
            None,
            mtime.as_ref().map(|metadata| i128::from(metadata.len())),
        );
        if mtime.is_none() {
            gaps.push("source-origin chain predecessor output capture stat is unavailable".into());
        }
        let mut invalid = capture_manifest.is_none_or(|manifest| {
            binding.get("manifest_path") != manifest.get("path")
                || binding.get("manifest_raw_sha256") != manifest.get("sha256")
                || binding.get("manifest_logical_sha256") != output.wrapper.get("logical_sha256")
                || strict_int(binding.get("manifest_filesystem_mtime_ns")) != mtime_ns
                || strict_int(binding.get("manifest_bytes")) != size_bytes
                || binding.get("manifest_only_read") != Some(&Value::Bool(true))
                || binding.get("referenced_paths_followed") != Some(&Value::Bool(false))
                || binding
                    .get("manifest_filesystem_mtime_utc")
                    .and_then(Value::as_str)
                    .zip(cutoff.as_deref())
                    .is_none_or(|(left, right)| !super::same_utc(left, right))
                || cutoff
                    .as_deref()
                    .is_none_or(|value| !super::mtime_matches(&output.manifest_path, value))
        });
        if full {
            invalid |= binding.get("manifest_canonical_json_sha256_observed")
                != output.wrapper.get("logical_sha256")
                || binding
                    .get("logical_sha256_verification")
                    .and_then(Value::as_str)
                    != Some(
                        "canonical_sorted_compact_ensure_ascii_false_allow_nan_false_after_removing_manifest_sha256",
                    )
                || !binding
                    .get("mtime_precision_comparison")
                    .and_then(Value::as_str)
                    .is_some_and(|value| !value.is_empty());
        }
        if invalid {
            errors.push(
                "source-origin chain predecessor audit does not bind output capture cutoff".into(),
            );
        }
    }
    if output
        .source_map
        .get("evaluator_id")
        .and_then(Value::as_str)
        != Some(evaluator_id)
    {
        errors.push("source-origin chain predecessor output evaluator differs".into());
    }
    if output.source_map.get("source_profile") != hist_author.get("source_profile")
        || output.source_map.get("author_origin") != hist_author.get("author_origin")
    {
        errors.push("source-origin chain predecessor output identity differs".into());
    }
    let _ = hist_capture;
    Some(output)
}

fn contains_forbidden_event(value: &Value) -> bool {
    match value {
        Value::Object(object) => object.iter().any(|(key, item)| {
            (key == "classification" && item.as_str() == Some("FORBIDDEN"))
                || (key == "forbidden_exposure" && item.as_str() == Some("CONFIRMED"))
                || contains_forbidden_event(item)
        }),
        Value::Array(items) => items.iter().any(contains_forbidden_event),
        _ => false,
    }
}
