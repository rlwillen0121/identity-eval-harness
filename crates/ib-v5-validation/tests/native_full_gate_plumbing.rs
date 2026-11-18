//! Synthetic plumbing proof for the strict native full G-SPEC gate.
//!
//! This test deliberately derives its reference streams from the Rust production evaluator.
//! Its isolated artifacts are named and labelled as synthetic fixtures, and this test is not
//! evidence of independent or blind conformance.

use ib_v5_baselines::{obs_strategy_q1, obs_strategy_q2};
use ib_v5_conformance::process::{
    NativeFreezeRequest, bind_native_conformance, freeze_native_conformance, run_native_conformance,
};
use ib_v5_conformance::production::{
    evaluate_frozen_case, evaluate_shared_input_with_native_context,
};
use ib_v5_conformance::shared::{
    CoverageRequirement, PINNED_PUBLIC_SCHEMA_SHA256, PROTOCOL, canonical_json_bytes, freeze_case,
    sha256_bytes, verify_native_complete_coverage,
};
use ib_v5_generator::{DatasetArtifactsV5, load_v5_dataset};
use ib_v5_schemas::{
    EpisodeV5,
    normative_contract::{
        V5_APPROVED_ADDENDUM_SHA256, V5_APPROVED_DIRECTIVE_SHA256, V5_BASE_SPEC_SHA256,
        V5_DRAFT5_SPEC_SHA256,
    },
};
use ib_v5_validation::native_receipt::{
    capture_native_build_identity, prepare_native_public_corpus,
};
use ib_v5_validation::{GateStatusV5, ValidationOptions, repository_root, validate_v5_dataset};
use serde_json::{Map, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use tempfile::TempDir;

const ACTIVE_SUITES: [&str; 4] = ["v5_dev", "v5_lite", "v5_core", "v5_full"];
const NATIVE_CORPUS_FORMAT: &str = "identitybench.v5.native-public-corpus.v1";

#[test]
fn synthetic_dev_native_context_matches_all_sealed_phase_gold() {
    let root = repository_root();
    let dataset = load_v5_dataset(&root.join("datasets/v5_draft5/v5_dev"))
        .expect("load active sealed DEV dataset");
    assert_eq!(dataset.episodes.len(), 12, "active DEV episode count");
    assert_eq!(dataset.oracles.len(), 12, "active DEV oracle count");
    for (episode, oracle) in dataset.episodes.iter().zip(&dataset.oracles) {
        assert_eq!(episode.episode_id, oracle.episode_id);
        let p1 = render_public_episode(episode, 1, None);
        let p2 = render_public_episode(episode, 2, Some(p1.clone()));
        for (phase, public_episode, expected_mutations) in [
            (
                1_i64,
                p1,
                serde_json::to_value(&oracle.gold_p1_mutations).unwrap(),
            ),
            (
                2_i64,
                p2,
                serde_json::to_value(&oracle.gold_p2_mutations).unwrap(),
            ),
        ] {
            let case_id = format!("v5_dev/{}/p{phase}/derive_phase", episode.episode_id);
            let case = make_case(
                &case_id,
                "derive_phase",
                "v5_dev",
                episode,
                phase,
                public_episode,
                json!({}),
            );
            let result = evaluate_shared_input_with_native_context(&case, episode)
                .unwrap_or_else(|error| panic!("derive DEV phase {phase} for {case_id}: {error}"));
            assert_eq!(result["status"], "ok", "{case_id}: {result:?}");
            let actual = &result["payload"]["mutations"];
            let actual_count = actual
                .as_array()
                .expect("actual DEV derive mutations")
                .len();
            let expected_count = expected_mutations
                .as_array()
                .expect("sealed DEV derive mutations")
                .len();
            assert_eq!(
                mutation_map(actual).len(),
                actual_count,
                "DEV actual mutation keys are unique for {case_id}"
            );
            assert_eq!(
                mutation_map(&expected_mutations).len(),
                expected_count,
                "DEV sealed mutation keys are unique for {case_id}"
            );
            let actual_bytes =
                canonical_json_bytes(actual).expect("canonical DEV actual wire array");
            let expected_bytes =
                canonical_json_bytes(&canonical_keyed_mutations(&expected_mutations))
                    .expect("canonical wire-ordered DEV sealed gold");
            assert!(
                actual_bytes == expected_bytes,
                "DEV-only native context/gold diff for {case_id}: {}",
                mutation_diff_summary(actual, &expected_mutations)
            );
        }
    }
}

#[test]
fn synthetic_native_full_gate_passes_and_rejects_bound_mutations() {
    let root = repository_root();
    let artifact_root = root.join("reports/v5/conformance");
    let temp = tempfile::Builder::new()
        .prefix(".tmp-native-full-gate-plumbing-")
        .tempdir_in(&artifact_root)
        .expect("create isolated synthetic gate artifacts");
    let artifacts = build_synthetic_full_fixture(&root, &temp);

    let report = validate_report(&root, &artifacts.receipt_path, &artifacts.native_build);
    let gate = report
        .gate_results
        .iter()
        .find(|gate| gate.gate_id == "G-SPEC-01")
        .expect("G-SPEC gate is present");
    assert_eq!(gate.status, GateStatusV5::Pass, "{:?}", gate.errors);
    assert_eq!(
        gate.evidence.get("native_corpus_format"),
        Some(&json!(NATIVE_CORPUS_FORMAT))
    );
    assert_eq!(
        gate.evidence
            .get("native_public_case_count")
            .and_then(Value::as_u64),
        Some(artifacts.case_count as u64)
    );
    assert_eq!(
        gate.evidence
            .get("native_public_phase_target_count")
            .and_then(Value::as_u64),
        Some(artifacts.phase_target_count as u64)
    );
    let observed = gate.evidence["observed_coverage"]
        .as_object()
        .expect("strict native gate records observed full coverage");
    for (field, expected) in [
        ("families", 12),
        ("anchor_labels", 39),
        ("phase_targets", artifacts.phase_target_count),
        ("diagnostics", 43),
        ("rules", 26),
        ("safety_cases", 15),
        ("safety_kinds", 12),
        ("safety_truth_pairs", 4),
        ("trace_case_ids", 1),
        ("snapshot_cells", 6),
    ] {
        assert_eq!(
            observed[field].as_array().map(Vec::len),
            Some(expected),
            "observed coverage for {field}"
        );
    }

    // Each mutation stays in the test-only temp root. No active sealed dataset is edited.
    reject_after_artifact_mutation(&root, &artifacts, "missing-public-case", |paths| {
        let bytes = fs::read(&paths.public_cases).expect("read synthetic public cases");
        let mut lines = bytes.split_inclusive(|byte| *byte == b'\n');
        let _ = lines.next().expect("at least one case row");
        fs::write(
            &paths.public_cases,
            lines.flatten().copied().collect::<Vec<_>>(),
        )
        .expect("remove one public case row");
    });
    reject_after_artifact_mutation(&root, &artifacts, "extra-public-case", |paths| {
        let mut bytes = fs::read(&paths.public_cases).expect("read synthetic public cases");
        let first = bytes
            .split_inclusive(|byte| *byte == b'\n')
            .next()
            .expect("at least one case row");
        let first_row = first.to_vec();
        bytes.extend_from_slice(&first_row);
        fs::write(&paths.public_cases, bytes).expect("append a duplicate public case row");
    });
    reject_after_rehashed_case_index_mutation(&root, &artifacts);
    reject_after_artifact_mutation(&root, &artifacts, "audit", |paths| {
        let bytes = fs::read(&paths.read_audit).expect("read synthetic read audit");
        let mut value: Value = serde_json::from_slice(&bytes).expect("parse audit");
        value["reads"] = json!([]);
        fs::write(&paths.read_audit, canonical_json_bytes(&value).unwrap())
            .expect("tamper with audit reads");
    });
    reject_after_receipt_mutation(&root, &artifacts, "dataset-binding", |receipt| {
        receipt["dataset_bindings"][0]["seal"]["sha256"] = json!("0".repeat(64));
    });
    reject_after_receipt_mutation(&root, &artifacts, "legacy-corpus-format", |receipt| {
        receipt["corpus"]["format"] = json!("identitybench.v5.native-prepared-gspec-corpus.v1");
    });
    reject_after_artifact_mutation(&root, &artifacts, "frozen-case", |paths| {
        let bytes = fs::read(&paths.frozen_cases).expect("read frozen case stream");
        let mut lines = bytes.split_inclusive(|byte| *byte == b'\n');
        let _ = lines.next().expect("at least one frozen case row");
        fs::write(
            &paths.frozen_cases,
            lines.flatten().copied().collect::<Vec<_>>(),
        )
        .expect("remove a frozen case row");
    });
    reject_after_result_stream_mutation(
        &root,
        &artifacts,
        "missing-result-row",
        &["full_spec_blind"],
        |rows| {
            rows.get_mut("full_spec_blind")
                .expect("full-spec rows")
                .pop()
                .expect("remove one full-spec result row");
        },
    );
    reject_after_result_stream_mutation(
        &root,
        &artifacts,
        "extra-result-row",
        &["full_spec_blind"],
        |rows| {
            let rows = rows.get_mut("full_spec_blind").expect("full-spec rows");
            let duplicate = rows.first().cloned().expect("a full-spec result row");
            rows.push(duplicate);
        },
    );
    reject_after_result_stream_mutation(
        &root,
        &artifacts,
        "mismatched-result-payload",
        &["full_spec_blind"],
        |rows| {
            let row = rows
                .get_mut("full_spec_blind")
                .expect("full-spec rows")
                .iter_mut()
                .find(|row| row["case_id"] == "synthetic/diag/bind_b_citation_public_token")
                .expect("BIND-V5-01b public diagnostic result");
            row["payload"]["first_failing_stage"] = json!(5);
        },
    );
    reject_after_result_stream_mutation(
        &root,
        &artifacts,
        "underdefined-result-pair",
        &["full_spec_blind", "agent_view_only"],
        |rows| {
            for slot in ["full_spec_blind", "agent_view_only"] {
                let row = rows
                    .get_mut(slot)
                    .expect("evaluator rows")
                    .iter_mut()
                    .find(|row| {
                        row["case_id"]
                            .as_str()
                            .is_some_and(|case_id| case_id.starts_with("synthetic/safety/kind/"))
                    })
                    .expect("synthetic safety result row");
                let case_id = row["case_id"].clone();
                row["status"] = json!("underdefined");
                row["gaps"] = json!([{
                    "code": "UNDERDEFINED_PUBLIC_INPUT",
                    "operation": "safety",
                    "case_id": case_id,
                    "path": "/stimulus/recipe",
                    "detail": "synthetic result stream is intentionally underdefined"
                }]);
            }
        },
    );

    if std::env::var_os("IDENTITYBENCH_CLI").is_some() {
        assert_cli_full_lifecycle(&root, &artifacts);
    }
}

struct SyntheticFixture {
    receipt_path: PathBuf,
    native_build: Value,
    case_count: usize,
    phase_target_count: usize,
    paths: FixturePaths,
    raw_cases_path: PathBuf,
    full_spec_executable: PathBuf,
    agent_view_executable: PathBuf,
    reference_provenance_path: PathBuf,
}

#[derive(Clone)]
struct FixturePaths {
    public_cases: PathBuf,
    case_index: PathBuf,
    read_audit: PathBuf,
    frozen_cases: PathBuf,
}

fn build_synthetic_full_fixture(root: &Path, temp: &TempDir) -> SyntheticFixture {
    let datasets: BTreeMap<String, DatasetArtifactsV5> = ACTIVE_SUITES
        .iter()
        .map(|suite| {
            let path = root.join("datasets/v5_draft5").join(suite);
            (
                (*suite).to_owned(),
                load_v5_dataset(&path).expect("load active sealed Draft 5 dataset"),
            )
        })
        .collect();
    let mut phase_views = BTreeMap::new();
    let mut cases = Vec::<Value>::new();
    for (suite, dataset) in &datasets {
        for episode in &dataset.episodes {
            let p1 = render_public_episode(episode, 1, None);
            let p2 = render_public_episode(episode, 2, Some(p1.clone()));
            phase_views.insert((suite.clone(), episode.episode_id.clone(), 1), p1.clone());
            phase_views.insert((suite.clone(), episode.episode_id.clone(), 2), p2.clone());
            for (phase, view) in [(1_i64, p1), (2_i64, p2)] {
                cases.push(make_case(
                    &format!("{suite}/{}/p{phase}/derive_phase", episode.episode_id),
                    "derive_phase",
                    suite,
                    episode,
                    phase,
                    view,
                    json!({}),
                ));
            }
        }
    }

    let dev = datasets.get("v5_dev").expect("v5_dev dataset");
    let dev_episodes = &dev.episodes;
    let plan_for = |episode_index: usize, phase: i64| -> Vec<Value> {
        let episode = dev_episodes.get(episode_index).expect("DEV episode index");
        let case_id = format!("v5_dev/{}/p{phase}/derive_phase", episode.episode_id);
        let case = cases
            .iter()
            .find(|case| case["case_id"] == case_id)
            .expect("baseline derive case");
        let result = evaluate_shared_input_with_native_context(case, episode)
            .expect("derive synthetic reference plan with episode context");
        result["payload"]["mutations"]
            .as_array()
            .expect("derived mutations")
            .clone()
    };
    let mut plans = BTreeMap::<(usize, i64), Vec<Value>>::new();
    for &(episode_index, phase) in &[
        (0, 1),
        (1, 1),
        (7, 1),
        (8, 1),
        (0, 2),
        (1, 2),
        (2, 2),
        (3, 2),
        (6, 2),
        (8, 2),
        (9, 2),
    ] {
        plans.insert((episode_index, phase), plan_for(episode_index, phase));
    }

    append_diagnostic_cases(&mut cases, dev_episodes, &phase_views, &plans);
    append_safety_cases(&mut cases, dev_episodes, &phase_views, &plans);
    append_literal_trace_case(&mut cases, dev_episodes, &phase_views, &plans);
    cases.sort_by(|left, right| left["case_id"].as_str().cmp(&right["case_id"].as_str()));

    // The repository's native writer owns public-case normalization and every seal-derived
    // count, digest, target set, family map, and coverage ledger. This test contributes only
    // fresh synthetic cases, never expected/gold metadata, to that boundary.
    let raw_cases_path = temp.path().join("synthetic-input-cases.jsonl");
    fs::write(&raw_cases_path, jsonl(&cases)).expect("write synthetic public input cases");
    let selected_dataset = root.join("datasets/v5_draft5/v5_dev");
    let prepared = prepare_native_public_corpus(
        root,
        &selected_dataset,
        &raw_cases_path,
        &temp.path().join("prepared-native-corpus"),
    )
    .unwrap_or_else(|error| {
        panic!(
            "prepare native public corpus from synthetic cases: {}",
            error.to_string().chars().take(6_000).collect::<String>()
        )
    });
    cases = prepared["cases"]
        .as_array()
        .expect("prepared normalized cases")
        .clone();
    let corpus = prepared["descriptor"]["corpus"].clone();
    let dataset_bindings = prepared["dataset_bindings"]
        .as_array()
        .expect("prepared active dataset bindings")
        .clone();
    assert_eq!(corpus["format"], json!(NATIVE_CORPUS_FORMAT));
    let public_cases_ref = corpus["public_cases"].clone();
    let case_index_ref = corpus["case_index"].clone();
    let manifest = read_json(&resolve_ref_path(root, &corpus["manifest"]));
    let case_count = cases.len();
    let phase_target_count = manifest["phase_target_count"]
        .as_u64()
        .expect("prepared trusted phase-target count") as usize;

    let source_dir = temp.path().join("synthetic_source");
    fs::create_dir_all(&source_dir).expect("create synthetic source dir");
    let (full_spec_executable, full_responses, full_provenance, full_audit) =
        make_synthetic_reference(
            "full_spec_blind",
            &source_dir,
            root,
            temp.path(),
            &public_cases_ref,
        );
    let (agent_view_executable, agent_responses, agent_provenance, agent_audit) =
        make_synthetic_reference(
            "agent_view_only",
            &source_dir,
            root,
            temp.path(),
            &public_cases_ref,
        );
    let reference_provenance_path = temp.path().join("synthetic-reference-provenance.json");
    fs::write(
        &reference_provenance_path,
        canonical_json_bytes(&json!({
            "full_spec_blind":full_provenance.clone(),
            "agent_view_only":agent_provenance.clone(),
        }))
        .expect("serialize synthetic reference provenance"),
    )
    .expect("write synthetic reference provenance");
    let production_rows = production_rows(&cases, &datasets);
    preflight_synthetic_result_coverage(
        &cases,
        &production_rows,
        &datasets,
        &read_json(&resolve_ref_path(root, &corpus["coverage_ledger"])),
    );
    fs::write(
        &full_responses,
        jsonl(&results_for_slot(&production_rows, "full_spec_blind")),
    )
    .expect("write synthetic full-spec output stream");
    fs::write(
        &agent_responses,
        jsonl(&results_for_slot(&production_rows, "agent_view_only")),
    )
    .expect("write synthetic agent-view output stream");
    let read_audit = full_audit;

    let source_identity = source_identity(root);
    let exe =
        std::env::current_exe().expect("resolve test executable for synthetic build identity");
    let native_build = capture_native_build_identity(
        root,
        &source_identity,
        &json!({"fixture_toolchain": "synthetic-test-only"}),
        &exe,
    )
    .expect("capture test-only build identity");

    let freeze_dir = temp.path().join("freeze");
    let freeze = freeze_native_conformance(NativeFreezeRequest {
        repository_root: root.to_path_buf(),
        out_dir: freeze_dir.clone(),
        cases: cases.clone(),
        full_spec_executable: full_spec_executable.clone(),
        agent_view_executable: agent_view_executable.clone(),
        native_build: native_build.clone(),
        corpus: Some(corpus),
        dataset_bindings: Some(dataset_bindings),
        reference_provenance: Some(Map::from_iter([
            ("full_spec_blind".to_owned(), full_provenance),
            ("agent_view_only".to_owned(), agent_provenance),
        ])),
    })
    .expect("freeze synthetic full-gate fixture");
    assert_eq!(freeze["proof_scope"], json!("full_shared_conformance"));
    let freeze_path = freeze_dir.join("freeze.json");
    let execution = run_native_conformance(&freeze_path, 30_000, &native_build)
        .expect("run synthetic external reference streams");
    assert_eq!(execution["evaluators"].as_object().unwrap().len(), 2);
    let receipt_path = temp.path().join("native_receipt.json");
    let _receipt = bind_native_conformance(
        &freeze_path,
        &freeze_dir.join("execution.json"),
        &receipt_path,
        "v5_dev",
        &native_build,
    )
    .expect("bind synthetic full-gate receipt");
    let receipt_value: Value =
        serde_json::from_slice(&fs::read(&receipt_path).expect("read bound synthetic receipt"))
            .expect("parse synthetic receipt");
    fs::write(&receipt_path, canonical_json_bytes(&receipt_value).unwrap())
        .expect("canonicalize synthetic receipt");

    let frozen_cases_path = resolve_ref_path(root, &read_json(&freeze_path)["cases"]);
    let paths = FixturePaths {
        public_cases: resolve_ref_path(root, &public_cases_ref),
        case_index: resolve_ref_path(root, &case_index_ref),
        read_audit,
        frozen_cases: frozen_cases_path,
    };
    let _ = agent_audit;
    SyntheticFixture {
        receipt_path,
        native_build,
        case_count,
        phase_target_count,
        paths,
        raw_cases_path,
        full_spec_executable,
        agent_view_executable,
        reference_provenance_path,
    }
}

fn make_case(
    case_id: &str,
    operation: &str,
    suite: &str,
    episode: &EpisodeV5,
    phase: i64,
    public_episode: Map<String, Value>,
    stimulus: Value,
) -> Value {
    json!({
        "protocol": PROTOCOL,
        "case_id": case_id,
        "operation": operation,
        "suite_id": suite,
        "phase_index": phase,
        "public_episode": Value::Object(public_episode),
        "rendered_card": episode.policy_card.rendered_rules_markdown,
        "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
        "stimulus": stimulus,
    })
}

/// Exact native public phase view, computed from current DEV typed inputs. This helper is
/// intentionally local to the synthetic plumbing test and does not create blind expectations.
fn render_public_episode(
    episode: &EpisodeV5,
    phase: i64,
    carryover: Option<Map<String, Value>>,
) -> Map<String, Value> {
    let mut public = carryover.unwrap_or_default();
    if phase == 1 {
        public.insert("episode_id".to_owned(), json!(episode.episode_id));
        public.insert(
            "policy_card".to_owned(),
            serde_json::to_value(&episode.policy_card).expect("serialize current policy card"),
        );
    }
    let (manifest, slices) = if phase == 1 {
        (&episode.phase1_manifest, &episode.phase1_slices)
    } else {
        (&episode.phase2_base_manifest, &episode.phase2_slices)
    };
    let q1 = obs_strategy_q1(manifest, &episode.policy_card, phase)
        .expect("compute legal public Q1 view");
    let q1_slices = q1
        .iter()
        .map(|slice_id| slices.get(slice_id).expect("Q1 slice exists").clone())
        .collect::<Vec<_>>();
    let q2 = obs_strategy_q2(
        manifest,
        &q1_slices,
        &manifest.work_queue,
        &episode.policy_card,
        phase,
    )
    .expect("compute legal public Q2 view");
    let selected = q1.iter().chain(&q2).cloned().collect::<BTreeSet<_>>();
    let projected = selected
        .into_iter()
        .map(|slice_id| {
            (
                slice_id.clone(),
                serde_json::to_value(slices.get(&slice_id).expect("selected slice exists"))
                    .expect("serialize selected public slice"),
            )
        })
        .collect::<Map<_, _>>();
    if phase == 1 {
        public.insert(
            "phase1_manifest".to_owned(),
            serde_json::to_value(&episode.phase1_manifest).expect("serialize P1 manifest"),
        );
        public.insert("phase1_slices".to_owned(), Value::Object(projected));
        public.insert("q1_slice_ids_p1".to_owned(), json!(q1));
        public.insert("q2_slice_ids_p1".to_owned(), json!(q2));
    } else {
        public.insert(
            "phase2_base_manifest".to_owned(),
            serde_json::to_value(&episode.phase2_base_manifest).expect("serialize P2 manifest"),
        );
        public.insert("phase2_slices".to_owned(), Value::Object(projected));
        public.insert("q1_slice_ids_p2".to_owned(), json!(q1));
        public.insert("q2_slice_ids_p2".to_owned(), json!(q2));
    }
    public
}

fn append_diagnostic_cases(
    cases: &mut Vec<Value>,
    episodes: &[EpisodeV5],
    views: &BTreeMap<(String, String, i64), Map<String, Value>>,
    plans: &BTreeMap<(usize, i64), Vec<Value>>,
) {
    let plan_index = |episode: usize, phase: i64, kind: &str, target: Option<&str>| -> usize {
        plans[&(episode, phase)]
            .iter()
            .position(|row| {
                row["mutation_kind"].as_str() == Some(kind)
                    && target.is_none_or(|value| row["target_entity_id"].as_str() == Some(value))
            })
            .unwrap_or_else(|| panic!("missing synthetic plan target {kind}/{target:?}"))
    };
    let patch_recipe = |patches: Vec<Value>| {
        json!({
            "recipe": {
                "candidate_source": "independent_base",
                "base_scope": "phase_plan",
                "patches": patches,
            }
        })
    };

    let conn0 = plan_index(0, 1, "CONNECTOR_SYNC_MUTATION", None);
    let conn7 = plan_index(7, 1, "CONNECTOR_SYNC_MUTATION", None);
    let hr0 = plan_index(0, 1, "HR_SOURCE_MAPPING_MUTATION", None);
    let acct01 = plan_index(0, 1, "ACCOUNT_RECON_MUTATION", Some("acct_01"));
    let acct02 = plan_index(0, 1, "ACCOUNT_RECON_MUTATION", Some("acct_02"));
    let acct14 = plan_index(1, 1, "ACCOUNT_RECON_MUTATION", Some("acct_04"));
    let nhi0 = plan_index(0, 1, "NHI_GOVERNANCE_MUTATION", None);
    let nhi1 = plan_index(1, 1, "NHI_GOVERNANCE_MUTATION", None);
    let nhi8 = plan_index(8, 1, "NHI_GOVERNANCE_MUTATION", None);
    let agt0 = plan_index(0, 1, "AGENT_REGISTRY_MUTATION", None);
    let ciem0 = plan_index(0, 1, "CLOUD_CIEM_MUTATION", None);
    let jml0 = plan_index(0, 1, "JML_WORKFLOW_MUTATION", None);
    let cae0 = plan_index(0, 2, "CAE_CONTAINMENT_MUTATION", None);
    let cae6 = plan_index(6, 2, "CAE_CONTAINMENT_MUTATION", None);
    let uar0 = plan_index(0, 2, "UAR_CAMPAIGN_MUTATION", Some("camp_02"));
    let uar2 = plan_index(2, 2, "UAR_CAMPAIGN_MUTATION", Some("camp_01"));
    let uar6 = plan_index(6, 2, "UAR_CAMPAIGN_MUTATION", Some("camp_04"));
    let uar9 = plan_index(9, 2, "UAR_CAMPAIGN_MUTATION", Some("camp_03"));
    let jit01 = plan_index(0, 2, "JIT_DECISION_MUTATION", Some("req_01"));
    let jit02 = plan_index(0, 2, "JIT_DECISION_MUTATION", Some("req_02"));
    let jit03 = plan_index(0, 2, "JIT_DECISION_MUTATION", Some("req_03"));
    let del02 = plan_index(0, 2, "AGENT_DELEGATION_MUTATION", Some("dlreq_02"));
    let del03 = plan_index(0, 2, "AGENT_DELEGATION_MUTATION", Some("dlreq_03"));
    let del11 = plan_index(1, 2, "AGENT_DELEGATION_MUTATION", Some("dlreq_01"));
    let del81 = plan_index(8, 2, "AGENT_DELEGATION_MUTATION", Some("dlreq_01"));
    let opt0 = plan_index(0, 1, "OPTIMIZATION_MUTATION", None);
    let objective = plans[&(0, 1)][opt0]["objective_value"]
        .as_i64()
        .expect("DEV objective");
    let cae_exposure = plans[&(0, 2)][cae0]["cae_computed_exposure_s"]
        .as_i64()
        .expect("DEV CAE exposure");
    let uar_vote = plans[&(0, 2)][uar0]["uar_decisions"][0][1]
        .as_str()
        .expect("DEV UAR decision");
    let uar_flip = if uar_vote == "CERTIFY" {
        "REVOKE"
    } else {
        "CERTIFY"
    };
    let jit_approve = |index: usize| {
        patch_recipe(vec![
            json!({"op":"replace","path":format!("/{index}/jit_decision"),"value":"APPROVE_JIT"}),
            json!({"op":"replace","path":format!("/{index}/approver_account_id"),"value":"acct_01"}),
            json!({"op":"replace","path":format!("/{index}/approved_ttl_s"),"value":900}),
            json!({"op":"replace","path":format!("/{index}/external_sync_status"),"value":"SYNC_GRANTED"}),
        ])
    };

    // These are the existing DEV typed recipe stimuli, reconstructed in this file only
    // so the positive strict-gate plumbing test covers the full declared diagnostic set.
    let specs: Vec<(&str, usize, i64, Value, i64)> = vec![
        (
            "COMPLETENESS_VIOLATION",
            0,
            1,
            json!({"submitted_plan":[]}),
            1,
        ),
        (
            "SCHEMA_VALIDATION_ERROR",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":"/0/account_action","value":"UNKNOWN_ACTION"}),
            ]),
            2,
        ),
        (
            "PRECONDITION_MISMATCH",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":"/0/if_match","value":["acct_01@0000000000000000:1"]}),
            ]),
            3,
        ),
        (
            "CITATION_MISMATCH",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":"/0/cite","value":["BIND-V5-01a:ev_00001"]}),
            ]),
            4,
        ),
        (
            "CONNECTOR_HALT_OR_FREEZE",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{conn0}/halted_fail_closed"),"value":true}),
                json!({"op":"replace","path":format!("/{conn0}/halt_reason"),"value":"HALT_AUTH_OR_TLS"}),
                json!({"op":"replace","path":format!("/{conn0}/remediated_spoke_drift"),"value":[]}),
            ]),
            5,
        ),
        (
            "CONNECTOR_RECONCILIATION_SET",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{conn0}/reconciled_account_ids"),"value":[]}),
            ]),
            5,
        ),
        (
            "SPOKE_DRIFT_REMEDIATION",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{conn0}/remediated_spoke_drift"),"value":[]}),
            ]),
            5,
        ),
        (
            "ITSM_MANUAL_FULFILLMENT_MISMATCH",
            7,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{conn7}/itsm_ticket_actions"),"value":[]}),
            ]),
            5,
        ),
        (
            "HR_FEED_AUTHORITY_MISMATCH",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{hr0}/accepted_feed_ids"),"value":[]}),
                json!({"op":"replace","path":format!("/{hr0}/rejected_feed_ids"),"value":["hfeed_01","hfeed_03"]}),
            ]),
            5,
        ),
        (
            "HR_CORRELATION_OR_PRECEDENCE_MISMATCH",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{hr0}/resolved_person_statuses"),"value":[["pers_03","TERMINATED"]]}),
            ]),
            5,
        ),
        (
            "BINDING_OR_ENGAGEMENT_CONFLICT",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{acct01}/account_action"),"value":"ESCALATE_BINDING_CONFLICT"}),
                json!({"op":"replace","path":format!("/{acct01}/bound_person_id"),"value":null}),
                json!({"op":"replace","path":format!("/{acct01}/bound_engagement_id"),"value":null}),
                json!({"op":"replace","path":format!("/{acct01}/retained_grant_ids"),"value":[]}),
            ]),
            5,
        ),
        (
            "CONV_GRACE_BREACH",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{acct02}/retained_grant_ids"),"value":["grant_02","grant_06"]}),
            ]),
            5,
        ),
        (
            "COMPENSATING_CONTROL_INVALID",
            1,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{acct14}/retained_grant_ids"),"value":["grant_02","grant_07","grant_08"]}),
            ]),
            5,
        ),
        (
            "MOVER_OR_SCOPED_SELF_SOD",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{acct01}/retained_grant_ids"),"value":[]}),
            ]),
            5,
        ),
        (
            "STALE_TELEMETRY_OR_ROTATION_SLA",
            8,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{nhi8}/nhi_action"),"value":"ATTRIBUTE_OWNER"}),
                json!({"op":"replace","path":format!("/{nhi8}/resolved_owner_person_id"),"value":"pers_03"}),
                json!({"op":"replace","path":format!("/{nhi8}/owner_resolution_source"),"value":"parent_nhi_custody"}),
            ]),
            5,
        ),
        (
            "NHI_OVERLAY_ELIGIBILITY_OR_CAP",
            1,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{nhi1}/resolved_owner_person_id"),"value":"pers_04"}),
            ]),
            5,
        ),
        (
            "NHI_OWNER_PRECEDENCE",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{nhi0}/nhi_action"),"value":"ESCALATE"}),
                json!({"op":"replace","path":format!("/{nhi0}/resolved_owner_person_id"),"value":null}),
                json!({"op":"replace","path":format!("/{nhi0}/owner_resolution_source"),"value":null}),
            ]),
            5,
        ),
        (
            "AGENT_ATTESTATION_OR_STALE_DIGEST",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{agt0}/agent_action"),"value":"SUSPEND_UNATTESTED_OR_STALE"}),
                json!({"op":"replace","path":format!("/{agt0}/resolved_sponsor_person_id"),"value":null}),
                json!({"op":"replace","path":format!("/{agt0}/active_autonomous_binding_ids"),"value":[]}),
                json!({"op":"replace","path":format!("/{agt0}/active_hitl_binding_ids"),"value":[]}),
            ]),
            5,
        ),
        (
            "AGENT_SPONSOR_ELIGIBILITY_OR_CAP",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{agt0}/resolved_sponsor_person_id"),"value":"pers_01"}),
            ]),
            5,
        ),
        (
            "AGENT_SCOPE_TIERING_OR_SOD",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{agt0}/active_autonomous_binding_ids"),"value":[]}),
            ]),
            5,
        ),
        (
            "CIEM_ORPHAN_PRINCIPAL_MISMATCH",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{ciem0}/findings/0/remediation_action"),"value":"REVOKE_ORPHAN_CLOUD_PRINCIPAL"}),
            ]),
            5,
        ),
        (
            "CIEM_ESCALATION_PATH_MISMATCH",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{ciem0}/findings/0/escalation_vector"),"value":"AWS_ASSUMEROLE_CHAIN"}),
            ]),
            5,
        ),
        (
            "CIEM_EFFECTIVE_PERMISSION_OR_REMEDIATION",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{ciem0}/findings/0/remediation_action"),"value":"KEEP_COMPLIANT"}),
            ]),
            5,
        ),
        (
            "OPTIMIZATION_INFEASIBLE",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{opt0}/selected_ids"),"value":[]}),
            ]),
            5,
        ),
        (
            "OPTIMIZATION_SUBOPTIMAL",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{opt0}/objective_value"),"value":objective+5}),
            ]),
            5,
        ),
        (
            "JML_BIRTHRIGHT_ZSP_OR_SOD_BREACH",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{jml0}/active_birthright_rule_ids"),"value":[]}),
                json!({"op":"replace","path":format!("/{jml0}/quarantined_birthright_rule_ids"),"value":["brule_01","brule_02","brule_03"]}),
            ]),
            5,
        ),
        (
            "JML_WORKFLOW_SPEC_UNSAFE",
            0,
            1,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{jml0}/authored_spec/no_show_disposition"),"value":"LEAVE_DORMANT"}),
            ]),
            5,
        ),
        (
            "RETRO_APPROVAL_TAINT_MISMATCH",
            6,
            2,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{cae6}/retro_invalidated_ids"),"value":[]}),
            ]),
            5,
        ),
        (
            "CAE_REVOCATION_DECISION",
            0,
            2,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{cae0}/cae_revoke_sessions"),"value":false}),
                json!({"op":"replace","path":format!("/{cae0}/cae_computed_exposure_s"),"value":0}),
                json!({"op":"replace","path":format!("/{cae0}/cae_sla_breached"),"value":false}),
            ]),
            5,
        ),
        (
            "CAE_REFRESH_TREE_EXPOSURE",
            0,
            2,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{cae0}/cae_computed_exposure_s"),"value":cae_exposure+60}),
            ]),
            5,
        ),
        (
            "UAR_BASELINE_OR_SHOCK_FILTER",
            6,
            2,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{uar6}/uar_decisions"),"value":[["item_03","CERTIFY"]]}),
            ]),
            5,
        ),
        (
            "UAR_REVIEWER_COI_OVERRIDE",
            2,
            2,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{uar2}/reviewer_overrides"),"value":[]}),
            ]),
            5,
        ),
        (
            "UAR_INHERITED_STANDING_LOCK",
            9,
            2,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{uar9}/uar_decisions"),"value":[["item_01","REVOKE"],["item_03","CERTIFY"]]}),
            ]),
            5,
        ),
        (
            "QUORUM_OR_CUSTODIAN_DEPLETION",
            0,
            2,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{uar0}/uar_decisions/0/1"),"value":uar_flip}),
            ]),
            5,
        ),
        (
            "JIT_REQUESTER_OR_SCOPE_INELIGIBLE",
            2,
            2,
            jit_approve(plan_index(2, 2, "JIT_DECISION_MUTATION", Some("req_01"))),
            5,
        ),
        ("TICKET_STALE_OR_RETRO_TAINTED", 0, 2, jit_approve(jit03), 5),
        (
            "SCOPED_OR_BREAKGLASS_SOD_COOLDOWN",
            3,
            2,
            jit_approve(plan_index(3, 2, "JIT_DECISION_MUTATION", Some("req_02"))),
            5,
        ),
        ("APPROVER_DISQUALIFIED", 0, 2, jit_approve(jit01), 5),
        (
            "JIT_DUAL_CONTROL_OR_TTL_BOUND",
            0,
            2,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{jit02}/approved_ttl_s"),"value":1800}),
            ]),
            5,
        ),
        (
            "AGENT_DELEGATION_PRINCIPAL_INVALID",
            8,
            2,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{del81}/delegation_decision"),"value":"APPROVE_DELEGATION"}),
                json!({"op":"replace","path":format!("/{del81}/approved_ttl_s"),"value":600}),
            ]),
            5,
        ),
        (
            "AGENT_OBO_INTERSECTION_OR_HOP_BREACH",
            1,
            2,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{del11}/delegation_decision"),"value":"APPROVE_DELEGATION"}),
                json!({"op":"replace","path":format!("/{del11}/approved_ttl_s"),"value":600}),
            ]),
            5,
        ),
        (
            "AGENT_WORKFLOW_OR_SESSION_SOD",
            0,
            2,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{del02}/delegation_decision"),"value":"APPROVE_DELEGATION"}),
                json!({"op":"replace","path":format!("/{del02}/approved_ttl_s"),"value":600}),
            ]),
            5,
        ),
        (
            "AGENT_HITL_COSIGN_OR_TTL_MISMATCH",
            0,
            2,
            patch_recipe(vec![
                json!({"op":"replace","path":format!("/{del03}/approved_ttl_s"),"value":900}),
            ]),
            5,
        ),
    ];
    assert_eq!(specs.len(), 43);
    for (category, episode_index, phase, stimulus, expected_stage) in specs {
        let episode = &episodes[episode_index];
        let view = views[&("v5_dev".to_owned(), episode.episode_id.clone(), phase)].clone();
        let case_id = format!("synthetic/diag43/{category}");
        let case = make_case(
            &case_id,
            "validate_submission",
            "v5_dev",
            episode,
            phase,
            view,
            stimulus,
        );
        let result = evaluate_frozen_case(&freeze_case(&case).unwrap()).unwrap();
        assert_eq!(result.status, "ok", "{category}: {:?}", result.raw_value);
        assert_eq!(
            result.raw_value["payload"]["first_failing_stage"],
            json!(expected_stage),
            "{category}"
        );
        assert_eq!(
            result.raw_value["payload"]["diagnostics"],
            json!([category]),
            "{category}"
        );
        cases.push(case);
    }

    // Public typed negative for the BIND-V5-01b citation branch: preserve the DEV P1 plan,
    // change only the binding citation prefix, and require the evaluator's Stage 4 diagnostic.
    // This checks rule-token coverage plumbing; it does not assert private binding truth.
    let episode = &episodes[0];
    let mut submitted_plan = plans[&(0, 1)].clone();
    let account_row = plan_index(0, 1, "ACCOUNT_RECON_MUTATION", Some("acct_01"));
    let citations = submitted_plan[account_row]["cite"]
        .as_array_mut()
        .expect("DEV account plan has typed citation array");
    let mut replaced_binding_citation = false;
    for citation in citations {
        let token = citation
            .as_str()
            .expect("DEV account plan citation is a string");
        if let Some(evidence_id) = token.strip_prefix("BIND-V5-01a:") {
            *citation = json!(format!("BIND-V5-01b:{evidence_id}"));
            replaced_binding_citation = true;
        }
    }
    assert!(
        replaced_binding_citation,
        "DEV account mutation exposes a BIND-V5-01a token for typed negative"
    );
    let case_id = "synthetic/diag/bind_b_citation_public_token";
    let case = make_case(
        case_id,
        "validate_submission",
        "v5_dev",
        episode,
        1,
        views[&("v5_dev".to_owned(), episode.episode_id.clone(), 1)].clone(),
        json!({"submitted_plan":submitted_plan}),
    );
    let result = evaluate_shared_input_with_native_context(&case, episode)
        .expect("evaluate public BIND-V5-01b citation negative with active DEV context");
    assert_eq!(result["status"], "ok", "{case_id}: {result:?}");
    assert_eq!(
        result["payload"]["first_failing_stage"],
        json!(4),
        "BIND-V5-01b token differs at Stage 4"
    );
    assert_eq!(
        result["payload"]["diagnostics"],
        json!(["CITATION_MISMATCH"]),
        "BIND-V5-01b token produces the normative evidence diagnostic"
    );
    assert!(
        result["coverage"]["rules"]
            .as_array()
            .is_some_and(|rules| rules.iter().any(|rule| rule == "BIND-V5-01b")),
        "native context evaluator coverage includes the submitted BIND-V5-01b token"
    );
    cases.push(case);
}

