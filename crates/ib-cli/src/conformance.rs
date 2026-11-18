//! Native conformance lifecycle commands.
//!
//! This module is deliberately a Rust-only adapter: external reference evaluators
//! speak the JSONL process protocol and no Python interpreter is started here.

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, ensure};
use clap::{Args, Subcommand};
use ib_v5_conformance::process::{
    NativeFreezeRequest, bind_native_conformance, freeze_native_conformance, run_native_conformance,
};
use ib_v5_validation::native_receipt::{
    capture_native_build_identity, prepare_native_gspec_corpus,
    validate_native_conformance_receipt, verify_native_build_identity,
};
use serde_json::{Map, Value, json};

mod embedded {
    include!(concat!(env!("OUT_DIR"), "/source_identity.rs"));
    include!(concat!(env!("OUT_DIR"), "/toolchain_identity.rs"));
}

#[derive(Debug, Subcommand)]
pub enum ConformanceCommand {
    /// Reconstruct and persist the public cases and corpus bindings before reference authoring.
    Prepare(PrepareArgs),
    /// Freeze diagnostic cases or the reconstructed full G-SPEC corpus.
    Freeze(FreezeArgs),
    /// Execute the two staged external reference evaluators.
    Run(RunArgs),
    /// Recompute native production results and seal the receipt.
    Bind(BindArgs),
    /// Verify a diagnostic receipt or the full G-SPEC receipt.
    Verify(VerifyArgs),
}

#[derive(Debug, Args)]
pub struct PrepareArgs {
    /// Canonical raw public-case JSONL to seal into a fresh full G-SPEC corpus.
    #[arg(long, conflicts_with = "full_gspec")]
    cases: Option<PathBuf>,
    /// Initial or prepared archival descriptor for the public four-suite corpus.
    #[arg(long, conflicts_with = "cases")]
    full_gspec: Option<PathBuf>,
    /// One selected sealed Draft 5 suite directory used to verify all four bindings.
    #[arg(long)]
    dataset: PathBuf,
    /// New repository-local directory for reconstructed public corpus sidecars.
    #[arg(long, short)]
    out: PathBuf,
}

#[derive(Debug, Args)]
pub struct FreezeArgs {
    /// Canonical PublicCase JSONL. This creates diagnostic evidence only.
    #[arg(long, conflicts_with = "full_gspec")]
    cases: Option<PathBuf>,
    /// Fresh native-public descriptor from `prepare --cases`; archival 1702-case descriptors are not eligible.
    #[arg(long, conflicts_with = "cases")]
    full_gspec: Option<PathBuf>,
    /// One selected sealed Draft 5 suite directory used to verify all four bindings.
    #[arg(long, requires = "full_gspec")]
    dataset: Option<PathBuf>,
    /// Independent-reference qualifications and immutable source/audit artifact refs.
    #[arg(long, requires = "full_gspec")]
    reference_provenance: Option<PathBuf>,
    /// Executable implementing the independently authored full-spec JSONL protocol.
    #[arg(long)]
    full_spec_executable: PathBuf,
    /// Executable implementing the independently authored agent-view JSONL protocol.
    #[arg(long)]
    agent_view_executable: PathBuf,
    /// New output directory for the frozen cases and read-only evaluator copies.
    #[arg(long, short)]
    out: PathBuf,
}

#[derive(Debug, Args)]
pub struct RunArgs {
    /// Freeze file produced by `v5 conformance freeze`.
    #[arg(long)]
    freeze: PathBuf,
    /// Wall-clock timeout for each staged evaluator process.
    #[arg(long, default_value_t = 600_000)]
    timeout_ms: u64,
}

#[derive(Debug, Args)]
pub struct BindArgs {
    /// Freeze file produced by `v5 conformance freeze`.
    #[arg(long)]
    freeze: PathBuf,
    /// Execution file produced by `v5 conformance run`.
    #[arg(long)]
    execution: PathBuf,
    /// Suite identifier recorded in the native receipt.
    #[arg(long)]
    suite: String,
    /// Destination receipt path.
    #[arg(long)]
    out: PathBuf,
}

#[derive(Debug, Args)]
pub struct VerifyArgs {
    /// Receipt produced by `v5 conformance bind`.
    #[arg(long)]
    receipt: PathBuf,
}

