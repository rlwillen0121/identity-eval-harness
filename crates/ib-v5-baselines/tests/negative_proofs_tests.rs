use std::collections::{BTreeMap, BTreeSet};
use std::sync::OnceLock;

use ib_v5_baselines::{
    NegativeProofReportV5, RuleBoundaryWitnessResultV5, build_negative_witnesses,
    run_negative_proofs,
};
use serde_json::Value;

static REPORT: OnceLock<NegativeProofReportV5> = OnceLock::new();

fn report() -> &'static NegativeProofReportV5 {
    REPORT.get_or_init(run_negative_proofs)
}

#[test]
fn directed_witnesses_are_typed_and_cover_all_classifier_boundaries() {
    let witnesses = build_negative_witnesses();
    assert_eq!(witnesses.len(), 43);
    let structural: Vec<_> = witnesses
        .iter()
        .filter(|witness| witness.expected_stage < 5)
        .collect();
    let semantic: Vec<_> = witnesses
        .iter()
        .filter(|witness| witness.expected_stage == 5)
        .collect();
    assert_eq!(structural.len(), 4);
    assert_eq!(semantic.len(), 39);
    assert!(
        semantic
            .iter()
            .all(|witness| witness.gold.len() == 1 && witness.submitted.len() == 1)
    );
    assert!(
        semantic
            .iter()
            .all(|witness| !witness.changed_fields.is_empty())
    );

    let single = semantic
        .iter()
        .filter(|witness| witness.changed_fields.len() == 1)
        .count();
    let coupled = semantic
        .iter()
        .filter(|witness| witness.changed_fields.len() > 1)
        .count();
    assert_eq!(single, 32);
    assert_eq!(coupled, 7);

    let by_id: BTreeMap<_, _> = semantic
        .iter()
        .map(|witness| (witness.witness_id.as_str(), *witness))
        .collect();
    for id in [
        "jit_p1_scope",
        "jit_p2_ticket",
        "jit_p3_sod",
        "jit_p4_approver",
    ] {
        assert_eq!(
            by_id[id].changed_fields,
            [
                "jit_decision",
                "approver_account_id",
                "approved_ttl_s",
                "external_sync_status",
            ]
        );
    }
    for id in [
        "delegation_d1_principal",
        "delegation_d2_intersection",
        "delegation_d3_workflow_sod",
    ] {
        assert_eq!(
            by_id[id].changed_fields,
            ["delegation_decision", "approved_ttl_s"]
        );
    }
}

#[test]
fn report_rejects_every_boundary_and_matches_python_report_surface() {
    let report = report();
    assert_eq!(report.attempted, 43);
    assert_eq!(report.rejected, 43);
    assert_eq!(report.accepted, 0);
    assert_eq!(report.error_count, 0);
    assert_eq!(report.pass_rate, 0.0);
    assert_eq!(report.structural_attempted, 4);
    assert_eq!(report.structural_rejected, 4);
    assert_eq!(report.semantic_attempted, 39);
    assert_eq!(report.semantic_rejected, 39);
    assert_eq!(report.classifier_single_field_attempted, 32);
    assert_eq!(report.classifier_single_field_rejected, 32);
    assert_eq!(report.classifier_single_field_accepted, 0);
    assert_eq!(report.classifier_single_field_pass_rate, 0.0);
    assert_eq!(report.classifier_coupled_attempted, 7);
    assert_eq!(report.classifier_coupled_rejected, 7);
    assert_eq!(report.classifier_coupled_accepted, 0);
    assert_eq!(report.classifier_coupled_pass_rate, 0.0);
    assert!(report.complete());
    assert!(report.errors.is_empty());
    assert!(report.missing_mutation_kinds.is_empty());
    assert!(report.missing_violation_categories.is_empty());
    assert!(report.missing_rule_ids.is_empty());
    assert_eq!(report.covered_mutation_kinds.len(), 12);
    assert_eq!(report.covered_rule_ids.len(), 26);
    assert_eq!(report.covered_violation_categories.len(), 43);

    let mapping = report.to_mapping();
    let object = mapping.as_object().expect("report mapping is an object");
    for key in [
        "attempted",
        "rejected",
        "accepted",
        "error_count",
        "pass_rate",
        "covered_mutation_kinds",
        "missing_mutation_kinds",
        "covered_rule_ids",
        "missing_rule_ids",
        "covered_violation_categories",
        "missing_violation_categories",
        "rule_boundary_attribution",
        "classifier_rule_attribution",
        "rule_witness_attempted",
        "rule_witness_matched",
        "rule_witness_errors",
        "rule_witnesses",
        "structural_attempted",
        "structural_rejected",
        "semantic_attempted",
        "semantic_rejected",
        "classifier_single_field_attempted",
        "classifier_single_field_rejected",
        "classifier_single_field_accepted",
        "classifier_single_field_pass_rate",
        "classifier_single_field_covered_mutation_kinds",
        "classifier_single_field_missing_mutation_kinds",
        "classifier_coupled_attempted",
        "classifier_coupled_rejected",
        "classifier_coupled_accepted",
        "classifier_coupled_pass_rate",
        "results",
        "errors",
        "complete",
    ] {
        assert!(object.contains_key(key), "missing report field {key}");
    }
    assert_eq!(object["complete"], Value::Bool(true));
}