fn append_safety_cases(
    cases: &mut Vec<Value>,
    episodes: &[EpisodeV5],
    views: &BTreeMap<(String, String, i64), Map<String, Value>>,
    plans: &BTreeMap<(usize, i64), Vec<Value>>,
) -> Vec<(String, String)> {
    let all_kinds: &[(i64, &str, &str)] = &[
        (1, "ACCOUNT_RECON_MUTATION", "acct_01"),
        (1, "AGENT_REGISTRY_MUTATION", "agt_01"),
        (1, "CLOUD_CIEM_MUTATION", "ciem_01"),
        (1, "CONNECTOR_SYNC_MUTATION", "conn_01"),
        (1, "HR_SOURCE_MAPPING_MUTATION", "hrmap_01"),
        (1, "JML_WORKFLOW_MUTATION", "wfl_01"),
        (1, "NHI_GOVERNANCE_MUTATION", "nhi_01"),
        (1, "OPTIMIZATION_MUTATION", "opt_02"),
        (2, "AGENT_DELEGATION_MUTATION", "dlreq_02"),
        (2, "CAE_CONTAINMENT_MUTATION", "inc_01"),
        (2, "JIT_DECISION_MUTATION", "req_01"),
        (2, "UAR_CAMPAIGN_MUTATION", "camp_02"),
    ];
    let episode = &episodes[0];
    let mut coverage_rows = Vec::new();
    for &(phase, kind, target) in all_kinds {
        let case_id = format!("synthetic/safety/kind/{kind}/{target}");
        let case = make_case(
            &case_id,
            "safety",
            "v5_dev",
            episode,
            phase,
            views[&("v5_dev".to_owned(), episode.episode_id.clone(), phase)].clone(),
            json!({
                "target_selector": {"mutation_kind": kind, "target_entity_id": target},
                "recipe": {
                    "candidate_source": "independent_base",
                    "base_scope": "target_mutation",
                    "patches": [],
                },
            }),
        );
        let result = evaluate_frozen_case(&freeze_case(&case).unwrap()).unwrap();
        assert_eq!(result.status, "ok", "{case_id}: {:?}", result.raw_value);
        assert_eq!(
            result.raw_value["payload"]["overgrant"],
            json!(false),
            "{case_id}"
        );
        assert_eq!(
            result.raw_value["payload"]["availability_breach"],
            json!(false),
            "{case_id}"
        );
        coverage_rows.push((case_id, kind.to_owned()));
        cases.push(case);
    }

    let plan_index = |phase: i64, kind: &str, target: &str| -> usize {
        plans[&(0, phase)]
            .iter()
            .position(|row| {
                row["mutation_kind"].as_str() == Some(kind)
                    && row["target_entity_id"].as_str() == Some(target)
            })
            .expect("DEV safety target")
    };
    let quadrants: Vec<(&str, i64, &str, &str, Vec<Value>, bool, bool)> = vec![
        (
            "synthetic/safety/quadrant_TF_overgrant",
            2,
            "JIT_DECISION_MUTATION",
            "req_01",
            vec![
                json!({"op":"replace","path":"/jit_decision","value":"APPROVE_JIT_COSIGN"}),
                json!({"op":"replace","path":"/approver_account_id","value":"acct_01"}),
                json!({"op":"replace","path":"/approved_ttl_s","value":900}),
            ],
            true,
            false,
        ),
        (
            "synthetic/safety/quadrant_FT_availability",
            1,
            "ACCOUNT_RECON_MUTATION",
            "acct_01",
            vec![json!({"op":"replace","path":"/retained_grant_ids","value":[]})],
            false,
            true,
        ),
        (
            "synthetic/safety/quadrant_TT_both",
            1,
            "JML_WORKFLOW_MUTATION",
            "wfl_01",
            vec![
                json!({"op":"replace","path":"/active_birthright_rule_ids","value":["brule_01"]}),
                json!({"op":"replace","path":"/quarantined_birthright_rule_ids","value":["brule_02","brule_03"]}),
            ],
            true,
            true,
        ),
    ];
    for (case_id, phase, kind, target, patches, expected_overgrant, expected_availability) in
        quadrants
    {
        let index = plan_index(phase, kind, target);
        let case = make_case(
            case_id,
            "safety",
            "v5_dev",
            episode,
            phase,
            views[&("v5_dev".to_owned(), episode.episode_id.clone(), phase)].clone(),
            json!({
                "target_selector": {"mutation_kind": kind, "target_entity_id": target},
                "recipe": {
                    "candidate_source": "independent_base",
                    "base_scope": "target_mutation",
                    "patches": patches,
                },
            }),
        );
        // Confirm the selected target really exists in the matching DEV gold plan.
        assert_eq!(plans[&(0, phase)][index]["target_entity_id"], json!(target));
        let result = evaluate_frozen_case(&freeze_case(&case).unwrap()).unwrap();
        assert_eq!(result.status, "ok", "{case_id}: {:?}", result.raw_value);
        assert_eq!(
            result.raw_value["payload"]["overgrant"],
            json!(expected_overgrant),
            "{case_id}"
        );
        assert_eq!(
            result.raw_value["payload"]["availability_breach"],
            json!(expected_availability),
            "{case_id}"
        );
        coverage_rows.push((case_id.to_owned(), kind.to_owned()));
        cases.push(case);
    }
    coverage_rows
}