pub fn execute(root: &Path, command: ConformanceCommand) -> Result<Value> {
    match command {
        ConformanceCommand::Prepare(args) => prepare(root, args),
        ConformanceCommand::Freeze(args) => freeze(root, args),
        ConformanceCommand::Run(args) => run(root, args),
        ConformanceCommand::Bind(args) => bind(root, args),
        ConformanceCommand::Verify(args) => verify(root, args),
    }
}

fn prepare(root: &Path, args: PrepareArgs) -> Result<Value> {
    ensure!(
        args.cases.is_some() ^ args.full_gspec.is_some(),
        "provide exactly one of --cases or --full-gspec"
    );
    let output_dir = crate::paths::resolve(root, &args.out);
    require_new_output(&output_dir)?;
    ensure_output_separate_from_draft5(root, &output_dir)?;
    let selected_dataset = paths_input(root, &args.dataset)?;
    let mut prepared = if let Some(cases_path) = args.cases.as_deref() {
        let cases_path = paths_input(root, cases_path)?;
        ib_v5_validation::native_receipt::prepare_native_public_corpus(
            root,
            &selected_dataset,
            &cases_path,
            &output_dir,
        )?
    } else {
        let descriptor_path = args
            .full_gspec
            .as_deref()
            .context("provide exactly one of --cases or --full-gspec")?;
        let descriptor = read_json_object(&paths_input(root, descriptor_path)?)?;
        let descriptor_value = Value::Object(descriptor);
        prepare_native_gspec_corpus(root, &selected_dataset, &descriptor_value, &output_dir)?
    };
    if let Some(object) = prepared.as_object_mut() {
        object.remove("cases");
        object
            .entry("public_cases_path".to_owned())
            .or_insert_with(|| json!(output_dir.join("public_cases.jsonl")));
        object
            .entry("descriptor_path".to_owned())
            .or_insert_with(|| json!(output_dir.join("descriptor.json")));
        object.insert("prepared_output_dir".to_owned(), json!(output_dir));
    }
    Ok(prepared)
}

/// Capture the source inventory and toolchain identity embedded by this executable's build.
pub(crate) fn current_native_build_identity(root: &Path) -> Result<Value> {
    let source = Value::Object(
        embedded::EMBEDDED_SOURCE_IDENTITY
            .iter()
            .map(|(path, digest)| ((*path).to_owned(), json!(digest)))
            .collect(),
    );
    let toolchain = Value::Object(
        embedded::EMBEDDED_TOOLCHAIN_IDENTITY
            .iter()
            .map(|(name, value)| ((*name).to_owned(), json!(value)))
            .collect(),
    );
    let executable = std::env::current_exe().context("resolve identitybench executable")?;
    let identity = capture_native_build_identity(root, &source, &toolchain, &executable)?;
    verify_native_build_identity(root, &identity, &executable)?;
    Ok(identity)
}