#[test]
fn directed_boundary_proof_mapping_matches_python_validation_projection() {
    let report = report();
    let mapping = report.directed_boundary_proof_mapping();
    // The fixture generator canonicalizes only the three OPT-V5-01 evidence
    // aliases whose Python dict/frozenset repr order changes with PYTHONHASHSEED.
    let python_draft5_dev: Value = serde_json::from_str(include_str!(
        "fixtures/negative_proof_python_draft5_dev.json"
    ))
    .expect("Python Draft5 dev projection fixture parses");
    assert_eq!(mapping, python_draft5_dev);
    let object = mapping.as_object().expect("proof mapping is an object");
    let mut keys: Vec<_> = object.keys().map(String::as_str).collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        [
            "accepted",
            "actual_rule_boundary_attribution",
            "attempted",
            "classifier_coupled",
            "classifier_rule_labels",
            "classifier_single_field",
            "complete",
            "error_count",
            "errors",
            "missing_classifier_rule_labels",
            "missing_mutation_kinds",
            "missing_verified_rule_ids",
            "missing_violation_categories",
            "mutation_kinds",
            "pass_rate",
            "rejected",
            "rule_boundary_attribution",
            "rule_witness_attempted",
            "rule_witness_errors",
            "rule_witness_matched",
            "rule_witnesses",
            "verified_rule_ids",
            "violation_categories",
            "witness_results",
        ]
    );

    assert_eq!(object["attempted"], report.attempted);
    assert_eq!(object["rejected"], report.rejected);
    assert_eq!(object["accepted"], report.accepted);
    assert_eq!(object["complete"], report.complete());
    assert_eq!(
        object["mutation_kinds"],
        serde_json::to_value(&report.covered_mutation_kinds).unwrap()
    );
    assert_eq!(
        object["missing_mutation_kinds"],
        serde_json::to_value(&report.missing_mutation_kinds).unwrap()
    );
    assert_eq!(
        object["verified_rule_ids"],
        serde_json::to_value(&report.covered_rule_ids).unwrap()
    );
    assert_eq!(
        object["violation_categories"],
        serde_json::to_value(&report.covered_violation_categories).unwrap()
    );
    assert_eq!(
        object["actual_rule_boundary_attribution"],
        object["rule_boundary_attribution"]
    );
    assert_eq!(
        object["classifier_rule_labels"],
        serde_json::to_value(
            report
                .classifier_rule_attribution
                .keys()
                .cloned()
                .collect::<Vec<_>>()
        )
        .unwrap()
    );
    let single = object["classifier_single_field"]
        .as_object()
        .expect("single-field proof summary is an object");
    assert_eq!(
        single["attempted"],
        report.classifier_single_field_attempted
    );
    assert_eq!(single["rejected"], report.classifier_single_field_rejected);
    assert_eq!(single["accepted"], report.classifier_single_field_accepted);
    assert_eq!(
        single["pass_rate"],
        report.classifier_single_field_pass_rate
    );
    assert_eq!(
        single["mutation_kinds"],
        serde_json::to_value(&report.classifier_single_field_covered_mutation_kinds).unwrap()
    );
    assert_eq!(
        single["missing_mutation_kinds"],
        serde_json::to_value(&report.classifier_single_field_missing_mutation_kinds).unwrap()
    );
    let coupled = object["classifier_coupled"]
        .as_object()
        .expect("coupled proof summary is an object");
    assert_eq!(coupled["attempted"], report.classifier_coupled_attempted);
    assert_eq!(coupled["rejected"], report.classifier_coupled_rejected);
    assert_eq!(coupled["accepted"], report.classifier_coupled_accepted);
    assert_eq!(coupled["pass_rate"], report.classifier_coupled_pass_rate);

    let witnesses = object["rule_witnesses"]
        .as_array()
        .expect("rule witnesses are an array");
    assert_eq!(witnesses.len(), report.rule_witnesses.len());
    assert!(witnesses.iter().all(|witness| witness["proven"] == true));
    let results = object["witness_results"]
        .as_array()
        .expect("classifier results are an array");
    assert_eq!(results.len(), report.results.len());
}