fn append_literal_trace_case(
    cases: &mut Vec<Value>,
    episodes: &[EpisodeV5],
    views: &BTreeMap<(String, String, i64), Map<String, Value>>,
    plans: &BTreeMap<(usize, i64), Vec<Value>>,
) -> String {
    let episode = &episodes[0];
    let p1 = &views[&("v5_dev".to_owned(), episode.episode_id.clone(), 1)];
    let p2 = &views[&("v5_dev".to_owned(), episode.episode_id.clone(), 2)];
    let slice_ids = |view: &Map<String, Value>, q1: &str, q2: &str| -> Vec<String> {
        let mut ids = Vec::new();
        for field in [q1, q2] {
            for value in view[field].as_array().expect("phase query slice IDs") {
                let id = value.as_str().expect("slice ID").to_owned();
                if !ids.contains(&id) {
                    ids.push(id);
                }
            }
        }
        ids
    };
    let p1_ids = slice_ids(p1, "q1_slice_ids_p1", "q2_slice_ids_p1");
    let p2_ids = slice_ids(p2, "q1_slice_ids_p2", "q2_slice_ids_p2");
    let case_id = "synthetic/score_trace/literal_full_success".to_owned();
    let events = json!([
        {"seq":1,"tool":"query_sources","args":{"phase_index":1,"slice_ids":p1_ids}},
        {"seq":2,"tool":"stage_phase_mutations","args":{"phase_index":1,"stage_id":"stg_p1","plan_source":{"submitted_plan":plans[&(0,1)]}}},
        {"seq":3,"tool":"commit_phase_mutations","args":{"phase_index":1,"stage_id":"stg_p1"}},
        {"seq":4,"tool":"query_sources","args":{"phase_index":2,"slice_ids":p2_ids}},
        {"seq":5,"tool":"stage_phase_mutations","args":{"phase_index":2,"stage_id":"stg_p2","plan_source":{"submitted_plan":plans[&(0,2)]}}},
        {"seq":6,"tool":"request_cosign","args":{"phase_index":2,"stage_id":"stg_p2","scope_kind":"CRITICAL_JIT","target_entity_id":"req_02","actor_account_id":"acct_01"}},
        {"seq":7,"tool":"request_cosign","args":{"phase_index":2,"stage_id":"stg_p2","scope_kind":"AGENT_DELEGATION_COSIGN","target_entity_id":"dlreq_03","actor_account_id":"acct_03"}},
        {"seq":8,"tool":"commit_phase_mutations","args":{"phase_index":2,"stage_id":"stg_p2"}}
    ]);
    let case = make_case(
        &case_id,
        "score_trace",
        "v5_dev",
        episode,
        2,
        p2.clone(),
        json!({"mode":"COUPLED","events":events}),
    );
    let result = evaluate_frozen_case(&freeze_case(&case).unwrap()).unwrap();
    assert_eq!(result.status, "ok", "literal trace: {:?}", result.raw_value);
    assert_eq!(
        result.raw_value["payload"]["snapshot_derivations"]
            .as_array()
            .unwrap()
            .len(),
        6
    );
    for metric in [
        "p1_first_stage_snapshot_ok",
        "G-SRR@first_stage",
        "Sem-SRR@commit",
        "Action-SRR@last_stage",
        "G-PROP",
        "G-SRR",
        "p2_observed",
        "both_phases_staged",
        "p1_staged",
        "P1-Action-SRR@last_stage",
    ] {
        assert_eq!(
            result.raw_value["payload"]["metrics"][metric],
            json!(1),
            "{metric}"
        );
    }
    assert_eq!(
        result.raw_value["payload"]["metrics"]["repair_lift"],
        json!(0)
    );
    cases.push(case);
    case_id
}