fn freeze(root: &Path, args: FreezeArgs) -> Result<Value> {
    ensure!(
        args.cases.is_some() ^ args.full_gspec.is_some(),
        "provide exactly one of --cases or --full-gspec"
    );
    let full_spec_executable = paths_input(root, &args.full_spec_executable)?;
    let agent_view_executable = paths_input(root, &args.agent_view_executable)?;
    let out_dir = crate::paths::resolve(root, &args.out);
    require_new_output(&out_dir)?;
    ensure_output_separate_from_draft5(root, &out_dir)?;

    let (cases, corpus, dataset_bindings, reference_provenance, freeze_dir, prepared_dir) =
        if let Some(case_path) = args.cases.as_deref() {
            ensure!(
                args.dataset.is_none() && args.reference_provenance.is_none(),
                "--dataset and --reference-provenance are available only with --full-gspec"
            );
            let full_spec_executable_hash = hash_executable_bytes(&full_spec_executable)?;
            let agent_view_executable_hash = hash_executable_bytes(&agent_view_executable)?;
            (
                read_canonical_jsonl(&paths_input(root, case_path)?)?,
                None,
                None,
                Some(diagnostic_reference_provenance(
                    &full_spec_executable_hash,
                    &agent_view_executable_hash,
                )),
                out_dir.clone(),
                None,
            )
        } else {
            let descriptor_path = args
                .full_gspec
                .as_deref()
                .context("--full-gspec descriptor is required")?;
            let selected_dataset = args
                .dataset
                .as_deref()
                .context("--dataset is required with --full-gspec")?;
            let provenance_path = args
                .reference_provenance
                .as_deref()
                .context("--reference-provenance is required with --full-gspec")?;
            let descriptor_source_path = paths_input(root, descriptor_path)?;
            let descriptor = read_json_object(&descriptor_source_path)?;
            ensure!(
                descriptor.get("format").and_then(Value::as_str)
                    == Some("identitybench.v5.native-public-corpus.v1"),
                "legacy prepared 1702-case corpora are archival and cannot be frozen for current G-SPEC; use `conformance prepare --cases`"
            );
            let selected_dataset = paths_input(root, selected_dataset)?;
            let proposed_prepared_dir = out_dir.join("prepared-corpus");
            let descriptor_value = Value::Object(descriptor.clone());
            let prepared = prepare_native_gspec_corpus(
                root,
                &selected_dataset,
                &descriptor_value,
                &proposed_prepared_dir,
            )?;
            let cases = prepared
                .get("cases")
                .and_then(Value::as_array)
                .context("native G-SPEC preparation did not return cases")?
                .clone();
            ensure!(
                !cases.is_empty(),
                "native public G-SPEC corpus must contain cases"
            );
            let corpus = prepared
                .get("corpus")
                .cloned()
                .context("native G-SPEC preparation omitted the corpus binding")?;
            let dataset_bindings = prepared
                .get("dataset_bindings")
                .and_then(Value::as_array)
                .context("native G-SPEC preparation omitted dataset bindings")?
                .clone();
            let provenance = read_json_object(&paths_input(root, provenance_path)?)?;
            let prepared_dir = prepared
                .get("descriptor_path")
                .and_then(Value::as_str)
                .and_then(|path| {
                    crate::paths::resolve(root, Path::new(path))
                        .parent()
                        .map(Path::to_path_buf)
                })
                .or_else(|| descriptor_source_path.parent().map(Path::to_path_buf))
                .unwrap_or(proposed_prepared_dir);
            fs::create_dir_all(&out_dir)?;
            (
                cases,
                Some(corpus),
                Some(dataset_bindings),
                Some(provenance),
                out_dir.join("frozen"),
                Some(prepared_dir),
            )
        };

    let request = NativeFreezeRequest {
        repository_root: root.to_path_buf(),
        out_dir: freeze_dir.clone(),
        cases,
        full_spec_executable,
        agent_view_executable,
        native_build: current_native_build_identity(root)?,
        corpus,
        dataset_bindings,
        reference_provenance,
    };
    let mut result = freeze_native_conformance(request)?;
    if let Some(object) = result.as_object_mut() {
        object.insert(
            "freeze_path".to_owned(),
            json!(freeze_dir.join("freeze.json")),
        );
        if let Some(prepared_dir) = prepared_dir {
            object.insert("prepared_corpus_dir".to_owned(), json!(prepared_dir));
        }
    }
    Ok(result)
}

fn run(root: &Path, args: RunArgs) -> Result<Value> {
    ensure!(
        args.timeout_ms > 0,
        "--timeout-ms must be greater than zero"
    );
    let freeze_path = paths_input(root, &args.freeze)?;
    let freeze = read_json_object(&freeze_path)?;
    let trusted_identity = current_native_build_identity(root)?;
    verify_frozen_build_identity(&freeze, &trusted_identity)?;
    Ok(run_native_conformance(
        &freeze_path,
        args.timeout_ms,
        &trusted_identity,
    )?)
}

fn bind(root: &Path, args: BindArgs) -> Result<Value> {
    let freeze_path = paths_input(root, &args.freeze)?;
    let freeze = read_json_object(&freeze_path)?;
    let trusted_identity = current_native_build_identity(root)?;
    verify_frozen_build_identity(&freeze, &trusted_identity)?;
    let execution_path = paths_input(root, &args.execution)?;
    let receipt_path = crate::paths::resolve(root, &args.out);
    Ok(bind_native_conformance(
        &freeze_path,
        &execution_path,
        &receipt_path,
        &args.suite,
        &trusted_identity,
    )?)
}

fn verify(root: &Path, args: VerifyArgs) -> Result<Value> {
    // Pass the lexical path intact so the verifier can reject symlinked parents
    // before canonicalization or reading the receipt bytes.
    let receipt_path = crate::paths::resolve(root, &args.receipt);
    let trusted_identity = current_native_build_identity(root)?;
    Ok(validate_native_conformance_receipt(
        &receipt_path,
        root,
        &trusted_identity,
    )?)
}

