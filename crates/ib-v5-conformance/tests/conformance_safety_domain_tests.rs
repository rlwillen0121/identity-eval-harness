//! Ported unit tests from `tests/v5/test_conformance_safety_domain.py`.

use std::path::PathBuf;

use ib_v5_conformance::safety_domain::{
    BoundBytesV1, MaterializationGapError, PINNED_SCHEMA_SHA256_V1,
    ReferenceMaterializationReceiptV1, agree_reference_materializations_v1,
    build_delivered_views_v1, inspect_agreed_stage2_domain_v1, inspect_stage2_domain_v1,
};
use ib_v5_conformance::shared::{
    P1_EPISODE_KEYS, P2_EPISODE_KEYS, PROTOCOL, canonical_json_bytes, sha256_bytes,
    strict_jsonl_loads,
};
use serde_json::{Map, Value, json};

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crates")
        .parent()
        .expect("repo root")
        .to_path_buf()
}

fn bound(bytes: Vec<u8>) -> BoundBytesV1 {
    let sha256 = sha256_bytes(&bytes);
    BoundBytesV1 {
        data: bytes,
        sha256,
    }
}

fn load_real_episode(index: usize) -> (Map<String, Value>, String) {
    let root = repo_root();
    let ep_raw = std::fs::read(
        root.join("reports/v5/conformance/inputs/draft5_v1/v5_dev/selected-episodes.jsonl"),
    )
    .expect("read selected-episodes.jsonl");
    let rows = strict_jsonl_loads(&ep_raw).expect("parse jsonl");
    let ep = rows[index].as_object().expect("ep obj").clone();
    let card = std::fs::read_to_string(
        root.join("reports/v5/conformance/inputs/draft5_v1/v5_dev/rendered-card.txt"),
    )
    .expect("read card");
    (ep, card)
}

fn build_safety_case_bytes(
    ep: &Map<String, Value>,
    card: &str,
    phase: i64,
    kind: &str,
    target_id: &str,
    submitted_plan: Value,
) -> Vec<u8> {
    let mut pub_ep = Map::new();
    let keys: &[&str] = if phase == 1 {
        &P1_EPISODE_KEYS
    } else {
        &P2_EPISODE_KEYS
    };
    for k in keys {
        pub_ep.insert((*k).to_owned(), ep[*k].clone());
    }
    let case = json!({
        "protocol": PROTOCOL,
        "case_id": format!("safety_{kind}_{target_id}"),
        "operation": "safety",
        "suite_id": "v5_dev",
        "phase_index": phase,
        "public_episode": Value::Object(pub_ep),
        "rendered_card": card,
        "schema_sha256": PINNED_SCHEMA_SHA256_V1,
        "stimulus": {
            "target_selector": {
                "mutation_kind": kind,
                "target_entity_id": target_id,
            },
            "submitted_plan": submitted_plan,
        }
    });
    canonical_json_bytes(&case).unwrap()
}

fn make_receipt(
    evaluator_id: &str,
    case_bytes: &[u8],
    card: &str,
    stimulus: &Value,
    selector: &Value,
    base_row: &Value,
    submitted_row: &Value,
) -> ReferenceMaterializationReceiptV1 {
    let base_plan = canonical_json_bytes(&Value::Array(vec![base_row.clone()])).unwrap();
    let sub_plan = canonical_json_bytes(&Value::Array(vec![submitted_row.clone()])).unwrap();
    let sel_base = canonical_json_bytes(base_row).unwrap();
    let sel_sub = canonical_json_bytes(submitted_row).unwrap();
    let stim_bytes = canonical_json_bytes(stimulus).unwrap();
    let loc_bytes = canonical_json_bytes(selector).unwrap();
    let schemas_bytes = std::fs::read(repo_root().join("src/identitybench/v5/schemas.py"))
        .expect("read schemas.py");
    ReferenceMaterializationReceiptV1 {
        evaluator_id: evaluator_id.to_owned(),
        case: bound(case_bytes.to_vec()),
        schema: bound(schemas_bytes),
        card: bound(card.as_bytes().to_vec()),
        stimulus: bound(stim_bytes.clone()),
        base_plan: bound(base_plan.clone()),
        pre_patch_plan: bound(base_plan),
        submitted_plan: bound(sub_plan),
        selected_base_atomic: bound(sel_base),
        selected_submitted_atomic: bound(sel_sub),
        source_stimulus: bound(stim_bytes),
        target_locator: bound(loc_bytes),
    }
}