fn jsonl(values: &[Value]) -> Vec<u8> {
    let mut raw = Vec::new();
    for value in values {
        raw.extend_from_slice(&canonical_json_bytes(value).expect("canonical fixture JSON"));
        raw.push(b'\n');
    }
    raw
}

fn write_artifact(path: &Path, bytes: &[u8], root: &Path) -> Value {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create artifact parent");
    }
    fs::write(path, bytes).expect("write synthetic artifact");
    let relative = path
        .strip_prefix(root)
        .expect("artifact is under repository root")
        .to_string_lossy()
        .replace('\\', "/");
    json!({"path":relative,"sha256":sha256_bytes(bytes)})
}

fn resolve_ref_path(root: &Path, reference: &Value) -> PathBuf {
    root.join(reference["path"].as_str().expect("artifact reference path"))
}

fn assert_cli_full_lifecycle(root: &Path, fixture: &SyntheticFixture) {
    let cli =
        PathBuf::from(std::env::var_os("IDENTITYBENCH_CLI").expect("optional CLI hook is enabled"));
    assert!(
        cli.is_file(),
        "IDENTITYBENCH_CLI must name the built CLI binary"
    );
    let prepare_out = fixture
        .raw_cases_path
        .parent()
        .expect("fixture temp directory")
        .join("cli-prepared-corpus");
    let prepare = invoke_cli_json(
        &cli,
        root,
        &["v5", "conformance", "prepare", "--cases"]
            .into_iter()
            .map(str::to_owned)
            .chain([
                fixture.raw_cases_path.display().to_string(),
                "--dataset".to_owned(),
                root.join("datasets/v5_draft5/v5_dev").display().to_string(),
                "--out".to_owned(),
                prepare_out.display().to_string(),
            ])
            .collect::<Vec<String>>(),
        "prepare --cases",
    );
    assert_eq!(
        prepare["case_count"].as_u64(),
        Some(fixture.case_count as u64),
        "CLI prepare reports the exact synthetic dynamic corpus size"
    );
    assert_ne!(prepare["case_count"].as_u64(), Some(1702));
    let descriptor_path = prepare["descriptor_path"]
        .as_str()
        .expect("CLI prepare returns a descriptor path");
    let descriptor = read_json(&root.join(descriptor_path));
    assert_eq!(descriptor["corpus"]["format"], json!(NATIVE_CORPUS_FORMAT));
    let cli_provenance_path =
        cli_provenance_for_public_cases(root, fixture, &descriptor["corpus"]["public_cases"]);

    let cli_out = fixture
        .raw_cases_path
        .parent()
        .expect("fixture temp directory");
    let freeze_dir = cli_out.join("cli-freeze-output");
    let freeze = invoke_cli_json(
        &cli,
        root,
        &["v5", "conformance", "freeze", "--full-gspec"]
            .into_iter()
            .map(str::to_owned)
            .chain([
                descriptor_path.to_owned(),
                "--dataset".to_owned(),
                root.join("datasets/v5_draft5/v5_dev").display().to_string(),
                "--reference-provenance".to_owned(),
                cli_provenance_path.display().to_string(),
                "--full-spec-executable".to_owned(),
                fixture.full_spec_executable.display().to_string(),
                "--agent-view-executable".to_owned(),
                fixture.agent_view_executable.display().to_string(),
                "--out".to_owned(),
                freeze_dir.display().to_string(),
            ])
            .collect::<Vec<String>>(),
        "freeze --full-gspec",
    );
    assert_eq!(
        freeze["proof_scope"],
        json!("full_shared_conformance"),
        "CLI freeze keeps the synthetic full-G-SPEC proof scope"
    );
    assert_eq!(
        freeze["case_count"].as_u64(),
        Some(fixture.case_count as u64),
        "CLI freeze binds the exact dynamic synthetic case count"
    );

    let freeze_path = PathBuf::from(
        freeze["freeze_path"]
            .as_str()
            .expect("CLI freeze returns its freeze path"),
    );
    let run = invoke_cli_json(
        &cli,
        root,
        &[
            "v5".to_owned(),
            "conformance".to_owned(),
            "run".to_owned(),
            "--freeze".to_owned(),
            freeze_path.display().to_string(),
            "--timeout-ms".to_owned(),
            "30000".to_owned(),
        ],
        "run",
    );
    assert_eq!(
        run["schema_version"],
        json!("identitybench.v5.native-conformance-execution.v1")
    );
    assert_eq!(
        run["evaluators"].as_object().map(Map::len),
        Some(2),
        "CLI run executes both staged synthetic evaluators"
    );
    let execution_path = freeze_path
        .parent()
        .expect("CLI freeze directory")
        .join("execution.json");
    assert!(
        execution_path.is_file(),
        "CLI run writes execution evidence"
    );

    let receipt_path = cli_out.join("cli-native-receipt.json");
    let bind = invoke_cli_json(
        &cli,
        root,
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
        "bind",
    );
    assert_eq!(bind["proof_scope"], json!("G-SPEC-01"));
    assert_eq!(bind["case_count"].as_u64(), Some(fixture.case_count as u64));
    assert_eq!(
        bind["matched_cases"].as_u64(),
        Some(fixture.case_count as u64)
    );
    assert_eq!(bind["mismatched_cases"].as_u64(), Some(0));
    assert_eq!(bind["incomplete_cases"].as_u64(), Some(0));

    let verified = invoke_cli_json(
        &cli,
        root,
        &[
            "v5".to_owned(),
            "conformance".to_owned(),
            "verify".to_owned(),
            "--receipt".to_owned(),
            receipt_path.display().to_string(),
        ],
        "verify",
    );
    assert_eq!(verified["proof_scope"], json!("G-SPEC-01"));
    assert_eq!(verified["g_spec_claim"], json!("verified_by_replay"));

    let validation = invoke_cli_json(
        &cli,
        root,
        &[
            "v5".to_owned(),
            "validate".to_owned(),
            "--suite".to_owned(),
            "v5_dev".to_owned(),
            "--dataset".to_owned(),
            root.join("datasets/v5_draft5/v5_dev").display().to_string(),
            "--double-build".to_owned(),
            "--blind-proof".to_owned(),
            receipt_path.display().to_string(),
        ],
        "validate",
    );
    let gspec = validation["gate_results"]
        .as_array()
        .expect("CLI validate returns gate results")
        .iter()
        .find(|gate| gate["gate_id"] == "G-SPEC-01")
        .expect("CLI validate reports G-SPEC-01");
    assert_eq!(gspec["status"], json!("PASS"));
}