fn verify_frozen_build_identity(
    freeze: &Map<String, Value>,
    trusted_identity: &Value,
) -> Result<()> {
    let expected = freeze
        .get("native_build")
        .context("freeze does not bind a native build identity")?;
    ensure!(
        expected == trusted_identity,
        "current identitybench source, toolchain, or executable differs from the frozen build"
    );
    Ok(())
}

fn paths_input(root: &Path, value: &Path) -> Result<PathBuf> {
    crate::paths::provenance_input(root, value)
}

fn require_new_output(path: &Path) -> Result<()> {
    match fs::symlink_metadata(path) {
        Ok(_) => anyhow::bail!("conformance output path already exists"),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(error.into()),
    }
}

fn read_json_object(path: &Path) -> Result<Map<String, Value>> {
    let raw = fs::read(path).with_context(|| format!("read JSON input {}", path.display()))?;
    let value = ib_v5_conformance::strict_json_loads(&raw)?;
    value
        .as_object()
        .cloned()
        .context("JSON input must be an object")
}

fn read_canonical_jsonl(path: &Path) -> Result<Vec<Value>> {
    let raw = fs::read(path).with_context(|| format!("read case JSONL {}", path.display()))?;
    let values = ib_v5_conformance::strict_jsonl_loads(&raw)?;
    ensure!(
        !values.is_empty(),
        "case JSONL must contain at least one PublicCase"
    );
    let mut canonical = Vec::new();
    for value in &values {
        canonical.extend_from_slice(&ib_v5_conformance::canonical_json_bytes(value)?);
        canonical.push(b'\n');
    }
    ensure!(
        raw == canonical,
        "case input must be canonical JSONL with a final newline"
    );
    Ok(values)
}

fn ensure_output_separate_from_draft5(root: &Path, output: &Path) -> Result<()> {
    let canonical_root = root.canonicalize()?;
    ensure!(
        canonical_future_path(output)?.starts_with(&canonical_root),
        "conformance --out must be inside the repository root"
    );
    for suite in ["v5_dev", "v5_lite", "v5_core", "v5_full"] {
        let dataset = crate::paths::default_dataset(root, suite);
        if dataset.is_dir() {
            crate::paths::ensure_output_separate(output, &dataset)?;
        }
    }
    Ok(())
}

fn canonical_future_path(path: &Path) -> Result<PathBuf> {
    ensure!(
        !path
            .components()
            .any(|component| matches!(component, std::path::Component::ParentDir)),
        "conformance output paths cannot contain '..'"
    );
    let mut existing = path;
    let mut missing = Vec::new();
    loop {
        match fs::symlink_metadata(existing) {
            Ok(metadata) => {
                ensure!(
                    !metadata.file_type().is_symlink(),
                    "conformance output paths cannot traverse symlinks"
                );
                ensure!(
                    metadata.is_dir() || missing.is_empty(),
                    "conformance output parent is not a directory"
                );
                let mut canonical = existing.canonicalize()?;
                for name in missing.iter().rev() {
                    canonical.push(name);
                }
                return Ok(canonical);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                let name = existing
                    .file_name()
                    .context("conformance output path has no existing ancestor")?;
                missing.push(name.to_owned());
                existing = existing
                    .parent()
                    .context("conformance output path has no parent")?;
            }
            Err(error) => return Err(error.into()),
        }
    }
}

fn hash_executable_bytes(path: &Path) -> Result<String> {
    let bytes =
        fs::read(path).with_context(|| format!("read evaluator executable {}", path.display()))?;
    Ok(ib_v5_conformance::sha256_bytes(&bytes))
}

fn diagnostic_reference_provenance(full_sha: &str, agent_sha: &str) -> Map<String, Value> {
    let entry = |slot: &str, digest: &str| {
        json!({
            "qualification": "diagnostic_fixture",
            "source_profile": "operator_supplied_executable_bytes",
            "author_origin": "not_independently_qualified_for_gspec",
            "task_id": "diagnostic-native-conformance",
            "source_closure_sha256": digest,
            "evaluator_slot": slot,
        })
    };
    Map::from_iter([
        (
            "full_spec_blind".to_owned(),
            entry("full_spec_blind", full_sha),
        ),
        (
            "agent_view_only".to_owned(),
            entry("agent_view_only", agent_sha),
        ),
    ])
}
