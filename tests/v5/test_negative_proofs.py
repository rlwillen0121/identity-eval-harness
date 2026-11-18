from __future__ import annotations

from typing import cast, get_args

from identitybench.v5.negative_proofs import build_negative_witnesses, run_negative_proofs
from identitybench.v5.schemas import RuleIdV5, ViolationCategoryV5


def test_directed_negative_proofs_reject_all_required_boundaries() -> None:
    report = run_negative_proofs()

    assert report.attempted == 43
    assert report.rejected == 43
    assert report.accepted == 0
    assert report.error_count == 0
    assert report.pass_rate == 0.0
    assert report.complete
    assert report.structural_attempted == report.structural_rejected == 4
    assert report.semantic_attempted == report.semantic_rejected == 39
    assert report.classifier_single_field_attempted == report.classifier_single_field_rejected == 32
    assert report.classifier_single_field_accepted == 0
    assert report.classifier_single_field_pass_rate == 0.0
    assert len(report.classifier_single_field_covered_mutation_kinds) == 12
    assert report.classifier_single_field_missing_mutation_kinds == ()
    assert report.classifier_coupled_attempted == report.classifier_coupled_rejected == 7
    assert report.classifier_coupled_accepted == 0
    assert report.classifier_coupled_pass_rate == 0.0
    assert report.missing_mutation_kinds == ()
    assert report.missing_rule_ids == ()
    assert report.missing_violation_categories == ()
    assert len(report.covered_mutation_kinds) == 12
    assert len(report.covered_rule_ids) == 26
    assert len(report.covered_violation_categories) == 43
    assert set(report.covered_rule_ids) == set(cast(tuple[str, ...], get_args(RuleIdV5)))
    assert set(report.covered_violation_categories) == set(
        cast(tuple[str, ...], get_args(ViolationCategoryV5))
    )


def test_actual_rule_coverage_uses_valid_world_oracle_rejections() -> None:
    report = run_negative_proofs()
    expected_rule_ids = set(cast(tuple[str, ...], get_args(RuleIdV5)))
    witnesses_by_rule = {witness.rule_id: witness for witness in report.rule_witnesses}

    assert report.rule_witness_attempted == report.rule_witness_matched == 26
    assert report.rule_witness_errors == ()
    assert report.missing_rule_ids == ()
    assert set(report.covered_rule_ids) == expected_rule_ids
    assert set(report.rule_boundary_attribution) == expected_rule_ids
    assert set(witnesses_by_rule) == expected_rule_ids
    for rule_id, witness in witnesses_by_rule.items():
        assert witness.proven
        assert witness.baseline_trace_valid
        assert witness.baseline_stage2_valid
        assert witness.mutant_stage2_valid
        assert witness.actual_stage == 5
        assert witness.rejected
        assert witness.actual_category == witness.expected_category
        assert witness.changed_field_count == len(witness.changed_fields) > 0
        assert witness.setup_evidence
        attribution = report.rule_boundary_attribution[rule_id]
        assert len(attribution) == 1
        assert witness.episode_id in attribution[0]
        assert f"changed_fields={','.join(witness.changed_fields)}" in attribution[0]
        assert ":ev_" not in attribution[0]
    assert "unmitigated witness" in witnesses_by_rule["SOD-V5-01"].setup_evidence
    assert "critical" in witnesses_by_rule["AGENT-REG-V5-01"].setup_evidence
    assert "infeasible" in witnesses_by_rule["OPT-V5-01"].setup_evidence