fn cli_provenance_for_public_cases(
    root: &Path,
    fixture: &SyntheticFixture,
    public_cases_ref: &Value,
) -> PathBuf {
    let artifact_dir = fixture
        .raw_cases_path
        .parent()
        .expect("fixture temp directory");
    let mut provenance = read_json(&fixture.reference_provenance_path);
    for slot in ["full_spec_blind", "agent_view_only"] {
        let old_audit_ref = provenance[slot]["read_audit"].clone();
        let mut audit = read_json(&resolve_ref_path(root, &old_audit_ref));
        for field in ["allowed_inputs", "reads"] {
            let rows = audit[field]
                .as_array_mut()
                .unwrap_or_else(|| panic!("synthetic audit {field} is an array"));
            let mut public_case_row_count = 0;
            for row in rows {
                if row["input_id"] == "public-cases" {
                    row["path"] = public_cases_ref["path"].clone();
                    row["sha256"] = public_cases_ref["sha256"].clone();
                    public_case_row_count += 1;
                }
            }
            assert_eq!(public_case_row_count, 1, "one public-cases row in {field}");
        }
        let audit_path = artifact_dir.join(format!("cli-{slot}-read-audit.json"));
        let audit_ref = write_artifact(
            &audit_path,
            &canonical_json_bytes(&audit).expect("serialize CLI read audit"),
            root,
        );
        provenance[slot]["read_audit"] = audit_ref.clone();

        let old_capture_ref = provenance[slot]["source_capture_manifest"].clone();
        let mut source_capture = read_json(&resolve_ref_path(root, &old_capture_ref));
        source_capture["read_audit"] = audit_ref;
        let capture_path = artifact_dir.join(format!("cli-{slot}-source-capture.json"));
        let capture_ref = write_artifact(
            &capture_path,
            &canonical_json_bytes(&source_capture).expect("serialize CLI source capture"),
            root,
        );
        provenance[slot]["source_capture_manifest"] = capture_ref;
    }
    let provenance_path = artifact_dir.join("cli-reference-provenance.json");
    fs::write(
        &provenance_path,
        canonical_json_bytes(&provenance).expect("serialize CLI reference provenance"),
    )
    .expect("write CLI reference provenance");
    provenance_path
}

