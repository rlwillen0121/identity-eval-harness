//! Provenance binding and G-SPEC validation for native Rust conformance runs.
//!
//! Native receipts use an additive schema; the older Python receipt path stays
//! supported as historical evidence. The native gate replays every result against
//! the frozen public corpus and the current Rust production evaluator.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Component, Path, PathBuf};

use ib_core::{hashing::sha256_hex, jcs_dumps};
use ib_v5_generator::{DatasetArtifactsV5, V5ArtifactError, load_v5_dataset};
use serde_json::{Map, Value, json};

pub const NATIVE_SHARED_PROOF_SCHEMA: &str = "identitybench.v5.native-shared-proof-receipt.v1";
pub const NATIVE_FREEZE_SCHEMA: &str = "identitybench.v5.native-blind-output-freeze.v1";

const NATIVE_BUILD_SCHEMA: &str = "identitybench.v5.native-build-identity.v1";
const SHA256_HEX_LEN: usize = 64;
const NATIVE_RECEIPT_FIELDS: [&str; 8] = [
    "schema_version",
    "proof_scope",
    "suite_id",
    "spec_sha256",
    "dataset_bindings",
    "corpus",
    "freeze",
    "evaluators",
];
const NATIVE_EVALUATORS: [&str; 3] = ["full_spec_blind", "agent_view_only", "production"];

/// Capture the source files compiled into `identitybench` and its executable digest.
/// `embedded_source_identity` and `toolchain_identity` come from `ib-cli/build.rs`'s
/// generated `OUT_DIR/source_identity.rs`; they are never reconstructed from the
/// runtime checkout.
pub fn capture_native_build_identity(
    repository_root: &Path,
    embedded_source_identity: &Value,
    toolchain_identity: &Value,
    executable_path: &Path,
) -> Result<Value, V5ArtifactError> {
    let root = repository_root.canonicalize().map_err(|error| {
        V5ArtifactError::new(format!("repository root is unavailable: {error}"))
    })?;
    let embedded = parse_source_identity(embedded_source_identity)?;
    let current = scan_workspace_source_identity(&root)?;
    if current != embedded {
        return Err(V5ArtifactError::new(
            "workspace Rust source differs from the source identity embedded in identitybench",
        ));
    }
    let binary_sha256 = hash_regular_file(executable_path, "identitybench executable")?;
    let identity_value =
        serde_json::to_value(&embedded).map_err(|error| V5ArtifactError::new(error.to_string()))?;
    let source_identity_sha256 = sha256_hex(
        &jcs_dumps(&identity_value).map_err(|error| V5ArtifactError::new(error.to_string()))?,
    );
    Ok(json!({
        "schema_version": NATIVE_BUILD_SCHEMA,
        "source_identity": identity_value,
        "source_identity_sha256": source_identity_sha256,
        "executable_sha256": binary_sha256,
        "toolchain": toolchain_identity,
    }))
}

/// Recheck the build identity at run, bind, and verify time.
pub fn verify_native_build_identity(
    repository_root: &Path,
    identity: &Value,
    current_executable_path: &Path,
) -> Result<(), V5ArtifactError> {
    let Some(object) = identity.as_object() else {
        return Err(V5ArtifactError::new("native build identity is malformed"));
    };
    let expected_fields = [
        "schema_version",
        "source_identity",
        "source_identity_sha256",
        "executable_sha256",
        "toolchain",
    ];
    if object.len() != expected_fields.len()
        || expected_fields
            .iter()
            .any(|field| !object.contains_key(*field))
        || object.get("schema_version").and_then(Value::as_str) != Some(NATIVE_BUILD_SCHEMA)
        || object.get("toolchain").and_then(Value::as_object).is_none()
    {
        return Err(V5ArtifactError::new("native build identity is malformed"));
    }
    let expected_source = parse_source_identity(&object["source_identity"])?;
    let expected_source_value = serde_json::to_value(&expected_source)
        .map_err(|error| V5ArtifactError::new(error.to_string()))?;
    let expected_source_sha = sha256_hex(
        &jcs_dumps(&expected_source_value)
            .map_err(|error| V5ArtifactError::new(error.to_string()))?,
    );
    if object.get("source_identity_sha256").and_then(Value::as_str)
        != Some(expected_source_sha.as_str())
    {
        return Err(V5ArtifactError::new(
            "native source identity digest does not match its file map",
        ));
    }
    let root = repository_root.canonicalize().map_err(|error| {
        V5ArtifactError::new(format!("repository root is unavailable: {error}"))
    })?;
    if scan_workspace_source_identity(&root)? != expected_source {
        return Err(V5ArtifactError::new(
            "bound Rust source or Cargo inputs changed after native freeze",
        ));
    }
    let actual_binary_sha = hash_regular_file(current_executable_path, "identitybench executable")?;
    if object.get("executable_sha256").and_then(Value::as_str) != Some(actual_binary_sha.as_str()) {
        return Err(V5ArtifactError::new(
            "bound identitybench executable changed after native freeze",
        ));
    }
    Ok(())
}

fn parse_source_identity(value: &Value) -> Result<BTreeMap<String, String>, V5ArtifactError> {
    let Some(object) = value.as_object() else {
        return Err(V5ArtifactError::new(
            "embedded Rust source identity is malformed",
        ));
    };
    if object.is_empty() {
        return Err(V5ArtifactError::new(
            "embedded Rust source identity is empty",
        ));
    }
    let mut output = BTreeMap::new();
    for (path, digest) in object {
        let Some(digest) = digest.as_str() else {
            return Err(V5ArtifactError::new(
                "embedded Rust source identity is malformed",
            ));
        };
        if path.is_empty() || path.contains('\\') || !is_sha256(digest) {
            return Err(V5ArtifactError::new(
                "embedded Rust source identity is malformed",
            ));
        }
        let rel = Path::new(path);
        if rel.is_absolute()
            || rel.components().any(|part| {
                matches!(
                    part,
                    Component::ParentDir | Component::CurDir | Component::Prefix(_)
                )
            })
        {
            return Err(V5ArtifactError::new(
                "embedded Rust source identity path is unsafe",
            ));
        }
        output.insert(path.clone(), digest.to_owned());
    }
    Ok(output)
}

fn scan_workspace_source_identity(
    root: &Path,
) -> Result<BTreeMap<String, String>, V5ArtifactError> {
    let mut paths = BTreeMap::<String, PathBuf>::new();
    for name in [
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain.toml",
        "build.rs",
    ] {
        add_source_file(root, Path::new(name), &mut paths)?;
    }
    let crates = root.join("crates");
    let entries = fs::read_dir(&crates).map_err(|error| {
        V5ArtifactError::new(format!("workspace crates are unavailable: {error}"))
    })?;
    for entry in entries {
        let entry = entry.map_err(|error| V5ArtifactError::new(error.to_string()))?;
        let file_type = entry
            .file_type()
            .map_err(|error| V5ArtifactError::new(error.to_string()))?;
        if !file_type.is_dir() || file_type.is_symlink() {
            continue;
        }
        let crate_dir = entry.path();
        let relative = crate_dir
            .strip_prefix(root)
            .map_err(|error| V5ArtifactError::new(error.to_string()))?;
        add_source_file(root, &relative.join("Cargo.toml"), &mut paths)?;
        add_source_file(root, &relative.join("build.rs"), &mut paths)?;
        let source_dir = crate_dir.join("src");
        if source_dir.is_dir() {
            collect_source_dir(root, &source_dir, &mut paths)?;
        }
    }
    let mut output = BTreeMap::new();
    for (relative, absolute) in paths {
        let bytes = fs::read(&absolute).map_err(|error| {
            V5ArtifactError::new(format!(
                "native source identity input is unavailable: {relative}: {error}"
            ))
        })?;
        output.insert(relative, sha256_hex(&bytes));
    }
    Ok(output)
}

fn collect_source_dir(
    root: &Path,
    directory: &Path,
    output: &mut BTreeMap<String, PathBuf>,
) -> Result<(), V5ArtifactError> {
    let entries = fs::read_dir(directory).map_err(|error| {
        V5ArtifactError::new(format!("Rust source directory is unavailable: {error}"))
    })?;
    for entry in entries {
        let entry = entry.map_err(|error| V5ArtifactError::new(error.to_string()))?;
        let file_type = entry
            .file_type()
            .map_err(|error| V5ArtifactError::new(error.to_string()))?;
        if file_type.is_symlink() {
            continue;
        }
        let path = entry.path();
        if file_type.is_dir() {
            collect_source_dir(root, &path, output)?;
        } else if file_type.is_file() && source_file_allowed(&path) {
            let relative = path
                .strip_prefix(root)
                .map_err(|error| V5ArtifactError::new(error.to_string()))?;
            add_source_file(root, relative, output)?;
        }
    }
    Ok(())
}

fn source_file_allowed(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|name| name.to_str()) else {
        return false;
    };
    let lower = name.to_ascii_lowercase();
    !lower.starts_with(".env")
        && ![".pem", ".key", ".p12", ".pfx", ".der", ".crt"]
            .iter()
            .any(|extension| lower.ends_with(extension))
        && !path.components().any(|part| {
            matches!(
                part.as_os_str().to_str(),
                Some("tests" | "fixtures" | "testdata" | "test_data")
            )
        })
}

fn add_source_file(
    root: &Path,
    relative: &Path,
    output: &mut BTreeMap<String, PathBuf>,
) -> Result<(), V5ArtifactError> {
    let absolute = root.join(relative);
    let metadata = match fs::symlink_metadata(&absolute) {
        Ok(value) => value,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(V5ArtifactError::new(error.to_string())),
    };
    if metadata.file_type().is_symlink() || !metadata.is_file() || !source_file_allowed(relative) {
        return Ok(());
    }
    let label = relative
        .to_str()
        .ok_or_else(|| V5ArtifactError::new("native source identity path is not UTF-8"))?
        .replace('\\', "/");
    output.insert(label, absolute);
    Ok(())
}

fn hash_regular_file(path: &Path, label: &str) -> Result<String, V5ArtifactError> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| V5ArtifactError::new(format!("{label} is unavailable: {error}")))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(V5ArtifactError::new(format!(
            "{label} is not a regular file"
        )));
    }
    let bytes = fs::read(path).map_err(|error| V5ArtifactError::new(error.to_string()))?;
    Ok(sha256_hex(&bytes))
}