def test_dev_counterfactuals_cover_absent_branches_and_dev_fixture_exercises_itsm() -> None:
    report = run_negative_proofs()
    variants = {
        witness.rule_id: witness
        for witness in report.rule_witnesses
        if "#cf_" in witness.episode_id
    }

    assert set(variants) == {
        "BIND-V5-01b",
        "CC-V5-01",
        "JIT-V5-01.P1",
        "AGENT-DEL-V5-01.D3",
    }
    itsm = next(witness for witness in report.rule_witnesses if witness.rule_id == "ITSM-V5-01")
    assert "#cf_" not in itsm.episode_id
    assert itsm.baseline_trace_valid
    assert itsm.baseline_stage2_valid
    assert itsm.mutant_stage2_valid
    assert itsm.actual_stage == 5
    assert itsm.actual_category == "ITSM_MANUAL_FULFILLMENT_MISMATCH"
    assert itsm.changed_fields == ("itsm_ticket_actions",)
    assert "actual disconnected connector" in itsm.setup_evidence
    assert "qualified fulfillers" in itsm.setup_evidence

    assert all(witness.proven for witness in variants.values())
    assert all(witness.episode_id.startswith("v5_dev_ep_") for witness in variants.values())
    assert all("dev-only variant" in witness.setup_evidence for witness in variants.values())
    assert "unique login_email" in variants["BIND-V5-01b"].setup_evidence
    assert "cc_01" in variants["CC-V5-01"].setup_evidence
    assert "no direct eligible grant" in variants["JIT-V5-01.P1"].setup_evidence
    assert "active, audited role_03 session" in variants["AGENT-DEL-V5-01.D3"].setup_evidence
    assert all(
        witness.changed_field_count > 1
        for witness in variants.values()
        if witness.rule_id
        in {
            "BIND-V5-01b",
            "JIT-V5-01.P1",
            "AGENT-DEL-V5-01.D3",
        }
    )
    assert variants["CC-V5-01"].changed_fields == ("retained_grant_ids",)


def test_semantic_witnesses_are_typed_and_mark_coupled_mutations_explicitly() -> None:
    witnesses = build_negative_witnesses()
    semantic = [witness for witness in witnesses if witness.expected_stage == 5]

    assert len(semantic) == 39
    assert all(len(witness.gold) == len(witness.submitted) == 1 for witness in semantic)
    assert all(len(witness.changed_fields) >= 1 for witness in semantic)
    assert all(
        witness.changed_fields
        == (
            "jit_decision",
            "approver_account_id",
            "approved_ttl_s",
            "external_sync_status",
        )
        for witness in semantic
        if witness.witness_id.startswith("jit_p")
    )
    assert all(
        witness.changed_fields == ("delegation_decision", "approved_ttl_s")
        for witness in semantic
        if witness.witness_id.startswith("delegation_d")
    )
    assert sum(len(witness.changed_fields) == 1 for witness in semantic) == 32
    assert sum(len(witness.changed_fields) > 1 for witness in semantic) == 7


def test_protocol_witnesses_stop_at_the_expected_stage() -> None:
    report = run_negative_proofs()
    by_id = {result.witness_id: result for result in report.results}

    expected = {
        "stage1_completeness": (1, "COMPLETENESS_VIOLATION"),
        "stage2_schema": (2, "SCHEMA_VALIDATION_ERROR"),
        "stage3_precondition": (3, "PRECONDITION_MISMATCH"),
        "stage4_citation": (4, "CITATION_MISMATCH"),
    }
    for witness_id, (stage, category) in expected.items():
        result = by_id[witness_id]
        assert result.rejected
        assert result.actual_stage == stage
        assert result.actual_category == category


def test_ciem_witnesses_use_valid_same_target_principal_findings() -> None:
    witnesses = {
        witness.witness_id: witness
        for witness in build_negative_witnesses()
        if witness.witness_id.startswith("ciem_")
    }
    assert set(witnesses) == {
        "ciem_orphan_principal",
        "ciem_escalation_path",
        "ciem_effective_permission",
    }
    for witness in witnesses.values():
        gold = witness.gold[0]
        submitted = witness.submitted[0]
        gold_ids = tuple(item.cprin_id for item in gold.findings)
        submitted_ids = tuple(item.cprin_id for item in submitted.findings)
        assert gold_ids == submitted_ids
        assert gold_ids and all(item.startswith("cprin_aws_") for item in gold_ids)