#[test]
fn delivered_views_v1_reconstructs_phase1_and_phase2_from_real_draft5_episode() {
    let (ep, card) = load_real_episode(0);
    let case_bytes =
        build_safety_case_bytes(&ep, &card, 2, "JIT_DECISION_MUTATION", "req_01", json!([]));
    let views = build_delivered_views_v1(&case_bytes).expect("build_delivered_views_v1");
    assert!(!views.phase1.rows.is_empty());
    let p2 = views.phase2.expect("phase2 view");
    assert!(p2.get("jit_requests", "req_01").is_some());
}

#[test]
fn receipt_agreement_and_stage2_domain_eligibility_and_fk_missing_checks() {
    let (ep, card) = load_real_episode(0);
    let base_row = json!({
        "mutation_kind": "NHI_GOVERNANCE_MUTATION",
        "target_entity_id": "nhi_01",
        "if_match": ["nhi_01@0123456789abcdef:1"],
        "cite": ["NHI-V5-01:ev_001"],
        "nhi_action": "ESCALATE",
        "resolved_owner_person_id": null,
        "owner_resolution_source": null,
    });
    let mut bad_fk_row = base_row.clone();
    bad_fk_row["resolved_owner_person_id"] = json!("pers_99999");
    bad_fk_row["nhi_action"] = json!("ATTRIBUTE_OWNER");
    bad_fk_row["owner_resolution_source"] = json!("parent_nhi_custody");

    let selector = json!({
        "mutation_kind": "NHI_GOVERNANCE_MUTATION",
        "target_entity_id": "nhi_01",
    });
    let submitted_plan = json!([bad_fk_row]);
    let case_bytes = build_safety_case_bytes(
        &ep,
        &card,
        1,
        "NHI_GOVERNANCE_MUTATION",
        "nhi_01",
        submitted_plan.clone(),
    );
    let stimulus = json!({
        "target_selector": selector,
        "submitted_plan": submitted_plan,
    });
    let r_full = make_receipt(
        "full_spec_blind",
        &case_bytes,
        &card,
        &stimulus,
        &selector,
        &base_row,
        &bad_fk_row,
    );
    let r_agent = make_receipt(
        "agent_view_only",
        &case_bytes,
        &card,
        &stimulus,
        &selector,
        &base_row,
        &bad_fk_row,
    );

    let obs = inspect_agreed_stage2_domain_v1(&case_bytes, &[r_full.clone(), r_agent.clone()])
        .expect("inspect agreed");
    // In ep[0], check whether hr_persons was delivered in phase 1 or remains a CARD_GAP
    let views = build_delivered_views_v1(&case_bytes).unwrap();
    if views.phase1.known_tables.contains("hr_persons") {
        assert_eq!(obs.stage2_status, "INELIGIBLE");
        assert!(
            obs.criterion_gaps
                .iter()
                .any(|g| g.code == "FK_MISSING" && g.criterion == "nhi_owner_person_exists")
        );
    } else {
        assert_eq!(obs.stage2_status, "UNKNOWN");
        assert!(
            obs.criterion_gaps
                .iter()
                .any(|g| g.code == "CARD_GAP" && g.criterion == "nhi_owner_person_exists")
        );
    }

    // Check FK_MISSING explicitly against a table known in ep[0] phase 1 (target_nhis)
    assert!(views.phase1.known_tables.contains("target_nhis"));
    let missing_nhi_row = json!({
        "mutation_kind": "NHI_GOVERNANCE_MUTATION",
        "target_entity_id": "nhi_99999",
        "if_match": ["nhi_99999@0123456789abcdef:1"],
        "cite": ["NHI-V5-01:ev_001"],
        "nhi_action": "ESCALATE",
        "resolved_owner_person_id": null,
        "owner_resolution_source": null,
    });
    let missing_nhi_case = build_safety_case_bytes(
        &ep,
        &card,
        1,
        "NHI_GOVERNANCE_MUTATION",
        "nhi_99999",
        json!([missing_nhi_row]),
    );
    let obs_missing_nhi = inspect_stage2_domain_v1(
        &missing_nhi_case,
        &canonical_json_bytes(&missing_nhi_row).unwrap(),
        &"a".repeat(64),
    )
    .unwrap();
    assert_eq!(obs_missing_nhi.stage2_status, "INELIGIBLE");
    assert!(
        obs_missing_nhi
            .criterion_gaps
            .iter()
            .any(|g| g.code == "FK_MISSING" && g.criterion == "target_nhi_exists")
    );

    // Valid ESCALATE mutation is ELIGIBLE
    let case_ok_bytes = build_safety_case_bytes(
        &ep,
        &card,
        1,
        "NHI_GOVERNANCE_MUTATION",
        "nhi_01",
        json!([base_row]),
    );
    let sel_bytes = canonical_json_bytes(&base_row).unwrap();
    let agree_sha = "a".repeat(64);
    let obs_ok = inspect_stage2_domain_v1(&case_ok_bytes, &sel_bytes, &agree_sha).unwrap();
    assert_eq!(obs_ok.stage2_status, "ELIGIBLE");
    assert!(obs_ok.criterion_gaps.is_empty());

    // Disagreement between references is rejected before Stage 2 runs
    let mut r_agent_tampered = r_agent;
    r_agent_tampered.submitted_plan = bound(b"[]".to_vec());
    let err =
        agree_reference_materializations_v1(&case_bytes, &[r_full, r_agent_tampered]).unwrap_err();
    assert!(matches!(err, MaterializationGapError(_)));
}