fn is_sha256(value: &str) -> bool {
    value.len() == SHA256_HEX_LEN
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn jcs_digest(value: &Value) -> Result<String, String> {
    jcs_dumps(value)
        .map(|bytes| sha256_hex(&bytes))
        .map_err(|error| error.to_string())
}

fn path_is_forbidden_before_read(root: &Path, path: &str) -> bool {
    if ib_v5_conformance::process::is_forbidden_blind_source_path(path) {
        return true;
    }
    let candidate = Path::new(path);
    if candidate.is_absolute()
        || path.contains('\\')
        || candidate.components().any(|part| {
            matches!(
                part,
                Component::ParentDir
                    | Component::CurDir
                    | Component::RootDir
                    | Component::Prefix(_)
            )
        })
    {
        return true;
    }
    let Ok(root) = root.canonicalize() else {
        return true;
    };
    let mut current = root.clone();
    for part in candidate.components() {
        let Component::Normal(part) = part else {
            return true;
        };
        current.push(part);
        let Ok(metadata) = fs::symlink_metadata(&current) else {
            return true;
        };
        if metadata.file_type().is_symlink() {
            return true;
        }
    }
    let Ok(resolved) = current.canonicalize() else {
        return true;
    };
    let Ok(relative) = resolved.strip_prefix(&root) else {
        return true;
    };
    ib_v5_conformance::process::is_forbidden_blind_source_path(
        &relative.to_string_lossy().replace('\\', "/"),
    )
}

fn actual_input_digest(root: &Path, path: &str) -> Option<String> {
    if path_is_forbidden_before_read(root, path) {
        return None;
    }
    let root = root.canonicalize().ok()?;
    let target = root.join(path).canonicalize().ok()?;
    if !target.starts_with(&root) || !target.is_file() {
        return None;
    }
    let bytes = fs::read(target).ok()?;
    Some(sha256_hex(&bytes))
}

fn native_audit_input_path_forbidden(root: &Path, role: &str, path: &str) -> bool {
    if role == "own_source" {
        ib_v5_conformance::process::is_forbidden_blind_source_path(path)
    } else {
        path_is_forbidden_before_read(root, path)
    }
}

fn allowed_input_digest(
    root: &Path,
    role: &str,
    path: &str,
    source_inventory: &[Value],
) -> Option<String> {
    if role == "own_source" {
        return source_inventory.iter().find_map(|row| {
            if row.get("path").and_then(Value::as_str) != Some(path) {
                return None;
            }
            row.get("sha256").and_then(Value::as_str).map(str::to_owned)
        });
    }
    actual_input_digest(root, path)
}

fn native_role_allowed(
    evaluator: &str,
    role: &str,
    path: &str,
    sha: &str,
    source_inventory: &[Value],
    freeze: &Map<String, Value>,
    receipt: &Map<String, Value>,
    root: &Path,
) -> bool {
    if role == "own_source" {
        return source_inventory.iter().any(|row| {
            row.get("path").and_then(Value::as_str) == Some(path)
                && row.get("sha256").and_then(Value::as_str) == Some(sha)
        });
    }
    let contract = freeze.get("normative_contract").and_then(Value::as_object);
    match role {
        "normative_spec" if evaluator == "full_spec_blind" => {
            let expected = [
                (
                    "SPEC-v5.md",
                    ib_v5_schemas::normative_contract::V5_BASE_SPEC_SHA256,
                ),
                (
                    "SPEC-v5.0-draft.5.md",
                    ib_v5_schemas::normative_contract::V5_DRAFT5_SPEC_SHA256,
                ),
                (
                    "SPEC-v5-ADDENDUM-01.md",
                    ib_v5_schemas::normative_contract::V5_APPROVED_ADDENDUM_SHA256,
                ),
                (
                    "SPEC-v5-DIRECTIVE-01.md",
                    ib_v5_schemas::normative_contract::V5_APPROVED_DIRECTIVE_SHA256,
                ),
            ];
            expected
                .iter()
                .any(|(approved_path, approved_sha)| *approved_path == path && *approved_sha == sha)
                && contract.is_some_and(|value| {
                    ["base_spec", "approved_addendum", "approved_directive"]
                        .iter()
                        .any(|key| {
                            value
                                .get(*key)
                                .and_then(Value::as_object)
                                .is_some_and(|reference| {
                                    reference.get("path").and_then(Value::as_str) == Some(path)
                                        && reference.get("sha256").and_then(Value::as_str)
                                            == Some(sha)
                                })
                        })
                        || path == "SPEC-v5.0-draft.5.md"
                })
        }
        "public_input" if evaluator == "full_spec_blind" => {
            reference_matches(freeze.get("cases"), path, sha)
                || freeze
                    .get("corpus")
                    .and_then(Value::as_object)
                    .and_then(|corpus| corpus.get("public_cases"))
                    .is_some_and(|reference| artifact_reference_matches(reference, path, sha))
        }
        "delivered_view" if evaluator == "agent_view_only" => {
            reference_matches(freeze.get("cases"), path, sha)
                || freeze
                    .get("corpus")
                    .and_then(Value::as_object)
                    .and_then(|corpus| corpus.get("public_cases"))
                    .is_some_and(|reference| artifact_reference_matches(reference, path, sha))
        }
        "public_schema" => {
            path == "crates/ib-v5-schemas/src/lib.rs"
                && actual_input_digest(root, path).as_deref() == Some(sha)
        }
        "blind_skill" => {
            path == ".agents/skills/blind-conformance/SKILL.md"
                && actual_input_digest(root, path).as_deref() == Some(sha)
        }
        // `other_public` and all unrecognized labels are intentionally never accepted.
        _ => {
            let _ = receipt;
            false
        }
    }
}

fn reference_matches(value: Option<&Value>, path: &str, sha: &str) -> bool {
    value.is_some_and(|reference| artifact_reference_matches(reference, path, sha))
        || value.and_then(Value::as_object).is_some_and(|artifacts| {
            artifacts
                .values()
                .any(|reference| artifact_reference_matches(reference, path, sha))
        })
}

fn artifact_reference_matches(value: &Value, path: &str, sha: &str) -> bool {
    value.as_object().is_some_and(|reference| {
        reference.get("path").and_then(Value::as_str) == Some(path)
            && reference.get("sha256").and_then(Value::as_str) == Some(sha)
    })
}

/// Validate a native receipt against the exact current native-public corpus and replay its
/// reference and production comparisons. Legacy native-prepared evidence remains archival
/// only; the Python receipt validator is unchanged.
pub(crate) fn validate_native_gspec_receipt(
    path: &Path,
    raw: &[u8],
    receipt: &Map<String, Value>,
    artifacts: &DatasetArtifactsV5,
    root: &Path,
    trusted_build_identity: Option<&Value>,
) -> Result<(Vec<String>, Vec<String>, Value), String> {
    use ib_v5_conformance::shared::{
        compare_native_production_result, compare_native_result_pair,
        validate_native_evaluator_result, verify_native_complete_coverage,
    };
    let mut errors = Vec::new();
    let mut gaps = Vec::new();
    let mut evidence = json!({
        "receipt_sha256": sha256_hex(raw),
        "native_proof_schema": NATIVE_SHARED_PROOF_SCHEMA,
        "tool_history_cryptographically_proven": false,
    });
    let (missing, extra) = exact_fields(receipt, &NATIVE_RECEIPT_FIELDS);
    if !missing.is_empty() {
        gaps.push(format!("native receipt omits fields {missing:?}"));
    }
    if !extra.is_empty() {
        errors.push(format!("native receipt has unexpected fields {extra:?}"));
    }
    if receipt.get("schema_version").and_then(Value::as_str) != Some(NATIVE_SHARED_PROOF_SCHEMA) {
        errors.push("native receipt schema is incorrect".to_owned());
    }
    if receipt.get("proof_scope").and_then(Value::as_str) != Some("G-SPEC-01") {
        gaps.push("diagnostic native receipt cannot establish full G-SPEC-01".to_owned());
    }
    if receipt.get("suite_id") != artifacts.seal.get("suite") {
        errors.push("native receipt suite_id does not match the requested dataset".to_owned());
    }
    if receipt.get("spec_sha256").and_then(Value::as_str)
        != Some(ib_v5_schemas::V5_BASE_SPEC_SHA256)
    {
        errors.push("native receipt SPEC-v5.md digest is stale or incorrect".to_owned());
    }

    let corpus_format = receipt
        .get("corpus")
        .and_then(Value::as_object)
        .and_then(|corpus| corpus.get("format"))
        .and_then(Value::as_str);
    if corpus_format != Some(crate::native_corpus::NATIVE_PUBLIC_CORPUS_FORMAT) {
        let (eligibility, reason) = match corpus_format {
            Some("identitybench.v5.native-prepared-gspec-corpus.v1") => (
                "archival_diagnostic_only",
                "the legacy fixed native-prepared corpus is archival/diagnostic-only",
            ),
            Some(_) => (
                "unsupported_corpus_format",
                "the native corpus format is unsupported for current G-SPEC-01",
            ),
            None => (
                "missing_native_public_corpus",
                "the receipt does not bind a native-public-corpus.v1",
            ),
        };
        evidence["native_corpus_eligibility"] = json!(eligibility);
        gaps.push(format!(
            "current native G-SPEC-01 requires a freshly authored native-public-corpus.v1; {reason}"
        ));
        return Ok((errors, gaps, evidence));
    }

    validate_active_native_dataset_bindings(receipt, root, artifacts, &mut errors, &mut gaps);
    let Some(loaded) = crate::native_corpus::load_native_public_corpus(
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
    let corpus = loaded.shared;
    let validated_native_datasets = Some(loaded.datasets_by_suite);
    evidence["native_production_context"] = json!("active_draft5_sealed_episode_v1");
    let freeze_ref = receipt.get("freeze");
    let Some((freeze_raw, freeze_path)) = crate::blind_receipt::shared_bound_bytes(
        root,
        freeze_ref,
        "native blind freeze",
        &mut errors,
        &mut gaps,
        false,
        true,
    ) else {
        return Ok((errors, gaps, evidence));
    };
    let Some(freeze) = crate::blind_receipt::shared_json_object(
        &freeze_raw,
        "native blind freeze",
        &mut errors,
        &mut gaps,
    ) else {
        return Ok((errors, gaps, evidence));
    };
    if freeze.get("schema_version").and_then(Value::as_str) != Some(NATIVE_FREEZE_SCHEMA)
        || freeze.get("proof_scope").and_then(Value::as_str) != Some("full_shared_conformance")
        || freeze.get("repository_root").and_then(Value::as_str) != Some(".")
    {
        gaps.push(
            "native freeze is diagnostic or uses an unsupported schema/root marker".to_owned(),
        );
    }
    verify_freeze_shape_and_source_index(&freeze, &mut errors);
    if freeze.get("frozen_before_comparison") != Some(&Value::Bool(true)) {
        errors.push("native references were not frozen before comparison".to_owned());
    }
    if freeze.get("normative_contract")
        != Some(&ib_v5_schemas::normative_contract::approved_v5_normative_contract())
    {
        errors.push("native freeze does not bind the approved normative contract".to_owned());
    }
    if freeze.get("dataset_bindings") != receipt.get("dataset_bindings")
        || freeze.get("corpus") != receipt.get("corpus")
    {
        errors.push("native freeze dataset/corpus references differ from the receipt".to_owned());
    }
    let expected_case_count = corpus.cases.len();
    if freeze.get("case_count").and_then(Value::as_i64) != i64::try_from(expected_case_count).ok()
        || freeze.get("case_manifest_sha256").and_then(Value::as_str)
            != Some(corpus.case_manifest_sha256.as_str())
        || freeze.get("case_index_sha256").and_then(Value::as_str)
            != Some(corpus.case_index_sha256.as_str())
    {
        errors.push("native freeze does not bind the exact validated case corpus".to_owned());
    }
    let Some((frozen_cases_raw, _)) = crate::blind_receipt::shared_bound_bytes(
        root,
        freeze.get("cases"),
        "native frozen case stream",
        &mut errors,
        &mut gaps,
        false,
        true,
    ) else {
        return Ok((errors, gaps, evidence));
    };
    let mut reconstructed_case_bytes = Vec::new();
    for case in &corpus.cases {
        reconstructed_case_bytes.extend_from_slice(
            &ib_v5_conformance::canonical_json_bytes(&case.public_case.to_json_value())
                .map_err(|error| error.to_string())?,
        );
        reconstructed_case_bytes.push(b'\n');
    }
    let corpus_cases_raw = read_bound_case_corpus(root, receipt, &mut errors, &mut gaps);
    let frozen_case_sha = freeze
        .get("cases")
        .and_then(Value::as_object)
        .and_then(|reference| reference.get("sha256"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if !native_frozen_case_stream_matches(
        &frozen_cases_raw,
        corpus_cases_raw.as_deref().unwrap_or_default(),
        &reconstructed_case_bytes,
        frozen_case_sha,
    ) {
        errors.push("frozen evaluator input bytes/order differ from the exact receipt.corpus.public_cases bytes".to_owned());
    }
    let Some(freeze_sources) = freeze
        .get("reference_provenance")
        .and_then(Value::as_object)
    else {
        gaps.push("native freeze omits independent evaluator provenance".to_owned());
        return Ok((errors, gaps, evidence));
    };
    let (provenance_missing, provenance_extra) =
        exact_fields(freeze_sources, &["full_spec_blind", "agent_view_only"]);
    if !provenance_missing.is_empty() || !provenance_extra.is_empty() {
        errors.push("native freeze reference provenance slots differ".to_owned());
    }
    let Some(native_build) = freeze.get("native_build") else {
        gaps.push("native freeze omits the compiled Rust source/binary identity".to_owned());
        return Ok((errors, gaps, evidence));
    };
    let current_executable = std::env::current_exe()
        .map_err(|error| format!("current identitybench executable is unavailable: {error}"))?;
    if trusted_build_identity.is_none() {
        gaps.push(
            "trusted identitybench build identity was not supplied by the native CLI".to_owned(),
        );
    } else if trusted_build_identity != Some(native_build) {
        errors.push("native freeze source/toolchain/binary identity differs from the active compiled identity".to_owned());
    }
    if let Err(error) = verify_native_build_identity(root, native_build, &current_executable) {
        errors.push(format!("native implementation binding failed: {error}"));
    }

    let Some(evaluators) = receipt.get("evaluators").and_then(Value::as_object) else {
        gaps.push("native receipt omits full-spec, agent-view, and production streams".to_owned());
        return Ok((errors, gaps, evidence));
    };
    let actual: BTreeMap<_, _> = evaluators.keys().map(|key| (key.as_str(), ())).collect();
    if actual
        .keys()
        .copied()
        .collect::<std::collections::BTreeSet<_>>()
        != NATIVE_EVALUATORS.into_iter().collect()
    {
        errors.push(
            "native receipt evaluator slots differ from the required three streams".to_owned(),
        );
        return Ok((errors, gaps, evidence));
    }

    let Some(first_entry) = evaluators.get("full_spec_blind").and_then(Value::as_object) else {
        return Ok((errors, gaps, evidence));
    };
    let Some(execution_ref) = first_entry.get("execution") else {
        gaps.push("native receipt does not bind its execution manifest".to_owned());
        return Ok((errors, gaps, evidence));
    };
    let Some((execution_raw, _execution_path)) = crate::blind_receipt::shared_bound_bytes(
        root,
        Some(execution_ref),
        "native bound execution manifest",
        &mut errors,
        &mut gaps,
        false,
        true,
    ) else {
        return Ok((errors, gaps, evidence));
    };
    let Some(execution) = crate::blind_receipt::shared_json_object(
        &execution_raw,
        "native bound execution manifest",
        &mut errors,
        &mut gaps,
    ) else {
        return Ok((errors, gaps, evidence));
    };
    if execution.get("schema_version").and_then(Value::as_str)
        != Some("identitybench.v5.native-conformance-execution.v1")
        || execution.get("freeze_sha256").and_then(Value::as_str)
            != Some(sha256_hex(&freeze_raw).as_str())
    {
        errors.push("native bound execution does not match the frozen input receipt".to_owned());
    }
    let Some((external_execution_raw, _)) = crate::blind_receipt::shared_bound_bytes(
        root,
        execution.get("external_execution"),
        "preproduction native execution manifest",
        &mut errors,
        &mut gaps,
        false,
        true,
    ) else {
        return Ok((errors, gaps, evidence));
    };
    if execution
        .get("external_execution_sha256")
        .and_then(Value::as_str)
        != Some(sha256_hex(&external_execution_raw).as_str())
    {
        errors.push("bound execution does not bind the preproduction execution bytes".to_owned());
    }
    let Some(external_execution) = crate::blind_receipt::shared_json_object(
        &external_execution_raw,
        "preproduction native execution manifest",
        &mut errors,
        &mut gaps,
    ) else {
        return Ok((errors, gaps, evidence));
    };
    if external_execution
        .get("schema_version")
        .and_then(Value::as_str)
        != Some("identitybench.v5.native-conformance-execution.v1")
        || external_execution.get("freeze_sha256") != execution.get("freeze_sha256")
        || external_execution.get("timeout_ms") != execution.get("timeout_ms")
    {
        errors.push(
            "preproduction execution does not bind the same freeze/runtime limits".to_owned(),
        );
    }
    let Some(execution_evaluators) = execution.get("evaluators").and_then(Value::as_object) else {
        gaps.push("native bound execution omits evaluator streams".to_owned());
        return Ok((errors, gaps, evidence));
    };
    if execution_evaluators
        .keys()
        .map(String::as_str)
        .collect::<std::collections::BTreeSet<_>>()
        != NATIVE_EVALUATORS.into_iter().collect()
    {
        errors.push(
            "native bound execution evaluator slots differ from the required streams".to_owned(),
        );
    }
    let Some(external_evaluators) = external_execution
        .get("evaluators")
        .and_then(Value::as_object)
    else {
        gaps.push("preproduction execution omits evaluator streams".to_owned());
        return Ok((errors, gaps, evidence));
    };
    if external_evaluators
        .keys()
        .map(String::as_str)
        .collect::<std::collections::BTreeSet<_>>()
        != ["full_spec_blind", "agent_view_only"].into_iter().collect()
    {
        errors
            .push("preproduction execution contains missing or extra evaluator streams".to_owned());
    }
    for slot in ["full_spec_blind", "agent_view_only"] {
        if external_evaluators.get(slot) != execution_evaluators.get(slot) {
            errors.push(format!(
                "bound execution does not preserve the exact preproduction {slot} row"
            ));
        }
    }

    let mut streams = BTreeMap::<String, Vec<Value>>::new();
    let mut closure_hashes = BTreeMap::<String, String>::new();
    let mut authors = BTreeMap::<String, (String, String)>::new();
    for evaluator in ["full_spec_blind", "agent_view_only", "production"] {
        let Some(entry) = evaluators.get(evaluator).and_then(Value::as_object) else {
            errors.push(format!(
                "native evaluator descriptor is malformed: {evaluator}"
            ));
            continue;
        };
        let mut required = vec![
            "qualification",
            "wire_evaluator_id",
            "source_profile",
            "source_closure_sha256",
            "runtime_manifest",
            "executable",
            "raw_receipts",
            "execution",
        ];
        if evaluator != "production" {
            required.extend([
                "author_origin",
                "task_id",
                "source_capture_manifest",
                "read_audit",
            ]);
        }
        let (missing, extra) = exact_fields(entry, &required);
        if !missing.is_empty() || !extra.is_empty() {
            errors.push(format!("native evaluator {evaluator} fields differ"));
            continue;
        }
        let expected_wire = evaluator;
        if entry.get("wire_evaluator_id").and_then(Value::as_str) != Some(expected_wire) {
            errors.push(format!("native evaluator {evaluator} wire ID differs"));
        }
        if entry.get("execution") != Some(execution_ref) {
            errors.push(format!(
                "native evaluator {evaluator} binds a different execution manifest"
            ));
        }
        let Some(execution_row) = execution_evaluators
            .get(evaluator)
            .and_then(Value::as_object)
        else {
            errors.push(format!("native execution row is unavailable: {evaluator}"));
            continue;
        };
        if execution_row.get("exit_code").and_then(Value::as_i64) != Some(0)
            || execution_row.get("stdout") != entry.get("raw_receipts")
            || execution_row.get("runtime_manifest") != entry.get("runtime_manifest")
        {
            errors.push(format!(
                "native execution does not bind the {evaluator} result/runtime artifacts"
            ));
        }
        let Some(source_profile) = entry.get("source_profile").and_then(Value::as_str) else {
            errors.push(format!(
                "native evaluator {evaluator} source profile is malformed"
            ));
            continue;
        };
        let Some(closure_sha) = entry.get("source_closure_sha256").and_then(Value::as_str) else {
            errors.push(format!(
                "native evaluator {evaluator} source closure is malformed"
            ));
            continue;
        };
        if source_profile.is_empty() || !is_sha256(closure_sha) {
            errors.push(format!(
                "native evaluator {evaluator} source identity is malformed"
            ));
            continue;
        }
        let internal_slot = match evaluator {
            "full_spec_blind" => "full_spec",
            "agent_view_only" => "agent_view",
            "production" => "production",
            _ => unreachable!(),
        };
        let executable_ref = entry.get("executable");
        let freeze_slot = if evaluator == "full_spec_blind" {
            "full_spec_blind"
        } else if evaluator == "agent_view_only" {
            "agent_view_only"
        } else {
            "production"
        };
        let Some(frozen_executable) = freeze
            .get("executables")
            .and_then(Value::as_object)
            .and_then(|items| items.get(freeze_slot))
        else {
            errors.push(format!(
                "native freeze omits executable binding for {evaluator}"
            ));
            continue;
        };
        if executable_ref != Some(frozen_executable)
            || execution_row.get("executable_sha256") != frozen_executable.get("sha256")
            || execution_row.get("executable_sha256_before") != frozen_executable.get("sha256")
            || execution_row.get("executable_sha256_after") != frozen_executable.get("sha256")
        {
            errors.push(format!(
                "native execution executable differs from its precomparison freeze: {evaluator}"
            ));
        }
        if evaluator == "production" {
            if entry.get("qualification").and_then(Value::as_str) != Some("native_production")
                || native_build["source_identity_sha256"].as_str() != Some(closure_sha)
            {
                errors.push(
                    "native production stream is not bound to the compiled Rust source".to_owned(),
                );
            }
        } else {
            let (Some(author_origin), Some(task_id)) = (
                entry.get("author_origin").and_then(Value::as_str),
                entry.get("task_id").and_then(Value::as_str),
            ) else {
                errors.push(format!(
                    "native reference identity is malformed: {evaluator}"
                ));
                continue;
            };
            if entry.get("qualification").and_then(Value::as_str) != Some("independent_reference")
                || author_origin.is_empty()
                || task_id.is_empty()
            {
                errors.push(format!(
                    "native reference is not independently qualified: {evaluator}"
                ));
            }
            authors.insert(
                evaluator.to_owned(),
                (author_origin.to_owned(), task_id.to_owned()),
            );
            let Some(frozen_provenance) = freeze_sources.get(evaluator).and_then(Value::as_object)
            else {
                errors.push(format!(
                    "native freeze omits reference provenance: {evaluator}"
                ));
                continue;
            };
            for field in [
                "qualification",
                "source_profile",
                "author_origin",
                "task_id",
                "source_closure_sha256",
                "source_capture_manifest",
                "read_audit",
            ] {
                if entry.get(field) != frozen_provenance.get(field) {
                    errors.push(format!(
                        "native {evaluator} {field} differs from the precomparison freeze"
                    ));
                }
            }
            validate_native_reference_source(
                entry,
                evaluator,
                root,
                &freeze,
                receipt,
                &mut errors,
                &mut gaps,
            );
        }
        closure_hashes.insert(internal_slot.to_owned(), closure_sha.to_owned());
        validate_native_runtime_artifact(
            entry.get("runtime_manifest"),
            evaluator,
            root,
            entry,
            execution_row,
            native_build,
            execution.get("timeout_ms").and_then(Value::as_u64),
            &mut errors,
            &mut gaps,
            corpus.cases.len(),
            &corpus.case_manifest_sha256,
        );
        validate_native_executable_artifact(
            entry.get("executable"),
            evaluator,
            root,
            &mut errors,
            &mut gaps,
            native_build,
        );
        let Some((stdout, _stdout_path)) = crate::blind_receipt::shared_bound_bytes(
            root,
            entry.get("raw_receipts"),
            &format!("{evaluator} raw results"),
            &mut errors,
            &mut gaps,
            false,
            true,
        ) else {
            continue;
        };
        let count = i64::try_from(corpus.cases.len()).unwrap_or(i64::MAX);
        let Some(rows) = crate::blind_receipt::shared_jsonl_rows(
            &stdout,
            &format!("{evaluator} raw results"),
            count,
            &mut errors,
            &mut gaps,
        ) else {
            continue;
        };
        streams.insert(evaluator.to_owned(), rows);
    }
    if let (Some(full), Some(agent)) = (
        authors.get("full_spec_blind"),
        authors.get("agent_view_only"),
    ) && (full.0 == agent.0 || full.1 == agent.1)
    {
        errors.push("native blind references share an author origin or task identity".to_owned());
    }
    if let Some(map) = evidence.as_object_mut() {
        map.insert(
            "native_freeze_sha256".to_owned(),
            json!(sha256_hex(&freeze_raw)),
        );
        map.insert("case_count".to_owned(), json!(corpus.cases.len()));
        map.insert(
            "case_manifest_sha256".to_owned(),
            json!(corpus.case_manifest_sha256),
        );
        map.insert(
            "case_index_sha256".to_owned(),
            json!(corpus.case_index_sha256),
        );
    }
    if streams.len() != 3 || closure_hashes.len() != 3 {
        return Ok((errors, gaps, evidence));
    }

    let mut paired = Vec::with_capacity(corpus.cases.len());
    let mut matched_production = 0usize;
    let full_rows = &streams["full_spec_blind"];
    let agent_rows = &streams["agent_view_only"];
    let production_rows = &streams["production"];
    let mut pair_closures = BTreeMap::new();
    pair_closures.insert("full_spec".to_owned(), closure_hashes["full_spec"].clone());
    pair_closures.insert(
        "agent_view".to_owned(),
        closure_hashes["agent_view"].clone(),
    );
    for (index, case) in corpus.cases.iter().enumerate() {
        let full = &full_rows[index];
        let agent = &agent_rows[index];
        let production_recorded = &production_rows[index];
        let (full_result, agent_result, pair) =
            match compare_native_result_pair(case, full, agent, &pair_closures) {
                Ok(value) => value,
                Err(error) => {
                    errors.push(format!(
                        "{}: native reference result failed protocol validation: {error}",
                        case.case_id()
                    ));
                    continue;
                }
            };
        if pair.status != "MATCH" {
            if pair.status == "INCOMPLETE" {
                gaps.push(format!(
                    "{}: native blind references are underdefined",
                    case.case_id()
                ));
            } else {
                errors.push(format!(
                    "{}: native blind references disagree",
                    case.case_id()
                ));
            }
            continue;
        }
        let native_episode = validated_native_datasets
            .as_ref()
            .and_then(|datasets| datasets.get(&case.public_case.suite_id))
            .and_then(|dataset| {
                dataset
                    .episodes
                    .iter()
                    .find(|episode| episode.episode_id == case.public_case.episode_id())
            });
        let Some(native_episode) = native_episode else {
            errors.push(format!(
                "{}: native production evaluation has no matching active sealed episode",
                case.case_id()
            ));
            continue;
        };
        let production_expected =
            match ib_v5_conformance::production::evaluate_shared_input_with_native_context(
                &case.public_case.to_json_value(),
                native_episode,
            ) {
                Ok(value) => value,
                Err(error) => {
                    errors.push(format!(
                        "{}: native production evaluation failed: {error}",
                        case.case_id()
                    ));
                    continue;
                }
            };
        let production_recorded_bytes =
            ib_v5_conformance::canonical_json_bytes(production_recorded)
                .map_err(|error| error.to_string())?;
        let production_expected_bytes =
            ib_v5_conformance::canonical_json_bytes(&production_expected)
                .map_err(|error| error.to_string())?;
        if production_recorded_bytes != production_expected_bytes {
            errors.push(format!(
                "{}: recorded production stream differs from current native evaluator",
                case.case_id()
            ));
            continue;
        }
        let mut all_closures = pair_closures.clone();
        all_closures.insert(
            "production".to_owned(),
            closure_hashes["production"].clone(),
        );
        let (_, production_pair) = match compare_native_production_result(
            case,
            (&full_result, &agent_result),
            &production_expected,
            &all_closures,
        ) {
            Ok(value) => value,
            Err(error) => {
                errors.push(format!(
                    "{}: native production comparison failed: {error}",
                    case.case_id()
                ));
                continue;
            }
        };
        if production_pair.status != "MATCH" {
            if production_pair.status == "INCOMPLETE" {
                gaps.push(format!(
                    "{}: native production result is underdefined",
                    case.case_id()
                ));
            } else {
                errors.push(format!(
                    "{}: native production differs from the independent references",
                    case.case_id()
                ));
            }
            continue;
        }
        let full_checked = validate_native_evaluator_result(full, case, "full_spec")
            .map_err(|error| error.to_string())?;
        let agent_checked = validate_native_evaluator_result(agent, case, "agent_view")
            .map_err(|error| error.to_string())?;
        if full_checked.status != "ok" || agent_checked.status != "ok" {
            gaps.push(format!(
                "{}: underdefined result cannot satisfy G-SPEC-01",
                case.case_id()
            ));
            continue;
        }
        if let Some(datasets) = validated_native_datasets.as_ref()
            && case.public_case.operation == "derive_phase"
            && let Err(error) = compare_native_phase_mutations_to_gold(
                case,
                &full_checked,
                &agent_checked,
                datasets,
            )
        {
            errors.push(format!("{}: {error}", case.case_id()));
            continue;
        }
        matched_production += 1;
        paired.push((full.clone(), agent.clone()));
    }
    if !errors.is_empty() || !gaps.is_empty() {
        if let Some(map) = evidence.as_object_mut() {
            map.insert("matched_case_count".to_owned(), json!(paired.len()));
            map.insert("production_matches".to_owned(), json!(matched_production));
        }
        return Ok((errors, gaps, evidence));
    }
    let coverage = verify_native_complete_coverage(
        &corpus.cases,
        &paired,
        &corpus.coverage,
        &corpus.coverage,
        &corpus.family_map,
    );
    match coverage {
        Ok(observed) => {
            if let Some(map) = evidence.as_object_mut() {
                map.insert("matched_case_count".to_owned(), json!(paired.len()));
                map.insert("production_matches".to_owned(), json!(matched_production));
                map.insert("observed_coverage".to_owned(), observed);
            }
        }
        Err(error) => gaps.push(format!("native shared coverage is incomplete: {error}")),
    }
    let _ = (path, freeze_path);
    Ok((errors, gaps, evidence))
}

fn compare_native_phase_mutations_to_gold(
    case: &ib_v5_conformance::shared::FrozenCase,
    full: &ib_v5_conformance::shared::EvaluatorResult,
    agent: &ib_v5_conformance::shared::EvaluatorResult,
    datasets: &BTreeMap<String, DatasetArtifactsV5>,
) -> Result<(), String> {
    let public = &case.public_case;
    let dataset = datasets
        .get(&public.suite_id)
        .ok_or_else(|| "phase result has no matching active sealed suite".to_owned())?;
    let oracle = dataset
        .oracles
        .iter()
        .find(|oracle| oracle.episode_id == public.episode_id())
        .ok_or_else(|| "phase result has no matching sealed oracle".to_owned())?;
    let expected = match public.phase_index {
        1 => &oracle.gold_p1_mutations,
        2 => &oracle.gold_p2_mutations,
        _ => return Err("phase result has an unsupported phase index".to_owned()),
    };
    let expected = serde_json::to_value(expected)
        .map_err(|error| format!("sealed gold mutations could not be serialized: {error}"))?;
    let expected_bytes = normalized_sealed_gold_mutation_bytes(&expected)?;
    for (slot, result) in [("full_spec_blind", full), ("agent_view_only", agent)] {
        let actual = result
            .payload
            .get("mutations")
            .ok_or_else(|| format!("{slot} derive_phase payload omits mutations"))?;
        let actual_bytes =
            ib_v5_conformance::canonical_json_bytes(actual).map_err(|error| error.to_string())?;
        if actual_bytes != expected_bytes {
            return Err(format!(
                "{slot} derive_phase mutation payload differs from the exact sealed phase-{} gold payload",
                public.phase_index
            ));
        }
    }
    Ok(())
}

fn normalized_sealed_gold_mutation_bytes(value: &Value) -> Result<Vec<u8>, String> {
    let Some(rows) = value.as_array() else {
        return Err("sealed gold mutations are not an array".to_owned());
    };
    let mut keyed = Vec::with_capacity(rows.len());
    for row in rows {
        let Some(mutation_kind) = row.get("mutation_kind").and_then(Value::as_str) else {
            return Err("sealed gold mutation has no string mutation_kind".to_owned());
        };
        let Some(target_entity_id) = row.get("target_entity_id").and_then(Value::as_str) else {
            return Err("sealed gold mutation has no string target_entity_id".to_owned());
        };
        keyed.push((
            mutation_kind.to_owned(),
            target_entity_id.to_owned(),
            row.clone(),
        ));
    }
    keyed.sort_by(|left, right| (&left.0, &left.1).cmp(&(&right.0, &right.1)));
    if keyed
        .windows(2)
        .any(|pair| pair[0].0 == pair[1].0 && pair[0].1 == pair[1].1)
    {
        return Err("sealed gold mutations contain duplicate mutation targets".to_owned());
    }
    let normalized = Value::Array(keyed.into_iter().map(|(_, _, row)| row).collect());
    ib_v5_conformance::canonical_json_bytes(&normalized).map_err(|error| error.to_string())
}

pub use crate::native_corpus::prepare_native_public_corpus;
pub use crate::native_prepare::prepare_native_gspec_corpus;

/// Verify a bound native receipt using the active compiled identity. Diagnostic receipts
/// are structurally verified and remain explicitly outside G-SPEC-01.
pub fn validate_native_conformance_receipt(
    path: &Path,
    repository_root: &Path,
    trusted_build_identity: &Value,
) -> Result<Value, V5ArtifactError> {
    let root = repository_root.canonicalize().map_err(|error| {
        V5ArtifactError::new(format!("repository root is unavailable: {error}"))
    })?;
    let receipt_path = resolve_public_receipt_path(&root, path)?;
    let metadata = fs::metadata(&receipt_path)
        .map_err(|error| V5ArtifactError::new(format!("native receipt is unavailable: {error}")))?;
    if !metadata.is_file() || metadata.len() > 64 * 1024 * 1024 {
        return Err(V5ArtifactError::new(
            "native receipt must be a bounded regular file",
        ));
    }
    let raw = fs::read(&receipt_path)
        .map_err(|error| V5ArtifactError::new(format!("native receipt is unavailable: {error}")))?;
    let receipt_value = ib_v5_conformance::strict_json_loads(&raw)
        .map_err(|error| V5ArtifactError::new(format!("native receipt is malformed: {error}")))?;
    let receipt = receipt_value
        .as_object()
        .ok_or_else(|| V5ArtifactError::new("native receipt must be a JSON object"))?;
    let (missing, extra) = exact_fields(receipt, &NATIVE_RECEIPT_FIELDS);
    if !missing.is_empty() || !extra.is_empty() {
        return Err(V5ArtifactError::new(format!(
            "native receipt fields differ: missing={missing:?}; extra={extra:?}"
        )));
    }
    if receipt.get("schema_version").and_then(Value::as_str) != Some(NATIVE_SHARED_PROOF_SCHEMA) {
        return Err(V5ArtifactError::new(
            "unsupported native conformance receipt schema",
        ));
    }
    let mut errors = Vec::new();
    let mut gaps = Vec::new();
    let (freeze_raw, _) = crate::blind_receipt::shared_bound_bytes(
        &root,
        receipt.get("freeze"),
        "native freeze",
        &mut errors,
        &mut gaps,
        false,
        true,
    )
    .ok_or_else(|| artifact_errors("native freeze is unavailable", errors, gaps))?;
    let freeze_value = ib_v5_conformance::strict_json_loads(&freeze_raw)
        .map_err(|error| V5ArtifactError::new(format!("native freeze is malformed: {error}")))?;
    let freeze = freeze_value
        .as_object()
        .ok_or_else(|| V5ArtifactError::new("native freeze must be a JSON object"))?;
    if freeze.get("schema_version").and_then(Value::as_str) != Some(NATIVE_FREEZE_SCHEMA)
        || freeze.get("frozen_before_comparison") != Some(&Value::Bool(true))
        || freeze.get("repository_root").and_then(Value::as_str) != Some(".")
    {
        return Err(V5ArtifactError::new(
            "native freeze schema, root, or precomparison marker is invalid",
        ));
    }
    let native_build = freeze
        .get("native_build")
        .ok_or_else(|| V5ArtifactError::new("native freeze omits the active build identity"))?;
    if native_build != trusted_build_identity {
        return Err(V5ArtifactError::new(
            "native freeze differs from the active compiled identity",
        ));
    }
    let mut identity_errors = Vec::new();
    verify_freeze_shape_and_source_index(freeze, &mut identity_errors);
    if !identity_errors.is_empty() {
        return Err(artifact_errors(
            "native freeze source inventory is invalid",
            identity_errors,
            Vec::new(),
        ));
    }
    let current_executable = std::env::current_exe().map_err(|error| {
        V5ArtifactError::new(format!("active executable is unavailable: {error}"))
    })?;
    verify_native_build_identity(&root, native_build, &current_executable)?;
    match receipt.get("proof_scope").and_then(Value::as_str) {
        Some("diagnostic_native_fixture") => {
            if freeze.get("proof_scope").and_then(Value::as_str)
                != Some("diagnostic_native_fixture")
                || receipt.get("corpus") != Some(&Value::Null)
                || receipt.get("dataset_bindings") != Some(&Value::Null)
            {
                return Err(V5ArtifactError::new(
                    "diagnostic receipt scope carries or claims full corpus evidence",
                ));
            }
            verify_native_diagnostic_receipt(&root, &raw, receipt, freeze, &freeze_raw)
        }
        Some("G-SPEC-01") => {
            if freeze.get("proof_scope").and_then(Value::as_str) != Some("full_shared_conformance")
            {
                return Err(V5ArtifactError::new(
                    "G-SPEC-01 receipt does not bind full shared conformance",
                ));
            }
            let suite_id = receipt
                .get("suite_id")
                .and_then(Value::as_str)
                .ok_or_else(|| V5ArtifactError::new("native receipt suite_id is malformed"))?;
            let binding = receipt
                .get("dataset_bindings")
                .and_then(Value::as_array)
                .and_then(|rows| {
                    rows.iter()
                        .find(|row| row.get("suite_id").and_then(Value::as_str) == Some(suite_id))
                })
                .ok_or_else(|| {
                    V5ArtifactError::new("native receipt does not bind its selected suite")
                })?;
            let episodes_path = binding["episodes"]["path"].as_str().ok_or_else(|| {
                V5ArtifactError::new("selected suite episode binding is malformed")
            })?;
            if !["v5_dev", "v5_lite", "v5_core", "v5_full"].contains(&suite_id)
                || episodes_path != format!("datasets/v5_draft5/{suite_id}/episodes.jsonl")
            {
                return Err(V5ArtifactError::new(
                    "selected suite binding is outside active Draft 5 dataset paths",
                ));
            }
            let selected_dir = root.join("datasets/v5_draft5").join(suite_id);
            let artifacts = load_v5_dataset(&selected_dir).map_err(|error| {
                V5ArtifactError::new(format!("selected bound dataset is invalid: {error}"))
            })?;
            let (errors, gaps, evidence) = validate_native_gspec_receipt(
                &receipt_path,
                &raw,
                receipt,
                &artifacts,
                &root,
                Some(trusted_build_identity),
            )
            .map_err(|error| {
                V5ArtifactError::new(format!("native G-SPEC replay failed closed: {error}"))
            })?;
            if !errors.is_empty() || !gaps.is_empty() {
                return Err(artifact_errors(
                    "native G-SPEC validation failed",
                    errors,
                    gaps,
                ));
            }
            Ok(json!({
                "receipt_sha256": sha256_hex(&raw),
                "proof_scope": "G-SPEC-01",
                "diagnostic_verified": false,
                "g_spec_claim": "verified_by_replay",
                "evidence": evidence,
            }))
        }
        _ => Err(V5ArtifactError::new(
            "native receipt proof_scope is unsupported",
        )),
    }
}

fn read_bound_case_corpus(
    root: &Path,
    receipt: &Map<String, Value>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) -> Option<Vec<u8>> {
    let reference = receipt
        .get("corpus")
        .and_then(Value::as_object)
        .and_then(|map| map.get("public_cases"));
    crate::blind_receipt::shared_bound_bytes(
        root,
        reference,
        "receipt public case corpus",
        errors,
        gaps,
        false,
        true,
    )
    .map(|(bytes, _)| bytes)
}

fn native_frozen_case_stream_matches(
    frozen_bytes: &[u8],
    corpus_bytes: &[u8],
    reconstructed_bytes: &[u8],
    frozen_sha256: &str,
) -> bool {
    frozen_bytes == corpus_bytes
        && frozen_bytes == reconstructed_bytes
        && is_sha256(frozen_sha256)
        && sha256_hex(frozen_bytes) == frozen_sha256
}

pub(crate) fn resolve_public_receipt_path(
    root: &Path,
    path: &Path,
) -> Result<PathBuf, V5ArtifactError> {
    let lexical = if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    };
    let relative = lexical
        .strip_prefix(root)
        .map_err(|_| V5ArtifactError::new("native receipt path is outside repository root"))?;
    if !safe_relative_path(&relative.to_string_lossy().replace('\\', "/"))
        || !native_receipt_artifact_path_allowed(relative)
        || is_sensitive_artifact_path(relative)
    {
        return Err(V5ArtifactError::new(
            "native receipt path is outside approved public conformance roots or is sensitive",
        ));
    }
    let mut current = root.to_path_buf();
    for part in relative.components() {
        let Component::Normal(part) = part else {
            return Err(V5ArtifactError::new("native receipt path is unsafe"));
        };
        current.push(part);
        let metadata = fs::symlink_metadata(&current).map_err(|error| {
            V5ArtifactError::new(format!("native receipt is unavailable: {error}"))
        })?;
        if metadata.file_type().is_symlink() {
            return Err(V5ArtifactError::new(
                "native receipt path contains a symlink component",
            ));
        }
    }
    let resolved = current
        .canonicalize()
        .map_err(|error| V5ArtifactError::new(format!("native receipt is unavailable: {error}")))?;
    let resolved_relative = resolved
        .strip_prefix(root)
        .map_err(|_| V5ArtifactError::new("native receipt resolves outside repository root"))?;
    if !resolved.is_file()
        || !native_receipt_artifact_path_allowed(resolved_relative)
        || is_sensitive_artifact_path(resolved_relative)
    {
        return Err(V5ArtifactError::new(
            "native receipt resolves outside approved public conformance roots or is sensitive",
        ));
    }
    Ok(resolved)
}

fn native_receipt_artifact_path_allowed(path: &Path) -> bool {
    let parts = path
        .components()
        .filter_map(|part| match part {
            Component::Normal(value) => value.to_str(),
            _ => None,
        })
        .collect::<Vec<_>>();
    parts.len() >= 3 && parts[..3] == ["reports", "v5", "conformance"]
}

fn is_sensitive_artifact_path(path: &Path) -> bool {
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

fn artifact_errors(label: &str, errors: Vec<String>, gaps: Vec<String>) -> V5ArtifactError {
    V5ArtifactError::new(format!("{label}: errors={errors:?}; gaps={gaps:?}"))
}

fn verify_freeze_shape_and_source_index(freeze: &Map<String, Value>, errors: &mut Vec<String>) {
    let expected_fields = [
        "schema_version",
        "proof_scope",
        "protocol",
        "repository_root",
        "frozen_before_comparison",
        "normative_contract",
        "case_count",
        "case_manifest_sha256",
        "case_index_sha256",
        "cases",
        "executables",
        "native_build",
        "dataset_bindings",
        "corpus",
        "reference_provenance",
        "runtime_limits",
        "source_inventory",
    ];
    let (missing, extra) = exact_fields(freeze, &expected_fields);
    if !missing.is_empty() || !extra.is_empty() {
        errors.push(format!(
            "native freeze fields differ: missing={missing:?}; extra={extra:?}"
        ));
    }
    let Some(source_map) = freeze
        .get("native_build")
        .and_then(Value::as_object)
        .and_then(|build| build.get("source_identity"))
        .and_then(Value::as_object)
    else {
        errors.push("native freeze omits embedded source identity inventory".to_owned());
        return;
    };
    let expected_index: Vec<Value> = source_map
        .iter()
        .map(|(path, digest)| json!({"path":path,"sha256":digest}))
        .collect();
    if freeze.get("source_inventory") != Some(&Value::Array(expected_index)) {
        errors
            .push("native freeze source inventory differs from the embedded build map".to_owned());
    }
    let expected_limits = json!({
        "timeout_ms_default": 600_000,
        "timeout_ms_max": 3_600_000,
        "stdin_bytes_max": 512 * 1024 * 1024,
        "stdout_bytes_max": 512 * 1024 * 1024,
        "stderr_bytes_max": 4 * 1024 * 1024,
    });
    if freeze.get("runtime_limits") != Some(&expected_limits) {
        errors.push(
            "native freeze runtime limits differ from the bounded process contract".to_owned(),
        );
    }
}

fn read_bound_artifact(
    root: &Path,
    reference: Option<&Value>,
    label: &str,
) -> Result<Vec<u8>, V5ArtifactError> {
    let mut errors = Vec::new();
    let mut gaps = Vec::new();
    crate::blind_receipt::shared_bound_bytes(
        root,
        reference,
        label,
        &mut errors,
        &mut gaps,
        false,
        true,
    )
    .map(|(bytes, _)| bytes)
    .ok_or_else(|| artifact_errors(&format!("{label} could not be verified"), errors, gaps))
}

fn verify_native_diagnostic_receipt(
    root: &Path,
    receipt_raw: &[u8],
    receipt: &Map<String, Value>,
    freeze: &Map<String, Value>,
    freeze_raw: &[u8],
) -> Result<Value, V5ArtifactError> {
    use ib_v5_conformance::shared::{
        compare_production_result, compare_result_pair, freeze_case, validate_evaluator_result,
    };
    let native_build = freeze
        .get("native_build")
        .ok_or_else(|| V5ArtifactError::new("diagnostic freeze omits native build identity"))?;

    if receipt.get("spec_sha256").and_then(Value::as_str)
        != Some(ib_v5_schemas::V5_BASE_SPEC_SHA256)
        || receipt.get("suite_id").and_then(Value::as_str).is_none()
    {
        return Err(V5ArtifactError::new(
            "diagnostic suite or specification identity is malformed",
        ));
    }
    let cases_raw = read_bound_artifact(root, freeze.get("cases"), "native frozen cases")?;
    let public_cases = ib_v5_conformance::strict_jsonl_loads(&cases_raw).map_err(|error| {
        V5ArtifactError::new(format!("native frozen cases are malformed: {error}"))
    })?;
    let inventory = ib_v5_conformance::process::freeze_cases(&public_cases).map_err(|error| {
        V5ArtifactError::new(format!("native frozen cases are invalid: {error}"))
    })?;
    let mut canonical_cases = Vec::new();
    for case_id in &inventory.ordered_ids {
        canonical_cases.extend_from_slice(&inventory.cases_by_id[case_id]);
        canonical_cases.push(b'\n');
    }
    if cases_raw != canonical_cases
        || public_cases.len()
            != freeze
                .get("case_count")
                .and_then(Value::as_u64)
                .unwrap_or(0) as usize
        || inventory.case_manifest_sha256
            != freeze
                .get("case_manifest_sha256")
                .and_then(Value::as_str)
                .unwrap_or("")
        || sha256_hex(&cases_raw) != freeze["cases"]["sha256"].as_str().unwrap_or("")
    {
        return Err(V5ArtifactError::new(
            "diagnostic frozen case bytes differ from the bound manifest",
        ));
    }
    let index_rows = inventory
        .ordered_ids
        .iter()
        .map(|case_id| json!({"case_id": case_id, "input_sha256": inventory.hashes[case_id]}))
        .collect::<Vec<_>>();
    let index_sha = sha256_hex(
        &ib_v5_conformance::canonical_json_bytes(&Value::Array(index_rows))
            .map_err(|error| V5ArtifactError::new(error.to_string()))?,
    );
    if freeze.get("case_index_sha256").and_then(Value::as_str) != Some(index_sha.as_str()) {
        return Err(V5ArtifactError::new(
            "diagnostic frozen case index digest differs",
        ));
    }

    let evaluators = receipt
        .get("evaluators")
        .and_then(Value::as_object)
        .ok_or_else(|| V5ArtifactError::new("diagnostic receipt omits evaluator streams"))?;
    let (missing, extra) = exact_fields(evaluators, &NATIVE_EVALUATORS);
    if !missing.is_empty() || !extra.is_empty() {
        return Err(V5ArtifactError::new(format!(
            "diagnostic evaluator slots differ: missing={missing:?}; extra={extra:?}"
        )));
    }
    let first = evaluators["full_spec_blind"]
        .as_object()
        .ok_or_else(|| V5ArtifactError::new("diagnostic full-spec descriptor is malformed"))?;
    let execution_ref = first
        .get("execution")
        .ok_or_else(|| V5ArtifactError::new("diagnostic receipt omits execution binding"))?;
    let execution_raw = read_bound_artifact(root, Some(execution_ref), "native bound execution")?;
    let execution_value =
        ib_v5_conformance::strict_json_loads(&execution_raw).map_err(|error| {
            V5ArtifactError::new(format!("native bound execution is malformed: {error}"))
        })?;
    let execution = execution_value
        .as_object()
        .ok_or_else(|| V5ArtifactError::new("native bound execution must be a JSON object"))?;
    if execution.get("schema_version").and_then(Value::as_str)
        != Some("identitybench.v5.native-conformance-execution.v1")
        || execution.get("freeze_sha256").and_then(Value::as_str)
            != Some(sha256_hex(freeze_raw).as_str())
    {
        return Err(V5ArtifactError::new(
            "native execution manifest does not bind the frozen inputs",
        ));
    }
    let external_raw = read_bound_artifact(
        root,
        execution.get("external_execution"),
        "preproduction execution",
    )?;
    if execution
        .get("external_execution_sha256")
        .and_then(Value::as_str)
        != Some(sha256_hex(&external_raw).as_str())
    {
        return Err(V5ArtifactError::new(
            "preproduction execution manifest digest differs",
        ));
    }
    let external_value = ib_v5_conformance::strict_json_loads(&external_raw).map_err(|error| {
        V5ArtifactError::new(format!("preproduction execution is malformed: {error}"))
    })?;
    let external = external_value
        .as_object()
        .ok_or_else(|| V5ArtifactError::new("preproduction execution must be a JSON object"))?;
    for field in ["schema_version", "freeze_sha256", "timeout_ms"] {
        if execution.get(field) != external.get(field) {
            return Err(V5ArtifactError::new(format!(
                "preproduction execution {field} differs from bound evidence"
            )));
        }
    }
    let execution_rows = execution
        .get("evaluators")
        .and_then(Value::as_object)
        .ok_or_else(|| V5ArtifactError::new("bound execution omits evaluator rows"))?;
    let external_rows = external
        .get("evaluators")
        .and_then(Value::as_object)
        .ok_or_else(|| V5ArtifactError::new("preproduction execution omits evaluator rows"))?;
    let (missing, extra) = exact_fields(execution_rows, &NATIVE_EVALUATORS);
    if !missing.is_empty() || !extra.is_empty() {
        return Err(V5ArtifactError::new(
            "bound execution evaluator slots differ",
        ));
    }
    let (missing, extra) = exact_fields(external_rows, &["full_spec_blind", "agent_view_only"]);
    if !missing.is_empty() || !extra.is_empty() {
        return Err(V5ArtifactError::new(
            "preproduction execution evaluator slots differ",
        ));
    }
    for slot in ["full_spec_blind", "agent_view_only"] {
        if execution_rows.get(slot) != external_rows.get(slot) {
            return Err(V5ArtifactError::new(format!(
                "bound execution changed preproduction {slot} row"
            )));
        }
    }

    let mut streams = BTreeMap::<String, Vec<Value>>::new();
    let mut closures = BTreeMap::<String, String>::new();
    for slot in NATIVE_EVALUATORS {
        let entry = evaluators[slot].as_object().ok_or_else(|| {
            V5ArtifactError::new(format!("native {slot} descriptor is malformed"))
        })?;
        let required: Vec<&str> = if slot == "production" {
            vec![
                "qualification",
                "wire_evaluator_id",
                "source_profile",
                "source_closure_sha256",
                "runtime_manifest",
                "executable",
                "raw_receipts",
                "execution",
            ]
        } else {
            vec![
                "qualification",
                "wire_evaluator_id",
                "source_profile",
                "author_origin",
                "task_id",
                "source_closure_sha256",
                "runtime_manifest",
                "executable",
                "raw_receipts",
                "execution",
            ]
        };
        let (missing, extra) = exact_fields(entry, &required);
        if !missing.is_empty() || !extra.is_empty() {
            return Err(V5ArtifactError::new(format!(
                "native {slot} descriptor fields differ"
            )));
        }
        if entry.get("execution") != Some(execution_ref)
            || entry.get("wire_evaluator_id").and_then(Value::as_str) != Some(slot)
        {
            return Err(V5ArtifactError::new(format!(
                "native {slot} does not bind its execution stream"
            )));
        }
        let qualification = if slot == "production" {
            "native_production"
        } else {
            "diagnostic_fixture"
        };
        if entry.get("qualification").and_then(Value::as_str) != Some(qualification) {
            return Err(V5ArtifactError::new(format!(
                "native {slot} qualification is malformed"
            )));
        }
        let closure_sha = entry
            .get("source_closure_sha256")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                V5ArtifactError::new(format!("native {slot} source closure is malformed"))
            })?;
        if !is_sha256(closure_sha) {
            return Err(V5ArtifactError::new(format!(
                "native {slot} source closure digest is malformed"
            )));
        }
        if slot != "production" {
            let provenance = freeze
                .get("reference_provenance")
                .and_then(Value::as_object)
                .and_then(|refs| refs.get(slot))
                .and_then(Value::as_object)
                .ok_or_else(|| {
                    V5ArtifactError::new(format!("diagnostic freeze omits {slot} identity"))
                })?;
            for field in [
                "qualification",
                "source_profile",
                "author_origin",
                "task_id",
                "source_closure_sha256",
            ] {
                if entry.get(field) != provenance.get(field) {
                    return Err(V5ArtifactError::new(format!(
                        "diagnostic {slot} identity differs from freeze"
                    )));
                }
            }
            if entry
                .get("author_origin")
                .and_then(Value::as_str)
                .is_none_or(str::is_empty)
                || entry
                    .get("task_id")
                    .and_then(Value::as_str)
                    .is_none_or(str::is_empty)
            {
                return Err(V5ArtifactError::new(format!(
                    "diagnostic {slot} identity is incomplete"
                )));
            }
        }
        let execution_row = execution_rows
            .get(slot)
            .and_then(Value::as_object)
            .ok_or_else(|| V5ArtifactError::new(format!("execution omits {slot}")))?;
        if execution_row.get("exit_code").and_then(Value::as_i64) != Some(0)
            || execution_row.get("stdout") != entry.get("raw_receipts")
            || execution_row.get("runtime_manifest") != entry.get("runtime_manifest")
        {
            return Err(V5ArtifactError::new(format!(
                "execution does not bind {slot} output"
            )));
        }
        let frozen_executable = freeze
            .get("executables")
            .and_then(Value::as_object)
            .and_then(|items| items.get(slot))
            .ok_or_else(|| V5ArtifactError::new(format!("freeze omits {slot} executable")))?;
        if entry.get("executable") != Some(frozen_executable)
            || execution_row.get("executable_sha256") != frozen_executable.get("sha256")
            || execution_row.get("executable_sha256_before") != frozen_executable.get("sha256")
            || execution_row.get("executable_sha256_after") != frozen_executable.get("sha256")
        {
            return Err(V5ArtifactError::new(format!(
                "frozen executable changed for {slot}"
            )));
        }
        let executable_raw =
            read_bound_artifact(root, entry.get("executable"), &format!("{slot} executable"))?;
        if sha256_hex(&executable_raw) != frozen_executable["sha256"].as_str().unwrap_or("") {
            return Err(V5ArtifactError::new(format!(
                "staged {slot} executable bytes changed"
            )));
        }
        if slot == "production"
            && (sha256_hex(&executable_raw)
                != native_build["executable_sha256"].as_str().unwrap_or("")
                || closure_sha
                    != native_build["source_identity_sha256"]
                        .as_str()
                        .unwrap_or(""))
        {
            return Err(V5ArtifactError::new(
                "production executable/source differs from active build",
            ));
        }
        let runtime_raw = read_bound_artifact(
            root,
            entry.get("runtime_manifest"),
            &format!("{slot} runtime manifest"),
        )?;
        let runtime_value =
            ib_v5_conformance::strict_json_loads(&runtime_raw).map_err(|error| {
                V5ArtifactError::new(format!(
                    "native {slot} runtime manifest is malformed: {error}"
                ))
            })?;
        let runtime = runtime_value
            .as_object()
            .ok_or_else(|| V5ArtifactError::new("runtime manifest must be an object"))?;
        if runtime.get("schema_version").and_then(Value::as_str)
            != Some("identitybench.v5.native-evaluator-runtime.v1")
            || runtime.get("evaluator_slot").and_then(Value::as_str) != Some(slot)
            || runtime.get("invoked_executable_sha256") != frozen_executable.get("sha256")
            || runtime.get("source_closure_sha256") != entry.get("source_closure_sha256")
            || runtime.get("case_count") != freeze.get("case_count")
            || runtime.get("case_manifest_sha256") != freeze.get("case_manifest_sha256")
            || runtime.get("raw_stdout_sha256")
                != entry
                    .get("raw_receipts")
                    .and_then(Value::as_object)
                    .and_then(|row| row.get("sha256"))
            || runtime.get("toolchain") != native_build.get("toolchain")
            || runtime.get("sandboxed") != Some(&Value::Bool(false))
            || runtime.get("isolation_claim").and_then(Value::as_str) != Some("none")
            || runtime.get("dynamic_linking_or_filesystem_isolation_verified")
                != Some(&Value::Bool(false))
        {
            return Err(V5ArtifactError::new(format!(
                "runtime manifest fails {slot} binding or overclaims isolation"
            )));
        }
        let expected_environment = if slot == "production" {
            "native_in_process"
        } else {
            "cleared_with_minimal_path_and_locale"
        };
        if runtime.get("process_environment").and_then(Value::as_str) != Some(expected_environment)
        {
            return Err(V5ArtifactError::new(format!(
                "runtime environment identity differs for {slot}"
            )));
        }
        let stdout = read_bound_artifact(
            root,
            entry.get("raw_receipts"),
            &format!("{slot} raw results"),
        )?;
        let rows = {
            let mut row_errors = Vec::new();
            let mut row_gaps = Vec::new();
            crate::blind_receipt::shared_jsonl_rows(
                &stdout,
                &format!("{slot} raw results"),
                i64::try_from(public_cases.len()).unwrap_or(i64::MAX),
                &mut row_errors,
                &mut row_gaps,
            )
            .ok_or_else(|| {
                artifact_errors("native result stream is invalid", row_errors, row_gaps)
            })?
        };
        let internal = match slot {
            "full_spec_blind" => "full_spec",
            "agent_view_only" => "agent_view",
            _ => "production",
        };
        for (public_case, row) in public_cases.iter().zip(&rows) {
            let case = freeze_case(public_case)
                .map_err(|error| V5ArtifactError::new(error.to_string()))?;
            validate_evaluator_result(row, &case, internal).map_err(|error| {
                V5ArtifactError::new(format!("native {slot} protocol check failed: {error}"))
            })?;
        }
        closures.insert(slot.to_owned(), closure_sha.to_owned());
        streams.insert(slot.to_owned(), rows);
    }

    let pair_closures = BTreeMap::from([
        ("full_spec".to_owned(), closures["full_spec_blind"].clone()),
        ("agent_view".to_owned(), closures["agent_view_only"].clone()),
    ]);
    let all_closures = BTreeMap::from([
        ("full_spec".to_owned(), closures["full_spec_blind"].clone()),
        ("agent_view".to_owned(), closures["agent_view_only"].clone()),
        ("production".to_owned(), closures["production"].clone()),
    ]);
    let mut matched = 0usize;
    let mut mismatched = 0usize;
    let mut incomplete = 0usize;
    for (index, public_case) in public_cases.iter().enumerate() {
        let case =
            freeze_case(public_case).map_err(|error| V5ArtifactError::new(error.to_string()))?;
        let (full, agent, pair) = compare_result_pair(
            &case,
            &streams["full_spec_blind"][index],
            &streams["agent_view_only"][index],
            &pair_closures,
        )
        .map_err(|error| {
            V5ArtifactError::new(format!("native diagnostic comparison failed: {error}"))
        })?;
        let production =
            ib_v5_conformance::evaluate_shared_input(&case.public_case.to_json_value()).map_err(
                |error| V5ArtifactError::new(format!("native production replay failed: {error}")),
            )?;
        let recorded = ib_v5_conformance::canonical_json_bytes(&streams["production"][index])
            .map_err(|error| V5ArtifactError::new(error.to_string()))?;
        let expected = ib_v5_conformance::canonical_json_bytes(&production)
            .map_err(|error| V5ArtifactError::new(error.to_string()))?;
        if recorded != expected {
            return Err(V5ArtifactError::new(format!(
                "{} recorded production differs from active implementation",
                case.case_id()
            )));
        }
        let (_, production_pair) =
            compare_production_result(&case, (&full, &agent), &production, &all_closures).map_err(
                |error| {
                    V5ArtifactError::new(format!(
                        "native diagnostic production comparison failed: {error}"
                    ))
                },
            )?;
        match (pair.status.as_str(), production_pair.status.as_str()) {
            ("MATCH", "MATCH") => matched += 1,
            ("INCOMPLETE", _) | (_, "INCOMPLETE") => incomplete += 1,
            _ => mismatched += 1,
        }
    }
    Ok(json!({
        "receipt_sha256": sha256_hex(receipt_raw),
        "proof_scope": "diagnostic_native_fixture",
        "diagnostic_verified": true,
        "g_spec_claim": "not_claimed",
        "case_count": public_cases.len(),
        "matched_cases": matched,
        "mismatched_cases": mismatched,
        "incomplete_cases": incomplete,
        "native_freeze_sha256": sha256_hex(freeze_raw),
    }))
}

fn exact_fields(value: &Map<String, Value>, expected: &[&str]) -> (Vec<String>, Vec<String>) {
    let expected: std::collections::BTreeSet<&str> = expected.iter().copied().collect();
    let actual: std::collections::BTreeSet<&str> = value.keys().map(String::as_str).collect();
    (
        expected
            .difference(&actual)
            .map(|item| (*item).to_owned())
            .collect(),
        actual
            .difference(&expected)
            .map(|item| (*item).to_owned())
            .collect(),
    )
}

fn validate_active_native_dataset_bindings(
    receipt: &Map<String, Value>,
    root: &Path,
    selected: &DatasetArtifactsV5,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) {
    let Some(rows) = receipt.get("dataset_bindings").and_then(Value::as_array) else {
        gaps.push("native G-SPEC receipt omits the four active Draft 5 suite bindings".to_owned());
        return;
    };
    if rows.len() != 4 {
        errors
            .push("native G-SPEC receipt must bind exactly four active Draft 5 suites".to_owned());
    }
    let mut seen = std::collections::BTreeSet::new();
    for row in rows {
        let Some(row) = row.as_object() else {
            errors.push("native dataset binding row is malformed".to_owned());
            continue;
        };
        let (missing, extra) = exact_fields(row, &["suite_id", "seal", "episodes", "oracles"]);
        if !missing.is_empty() || !extra.is_empty() {
            errors.push("native dataset binding fields differ".to_owned());
            continue;
        }
        let Some(suite) = row.get("suite_id").and_then(Value::as_str) else {
            errors.push("native dataset binding suite_id is malformed".to_owned());
            continue;
        };
        if !["v5_dev", "v5_lite", "v5_core", "v5_full"].contains(&suite)
            || !seen.insert(suite.to_owned())
        {
            errors.push("native dataset binding has an unsupported or duplicate suite".to_owned());
            continue;
        }
        let expected = [
            ("seal", format!("datasets/v5_draft5/{suite}/SEAL.json")),
            (
                "episodes",
                format!("datasets/v5_draft5/{suite}/episodes.jsonl"),
            ),
            (
                "oracles",
                format!("datasets/v5_draft5/{suite}/oracles.jsonl"),
            ),
        ];
        let mut payloads = BTreeMap::<&str, Vec<u8>>::new();
        for (label, path) in expected {
            let Some(reference) = row.get(label) else {
                errors.push(format!("native dataset binding omits {suite} {label}"));
                continue;
            };
            if reference.get("path").and_then(Value::as_str) != Some(path.as_str()) {
                errors.push(format!(
                    "native {suite} {label} binding is not the active Draft 5 path"
                ));
                continue;
            }
            if let Some((bytes, _)) = crate::blind_receipt::shared_bound_bytes(
                root,
                Some(reference),
                &format!("active {suite} {label}"),
                errors,
                gaps,
                false,
                true,
            ) {
                payloads.insert(label, bytes);
            }
        }
        let suite_dir = root.join("datasets/v5_draft5").join(suite);
        match load_v5_dataset(&suite_dir) {
            Ok(current) => {
                for (label, actual) in [
                    ("seal", current.seal_bytes.as_slice()),
                    ("episodes", current.episodes_bytes.as_slice()),
                    ("oracles", current.oracles_bytes.as_slice()),
                ] {
                    if payloads
                        .get(label)
                        .is_some_and(|bound| bound.as_slice() != actual)
                    {
                        errors.push(format!(
                            "native {suite} {label} bytes differ from the active sealed suite"
                        ));
                    }
                }
                if current.seal.get("suite").and_then(Value::as_str) != Some(suite) {
                    errors.push(format!(
                        "active Draft 5 dataset directory does not seal as {suite}"
                    ));
                }
                if selected.seal.get("suite").and_then(Value::as_str) == Some(suite)
                    && (selected.seal_bytes != current.seal_bytes
                        || selected.episodes_bytes != current.episodes_bytes
                        || selected.oracles_bytes != current.oracles_bytes)
                {
                    errors.push(format!(
                        "selected dataset artifacts differ from active Draft 5 {suite}"
                    ));
                }
            }
            Err(error) => errors.push(format!(
                "active Draft 5 dataset {suite} failed validation: {error}"
            )),
        }
    }
    if seen
        != ["v5_dev", "v5_lite", "v5_core", "v5_full"]
            .into_iter()
            .map(str::to_owned)
            .collect()
    {
        errors.push(
            "native dataset binding suite set differs from the active Draft 5 quartet".to_owned(),
        );
    }
}

fn validate_native_reference_source(
    entry: &Map<String, Value>,
    evaluator: &str,
    root: &Path,
    freeze: &Map<String, Value>,
    receipt: &Map<String, Value>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
) {
    let Some((raw, _)) = crate::blind_receipt::shared_bound_bytes(
        root,
        entry.get("source_capture_manifest"),
        &format!("{evaluator} source capture"),
        errors,
        gaps,
        false,
        true,
    ) else {
        return;
    };
    let Some(source) = crate::blind_receipt::shared_json_object(
        &raw,
        "native evaluator source capture",
        errors,
        gaps,
    ) else {
        return;
    };
    let (source_missing, source_extra) = exact_fields(
        &source,
        &[
            "schema_version",
            "author_origin",
            "task_id",
            "frozen_before_comparison",
            "files",
            "source_closure_sha256",
            "read_audit",
        ],
    );
    if !source_missing.is_empty() || !source_extra.is_empty() {
        errors.push(format!("{evaluator} source capture fields differ"));
    }
    if source.get("schema_version").and_then(Value::as_str)
        != Some("identitybench.v5.native-reference-source-capture.v1")
        || source.get("author_origin") != entry.get("author_origin")
        || source.get("task_id") != entry.get("task_id")
        || source.get("source_closure_sha256") != entry.get("source_closure_sha256")
        || source.get("frozen_before_comparison") != Some(&Value::Bool(true))
        || source.get("read_audit") != entry.get("read_audit")
    {
        errors.push(format!(
            "{evaluator} source capture identity differs from its evaluator descriptor"
        ));
    }
    let Some(files) = source.get("files").and_then(Value::as_array) else {
        errors.push(format!("{evaluator} source capture has no file inventory"));
        return;
    };
    if files.is_empty() {
        errors.push(format!("{evaluator} source capture is empty"));
    }
    let source_author = entry
        .get("author_origin")
        .and_then(Value::as_str)
        .unwrap_or("");
    let source_task = entry.get("task_id").and_then(Value::as_str).unwrap_or("");
    let mut source_inventory = Vec::with_capacity(files.len());
    let mut seen_source_paths = std::collections::BTreeSet::new();
    for row in files {
        let Some(row) = row.as_object() else {
            errors.push(format!("{evaluator} source file descriptor is malformed"));
            continue;
        };
        let (missing, extra) = exact_fields(row, &["path", "artifact"]);
        if !missing.is_empty() || !extra.is_empty() {
            errors.push(format!("{evaluator} source file descriptor fields differ"));
            continue;
        }
        let Some(source_path) = row.get("path").and_then(Value::as_str) else {
            errors.push(format!("{evaluator} source path is malformed"));
            continue;
        };
        if ib_v5_conformance::process::is_forbidden_blind_source_path(source_path) {
            errors.push(format!(
                "{evaluator} source capture includes forbidden implementation path"
            ));
            continue;
        }
        if !safe_relative_path(source_path)
            || !native_reference_source_path_allowed(source_path, source_author, source_task)
            || (evaluator == "agent_view_only" && is_normative_path(source_path))
        {
            errors.push(format!("{evaluator} source capture includes a path outside its task-bound source/build inventory"));
            continue;
        }
        if !seen_source_paths.insert(source_path.to_owned()) {
            errors.push(format!(
                "{evaluator} source capture contains a duplicate path"
            ));
            continue;
        }
        let Some(artifact_path) = row
            .get("artifact")
            .and_then(Value::as_object)
            .and_then(|reference| reference.get("path"))
            .and_then(Value::as_str)
        else {
            errors.push(format!("{evaluator} source artifact path is malformed"));
            continue;
        };
        if path_is_forbidden_before_read(root, artifact_path) {
            errors.push(format!(
                "{evaluator} source capture resolves to forbidden implementation input"
            ));
            continue;
        }
        let Some((source_bytes, resolved_source)) = crate::blind_receipt::shared_bound_bytes(
            root,
            row.get("artifact"),
            &format!("{evaluator} captured source file"),
            errors,
            gaps,
            false,
            true,
        ) else {
            continue;
        };
        if resolved_source
            .strip_prefix(root)
            .ok()
            .is_none_or(|resolved| {
                ib_v5_conformance::process::is_forbidden_blind_source_path(
                    &resolved.to_string_lossy().replace('\\', "/"),
                )
            })
        {
            errors.push(format!(
                "{evaluator} captured source resolved to forbidden implementation input"
            ));
            continue;
        }
        source_inventory.push(json!({"path": source_path, "sha256": sha256_hex(&source_bytes)}));
    }
    source_inventory.sort_by(|left, right| {
        left.get("path")
            .and_then(Value::as_str)
            .cmp(&right.get("path").and_then(Value::as_str))
    });
    let source_digest = match jcs_digest(&Value::Array(source_inventory.clone())) {
        Ok(value) => value,
        Err(error) => {
            errors.push(format!(
                "{evaluator} source closure could not be hashed: {error}"
            ));
            String::new()
        }
    };
    if source.get("source_closure_sha256").and_then(Value::as_str) != Some(source_digest.as_str())
        || entry.get("source_closure_sha256").and_then(Value::as_str)
            != Some(source_digest.as_str())
    {
        errors.push(format!(
            "{evaluator} source closure digest does not match its captured bytes"
        ));
    }
    let Some((audit_raw, _)) = crate::blind_receipt::shared_bound_bytes(
        root,
        source.get("read_audit"),
        &format!("{evaluator} read audit"),
        errors,
        gaps,
        false,
        true,
    ) else {
        return;
    };
    let Some(audit) = crate::blind_receipt::shared_json_object(
        &audit_raw,
        "native evaluator read audit",
        errors,
        gaps,
    ) else {
        return;
    };
    let (audit_missing, audit_extra) = exact_fields(
        &audit,
        &[
            "schema_version",
            "scope",
            "author_origin",
            "task_id",
            "frozen_before_comparison",
            "allowed_inputs",
            "reads",
        ],
    );
    if !audit_missing.is_empty() || !audit_extra.is_empty() {
        errors.push(format!("{evaluator} read audit fields differ"));
    }
    if audit.get("schema_version").and_then(Value::as_str)
        != Some("identitybench.v5.native-blind-read-audit.v1")
        || audit.get("scope").and_then(Value::as_str) != Some(evaluator)
        || audit.get("frozen_before_comparison") != Some(&Value::Bool(true))
        || audit.get("author_origin") != entry.get("author_origin")
        || audit.get("task_id") != entry.get("task_id")
    {
        errors.push(format!(
            "{evaluator} read audit is not bound to a precomparison task"
        ));
    }
    let Some(allowed_inputs) = audit.get("allowed_inputs").and_then(Value::as_array) else {
        errors.push(format!(
            "{evaluator} read audit has no hashed allowed-input inventory"
        ));
        return;
    };
    let Some(reads) = audit.get("reads").and_then(Value::as_array) else {
        errors.push(format!("{evaluator} read audit has no actual read records"));
        return;
    };
    if allowed_inputs.is_empty() || reads.is_empty() {
        errors.push(format!(
            "{evaluator} read audit inventory or record list is empty"
        ));
    }
    let mut input_inventory = BTreeMap::new();
    for input in allowed_inputs {
        let Some(input) = input.as_object() else {
            errors.push(format!("{evaluator} allowed input is malformed"));
            continue;
        };
        let (missing, extra) = exact_fields(input, &["input_id", "role", "path", "sha256"]);
        if !missing.is_empty() || !extra.is_empty() {
            errors.push(format!("{evaluator} allowed-input fields differ"));
            continue;
        }
        let (Some(input_id), Some(role), Some(path), Some(sha)) = (
            input.get("input_id").and_then(Value::as_str),
            input.get("role").and_then(Value::as_str),
            input.get("path").and_then(Value::as_str),
            input.get("sha256").and_then(Value::as_str),
        ) else {
            errors.push(format!("{evaluator} allowed-input identity is malformed"));
            continue;
        };
        if input_id.is_empty()
            || !safe_relative_path(path)
            || !is_sha256(sha)
            || input_inventory.contains_key(input_id)
        {
            errors.push(format!(
                "{evaluator} allowed-input identity is duplicate or malformed"
            ));
            continue;
        }
        if native_audit_input_path_forbidden(root, role, path) {
            errors.push(format!(
                "{evaluator} allowed input includes forbidden implementation access"
            ));
            continue;
        }
        if !native_role_allowed(
            evaluator,
            role,
            path,
            sha,
            &source_inventory,
            freeze,
            receipt,
            root,
        ) {
            errors.push(format!(
                "{evaluator} allowed input is outside its view/source contract: {path:?}"
            ));
            continue;
        }
        if allowed_input_digest(root, role, path, &source_inventory).as_deref() != Some(sha) {
            errors.push(format!(
                "{evaluator} allowed input path/hash does not match current artifact bytes"
            ));
        }
        input_inventory.insert(input_id.to_owned(), input.clone());
    }
    for read in reads {
        let Some(read) = read.as_object() else {
            errors.push(format!("{evaluator} read record is malformed"));
            continue;
        };
        let (missing, extra) = exact_fields(read, &["input_id", "role", "path", "sha256"]);
        if !missing.is_empty() || !extra.is_empty() {
            errors.push(format!("{evaluator} read-record fields differ"));
            continue;
        }
        let Some(input_id) = read.get("input_id").and_then(Value::as_str) else {
            errors.push(format!("{evaluator} read-record input_id is malformed"));
            continue;
        };
        match input_inventory.get(input_id) {
            Some(allowed) if *allowed == *read => {}
            _ => errors.push(format!(
                "{evaluator} read record does not match a hashed allowed-input row"
            )),
        }
    }
    for (input_id, _) in &input_inventory {
        if !reads
            .iter()
            .any(|row| row.get("input_id").and_then(Value::as_str) == Some(input_id.as_str()))
        {
            errors.push(format!(
                "{evaluator} allowed input {input_id:?} was never recorded as read"
            ));
        }
    }
    let allowed_roles: std::collections::BTreeSet<&str> = allowed_inputs
        .iter()
        .filter_map(|row| row.get("role").and_then(Value::as_str))
        .collect();
    let required_view_role = if evaluator == "agent_view_only" {
        "delivered_view"
    } else {
        "public_input"
    };
    for required_role in [
        required_view_role,
        "public_schema",
        "blind_skill",
        "own_source",
    ] {
        if !allowed_roles.contains(required_role)
            || !reads
                .iter()
                .any(|row| row.get("role").and_then(Value::as_str) == Some(required_role))
        {
            errors.push(format!(
                "{evaluator} audit does not bind and read required role {required_role:?}"
            ));
        }
    }
    if evaluator == "full_spec_blind"
        && !reads
            .iter()
            .any(|row| row.get("role").and_then(Value::as_str) == Some("normative_spec"))
    {
        errors.push(
            "full_spec_blind audit does not record an approved normative specification read"
                .to_owned(),
        );
    }
    if evaluator == "agent_view_only" && allowed_roles.contains("normative_spec") {
        errors.push("agent_view_only audit exposes normative full-spec materials".to_owned());
    }
    if evaluator == "full_spec_blind" {
        for required_path in [
            "SPEC-v5.md",
            "SPEC-v5.0-draft.5.md",
            "SPEC-v5-ADDENDUM-01.md",
            "SPEC-v5-DIRECTIVE-01.md",
        ] {
            if !reads.iter().any(|row| {
                row.get("role").and_then(Value::as_str) == Some("normative_spec")
                    && row.get("path").and_then(Value::as_str) == Some(required_path)
            }) {
                errors.push(format!(
                    "full_spec_blind audit omits required normative input {required_path}"
                ));
            }
        }
    }
    for source in &source_inventory {
        let Some(source_path) = source.get("path").and_then(Value::as_str) else {
            continue;
        };
        let Some(source_sha) = source.get("sha256").and_then(Value::as_str) else {
            continue;
        };
        let captured = reads.iter().any(|row| {
            row.get("role").and_then(Value::as_str) == Some("own_source")
                && row.get("path").and_then(Value::as_str) == Some(source_path)
                && row.get("sha256").and_then(Value::as_str) == Some(source_sha)
        });
        if !captured {
            errors.push(format!(
                "{evaluator} read audit omits its own captured source {source_path:?}"
            ));
        }
    }
}

fn safe_relative_path(path: &str) -> bool {
    let relative = Path::new(path);
    !path.is_empty()
        && !path.contains('\\')
        && !relative.is_absolute()
        && !path.split('/').any(str::is_empty)
        && relative
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
}

fn is_normative_path(path: &str) -> bool {
    let name = Path::new(path)
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("");
    name.starts_with("SPEC-v5") && name.ends_with(".md")
}

fn native_reference_source_path_allowed(path: &str, author_origin: &str, task_id: &str) -> bool {
    fn identity_component(value: &str) -> bool {
        !value.is_empty()
            && value != "."
            && value != ".."
            && value
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-' || byte == b'_')
    }
    if !identity_component(author_origin) || !identity_component(task_id) {
        return false;
    }
    let prefix = format!("tests/conformance/v5/native/{author_origin}/{task_id}/");
    if !path.starts_with(&prefix) {
        return false;
    }
    let relative = path.strip_prefix(&prefix).unwrap_or_default();
    let Some(name) = Path::new(relative)
        .file_name()
        .and_then(|value| value.to_str())
    else {
        return false;
    };
    let lower = name.to_ascii_lowercase();
    let allowed_name = matches!(
        lower.as_str(),
        "cargo.toml"
            | "cargo.lock"
            | "makefile"
            | "pyproject.toml"
            | "requirements.txt"
            | "go.mod"
            | "go.sum"
            | "package.json"
            | "package-lock.json"
            | "tsconfig.json"
    );
    let allowed_extension = Path::new(name)
        .extension()
        .and_then(|value| value.to_str())
        .is_some_and(|extension| {
            [
                "rs", "py", "sh", "bash", "c", "h", "cc", "cpp", "hpp", "go", "js", "ts", "java",
                "kt", "rb", "pl", "swift", "scala", "lua",
            ]
            .contains(&extension.to_ascii_lowercase().as_str())
        });
    allowed_name || allowed_extension
}

fn validate_native_runtime_artifact(
    reference: Option<&Value>,
    evaluator: &str,
    root: &Path,
    entry: &Map<String, Value>,
    execution_entry: &Map<String, Value>,
    native_build: &Value,
    execution_timeout_ms: Option<u64>,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
    case_count: usize,
    case_manifest_sha256: &str,
) {
    let Some((raw, _)) = crate::blind_receipt::shared_bound_bytes(
        root,
        reference,
        &format!("{evaluator} runtime manifest"),
        errors,
        gaps,
        false,
        true,
    ) else {
        return;
    };
    let Some(manifest) =
        crate::blind_receipt::shared_json_object(&raw, "native runtime manifest", errors, gaps)
    else {
        return;
    };
    let raw_sha = entry
        .get("raw_receipts")
        .and_then(Value::as_object)
        .and_then(|item| item.get("sha256"));
    let executable_sha = entry
        .get("executable")
        .and_then(Value::as_object)
        .and_then(|item| item.get("sha256"));
    let source_sha = entry.get("source_closure_sha256");
    let expected_environment = if evaluator == "production" {
        "native_in_process"
    } else {
        "cleared_with_minimal_path_and_locale"
    };
    if manifest.get("schema_version").and_then(Value::as_str)
        != Some("identitybench.v5.native-evaluator-runtime.v1")
        || manifest.get("evaluator_slot").and_then(Value::as_str) != Some(evaluator)
        || manifest.get("sandboxed") != Some(&Value::Bool(false))
        || manifest.get("isolation_claim").and_then(Value::as_str) != Some("none")
        || manifest.get("dynamic_linking_or_filesystem_isolation_verified")
            != Some(&Value::Bool(false))
        || manifest.get("process_environment").and_then(Value::as_str) != Some(expected_environment)
        || manifest.get("os").and_then(Value::as_str) != Some(std::env::consts::OS)
        || manifest.get("arch").and_then(Value::as_str) != Some(std::env::consts::ARCH)
        || manifest.get("invoked_executable_sha256") != executable_sha
        || manifest.get("source_closure_sha256") != source_sha
        || manifest.get("case_count").and_then(Value::as_u64) != u64::try_from(case_count).ok()
        || manifest.get("case_manifest_sha256").and_then(Value::as_str)
            != Some(case_manifest_sha256)
        || manifest.get("raw_stdout_sha256") != raw_sha
        || manifest.get("toolchain") != native_build.get("toolchain")
        || (evaluator != "production"
            && (manifest.get("timeout_ms").and_then(Value::as_u64) != execution_timeout_ms
                || execution_timeout_ms.is_none_or(|timeout| timeout == 0 || timeout > 3_600_000)))
        || execution_entry.get("stdout") != entry.get("raw_receipts")
        || execution_entry.get("runtime_manifest") != entry.get("runtime_manifest")
    {
        errors.push(format!("{evaluator} runtime manifest fails executable/source/corpus/output binding or overclaims isolation"));
    }
}

fn validate_native_executable_artifact(
    reference: Option<&Value>,
    evaluator: &str,
    root: &Path,
    errors: &mut Vec<String>,
    gaps: &mut Vec<String>,
    native_build: &Value,
) {
    let Some((bytes, _)) = crate::blind_receipt::shared_bound_bytes(
        root,
        reference,
        &format!("{evaluator} executable"),
        errors,
        gaps,
        false,
        true,
    ) else {
        return;
    };
    let actual = sha256_hex(&bytes);
    if evaluator == "production"
        && (native_build
            .get("executable_sha256")
            .and_then(Value::as_str)
            != Some(actual.as_str())
            || std::env::current_exe()
                .ok()
                .and_then(|path| fs::read(path).ok())
                .is_none_or(|current| sha256_hex(&current) != actual))
    {
        errors.push(
            "native production executable artifact differs from the active identitybench binary"
                .to_owned(),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_map_rejects_parent_paths_and_invalid_digests() {
        assert!(parse_source_identity(&json!({"../src/lib.rs": "a".repeat(64)})).is_err());
        assert!(parse_source_identity(&json!({"src/lib.rs": "A".repeat(64)})).is_err());
    }

    #[test]
    fn native_reference_source_inventory_is_bound_to_author_task_code_roots() {
        assert!(native_reference_source_path_allowed(
            "tests/conformance/v5/native/author_a/task_1/src/evaluator.rs",
            "author_a",
            "task_1",
        ));
        for mutated in [
            "reports/v5/conformance/old-results.jsonl",
            "reports/v5/conformance/authors/old-answer.py",
            "datasets/v5_draft5/v5_dev/oracles.jsonl",
            "tests/conformance/v5/native/author_a/other_task/evaluator.py",
            "tests/conformance/v5/native/author_a/task_1/output.jsonl",
        ] {
            assert!(
                !native_reference_source_path_allowed(mutated, "author_a", "task_1"),
                "reused or non-source artifact was accepted as original source: {mutated}"
            );
        }
        assert!(ib_v5_conformance::process::is_forbidden_blind_source_path(
            "crates/ib-v5-oracle/src/lib.rs"
        ));
    }

    #[test]
    fn own_source_audit_hashes_the_captured_artifact_not_a_checkout_path() {
        let path = "tests/conformance/v5/native/author_a/task_1/src/evaluator.rs";
        let digest = "a".repeat(64);
        let inventory = vec![json!({"path":path,"sha256":digest})];
        assert_eq!(
            allowed_input_digest(
                Path::new("/missing-repository"),
                "own_source",
                path,
                &inventory
            ),
            Some("a".repeat(64))
        );
        assert_eq!(
            allowed_input_digest(
                Path::new("/missing-repository"),
                "public_input",
                path,
                &inventory
            ),
            None
        );
    }

    #[test]
    fn own_source_audit_allows_an_absent_logical_path_only_with_exact_capture_binding() {
        let root = tempfile::tempdir().expect("temporary repository");
        let path = "tests/conformance/v5/native/author_a/task_1/src/evaluator.rs";
        let digest = "a".repeat(64);
        let inventory = vec![json!({"path":path,"sha256":digest})];
        assert!(safe_relative_path(path));
        assert!(native_reference_source_path_allowed(
            path, "author_a", "task_1"
        ));
        assert!(!root.path().join(path).exists());
        assert!(!native_audit_input_path_forbidden(
            root.path(),
            "own_source",
            path
        ));
        assert!(native_audit_input_path_forbidden(
            root.path(),
            "public_input",
            path
        ));
        assert!(native_role_allowed(
            "agent_view_only",
            "own_source",
            path,
            &digest,
            &inventory,
            &Map::new(),
            &Map::new(),
            root.path(),
        ));
        assert_eq!(
            allowed_input_digest(root.path(), "own_source", path, &inventory).as_deref(),
            Some(digest.as_str())
        );

        let wrong_digest = "b".repeat(64);
        assert!(!native_role_allowed(
            "agent_view_only",
            "own_source",
            path,
            &wrong_digest,
            &inventory,
            &Map::new(),
            &Map::new(),
            root.path(),
        ));
        assert_ne!(
            allowed_input_digest(root.path(), "own_source", path, &inventory).as_deref(),
            Some(wrong_digest.as_str())
        );

        let unlisted_path = "tests/conformance/v5/native/author_a/task_1/src/unlisted.rs";
        assert!(!native_role_allowed(
            "agent_view_only",
            "own_source",
            unlisted_path,
            &digest,
            &inventory,
            &Map::new(),
            &Map::new(),
            root.path(),
        ));
        assert_eq!(
            allowed_input_digest(root.path(), "own_source", unlisted_path, &inventory),
            None
        );
        assert!(native_audit_input_path_forbidden(
            root.path(),
            "own_source",
            "crates/ib-v5-oracle/src/lib.rs"
        ));
    }

    #[cfg(unix)]
    #[test]
    fn captured_source_artifact_guard_rejects_oracle_paths_and_symlink_aliases() {
        use std::os::unix::fs::symlink;

        let root = tempfile::tempdir().expect("temporary repository");
        let canonical_root = root.path().canonicalize().expect("canonical temp root");
        let conformance = canonical_root.join("reports/v5/conformance");
        let oracle = canonical_root.join("datasets/v5_draft5/v5_dev/oracles.jsonl");
        fs::create_dir_all(&conformance).expect("create approved artifact root");
        let safe_artifact = conformance.join("worker-source.rs");
        fs::write(&safe_artifact, b"captured source fixture").expect("write safe source fixture");
        fs::create_dir_all(oracle.parent().expect("oracle parent"))
            .expect("create sealed dataset fixture directory");
        fs::write(&oracle, b"synthetic sealed oracle bytes").expect("write oracle fixture");
        assert!(!path_is_forbidden_before_read(
            &canonical_root,
            "reports/v5/conformance/worker-source.rs"
        ));
        assert!(path_is_forbidden_before_read(
            &canonical_root,
            "datasets/v5_draft5/v5_dev/oracles.jsonl"
        ));

        let captured_alias = conformance.join("captured-source.rs");
        symlink(&oracle, &captured_alias).expect("create captured-artifact alias");
        assert!(path_is_forbidden_before_read(
            &canonical_root,
            "reports/v5/conformance/captured-source.rs"
        ));
    }

    #[test]
    fn derive_phase_references_must_match_exact_active_sealed_mutation_payload() {
        use ib_v5_conformance::shared::{
            Coverage, EvaluatorResult, FrozenCase, PROTOCOL, PublicCase,
        };

        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .expect("repository root");
        let dataset =
            load_v5_dataset(&root.join("datasets/v5_draft5/v5_dev")).expect("active v5_dev suite");
        let (episode, oracle) = dataset
            .episodes
            .iter()
            .zip(&dataset.oracles)
            .find(|(_, oracle)| !oracle.gold_p1_mutations.is_empty())
            .expect("episode with a phase 1 target");
        let case = FrozenCase {
            public_case: PublicCase {
                protocol: PROTOCOL.to_owned(),
                case_id: format!("v5_dev/{}/p1/gold-check", episode.episode_id),
                operation: "derive_phase".to_owned(),
                suite_id: "v5_dev".to_owned(),
                phase_index: 1,
                public_episode: Map::from_iter([(
                    "episode_id".to_owned(),
                    Value::String(episode.episode_id.clone()),
                )]),
                rendered_card: String::new(),
                schema_sha256: ib_v5_conformance::shared::PINNED_PUBLIC_SCHEMA_SHA256.to_owned(),
                stimulus: Map::new(),
            },
            canonical_bytes: Vec::new(),
            input_sha256: String::new(),
        };
        let mutations = serde_json::to_value(&oracle.gold_p1_mutations).expect("serialize gold");
        let result = |evaluator_id: &str, mutations: Value| EvaluatorResult {
            protocol: PROTOCOL.to_owned(),
            evaluator_id: evaluator_id.to_owned(),
            case_id: case.case_id().to_owned(),
            input_sha256: String::new(),
            status: "ok".to_owned(),
            payload: json!({"mutations":mutations}).as_object().unwrap().clone(),
            materialization: Value::Null,
            coverage: Coverage {
                phase_targets: Vec::new(),
                diagnostics: Vec::new(),
                rules: Vec::new(),
                safety_cases: Vec::new(),
                snapshots: Vec::new(),
            },
            gaps: Vec::new(),
            raw_value: Value::Null,
        };
        let full = result("full_spec", mutations.clone());
        let agent = result("agent_view", mutations.clone());
        let datasets = BTreeMap::from([("v5_dev".to_owned(), dataset)]);
        assert!(compare_native_phase_mutations_to_gold(&case, &full, &agent, &datasets).is_ok());

        let changed_agent = result("agent_view", json!([]));
        assert!(
            compare_native_phase_mutations_to_gold(&case, &full, &changed_agent, &datasets)
                .expect_err("changed mutation payload must be rejected")
                .contains("exact sealed phase-1 gold payload")
        );
    }

    #[test]
    fn sealed_gold_adapter_sorts_expected_only_and_rejects_duplicate_targets() {
        let archived_order = json!([
            {
                "mutation_kind":"Z_MUTATION",
                "target_entity_id":"target_02",
                "semantic_value":"z"
            },
            {
                "mutation_kind":"A_MUTATION",
                "target_entity_id":"target_01",
                "semantic_value":"a"
            }
        ]);
        let expected_wire_order = json!([
            {
                "mutation_kind":"A_MUTATION",
                "target_entity_id":"target_01",
                "semantic_value":"a"
            },
            {
                "mutation_kind":"Z_MUTATION",
                "target_entity_id":"target_02",
                "semantic_value":"z"
            }
        ]);
        let normalized = normalized_sealed_gold_mutation_bytes(&archived_order)
            .expect("normalize only the expected sealed array");
        assert_eq!(
            normalized,
            ib_v5_conformance::canonical_json_bytes(&expected_wire_order)
                .expect("canonical wire order")
        );

        let unchanged_wire_candidate =
            ib_v5_conformance::canonical_json_bytes(&expected_wire_order)
                .expect("canonical candidate wire order");
        assert_eq!(unchanged_wire_candidate, normalized);
        let wrong_order_candidate =
            ib_v5_conformance::canonical_json_bytes(&archived_order).expect("canonical candidate");
        assert_ne!(wrong_order_candidate, normalized);
        let changed_candidate = json!([
            {
                "mutation_kind":"A_MUTATION",
                "target_entity_id":"target_01",
                "semantic_value":"altered"
            },
            {
                "mutation_kind":"Z_MUTATION",
                "target_entity_id":"target_02",
                "semantic_value":"z"
            }
        ]);
        assert_ne!(
            ib_v5_conformance::canonical_json_bytes(&changed_candidate)
                .expect("canonical changed candidate"),
            normalized
        );

        let duplicated_gold = json!([
            {"mutation_kind":"ACCOUNT_RECON_MUTATION","target_entity_id":"acct_01"},
            {"mutation_kind":"ACCOUNT_RECON_MUTATION","target_entity_id":"acct_01"}
        ]);
        assert!(
            normalized_sealed_gold_mutation_bytes(&duplicated_gold)
                .expect_err("duplicate gold targets must be rejected")
                .contains("duplicate mutation targets")
        );
    }

    #[test]
    fn native_role_audit_rejects_full_spec_and_other_public_for_agent_view() {
        let freeze_value = json!({
            "cases": {"path":"reports/v5/conformance/frozen/cases.jsonl","sha256":"1".repeat(64)},
            "corpus": {
                "public_cases": {"path":"reports/v5/conformance/corpus/public_cases.jsonl","sha256":"2".repeat(64)},
                "coverage_ledger": {"path":"reports/v5/conformance/corpus/coverage-ledger.json","sha256":"5".repeat(64)},
                "coverage": {"path":"reports/v5/conformance/corpus/coverage.json","sha256":"3".repeat(64)},
                "family_map": {"path":"reports/v5/conformance/corpus/family_map.json","sha256":"4".repeat(64)},
            },
        });
        let freeze = freeze_value.as_object().unwrap().clone();
        let receipt = Map::new();
        let source_inventory = Vec::new();
        assert!(!native_role_allowed(
            "agent_view_only",
            "normative_spec",
            "SPEC-v5.md",
            ib_v5_schemas::normative_contract::V5_BASE_SPEC_SHA256,
            &source_inventory,
            &freeze,
            &receipt,
            Path::new("."),
        ));
        assert!(!native_role_allowed(
            "agent_view_only",
            "other_public",
            "reports/v5/conformance/prior-output.jsonl",
            &"0".repeat(64),
            &source_inventory,
            &freeze,
            &receipt,
            Path::new("."),
        ));
        assert!(native_role_allowed(
            "full_spec_blind",
            "public_input",
            "reports/v5/conformance/frozen/cases.jsonl",
            &"1".repeat(64),
            &source_inventory,
            &freeze,
            &receipt,
            Path::new("."),
        ));
        assert!(native_role_allowed(
            "full_spec_blind",
            "public_input",
            "reports/v5/conformance/corpus/public_cases.jsonl",
            &"2".repeat(64),
            &source_inventory,
            &freeze,
            &receipt,
            Path::new("."),
        ));
        for (path, digest) in [
            ("reports/v5/conformance/corpus/coverage.json", "3"),
            ("reports/v5/conformance/corpus/coverage-ledger.json", "5"),
            ("reports/v5/conformance/corpus/family_map.json", "4"),
        ] {
            assert!(!native_role_allowed(
                "full_spec_blind",
                "public_input",
                path,
                &digest.repeat(64),
                &source_inventory,
                &freeze,
                &receipt,
                Path::new("."),
            ));
        }
    }

    #[test]
    fn native_dataset_guard_rejects_archival_other_suite_binding() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .expect("repository root");
        let selected_dir = root.join("datasets/v5_draft5/v5_dev");
        let selected = load_v5_dataset(&selected_dir).expect("active selected suite");
        let selected_before = selected.clone();
        let active = root.join("datasets/v5_draft5/v5_lite");
        let archival_oracles = root.join("datasets/v5_lite/oracles.jsonl");
        let binding = json!({
            "suite_id": "v5_lite",
            "seal": {
                "path": "datasets/v5_draft5/v5_lite/SEAL.json",
                "sha256": sha256_hex(&fs::read(active.join("SEAL.json")).expect("active seal")),
            },
            "episodes": {
                "path": "datasets/v5_draft5/v5_lite/episodes.jsonl",
                "sha256": sha256_hex(&fs::read(active.join("episodes.jsonl")).expect("active episodes")),
            },
            "oracles": {
                "path": "datasets/v5_lite/oracles.jsonl",
                "sha256": sha256_hex(&fs::read(archival_oracles).expect("archival fixture bytes")),
            },
        });
        let receipt = json!({"dataset_bindings": [binding]});
        let mut errors = Vec::new();
        let mut gaps = Vec::new();
        validate_active_native_dataset_bindings(
            receipt.as_object().expect("receipt object"),
            root,
            &selected,
            &mut errors,
            &mut gaps,
        );
        assert!(
            errors
                .iter()
                .any(|error| error.contains("active Draft 5 path"))
        );
        assert_eq!(selected.seal_bytes, selected_before.seal_bytes);
        assert_eq!(selected.episodes_bytes, selected_before.episodes_bytes);
        assert_eq!(selected.oracles_bytes, selected_before.oracles_bytes);
    }

    #[test]
    fn legacy_native_prepared_corpus_is_diagnostic_only_for_current_gspec() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .expect("repository root");
        let artifacts =
            load_v5_dataset(&root.join("datasets/v5_draft5/v5_dev")).expect("active v5_dev suite");
        let receipt = json!({
            "schema_version": NATIVE_SHARED_PROOF_SCHEMA,
            "proof_scope": "G-SPEC-01",
            "suite_id": "v5_dev",
            "spec_sha256": ib_v5_schemas::V5_BASE_SPEC_SHA256,
            "dataset_bindings": [],
            "corpus": {"format":"identitybench.v5.native-prepared-gspec-corpus.v1"},
            "freeze": null,
            "evaluators": {},
        });
        let raw = ib_v5_conformance::canonical_json_bytes(&receipt).expect("canonical receipt");
        let (errors, gaps, evidence) = validate_native_gspec_receipt(
            Path::new("reports/v5/conformance/legacy-receipt.json"),
            &raw,
            receipt.as_object().expect("receipt object"),
            &artifacts,
            root,
            None,
        )
        .expect("legacy receipt must return a scoped ineligibility result");
        assert!(errors.is_empty(), "{errors:?}");
        assert!(
            gaps.iter()
                .any(|gap| gap.contains("archival/diagnostic-only")),
            "{gaps:?}"
        );
        assert_eq!(
            evidence.get("native_corpus_eligibility"),
            Some(&json!("archival_diagnostic_only"))
        );
    }

    #[test]
    fn changed_valid_frozen_case_row_does_not_match_receipt_public_cases() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .expect("repository root");
        let episodes_path =
            root.join("reports/v5/conformance/inputs/draft5_v1/v5_dev/selected-episodes.jsonl");
        let episode = ib_v5_conformance::strict_jsonl_loads(
            &fs::read(episodes_path).expect("selected public episode view"),
        )
        .expect("parse selected public episode view")
        .into_iter()
        .next()
        .expect("selected public episode exists");
        let episode_object = episode.as_object().expect("public episode object");
        let episode_id = episode_object["episode_id"]
            .as_str()
            .expect("episode identity");
        let mut public_episode = Map::new();
        for key in ib_v5_conformance::shared::P1_EPISODE_KEYS {
            public_episode.insert(key.to_owned(), episode_object[key].clone());
        }
        let rendered_card = fs::read_to_string(
            root.join("reports/v5/conformance/inputs/draft5_v1/v5_dev/rendered-card.txt"),
        )
        .expect("rendered public card");
        let case = json!({
            "protocol": ib_v5_conformance::shared::PROTOCOL,
            "case_id": format!("v5_dev/{episode_id}/p1/derive_phase"),
            "operation": "derive_phase",
            "suite_id": "v5_dev",
            "phase_index": 1,
            "public_episode": Value::Object(public_episode),
            "rendered_card": rendered_card,
            "schema_sha256": ib_v5_conformance::shared::PINNED_PUBLIC_SCHEMA_SHA256,
            "stimulus": {},
        });
        let frozen = ib_v5_conformance::shared::freeze_case(&case).expect("valid frozen input");
        let mut corpus_bytes = frozen.canonical_bytes.clone();
        corpus_bytes.push(b'\n');
        let digest = sha256_hex(&corpus_bytes);
        assert!(native_frozen_case_stream_matches(
            &corpus_bytes,
            &corpus_bytes,
            &corpus_bytes,
            &digest,
        ));

        let mut changed_case = case.clone();
        let card = changed_case["rendered_card"].as_str().unwrap().to_owned();
        changed_case["rendered_card"] = Value::String(format!("{card}\n"));
        let changed = ib_v5_conformance::shared::freeze_case(&changed_case)
            .expect("changed row remains a valid public case");
        let mut changed_bytes = changed.canonical_bytes;
        changed_bytes.push(b'\n');
        let changed_digest = sha256_hex(&changed_bytes);
        assert!(!native_frozen_case_stream_matches(
            &changed_bytes,
            &corpus_bytes,
            &corpus_bytes,
            &changed_digest,
        ));
        assert!(!native_frozen_case_stream_matches(
            b"",
            &corpus_bytes,
            &corpus_bytes,
            &digest,
        ));
    }

    #[cfg(unix)]
    #[test]
    fn native_receipt_path_rejects_sensitive_files_and_symlinked_parents_before_read() {
        use std::os::unix::fs::symlink;

        let root = tempfile::tempdir().expect("temporary repository");
        let canonical_root = root
            .path()
            .canonicalize()
            .expect("canonical temporary root");
        let conformance = canonical_root.join("reports/v5/conformance");
        fs::create_dir_all(&conformance).expect("create public root");
        let public = conformance.join("receipt.json");
        fs::write(&public, b"{}\n").expect("write public receipt");
        assert!(resolve_public_receipt_path(&canonical_root, &public).is_ok());

        let private = conformance.join(".env");
        fs::write(&private, b"secret").expect("write private fixture");
        assert!(resolve_public_receipt_path(&canonical_root, &private).is_err());

        let target = canonical_root.join("reports/v5/conformance/target");
        fs::create_dir_all(&target).expect("create symlink target");
        fs::write(target.join("receipt.json"), b"{}\n").expect("write target receipt");
        let alias = conformance.join("alias");
        symlink(&target, &alias).expect("create symlink parent");
        assert!(resolve_public_receipt_path(&canonical_root, &alias.join("receipt.json")).is_err());
    }
}
