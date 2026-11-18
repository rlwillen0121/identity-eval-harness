use std::path::Path;

use crate::acceptance_gates::{
    SealEraV5, cal_03_gate, double_build_gate, iga_review_gate, inventory_gate, parse_strict_json,
    seal_gate, tok_gate,
};
use crate::{GateStatusV5, repository_root};
use ib_v5_generator::{DatasetArtifactsV5, load_v5_dataset};
use serde_json::{Map, Value, json};
use tempfile::tempdir;

fn fixture(suite_path: &str) -> DatasetArtifactsV5 {
    let root = repository_root();
    load_v5_dataset(&root.join(suite_path)).expect("checked-in sealed fixture loads")
}

fn valid_review_manifest(artifacts: &DatasetArtifactsV5, suite_id: &str) -> Value {
    let episodes_sha = ib_core::hashing::sha256_hex(&artifacts.episodes_bytes);
    let criteria = json!({
        "plausibility": true,
        "policy_evidence_necessity": true,
        "unique_answerability": true,
        "negative_assertions": true,
        "shortcut_resistance": true,
    });
    json!({
        "suite_id": suite_id,
        "episodes_sha256": episodes_sha,
        "reviewers": [
            {"reviewer_id":"reviewer-a", "qualifications_summary":"IGA practitioner", "independent":true},
            {"reviewer_id":"reviewer-b", "qualifications_summary":"IGA practitioner", "independent":true}
        ],
        "episode_reviews": artifacts.episodes.iter().map(|episode| json!({
            "episode_id": episode.episode_id,
            "reviewer_ids": ["reviewer-a", "reviewer-b"],
            "review_date": "2026-11-17",
            "disposition": "APPROVED",
            "criteria_checked": criteria.clone(),
            "findings": []
        })).collect::<Vec<_>>()
    })
}

#[test]
fn inventory_gate_reports_complete_frozen_sets() {
    let gate = inventory_gate();
    assert_eq!(gate.status, GateStatusV5::Pass, "{:?}", gate.errors);
    assert_eq!(gate.evidence["rule_count"], 26);
    assert_eq!(gate.evidence["category_count"], 43);
    assert_eq!(gate.evidence["policy_anchor_count"], 39);
}

#[test]
fn disabled_double_build_is_unknown_and_does_not_claim_parity() {
    let artifacts = fixture("datasets/v5_dev");
    let gate = double_build_gate(&artifacts, false);
    assert_eq!(gate.status, GateStatusV5::Unknown);
    assert_eq!(gate.evidence["double_build"], false);
    assert_eq!(
        gate.errors,
        vec!["--double-build was not requested".to_owned()]
    );
}

#[test]
fn local_seal_gate_checks_read_only_fixture_against_repository_norms() {
    let artifacts = fixture("datasets/v5_dev");
    let (gate, era) = seal_gate(&artifacts, &repository_root());
    assert_eq!(gate.status, GateStatusV5::Pass, "{:?}", gate.errors);
    assert_eq!(era, SealEraV5::HistoricalDraft4Base);
    assert_eq!(gate.evidence["seed_disjointness_verified"], true);
    assert_eq!(gate.evidence["candidate_claim_eligible"], false);
}

#[test]
fn double_build_gate_rebuilds_dev_fixture_without_mutating_it() {
    let artifacts = fixture("datasets/v5_dev");
    let original_seal = artifacts.seal_bytes.clone();
    let original_episodes = artifacts.episodes_bytes.clone();
    let original_oracles = artifacts.oracles_bytes.clone();
    let gate = double_build_gate(&artifacts, true);
    assert_eq!(gate.status, GateStatusV5::Pass, "{:?}", gate.errors);
    assert_eq!(gate.evidence["builds_identical_episodes"], true);
    assert_eq!(gate.evidence["builds_identical_oracles"], true);
    assert_eq!(artifacts.seal_bytes, original_seal);
    assert_eq!(artifacts.episodes_bytes, original_episodes);
    assert_eq!(artifacts.oracles_bytes, original_oracles);
}