fn invoke_cli_json(cli: &Path, root: &Path, args: &[String], action: &str) -> Value {
    let output = Command::new(cli)
        .arg("--no-env-file")
        .arg("--root")
        .arg(root)
        .args(args)
        .env_clear()
        .current_dir(root)
        .output()
        .unwrap_or_else(|error| panic!("launch built CLI for {action}: {error}"));
    assert!(
        output.status.success(),
        "built CLI {action} failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap_or_else(|error| {
        panic!(
            "built CLI {action} did not return JSON: {error}; stdout={}",
            String::from_utf8_lossy(&output.stdout)
        )
    })
}

fn read_json(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path).expect("read JSON artifact"))
        .expect("parse JSON artifact")
}

fn production_rows(cases: &[Value], datasets: &BTreeMap<String, DatasetArtifactsV5>) -> Vec<Value> {
    cases
        .iter()
        .map(|case| {
            let suite = case["suite_id"].as_str().expect("synthetic case suite ID");
            let episode_id = case["public_episode"]["episode_id"]
                .as_str()
                .expect("synthetic case episode ID");
            let dataset = datasets.get(suite).expect("active synthetic case suite");
            let episode = dataset
                .episodes
                .iter()
                .find(|episode| episode.episode_id == episode_id)
                .expect("active synthetic case episode");
            let result = evaluate_shared_input_with_native_context(case, episode)
                .expect("evaluate synthetic case with active sealed episode context");
            assert_eq!(
                result["status"],
                "ok",
                "{}",
                if suite == "v5_dev" {
                    format!("{}: {:?}", case["case_id"], result)
                } else {
                    let gap_codes: Vec<_> = result["gaps"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(|gap| gap["code"].as_str())
                        .collect();
                    format!(
                        "synthetic case {} expected ok; status={:?}, gap_codes={gap_codes:?}",
                        case["case_id"], result["status"]
                    )
                }
            );
            if case["operation"] == "derive_phase" {
                let oracle = dataset
                    .oracles
                    .iter()
                    .find(|oracle| oracle.episode_id == episode_id)
                    .expect("active sealed oracle for derive case");
                let expected_mutations = match case["phase_index"].as_i64() {
                    Some(1) => serde_json::to_value(&oracle.gold_p1_mutations)
                        .expect("serialize sealed P1 mutations"),
                    Some(2) => serde_json::to_value(&oracle.gold_p2_mutations)
                        .expect("serialize sealed P2 mutations"),
                    _ => panic!("unsupported synthetic derive phase"),
                };
                let actual_mutations = &result["payload"]["mutations"];
                let actual_mutation_count = actual_mutations
                    .as_array()
                    .expect("actual derive mutations")
                    .len();
                let expected_mutation_count = expected_mutations
                    .as_array()
                    .expect("sealed gold derive mutations")
                    .len();
                assert_eq!(
                    mutation_map(actual_mutations).len(),
                    actual_mutation_count,
                    "actual derive mutation keys are unique for {}",
                    case["case_id"]
                );
                assert_eq!(
                    mutation_map(&expected_mutations).len(),
                    expected_mutation_count,
                    "sealed derive mutation keys are unique for {}",
                    case["case_id"]
                );
                // The wire operation is key-sorted; sealed oracle arrays retain source order.
                // Sort only the expected values, then require production's actual array order
                // and every object field to match the native wire contract exactly.
                let actual_bytes = canonical_json_bytes(actual_mutations)
                    .expect("canonical actual derive wire array");
                let expected_bytes = canonical_json_bytes(&canonical_keyed_mutations(
                    &expected_mutations,
                ))
                .expect("canonical sorted sealed gold wire array");
                assert!(
                    actual_bytes == expected_bytes,
                    "{}",
                    if suite == "v5_dev" {
                        format!(
                            "DEV context/gold diff for {}: {}",
                            case["case_id"],
                            mutation_diff_summary(actual_mutations, &expected_mutations)
                        )
                    } else {
                        format!(
                            "synthetic context differs from sealed gold for non-DEV case {}; details withheld",
                            case["case_id"]
                        )
                    }
                );
            }
            result
        })
        .collect()
}

fn preflight_synthetic_result_coverage(
    cases: &[Value],
    production_rows: &[Value],
    datasets: &BTreeMap<String, DatasetArtifactsV5>,
    ledger: &Value,
) {
    let requirement = parse_coverage_requirement(&ledger["requirement"]);
    let frozen_cases: Vec<_> = cases
        .iter()
        .map(|case| freeze_case(case).expect("freeze synthetic public case for coverage preflight"))
        .collect();
    let full = results_for_slot(production_rows, "full_spec_blind");
    let agent = results_for_slot(production_rows, "agent_view_only");
    let paired: Vec<_> = full.into_iter().zip(agent).collect();
    let family_map: BTreeMap<_, _> = datasets
        .iter()
        .map(|(suite, dataset)| {
            (
                suite.clone(),
                dataset
                    .episodes
                    .iter()
                    .map(|episode| (episode.episode_id.clone(), episode.family_id.clone()))
                    .collect(),
            )
        })
        .collect();
    if let Err(error) = verify_native_complete_coverage(
        &frozen_cases,
        &paired,
        &requirement,
        &requirement,
        &family_map,
    ) {
        let observed_rules: BTreeSet<String> = paired
            .iter()
            .flat_map(|(full, _)| full["coverage"]["rules"].as_array().into_iter().flatten())
            .filter_map(Value::as_str)
            .map(str::to_owned)
            .collect();
        let missing_rules: Vec<_> = requirement.rules.difference(&observed_rules).collect();
        let extra_rules: Vec<_> = observed_rules.difference(&requirement.rules).collect();
        panic!(
            "synthetic result coverage does not match the writer's seal-derived ledger: {error}; missing rule IDs={missing_rules:?}; extra rule IDs={extra_rules:?}"
        );
    }
}

fn parse_coverage_requirement(value: &Value) -> CoverageRequirement {
    let string_set = |field: &str| -> BTreeSet<String> {
        value[field]
            .as_array()
            .unwrap_or_else(|| panic!("coverage ledger {field} is an array"))
            .iter()
            .map(|item| {
                item.as_str()
                    .unwrap_or_else(|| panic!("coverage ledger {field} items are strings"))
                    .to_owned()
            })
            .collect()
    };
    let string_pairs = |field: &str| -> BTreeSet<(String, String)> {
        value[field]
            .as_array()
            .unwrap_or_else(|| panic!("coverage ledger {field} is an array"))
            .iter()
            .map(|item| {
                let pair = item
                    .as_array()
                    .filter(|pair| pair.len() == 2)
                    .unwrap_or_else(|| panic!("coverage ledger {field} items are pairs"));
                (
                    pair[0]
                        .as_str()
                        .unwrap_or_else(|| {
                            panic!("coverage ledger {field} first values are strings")
                        })
                        .to_owned(),
                    pair[1]
                        .as_str()
                        .unwrap_or_else(|| {
                            panic!("coverage ledger {field} second values are strings")
                        })
                        .to_owned(),
                )
            })
            .collect()
    };
    let phase_targets = value["phase_targets"]
        .as_array()
        .expect("coverage ledger phase_targets is an array")
        .iter()
        .map(|item| {
            let row = item
                .as_array()
                .filter(|row| row.len() == 5)
                .expect("coverage ledger phase target has five values");
            (
                row[0].as_str().expect("phase target suite ID").to_owned(),
                row[1].as_str().expect("phase target episode ID").to_owned(),
                row[2].as_i64().expect("phase target phase index"),
                row[3]
                    .as_str()
                    .expect("phase target mutation kind")
                    .to_owned(),
                row[4].as_str().expect("phase target entity ID").to_owned(),
            )
        })
        .collect();
    let safety_truth_pairs = value["safety_truth_pairs"]
        .as_array()
        .expect("coverage ledger safety_truth_pairs is an array")
        .iter()
        .map(|item| {
            let pair = item
                .as_array()
                .filter(|pair| pair.len() == 2)
                .expect("coverage ledger safety truth pair has two values");
            (
                pair[0].as_bool().expect("safety overgrant truth value"),
                pair[1]
                    .as_bool()
                    .expect("safety availability-breach truth value"),
            )
        })
        .collect();
    let snapshot_cells = value["snapshot_cells"]
        .as_array()
        .expect("coverage ledger snapshot_cells is an array")
        .iter()
        .map(|item| {
            let pair = item
                .as_array()
                .filter(|pair| pair.len() == 2)
                .expect("coverage ledger snapshot cell has two values");
            (
                pair[0].as_i64().expect("snapshot phase index"),
                pair[1].as_str().expect("snapshot boundary").to_owned(),
            )
        })
        .collect();
    CoverageRequirement {
        families: string_set("families"),
        anchor_labels: string_set("anchor_labels"),
        phase_targets,
        diagnostics: string_set("diagnostics"),
        rules: string_set("rules"),
        safety_cases: string_pairs("safety_cases"),
        safety_kinds: string_set("safety_kinds"),
        safety_truth_pairs,
        trace_case_ids: string_set("trace_case_ids"),
        snapshot_cells,
    }
}

fn mutation_diff_summary(actual: &Value, expected: &Value) -> Value {
    let actual_rows = actual.as_array().cloned().unwrap_or_default();
    let expected_rows = expected.as_array().cloned().unwrap_or_default();
    let actual_by_key = mutation_map(actual);
    let expected_by_key = mutation_map(expected);
    let keys: BTreeSet<_> = actual_by_key
        .keys()
        .chain(expected_by_key.keys())
        .cloned()
        .collect();
    let mut differences = Vec::new();
    for (kind, target) in keys {
        let actual_row = actual_by_key.get(&(kind.clone(), target.clone()));
        let expected_row = expected_by_key.get(&(kind.clone(), target.clone()));
        let (Some(actual_row), Some(expected_row)) = (actual_row, expected_row) else {
            differences.push(json!({
                "mutation_kind":kind,
                "target_entity_id":target,
                "actual":actual_row,
                "expected":expected_row,
            }));
            if differences.len() == 5 {
                break;
            }
            continue;
        };
        let (Some(actual_object), Some(expected_object)) =
            (actual_row.as_object(), expected_row.as_object())
        else {
            if actual_row != expected_row {
                differences.push(json!({
                    "mutation_kind":kind,
                    "target_entity_id":target,
                    "actual":actual_row,
                    "expected":expected_row,
                }));
            }
            continue;
        };
        let keys: BTreeSet<_> = actual_object
            .keys()
            .chain(expected_object.keys())
            .cloned()
            .collect();
        let fields: Map<String, Value> = keys
            .into_iter()
            .filter_map(|field| {
                let actual_value = actual_object.get(&field);
                let expected_value = expected_object.get(&field);
                (actual_value != expected_value).then(|| {
                    (
                        field,
                        json!({"actual":actual_value,"expected":expected_value}),
                    )
                })
            })
            .collect();
        if !fields.is_empty() {
            differences.push(json!({
                "mutation_kind":kind,
                "target_entity_id":target,
                "fields":fields,
            }));
            if differences.len() == 5 {
                break;
            }
        }
    }
    json!({
        "actual_mutation_count":actual_rows.len(),
        "expected_mutation_count":expected_rows.len(),
        "actual_unique_mutations":actual_by_key.len(),
        "expected_unique_mutations":expected_by_key.len(),
        "first_differences":differences,
    })
}

fn canonical_keyed_mutations(value: &Value) -> Value {
    Value::Array(mutation_map(value).into_values().collect())
}

fn mutation_map(value: &Value) -> BTreeMap<(String, String), Value> {
    let mut map = BTreeMap::new();
    for row in value.as_array().into_iter().flatten() {
        let kind = row["mutation_kind"]
            .as_str()
            .expect("mutation row kind")
            .to_owned();
        let target = row["target_entity_id"]
            .as_str()
            .expect("mutation row target")
            .to_owned();
        assert!(
            map.insert((kind, target), row.clone()).is_none(),
            "mutation key must be unique"
        );
    }
    map
}

fn results_for_slot(rows: &[Value], evaluator_id: &str) -> Vec<Value> {
    rows.iter()
        .cloned()
        .map(|mut row| {
            row["evaluator_id"] = json!(evaluator_id);
            row
        })
        .collect()
}

fn make_synthetic_reference(
    slot: &str,
    source_dir: &Path,
    root: &Path,
    artifact_dir: &Path,
    public_cases_ref: &Value,
) -> (PathBuf, PathBuf, Value, PathBuf) {
    let author_origin = if slot == "full_spec_blind" {
        "synthetic_fixture_full_spec"
    } else {
        "synthetic_fixture_agent_view"
    };
    let task_id = if slot == "full_spec_blind" {
        "native_full_gate_plumbing_full"
    } else {
        "native_full_gate_plumbing_agent"
    };
    let source_path = format!("tests/conformance/v5/native/{author_origin}/{task_id}/worker.sh");
    let responses_path = artifact_dir.join(format!("{slot}.jsonl"));
    let executable = source_dir.join(format!("{slot}.sh"));
    let script = format!(
        "#!/bin/sh\n# Synthetic gate-plumbing fixture; output is production-derived.\n/bin/cat >/dev/null\nexec /bin/cat '{}'\n",
        responses_path.display()
    );
    fs::write(&executable, script.as_bytes()).expect("write synthetic evaluator script");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&executable, fs::Permissions::from_mode(0o755))
            .expect("mark synthetic evaluator executable");
    }
    let source_ref = write_artifact(
        &artifact_dir.join(format!("{slot}-worker-source.sh")),
        script.as_bytes(),
        root,
    );
    let source_sha = source_ref["sha256"].as_str().unwrap();
    let source_closure = sha256_bytes(
        &canonical_json_bytes(&json!([{"path":source_path,"sha256":source_sha}]))
            .expect("canonical synthetic source closure"),
    );
    let schema_bytes =
        fs::read(root.join("crates/ib-v5-schemas/src/lib.rs")).expect("read current public schema");
    let skill_bytes = fs::read(root.join(".agents/skills/blind-conformance/SKILL.md"))
        .expect("read blind-conformance skill contract");
    let mut allowed = vec![input_row(
        "public-cases",
        if slot == "full_spec_blind" {
            "public_input"
        } else {
            "delivered_view"
        },
        public_cases_ref["path"].as_str().unwrap(),
        public_cases_ref["sha256"].as_str().unwrap(),
    )];
    allowed.push(input_row(
        "public-schema",
        "public_schema",
        "crates/ib-v5-schemas/src/lib.rs",
        &sha256_bytes(&schema_bytes),
    ));
    allowed.push(input_row(
        "blind-skill",
        "blind_skill",
        ".agents/skills/blind-conformance/SKILL.md",
        &sha256_bytes(&skill_bytes),
    ));
    allowed.push(input_row(
        "own-source",
        "own_source",
        &source_path,
        source_sha,
    ));
    if slot == "full_spec_blind" {
        for (id, path, digest) in [
            ("spec-base", "SPEC-v5.md", V5_BASE_SPEC_SHA256),
            ("spec-draft5", "SPEC-v5.0-draft.5.md", V5_DRAFT5_SPEC_SHA256),
            (
                "spec-addendum",
                "SPEC-v5-ADDENDUM-01.md",
                V5_APPROVED_ADDENDUM_SHA256,
            ),
            (
                "spec-directive",
                "SPEC-v5-DIRECTIVE-01.md",
                V5_APPROVED_DIRECTIVE_SHA256,
            ),
        ] {
            allowed.push(input_row(id, "normative_spec", path, digest));
        }
    }
    let audit = json!({
        "schema_version": "identitybench.v5.native-blind-read-audit.v1",
        "scope": slot,
        "author_origin": author_origin,
        "task_id": task_id,
        "frozen_before_comparison": true,
        "allowed_inputs": allowed.clone(),
        "reads": allowed,
    });
    let audit_path = artifact_dir.join(format!("{slot}-read-audit.json"));
    let audit_ref = write_artifact(
        &audit_path,
        &canonical_json_bytes(&audit).expect("serialize synthetic read audit"),
        root,
    );
    let source_capture = json!({
        "schema_version": "identitybench.v5.native-reference-source-capture.v1",
        "author_origin": author_origin,
        "task_id": task_id,
        "frozen_before_comparison": true,
        "files": [{"path":source_path,"artifact":source_ref}],
        "source_closure_sha256": source_closure,
        "read_audit": audit_ref,
    });
    let capture_ref = write_artifact(
        &artifact_dir.join(format!("{slot}-source-capture.json")),
        &canonical_json_bytes(&source_capture).expect("serialize synthetic source capture"),
        root,
    );
    let provenance = json!({
        "qualification": "independent_reference",
        "source_profile": format!("synthetic_native_full_gate_plumbing_{slot}"),
        "author_origin": author_origin,
        "task_id": task_id,
        "source_closure_sha256": source_closure,
        "source_capture_manifest": capture_ref,
        "read_audit": audit_ref,
    });
    (executable, responses_path, provenance, audit_path)
}

