//! Ported unit tests from `tests/v5/test_conformance_shared.py`.

use std::collections::{BTreeMap, BTreeSet};
use std::path::PathBuf;

use ib_v5_conformance::shared::{
    CASE_KEYS, ConformanceProtocolError, CoverageRequirement, EvaluatorClosure,
    FAMILY_MAP_V2_BINDING_PROVENANCE, FAMILY_MAP_V2_SOURCE_PATH, FAMILY_MAP_V2_SUITE_PROVENANCE,
    FAMILY_MAP_V2_VERSION, FROZEN_SPEC_SHA256, LEGACY_FAMILY_MAP_SHA256, P1_EPISODE_KEYS,
    P2_EPISODE_KEYS, PINNED_PUBLIC_SCHEMA_SHA256, PROTOCOL, SourceFile, canonical_json_bytes,
    compare_production_result, compare_result_pair, freeze_case, freeze_source_closure,
    json_diff_paths, load_family_map, serialize_recipe_case, sha256_bytes, sha256_json,
    strict_json_loads, strict_jsonl_loads, validate_case_bijection, validate_evaluator_result,
    validate_public_case, verify_case_index, verify_complete_coverage,
};
use serde_json::{Map, Value, json};

fn make_p1_episode(episode_id: &str) -> Value {
    json!({
        "episode_id": episode_id,
        "phase1_manifest": {"phase_index": 1},
        "phase1_slices": {},
        "policy_card": {"spec_version": "5.0"},
        "q1_slice_ids_p1": [],
        "q2_slice_ids_p1": [],
    })
}

fn make_case(case_id: &str, op: &str, stim: Value) -> Value {
    json!({
        "protocol": PROTOCOL,
        "case_id": case_id,
        "operation": op,
        "suite_id": "v5_dev",
        "phase_index": 1,
        "public_episode": make_p1_episode("ep_01"),
        "rendered_card": "card-text",
        "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
        "stimulus": stim,
    })
}

fn make_valid_mutation() -> Value {
    json!({
        "mutation_kind": "NHI_GOVERNANCE_MUTATION",
        "target_entity_id": "nhi_01",
        "if_match": ["nhi_01@0123456789abcdef:1"],
        "cite": ["NHI-V5-01:ev_001"],
        "nhi_action": "ESCALATE",
        "resolved_owner_person_id": null,
        "owner_resolution_source": null,
    })
}

fn make_anchors_39() -> Map<String, Value> {
    let mut m = Map::new();
    for i in 1..=26 {
        m.insert(format!("RULE-{i:02}"), json!([format!("a_{i}")]));
    }
    for i in 1..=13 {
        m.insert(format!("BOUNDARY:B{i:02}"), json!([format!("b_{i}")]));
    }
    m
}

fn make_ok_result(
    wire_id: &str,
    case: &ib_v5_conformance::shared::FrozenCase,
    payload: Value,
    materialization: Value,
    coverage: Value,
) -> Value {
    json!({
        "protocol": PROTOCOL,
        "evaluator_id": wire_id,
        "case_id": case.case_id(),
        "input_sha256": case.input_sha256,
        "status": "ok",
        "payload": payload,
        "materialization": materialization,
        "coverage": coverage,
        "gaps": [],
    })
}