#[test]
fn token_gate_checks_prompt_and_witness_compatibility_for_sealed_dev() {
    let artifacts = fixture("datasets/v5_dev");
    let (seal, era) = seal_gate(&artifacts, &repository_root());
    assert_eq!(seal.status, GateStatusV5::Pass, "{:?}", seal.errors);
    let gate = tok_gate(&artifacts, era);
    assert_eq!(gate.status, GateStatusV5::Pass, "{:?}", gate.errors);
    assert_eq!(gate.evidence["token_ceiling_applied"], false);
    assert_eq!(gate.evidence["witness_preflight_episodes_checked"], 12);
    assert_eq!(gate.evidence["witness_preflight_compatible_count"], 12);
}

#[test]
fn draft5_token_gate_matches_python_witness_evidence() {
    let artifacts = fixture("datasets/v5_draft5/v5_dev");
    let gate = tok_gate(&artifacts, SealEraV5::Draft5Consensus);
    assert_eq!(gate.status, GateStatusV5::Pass, "{:?}", gate.errors);
    assert_eq!(gate.evidence["max_witness_request_tokens_o200k"], 34_837);
}

#[test]
fn review_gate_handles_development_missing_held_out_missing_and_valid_manifest() {
    let artifacts = fixture("datasets/v5_dev");
    let dev = iga_review_gate(&artifacts, "v5_dev", None);
    assert_eq!(dev.status, GateStatusV5::Pass, "{:?}", dev.errors);

    let held_out = iga_review_gate(&artifacts, "v5_core", None);
    assert_eq!(held_out.status, GateStatusV5::Unknown);
    assert_eq!(held_out.evidence["held_out_review_required"], true);

    let temp = tempdir().expect("temporary review manifest directory");
    let path = temp.path().join("review.json");
    std::fs::write(
        &path,
        serde_json::to_vec(&valid_review_manifest(&artifacts, "v5_dev")).unwrap(),
    )
    .unwrap();
    let valid = iga_review_gate(&artifacts, "v5_dev", Some(&path));
    assert_eq!(valid.status, GateStatusV5::Pass, "{:?}", valid.errors);
    assert_eq!(valid.evidence["reviewer_count"], 2);
    assert_eq!(valid.evidence["reviewed_episode_count"], 12);
}