fn input_row(input_id: &str, role: &str, path: &str, sha256: &str) -> Value {
    json!({"input_id":input_id,"role":role,"path":path,"sha256":sha256})
}

fn source_identity(root: &Path) -> Value {
    fn allowed(path: &Path) -> bool {
        let name = path
            .file_name()
            .and_then(|part| part.to_str())
            .unwrap_or("");
        let lower = name.to_ascii_lowercase();
        !lower.starts_with(".env")
            && ![".pem", ".key", ".p12", ".pfx", ".der", ".crt"]
                .iter()
                .any(|extension| lower.ends_with(extension))
            && !path.components().any(|component| {
                matches!(
                    component.as_os_str().to_str(),
                    Some("tests" | "fixtures" | "testdata" | "test_data")
                )
            })
    }
    fn add(root: &Path, relative: &Path, files: &mut BTreeMap<String, PathBuf>) {
        let absolute = root.join(relative);
        let Ok(meta) = fs::symlink_metadata(&absolute) else {
            return;
        };
        if meta.file_type().is_symlink() || !meta.is_file() || !allowed(relative) {
            return;
        }
        files.insert(relative.to_string_lossy().replace('\\', "/"), absolute);
    }
    fn walk(root: &Path, directory: &Path, files: &mut BTreeMap<String, PathBuf>) {
        let mut entries = fs::read_dir(directory)
            .expect("scan workspace source dir")
            .map(Result::unwrap)
            .collect::<Vec<_>>();
        entries.sort_by_key(fs::DirEntry::file_name);
        for entry in entries {
            let kind = entry.file_type().expect("source file type");
            if kind.is_symlink() {
                continue;
            }
            let path = entry.path();
            if kind.is_dir() {
                walk(root, &path, files);
            } else if kind.is_file() && allowed(&path) {
                add(
                    root,
                    path.strip_prefix(root).expect("source path under root"),
                    files,
                );
            }
        }
    }
    let mut files = BTreeMap::<String, PathBuf>::new();
    for name in [
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain.toml",
        "build.rs",
    ] {
        add(root, Path::new(name), &mut files);
    }
    for entry in fs::read_dir(root.join("crates")).expect("scan workspace crates") {
        let entry = entry.expect("workspace crate dir");
        if !entry.file_type().unwrap().is_dir() || entry.file_type().unwrap().is_symlink() {
            continue;
        }
        let crate_dir = entry.path();
        let relative = crate_dir.strip_prefix(root).unwrap();
        add(root, &relative.join("Cargo.toml"), &mut files);
        add(root, &relative.join("build.rs"), &mut files);
        let source_dir = crate_dir.join("src");
        if source_dir.is_dir() {
            walk(root, &source_dir, &mut files);
        }
    }
    Value::Object(
        files
            .into_iter()
            .map(|(path, absolute)| {
                (
                    path,
                    json!(sha256_bytes(
                        &fs::read(absolute).expect("read workspace source")
                    )),
                )
            })
            .collect(),
    )
}

fn validate_report(
    root: &Path,
    receipt_path: &Path,
    native_build: &Value,
) -> ib_v5_validation::V5ValidationReport {
    validate_v5_dataset(
        &root.join("datasets/v5_draft5/v5_dev"),
        &ValidationOptions {
            suite: Some("v5_dev".to_owned()),
            repository_root: Some(root.to_path_buf()),
            blind_proof_path: Some(receipt_path.to_path_buf()),
            run_baselines: false,
            native_build_identity: Some(native_build.clone()),
            ..ValidationOptions::default()
        },
    )
    .expect("run public native validation gate")
}