#[test]
fn canonical_json_and_strict_parser_reject_ambiguous_values() {
    let val = strict_json_loads(br#"{"b":1,"a":[true,null,"hi"]}"#).unwrap();
    assert_eq!(
        canonical_json_bytes(&val).unwrap(),
        br#"{"a":[true,null,"hi"],"b":1}"#
    );
    assert!(matches!(
        strict_json_loads(br#"{"a":1,"a":2}"#),
        Err(ConformanceProtocolError::Protocol(_))
    ));
    assert!(matches!(
        strict_json_loads(br#"{"a":NaN}"#),
        Err(ConformanceProtocolError::Protocol(_))
    ));
    assert!(matches!(
        strict_json_loads(br#"{"a":Infinity}"#),
        Err(ConformanceProtocolError::Protocol(_))
    ));
    assert!(matches!(
        strict_json_loads(br#"{"a":1e500}"#),
        Err(ConformanceProtocolError::Protocol(_))
    ));
    let lines = strict_jsonl_loads(b"{\"a\":1}\n\n{\"b\":2}\n").unwrap();
    assert_eq!(lines.len(), 2);
}

#[test]
fn case_projection_rejects_family_metadata_and_p1_future_fields() {
    let mut bad = make_case("c1", "derive_phase", json!({}));
    bad["public_episode"]
        .as_object_mut()
        .unwrap()
        .insert("phase2_slices".to_owned(), json!({}));
    assert!(validate_public_case(&bad).is_err());

    let mut bad2 = make_case("c1", "derive_phase", json!({}));
    bad2.as_object_mut()
        .unwrap()
        .insert("family_id".to_owned(), json!("F01"));
    assert!(validate_public_case(&bad2).is_err());

    let _ = (CASE_KEYS, P1_EPISODE_KEYS, P2_EPISODE_KEYS);
}

#[test]
fn recipe_serializer_preserves_patch_order_and_json_pointer_syntax() {
    let base = make_case("ep1/p1", "derive_phase", json!({}));
    let rec = json!({
        "recipe_id": "ep1/p1/val",
        "input_ref": "ep1/p1",
        "operation": "validate_submission",
        "stimulus": {
            "recipe": {
                "candidate_source": "independent_base",
                "base_scope": "phase_plan",
                "patches": [
                    {"op": "copy", "from": "/0/cite~1a~0b", "path": "/0/if_match"},
                    {"op": "remove", "path": "/1"}
                ]
            }
        }
    });
    let frozen = serialize_recipe_case(&base, &rec).unwrap();
    assert_eq!(frozen.case_id(), "ep1/p1/val");
    assert_eq!(frozen.public_case.operation, "validate_submission");

    // Invalid RFC 6901 pointer (~2) rejected
    let mut bad_rec = rec.clone();
    bad_rec["stimulus"]["recipe"]["patches"][0]["from"] = json!("/0/~2");
    assert!(serialize_recipe_case(&base, &bad_rec).is_err());
}

#[test]
fn case_index_and_bijection_checks() {
    let c1 = freeze_case(&make_case("c1", "derive_phase", json!({}))).unwrap();
    let c2 = freeze_case(&make_case("c2", "derive_phase", json!({}))).unwrap();
    let idx = vec![
        json!({"case_id": "c1", "canonical_case_sha256": c1.input_sha256}),
        json!({"case_id": "c2", "canonical_case_sha256": c2.input_sha256}),
    ];
    verify_case_index(&[c1.clone(), c2.clone()], &idx, "canonical_case_sha256").unwrap();

    let bad_idx = vec![json!({"case_id": "c1", "canonical_case_sha256": "0".repeat(64)})];
    assert!(
        verify_case_index(&[c1.clone(), c2.clone()], &bad_idx, "canonical_case_sha256").is_err()
    );

    let r1 = json!({"case_id": "c1", "evaluator_id": "full_spec_blind"});
    let r2 = json!({"case_id": "c2", "evaluator_id": "full_spec_blind"});
    let res_arr = [r1.clone(), r2];
    let map = validate_case_bijection(&[c1.clone(), c2.clone()], &res_arr, "full_spec").unwrap();
    assert_eq!(map.len(), 2);
    assert!(validate_case_bijection(&[c1, c2], &[r1], "full_spec").is_err());
}

#[test]
fn load_family_map_v1_and_v2_validation() {
    let mut expected = BTreeMap::new();
    let mut suites_v1 = Map::new();
    let mut suites_v2 = Map::new();
    for (idx, suite) in ["v5_dev", "v5_lite", "v5_core", "v5_full"]
        .iter()
        .enumerate()
    {
        let ep_sha = format!("{:064x}", idx + 1);
        expected.insert((*suite).to_owned(), ep_sha.clone());
        let episodes: Vec<Value> = (1..=12)
            .map(|f| json!({"episode_id": format!("ep_{f:02}"), "family_id": format!("F{f:02}")}))
            .collect();
        suites_v1.insert(
            (*suite).to_owned(),
            json!({
                "source_episode_sha256": ep_sha,
                "seal_episode_binding": {"episode_count": 12, "episodes_sha256": ep_sha},
                "episodes": episodes,
            }),
        );
        suites_v2.insert(
            (*suite).to_owned(),
            json!({
                "source_episode_sha256": ep_sha,
                "source_episode_binding": {
                    "episode_count": 12,
                    "episodes_sha256": ep_sha,
                    "provenance": FAMILY_MAP_V2_SUITE_PROVENANCE,
                },
                "episodes": episodes,
            }),
        );
    }
    let v1_bytes = canonical_json_bytes(&json!({"suites": Value::Object(suites_v1)})).unwrap();
    let (m1, _) = load_family_map(&v1_bytes, &expected).unwrap();
    assert_eq!(m1["v5_dev"]["ep_01"], "F01");

    let v2_bytes = canonical_json_bytes(&json!({
        "version": FAMILY_MAP_V2_VERSION,
        "binding_provenance": FAMILY_MAP_V2_BINDING_PROVENANCE,
        "source_mapping": {
            "mapping_reused": true,
            "path": FAMILY_MAP_V2_SOURCE_PATH,
            "sha256": LEGACY_FAMILY_MAP_SHA256,
            "use": "coverage metadata only; never evaluator input",
        },
        "suites": Value::Object(suites_v2),
    }))
    .unwrap();
    let (m2, _) = load_family_map(&v2_bytes, &expected).unwrap();
    assert_eq!(m2, m1);

    let bad_v2 = br#"{"version":"unknown","suites":{}}"#;
    assert!(load_family_map(bad_v2, &expected).is_err());
}

#[test]
fn blind_pair_materialization_mismatch_and_production_comparison() {
    let mut_row = make_valid_mutation();
    let c_recipe = freeze_case(&make_case(
        "c_rec",
        "validate_submission",
        json!({
            "recipe": {
                "candidate_source": "independent_base",
                "base_scope": "phase_plan",
                "patches": []
            }
        }),
    ))
    .unwrap();
    let cov = json!({
        "phase_targets": [],
        "diagnostics": [],
        "rules": ["NHI-V5-01"],
        "safety_cases": [],
        "snapshots": [],
    });
    let mat_a = json!({"base_plan": [mut_row], "submitted_plan": [mut_row]});
    let mat_b = json!({"base_plan": [mut_row], "submitted_plan": []});
    let payload = json!({"first_failing_stage": null, "diagnostics": []});
    let full = make_ok_result(
        "full_spec_blind",
        &c_recipe,
        payload.clone(),
        mat_a.clone(),
        cov.clone(),
    );
    let agent = make_ok_result(
        "agent_view_only",
        &c_recipe,
        payload.clone(),
        mat_b,
        cov.clone(),
    );

    let mut hashes = BTreeMap::new();
    hashes.insert("full_spec".to_owned(), "a".repeat(64));
    hashes.insert("agent_view".to_owned(), "b".repeat(64));

    let (_, _, cmp) = compare_result_pair(&c_recipe, &full, &agent, &hashes).unwrap();
    assert_eq!(cmp.status, "MISMATCH");
    assert_eq!(cmp.mismatch_paths, vec!["$.materialization"]);

    // Matching pair + production comparison
    let agent_match = make_ok_result(
        "agent_view_only",
        &c_recipe,
        payload.clone(),
        mat_a.clone(),
        cov.clone(),
    );
    let (l, r, cmp_ok) = compare_result_pair(&c_recipe, &full, &agent_match, &hashes).unwrap();
    assert_eq!(cmp_ok.status, "MATCH");

    hashes.insert("production".to_owned(), "c".repeat(64));
    let prod_bad = make_ok_result(
        "production",
        &c_recipe,
        json!({"first_failing_stage": 1, "diagnostics": ["COMPLETENESS_VIOLATION"]}),
        mat_a,
        json!({"phase_targets": [], "diagnostics": ["COMPLETENESS_VIOLATION"], "rules": ["NHI-V5-01"], "safety_cases": [], "snapshots": []}),
    );
    let (_, prod_cmp) = compare_production_result(&c_recipe, (&l, &r), &prod_bad, &hashes).unwrap();
    assert_eq!(prod_cmp.status, "MISMATCH");
    assert!(
        prod_cmp
            .mismatch_paths
            .contains(&"$.payload/first_failing_stage".to_owned())
    );
}

#[test]
fn result_validation_rejects_answer_hints_and_non_ok_null_metrics_rules() {
    let c_derive = freeze_case(&make_case("c_drv", "derive_phase", json!({}))).unwrap();
    let mut_row = make_valid_mutation();
    let bad_derive = make_ok_result(
        "full_spec_blind",
        &c_derive,
        json!({
            "mutations": [mut_row],
            "policy_card_anchor_map": Value::Object(make_anchors_39()),
            "expected": true,
        }),
        Value::Null,
        json!({
            "phase_targets": [{
                "suite_id": "v5_dev",
                "episode_id": "ep_01",
                "phase_index": 1,
                "mutation_kind": "NHI_GOVERNANCE_MUTATION",
                "target_entity_id": "nhi_01",
            }],
            "diagnostics": [],
            "rules": ["NHI-V5-01"],
            "safety_cases": [],
            "snapshots": [],
        }),
    );
    assert!(validate_evaluator_result(&bad_derive, &c_derive, "full_spec").is_err());

    // score_trace with ok cannot have null metrics, underdefined with gap can
    let c_trace = freeze_case(&make_case(
        "c_tr",
        "score_trace",
        json!({"mode": "COUPLED", "events": []}),
    ))
    .unwrap();
    let null_metrics = json!({
        "p1_first_stage_snapshot_ok": null, "G-SRR@first_stage": null, "Sem-SRR@commit": null,
        "Action-SRR@last_stage": null, "G-PROP": null, "G-SRR": null, "p2_observed": null,
        "both_phases_staged": null, "p1_staged": null, "P1-Action-SRR@last_stage": null, "repair_lift": null,
    });
    let ok_null = make_ok_result(
        "full_spec_blind",
        &c_trace,
        json!({"metrics": null_metrics, "snapshot_derivations": []}),
        json!({"event_plans": []}),
        json!({"phase_targets": [], "diagnostics": [], "rules": [], "safety_cases": [], "snapshots": []}),
    );
    assert!(validate_evaluator_result(&ok_null, &c_trace, "full_spec").is_err());

    let mut underdefined = ok_null;
    underdefined["status"] = json!("underdefined");
    underdefined["gaps"] = json!([{
        "code": "OTHER",
        "operation": "score_trace",
        "case_id": "c_tr",
        "path": "",
        "detail": "gap",
    }]);
    validate_evaluator_result(&underdefined, &c_trace, "full_spec").unwrap();
}

#[test]
fn json_diff_paths_distinguishes_int_and_float_like_python() {
    let diffs = json_diff_paths(&json!(1), &json!(1.0), "$.payload");
    assert_eq!(diffs, vec!["$.payload"]);
    let same_floats = json_diff_paths(&json!(1.0), &json!(1.000), "$.payload");
    assert!(same_floats.is_empty());
}

#[test]
fn reduced_coverage_contract_is_rejected_and_normative_contract_is_full_spec_only() {
    let c = freeze_case(&make_case("c1", "derive_phase", json!({}))).unwrap();
    let req = CoverageRequirement {
        families: BTreeSet::from(["F01".to_owned()]),
        anchor_labels: BTreeSet::new(),
        phase_targets: BTreeSet::new(),
        diagnostics: BTreeSet::new(),
        rules: BTreeSet::new(),
        safety_cases: BTreeSet::new(),
        safety_kinds: BTreeSet::new(),
        safety_truth_pairs: BTreeSet::from([(false, false)]),
        trace_case_ids: BTreeSet::new(),
        snapshot_cells: BTreeSet::new(),
    };
    let err = verify_complete_coverage(&[c], &[], &req, &BTreeMap::new()).unwrap_err();
    assert!(matches!(err, ConformanceProtocolError::Coverage(_)));

    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .to_path_buf();
    let spec_sha = sha256_bytes(&std::fs::read(root.join("SPEC-v5.md")).unwrap());
    assert_eq!(spec_sha, FROZEN_SPEC_SHA256);
    let closure = EvaluatorClosure {
        evaluator_id: "agent_view".to_owned(),
        source_root: root,
        entrypoint_module: "tests.conformance.v5.fresh_card_only_v2.worker".to_owned(),
        files: vec![SourceFile::new(
            "src/identitybench/v5/schemas.py",
            PINNED_PUBLIC_SCHEMA_SHA256,
        )],
        import_roots: vec!["src".to_owned()],
        python_executable: "python3".to_owned(),
        dependency_lock_sha256: String::new(),
        normative_contract_id: Some(
            ib_v5_schemas::normative_contract::V5_PROMOTED_DRAFT5_CONTRACT_ID.to_owned(),
        ),
        python_version: None,
        cache_tag: None,
    };
    assert!(freeze_source_closure(&closure).is_err());
    let _ = sha256_json(&json!({"a": 1})).unwrap();
}