#[test]
fn review_gate_fails_sensitive_and_duplicate_key_inputs() {
    let artifacts = fixture("datasets/v5_dev");
    let temp = tempdir().expect("temporary review manifest directory");
    let sensitive_dir = temp.path().join("private-token-store");
    std::fs::create_dir(&sensitive_dir).unwrap();
    let sensitive_path = sensitive_dir.join("review.json");
    std::fs::write(&sensitive_path, b"{}").unwrap();
    let sensitive = iga_review_gate(&artifacts, "v5_dev", Some(&sensitive_path));
    assert_eq!(sensitive.status, GateStatusV5::Fail);
    assert!(sensitive.errors[0].contains("sensitive"));

    let parse_error = parse_strict_json(br#"{"outer":{"same":1,"same":2}}"#)
        .expect_err("duplicate keys must not be silently accepted");
    assert!(parse_error.contains("duplicate JSON object key"));
}

#[test]
fn cal_03_profiles_are_verified_but_live_contrasts_remain_unknown() {
    let gate = cal_03_gate();
    assert_eq!(gate.status, GateStatusV5::Unknown, "{:?}", gate.errors);
    assert_eq!(gate.evidence["profiles_verified"], true);
    assert_eq!(
        gate.errors,
        vec![
            "ablation, topology, and steering disclosure contrasts require matched model runs"
                .to_owned()
        ]
    );
}

#[test]
fn draft5_seal_mutation_is_detected_without_touching_the_fixture() {
    let mut artifacts = fixture("datasets/v5_draft5/v5_dev");
    artifacts.seal.insert(
        "protocol_spec_sha256".to_owned(),
        Value::String("0".repeat(64)),
    );
    let (gate, era) = seal_gate(&artifacts, &repository_root());
    assert_eq!(era, SealEraV5::Draft5Consensus);
    assert_eq!(gate.status, GateStatusV5::Fail);
    assert!(gate.errors.iter().any(|error| {
        error.contains("seal.protocol_spec_sha256 does not match local SPEC-v5.0-draft.5.md")
    }));
}

#[test]
fn production_readiness_qualification_requires_exact_closure_and_runtime_binding() {
    let root = tempdir().expect("temporary public evidence root");
    let evidence_path = root
        .path()
        .join("reports/v5/conformance/production-readiness.json");
    std::fs::create_dir_all(evidence_path.parent().unwrap()).unwrap();

    let closure_sha = "a".repeat(64);
    let runtime_sha = "b".repeat(64);
    let readiness = json!({
        "record_type": "production_adapter_readiness",
        "status": "QUALIFIED_ELIGIBLE",
        "source_closure_sha256": closure_sha,
        "runtime_manifest_sha256": runtime_sha,
    });
    let raw = serde_json::to_vec(&readiness).unwrap();
    let evidence_sha = ib_v5_conformance::shared::sha256_bytes(&raw);
    std::fs::write(&evidence_path, &raw).unwrap();

    let qualification = json!({
        "kind": "production_readiness",
        "evidence": {
            "path": "reports/v5/conformance/production-readiness.json",
            "sha256": evidence_sha,
        }
    });
    let runtime_manifest = Map::from_iter([("manifest_sha256".to_owned(), json!(runtime_sha))]);
    let mut errors = Vec::new();
    let mut gaps = Vec::new();
    let mut provenance = Map::new();
    let valid = crate::shared_origin::validate_shared_qualification(
        "production",
        &qualification,
        "production_v1",
        "production_adapter",
        &closure_sha,
        &"c".repeat(64),
        &"d".repeat(64),
        None,
        None,
        &runtime_manifest,
        root.path(),
        &mut errors,
        &mut gaps,
        &mut provenance,
    );
    assert!(valid, "errors={errors:?}; gaps={gaps:?}");
    assert!(errors.is_empty());
    assert!(gaps.is_empty());

    let mut mismatched_runtime = Map::new();
    mismatched_runtime.insert("manifest_sha256".to_owned(), json!("e".repeat(64)));
    let mut mismatch_errors = Vec::new();
    let mut mismatch_gaps = Vec::new();
    let invalid = crate::shared_origin::validate_shared_qualification(
        "production",
        &qualification,
        "production_v1",
        "production_adapter",
        &closure_sha,
        &"c".repeat(64),
        &"d".repeat(64),
        None,
        None,
        &mismatched_runtime,
        root.path(),
        &mut mismatch_errors,
        &mut mismatch_gaps,
        &mut Map::new(),
    );
    assert!(!invalid);
    assert!(
        mismatch_gaps
            .iter()
            .any(|gap| gap.contains("exact production closure"))
    );
}

#[test]
fn author_origin_qualification_unknown_shapes_fail_closed() {
    let mut errors = Vec::new();
    let mut gaps = Vec::new();
    let mut provenance = Map::new();
    let valid = crate::shared_origin::validate_shared_qualification(
        "agent_view_only",
        &json!({"kind": "author_origin", "adjudication": {}, "unexpected": true}),
        "fresh_card_only_v2",
        "fresh_independent_card_only_author_v2",
        &"a".repeat(64),
        &"b".repeat(64),
        &"c".repeat(64),
        None,
        None,
        &Map::new(),
        Path::new("."),
        &mut errors,
        &mut gaps,
        &mut provenance,
    );
    assert!(!valid);
    assert!(errors.is_empty());
    assert!(gaps.iter().any(|gap| gap.contains("unsupported shape")));
    assert!(provenance.is_empty());
}