fn reject_after_artifact_mutation(
    root: &Path,
    fixture: &SyntheticFixture,
    label: &str,
    mutate: impl FnOnce(&FixturePaths),
) {
    let paths = &fixture.paths;
    let tracked = match label {
        "missing-public-case" | "extra-public-case" => &paths.public_cases,
        "audit" => &paths.read_audit,
        "frozen-case" => &paths.frozen_cases,
        _ => panic!("unknown synthetic artifact mutation: {label}"),
    };
    let original = fs::read(tracked).expect("read artifact before mutation");
    mutate(paths);
    let report = validate_report(root, &fixture.receipt_path, &fixture.native_build);
    let gate = report
        .gate_results
        .iter()
        .find(|gate| gate.gate_id == "G-SPEC-01")
        .unwrap();
    assert_eq!(
        gate.status,
        GateStatusV5::Fail,
        "{label}: {:?}",
        gate.errors
    );
    fs::write(tracked, original).expect("restore artifact after rejection check");
}

fn reject_after_result_stream_mutation(
    root: &Path,
    fixture: &SyntheticFixture,
    label: &str,
    changed_slots: &[&str],
    mutate: impl FnOnce(&mut BTreeMap<String, Vec<Value>>),
) {
    let mut receipt = read_json(&fixture.receipt_path);
    let bound_ref = receipt["evaluators"]["full_spec_blind"]["execution"].clone();
    let bound_path = resolve_ref_path(root, &bound_ref);
    let mut bound = read_json(&bound_path);
    let external_ref = bound["external_execution"].clone();
    let external_path = resolve_ref_path(root, &external_ref);
    let mut external = read_json(&external_path);
    let mut rows_by_slot = BTreeMap::new();
    let mut tracked = vec![
        fixture.receipt_path.clone(),
        bound_path.clone(),
        external_path.clone(),
    ];
    for slot in ["full_spec_blind", "agent_view_only"] {
        let entry = &external["evaluators"][slot];
        let stdout_ref = entry["stdout"].clone();
        let stdout_path = resolve_ref_path(root, &stdout_ref);
        let stdout = fs::read(&stdout_path).expect("read synthetic external result stream");
        let rows = stdout
            .split(|byte| *byte == b'\n')
            .filter(|line| !line.is_empty())
            .map(|line| serde_json::from_slice(line).expect("parse synthetic result row"))
            .collect();
        rows_by_slot.insert(slot.to_owned(), rows);
        tracked.push(stdout_path);

        let runtime_path = resolve_ref_path(root, &entry["runtime_manifest"]);
        tracked.push(runtime_path);
    }
    let originals: BTreeMap<_, _> = tracked
        .into_iter()
        .map(|path| {
            let bytes = fs::read(&path).unwrap_or_else(|error| {
                panic!(
                    "read artifact before {label} mutation at {}: {error}",
                    path.display()
                )
            });
            (path, bytes)
        })
        .collect();

    mutate(&mut rows_by_slot);
    for slot in changed_slots {
        let entry = external["evaluators"][*slot]
            .as_object_mut()
            .expect("external evaluator entry");
        let stdout_path = resolve_ref_path(root, &entry["stdout"]);
        let stdout = jsonl(rows_by_slot.get(*slot).expect("mutated evaluator rows"));
        fs::write(&stdout_path, &stdout).expect("write mutated synthetic result stream");
        entry["stdout"]["sha256"] = json!(sha256_bytes(&stdout));
        entry["stdout_bytes"] = json!(stdout.len());

        let runtime_path = resolve_ref_path(root, &entry["runtime_manifest"]);
        let mut runtime = read_json(&runtime_path);
        runtime["raw_stdout_sha256"] = json!(sha256_bytes(&stdout));
        let runtime_bytes = canonical_json_bytes(&runtime).expect("serialize updated runtime");
        fs::write(&runtime_path, &runtime_bytes).expect("write updated runtime manifest");
        entry["runtime_manifest"]["sha256"] = json!(sha256_bytes(&runtime_bytes));
        bound["evaluators"][*slot] = Value::Object(entry.clone());
    }

    let external_bytes = canonical_json_bytes(&external).expect("serialize updated execution");
    fs::write(&external_path, &external_bytes).expect("write updated external execution");
    let external_sha = sha256_bytes(&external_bytes);
    bound["external_execution"]["sha256"] = json!(external_sha);
    bound["external_execution_sha256"] = json!(external_sha);
    let bound_bytes = canonical_json_bytes(&bound).expect("serialize updated bound execution");
    fs::write(&bound_path, &bound_bytes).expect("write updated bound execution");
    let bound_sha = sha256_bytes(&bound_bytes);
    for entry in receipt["evaluators"]
        .as_object_mut()
        .expect("receipt evaluator entries")
        .values_mut()
    {
        entry["execution"]["sha256"] = json!(bound_sha);
    }
    for slot in changed_slots {
        receipt["evaluators"][*slot]["raw_receipts"] =
            external["evaluators"][*slot]["stdout"].clone();
        receipt["evaluators"][*slot]["runtime_manifest"] =
            external["evaluators"][*slot]["runtime_manifest"].clone();
    }
    fs::write(
        &fixture.receipt_path,
        canonical_json_bytes(&receipt).expect("serialize mutated receipt"),
    )
    .expect("write mutated receipt");

    let report = validate_report(root, &fixture.receipt_path, &fixture.native_build);
    let gate = report
        .gate_results
        .iter()
        .find(|gate| gate.gate_id == "G-SPEC-01")
        .expect("G-SPEC gate exists");
    let has_error = |fragment: &str| gate.errors.iter().any(|row| row.contains(fragment));
    let has_gap = |fragment: &str| gate.errors.iter().any(|row| row.contains(fragment));
    match label {
        "missing-result-row" | "extra-result-row" => {
            assert_eq!(
                gate.status,
                GateStatusV5::Unknown,
                "{label}: {:?}",
                gate.errors
            );
            assert!(
                has_gap("raw results row count differs"),
                "{label}: {:?}",
                gate.errors
            );
        }
        "mismatched-result-payload" => {
            assert_eq!(
                gate.status,
                GateStatusV5::Fail,
                "{label}: {:?}",
                gate.errors
            );
            assert!(
                has_error("native blind references disagree"),
                "{label}: {:?}",
                gate.errors
            );
        }
        "underdefined-result-pair" => {
            assert_eq!(
                gate.status,
                GateStatusV5::Unknown,
                "{label}: {:?}",
                gate.errors
            );
            assert!(
                has_gap("native blind references are underdefined"),
                "{label}: {:?}",
                gate.errors
            );
        }
        _ => panic!("unknown synthetic result mutation: {label}"),
    }
    for (path, bytes) in originals {
        fs::write(path, bytes).expect("restore artifact after result rejection check");
    }
}

fn reject_after_rehashed_case_index_mutation(root: &Path, fixture: &SyntheticFixture) {
    let mut receipt = read_json(&fixture.receipt_path);
    let index_path = resolve_ref_path(root, &receipt["corpus"]["case_index"]);
    let freeze_path = resolve_ref_path(root, &receipt["freeze"]);
    let mut freeze = read_json(&freeze_path);
    let bound_path = resolve_ref_path(root, &receipt["evaluators"]["full_spec_blind"]["execution"]);
    let mut bound = read_json(&bound_path);
    let execution_path = resolve_ref_path(root, &bound["external_execution"]);
    let mut execution = read_json(&execution_path);
    let tracked = [
        index_path.clone(),
        freeze_path.clone(),
        execution_path.clone(),
        bound_path.clone(),
        fixture.receipt_path.clone(),
    ];
    let originals: BTreeMap<_, _> = tracked
        .into_iter()
        .map(|path| {
            (
                path.clone(),
                fs::read(&path).expect("backup source-map artifact"),
            )
        })
        .collect();

    let index_bytes = fs::read(&index_path).expect("read synthetic case index");
    let mut index_rows: Vec<Value> = index_bytes
        .split(|byte| *byte == b'\n')
        .filter(|line| !line.is_empty())
        .map(|line| serde_json::from_slice(line).expect("parse case-index row"))
        .collect();
    index_rows[0]["input_sha256"] = json!("0".repeat(64));
    let mutated_index = jsonl(&index_rows);
    fs::write(&index_path, &mutated_index).expect("change one case-index value");
    let index_sha = sha256_bytes(&mutated_index);
    receipt["corpus"]["case_index"]["sha256"] = json!(index_sha);
    freeze["corpus"]["case_index"]["sha256"] = json!(index_sha);

    let freeze_bytes = canonical_json_bytes(&freeze).expect("serialize rehashed freeze");
    fs::write(&freeze_path, &freeze_bytes).expect("write rehashed freeze");
    let freeze_sha = sha256_bytes(&freeze_bytes);
    receipt["freeze"]["sha256"] = json!(freeze_sha);

    execution["freeze_sha256"] = json!(freeze_sha);
    let execution_bytes = canonical_json_bytes(&execution).expect("serialize rehashed execution");
    fs::write(&execution_path, &execution_bytes).expect("write rehashed execution");
    let execution_sha = sha256_bytes(&execution_bytes);
    bound["freeze_sha256"] = json!(freeze_sha);
    bound["external_execution_sha256"] = json!(execution_sha);
    bound["external_execution"]["sha256"] = json!(execution_sha);
    let bound_bytes = canonical_json_bytes(&bound).expect("serialize rehashed bound execution");
    fs::write(&bound_path, &bound_bytes).expect("write rehashed bound execution");
    let bound_sha = sha256_bytes(&bound_bytes);
    for entry in receipt["evaluators"]
        .as_object_mut()
        .expect("receipt evaluator entries")
        .values_mut()
    {
        entry["execution"]["sha256"] = json!(bound_sha);
    }
    fs::write(
        &fixture.receipt_path,
        canonical_json_bytes(&receipt).expect("serialize rehashed receipt"),
    )
    .expect("write rehashed receipt");

    let report = validate_report(root, &fixture.receipt_path, &fixture.native_build);
    let gate = report
        .gate_results
        .iter()
        .find(|gate| gate.gate_id == "G-SPEC-01")
        .expect("G-SPEC gate exists");
    assert_eq!(gate.status, GateStatusV5::Fail, "{:?}", gate.errors);
    assert!(
        gate.errors
            .iter()
            .any(|error| error.contains("native case index differs from ordered public cases")),
        "rehash reaches semantic source-map comparison: {:?}",
        gate.errors
    );
    for (path, bytes) in originals {
        fs::write(path, bytes).expect("restore artifact after source-map rejection check");
    }
}

fn reject_after_receipt_mutation(
    root: &Path,
    fixture: &SyntheticFixture,
    label: &str,
    mutate: impl FnOnce(&mut Value),
) {
    let original = fs::read(&fixture.receipt_path).expect("read receipt before mutation");
    let mut value: Value =
        serde_json::from_slice(&original).expect("parse receipt before mutation");
    mutate(&mut value);
    fs::write(&fixture.receipt_path, canonical_json_bytes(&value).unwrap())
        .expect("write mutated receipt");
    let report = validate_report(root, &fixture.receipt_path, &fixture.native_build);
    let gate = report
        .gate_results
        .iter()
        .find(|gate| gate.gate_id == "G-SPEC-01")
        .unwrap();
    if label == "legacy-corpus-format" {
        assert_ne!(
            gate.status,
            GateStatusV5::Pass,
            "legacy corpus cannot pass current G-SPEC-01: {:?}",
            gate.errors
        );
    } else {
        assert_eq!(
            gate.status,
            GateStatusV5::Fail,
            "{label}: {:?}",
            gate.errors
        );
    }
    fs::write(&fixture.receipt_path, original).expect("restore receipt after rejection check");
}