#[test]
fn raw_atomic_json_rejects_duplicate_keys_and_non_canonical_bytes() {
    let (ep, card) = load_real_episode(0);
    let case_bytes = build_safety_case_bytes(
        &ep,
        &card,
        1,
        "NHI_GOVERNANCE_MUTATION",
        "nhi_01",
        json!([]),
    );
    let dup_json =
        br#"{"mutation_kind":"NHI_GOVERNANCE_MUTATION","mutation_kind":"NHI_GOVERNANCE_MUTATION"}"#;
    let obs = inspect_stage2_domain_v1(&case_bytes, dup_json, &"b".repeat(64)).unwrap();
    assert_eq!(obs.stage2_status, "INELIGIBLE");
    assert_eq!(obs.criterion_gaps[0].code, "SCHEMA_VALIDATION_ERROR");

    // Non-canonical whitespace in candidate rejected with MaterializationGapError
    let non_canon = b"{ }";
    let err = inspect_stage2_domain_v1(&case_bytes, non_canon, &"b".repeat(64)).unwrap_err();
    assert!(err.to_string().contains("canonical-json-v1"));
}

#[test]
fn selector_mismatch_and_unknown_schema_context_handling() {
    let (ep, card) = load_real_episode(0);
    let case_bytes = build_safety_case_bytes(
        &ep,
        &card,
        1,
        "NHI_GOVERNANCE_MUTATION",
        "nhi_01",
        json!([]),
    );
    let wrong_target = canonical_json_bytes(&json!({
        "mutation_kind": "NHI_GOVERNANCE_MUTATION",
        "target_entity_id": "nhi_99",
        "if_match": ["nhi_99@0123456789abcdef:1"],
        "cite": ["NHI-V5-01:ev_001"],
        "nhi_action": "ESCALATE",
        "resolved_owner_person_id": null,
        "owner_resolution_source": null,
    }))
    .unwrap();
    let err = inspect_stage2_domain_v1(&case_bytes, &wrong_target, &"c".repeat(64)).unwrap_err();
    assert!(err.to_string().contains("target selector"));

    // Bad schema digest returns UNKNOWN
    let bad_case = canonical_json_bytes(&json!({
        "schema_sha256": "0".repeat(64),
        "rendered_card": card,
        "stimulus": {},
    }))
    .unwrap();
    let obs = inspect_stage2_domain_v1(&bad_case, &wrong_target, &"c".repeat(64)).unwrap();
    assert_eq!(obs.stage2_status, "UNKNOWN");
    assert_eq!(obs.criterion_gaps[0].criterion, "schema_context");
}