#[test]
fn actual_rule_witnesses_use_real_stage5_mutants_and_dev_variants() {
    let report = report();
    let rule_ids: BTreeSet<_> = report
        .rule_witnesses
        .iter()
        .map(|witness| witness.rule_id.as_str())
        .collect();
    assert_eq!(rule_ids.len(), 26);
    assert_eq!(report.rule_witness_attempted, 26);
    assert_eq!(report.rule_witness_matched, 26);
    assert!(report.rule_witness_errors.is_empty());
    assert!(
        report
            .rule_witnesses
            .iter()
            .all(RuleBoundaryWitnessResultV5::proven)
    );

    let by_rule: BTreeMap<_, _> = report
        .rule_witnesses
        .iter()
        .map(|witness| (witness.rule_id.as_str(), witness))
        .collect();
    assert!(
        by_rule["SOD-V5-01"]
            .setup_evidence
            .contains("unmitigated witness")
    );
    assert!(
        by_rule["AGENT-REG-V5-01"]
            .setup_evidence
            .contains("critical")
    );
    assert!(by_rule["OPT-V5-01"].setup_evidence.contains("infeasible"));

    let variants: BTreeMap<_, _> = report
        .rule_witnesses
        .iter()
        .filter(|witness| witness.episode_id.contains("#cf_"))
        .map(|witness| (witness.rule_id.as_str(), witness))
        .collect();
    assert_eq!(
        variants.keys().copied().collect::<BTreeSet<_>>(),
        [
            "BIND-V5-01b",
            "CC-V5-01",
            "JIT-V5-01.P1",
            "AGENT-DEL-V5-01.D3",
        ]
        .into_iter()
        .collect()
    );
    assert!(
        variants["BIND-V5-01b"]
            .setup_evidence
            .contains("unique login_email")
    );
    assert!(variants["CC-V5-01"].setup_evidence.contains("cc_01"));
    assert!(
        variants["JIT-V5-01.P1"]
            .setup_evidence
            .contains("no direct eligible grant")
    );
    assert!(
        variants["AGENT-DEL-V5-01.D3"]
            .setup_evidence
            .contains("active, audited role_03 session")
    );

    let itsm = by_rule["ITSM-V5-01"];
    assert!(!itsm.episode_id.contains("#cf_"));
    assert_eq!(itsm.expected_category, "ITSM_MANUAL_FULFILLMENT_MISMATCH");
    assert!(
        itsm.setup_evidence
            .contains("actual disconnected connector")
    );
    assert!(itsm.setup_evidence.contains("qualified fulfillers"));
}
