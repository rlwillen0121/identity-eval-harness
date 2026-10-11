"""SPEC §13.1 deterministic grounding, including Appendix A's three scored outputs."""

from __future__ import annotations

from typing import Any

import pytest

from identitybench.schemas.case import Case
from identitybench.schemas.enums import Effect, Label, RequestDecision
from identitybench.schemas.gold import Certificate, Claim, Finding, JmlGold, RequestGold
from identitybench.schemas.predictions import Prediction
from identitybench.scoring.grounding import UnitGrounding
from identitybench.scoring.settings import ScoreSettings
from identitybench.scoring.summary import UnitRecord, score_cases, score_config
from identitybench.scoring.units import HIGH_RISK_TIERS
from identitybench.world.entities import RiskTier
from tests.conformance.scores import score_request_output
from tests.scoring.builders import (
    LEAVE_DAY,
    claim_dict,
    day,
    entitlement,
    identity,
    leaver_case,
    leaver_plan,
    oracle_output,
    predict,
    request_case,
    request_output,
    request_view,
    scenario,
    scenario_case,
)

SETTINGS = ScoreSettings(n_boot=200)
APPENDIX_A = "appendix-a-sod002-expired-control"


def _unit(case: Case, prediction: Prediction) -> UnitRecord:
    scored = score_config([case], [prediction], settings=SETTINGS)
    (record,) = scored.records
    (unit,) = record.units
    return unit


def _grounding(case: Case, output: dict[str, Any]) -> UnitGrounding:
    return _unit(case, predict(case, output)).grounding


# --------------------------------------------------------------------------------------------
# Appendix A
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["A", "B", "C"])
def test_appendix_a_outputs_reproduce_the_table(label: str) -> None:
    """SPEC Appendix A: "the scorer must reproduce this table exactly"."""
    spec = scenario(APPENDIX_A)
    case = scenario_case(APPENDIX_A)
    assert isinstance(case.gold, RequestGold)
    (scored,) = [o for o in spec.outputs if o.label == label]
    unit = _unit(case, predict(case, scored.output.model_dump()))
    wrongful = unit.pred is Label.A and unit.gold is not Label.A
    actual: dict[str, object] = {
        "correct": unit.correct,
        "citation_valid": unit.grounding.citation_valid,
        "grounded": unit.grounding.grounded,
        "rule_recall": unit.grounding.rule_recall,
        "rule_precision": unit.grounding.rule_precision,
        "unsupported_rules": unit.grounding.unsupported_rules,
        "false_claims": unit.grounding.false_claims,
        "wrongful_approval": wrongful,
        "high_risk_wrongful_approval": wrongful and unit.risk_tier in HIGH_RISK_TIERS,
        "risk_cost": unit.cost,
    }
    stated = scored.expect.model_dump(exclude_none=True)
    assert stated, "the table states scores for every output"
    assert {k: actual[k] for k in stated} == {
        k: tuple(v) if isinstance(v, list) else v for k, v in stated.items()
    }
    reference = score_request_output(case.view, case.gold, scored.output)
    assert actual == {
        "correct": reference.correct,
        "citation_valid": reference.citation_valid,
        "grounded": reference.grounded,
        "rule_recall": reference.rule_recall,
        "rule_precision": reference.rule_precision,
        "unsupported_rules": reference.unsupported_rules,
        "false_claims": reference.false_claims,
        "wrongful_approval": reference.wrongful_approval,
        "high_risk_wrongful_approval": reference.high_risk_wrongful_approval,
        "risk_cost": reference.risk_cost,
    }


def test_appendix_a_output_c_counts_in_war_and_high_risk_war() -> None:
    spec = scenario(APPENDIX_A)
    case = scenario_case(APPENDIX_A)
    by_label = {o.label: o.output.model_dump() for o in spec.outputs}
    scores = score_cases([case], [predict(case, by_label["C"])], settings=SETTINGS)
    assert scores.requests is not None
    m = scores.requests.metrics
    assert (m.war.point, m.high_risk_war.point) == (1.0, 1.0)
    assert scores.requests.cost.mean_cost.point == 20.0
    assert scores.requests.grounding.false_claim_rate.point == 1.0  # one claim, false
    output_a = score_cases([case], [predict(case, by_label["A"])], settings=SETTINGS).requests
    assert output_a is not None
    assert output_a.grounding.grounded_accuracy.point == 1.0
    assert output_a.grounding.grounded_accuracy.primary
    assert output_a.metrics.war.point == 0.0


def test_unsupported_citation_breaks_citation_validity() -> None:
    case = scenario_case(APPENDIX_A)
    output = oracle_output(case)
    output["citations"]["rules"] = ["SOD-002", "FAKE-001"]
    g = _grounding(case, output)
    assert g.unsupported_rules == ("FAKE-001",)
    assert g.rule_precision == 0.5
    assert g.rule_recall == 1.0
    assert not g.citation_valid and not g.grounded
    assert g.reasons_satisfied == 1  # the certificate itself is fully cited


def test_cited_rule_ids_are_compared_in_registry_form() -> None:
    """A formatting slip in a rule ID doesn't turn Appendix A's grounded output A ungrounded."""
    spec = scenario(APPENDIX_A)
    case = scenario_case(APPENDIX_A)
    output = next(o for o in spec.outputs if o.label == "A").output.model_dump()
    output["citations"]["rules"] = [" sod-002", "Mit-001 ", "SOD-002"]
    g = _grounding(case, output)
    assert g.cited_rules == ("MIT-001", "SOD-002")
    assert g.unsupported_rules == () and g.rule_precision == 1.0 and g.rule_recall == 1.0
    assert g.citation_valid and g.grounded
    unknown = _grounding(case, request_output("DENY", rules=["sod-002", " nope-9 "]))
    assert unknown.unsupported_rules == ("NOPE-9",)


def test_irrelevant_true_claim_does_not_satisfy_a_certificate() -> None:
    """§13.1: citing a control's identity_id instead of its expires_on doesn't ground."""
    case = scenario_case(APPENDIX_A)
    output = oracle_output(case)
    evidence = [e for e in output["citations"]["evidence"] if e["id"] != "mc_017"]
    output["citations"]["evidence"] = [*evidence, claim_dict("mc_017", "identity_id", "u_10233")]
    g = _grounding(case, output)
    assert g.false_claims == 0 and g.citation_valid
    assert not g.grounded and g.reasons_satisfied == 0


def test_claims_match_on_canonical_values() -> None:
    case = scenario_case(APPENDIX_A)
    output = oracle_output(case)
    for claim in output["citations"]["evidence"]:
        if isinstance(claim["value"], str):
            claim["value"] = f"  {claim['value'].upper()} "
    assert _grounding(case, output).grounded


# --------------------------------------------------------------------------------------------
# Certificate groups (tied caps) and several sufficient reasons
# --------------------------------------------------------------------------------------------


def test_tied_caps_are_one_reason() -> None:
    case = scenario_case("req-temp001-temp002-tied")
    assert isinstance(case.gold, RequestGold)
    assert sorted(c.rule for c in case.gold.certificates) == ["TEMP-001", "TEMP-002"]
    temp002 = next(c for c in case.gold.certificates if c.rule == "TEMP-002")
    evidence = [claim_dict(c.id, c.field, c.value) for c in temp002.claim_sets[0]]
    g = _grounding(case, request_output("APPROVE", cap=7, rules=["TEMP-002"], evidence=evidence))
    assert g.reasons == 1
    assert g.rule_recall == 1.0 and g.exhaustive_coverage == 1.0
    assert g.citation_valid and g.grounded
    bare = _grounding(case, request_output("APPROVE", cap=7, rules=["TEMP-001"]))
    assert bare.rule_recall == 1.0 and bare.citation_valid and not bare.grounded


def test_several_sufficient_reasons_and_exhaustive_coverage() -> None:
    case = scenario_case("req-life003-terminated-contractor-two-denials")
    assert isinstance(case.gold, RequestGold)
    certificates = {c.rule: c for c in case.gold.certificates}
    assert sorted(certificates) == ["LIFE-001", "LIFE-003"]

    def evidence(*rules: str) -> list[dict[str, object]]:
        return [
            claim_dict(c.id, c.field, c.value)
            for rule in rules
            for c in certificates[rule].claim_sets[0]
        ]

    one = _grounding(
        case, request_output("DENY", rules=["LIFE-001"], evidence=evidence("LIFE-001"))
    )
    assert one.reasons == 2 and one.reasons_satisfied == 1
    assert one.rule_recall == 0.5 and one.exhaustive_coverage == 0.5
    assert one.grounded  # one satisfied certificate is enough
    both = _grounding(
        case,
        request_output(
            "DENY", rules=["LIFE-001", "LIFE-003"], evidence=evidence("LIFE-001", "LIFE-003")
        ),
    )
    assert both.rule_recall == 1.0 and both.exhaustive_coverage == 1.0
    wrong = _grounding(
        case,
        request_output(
            "ESCALATE", rules=["LIFE-001", "LIFE-003"], evidence=evidence("LIFE-001", "LIFE-003")
        ),
    )
    assert not wrong.citation_valid and not wrong.grounded  # correctness comes first
    # Both reasons are cited in full, but they are reasons for DENY, not for the escalation.
    assert wrong.reasons_satisfied == 2 and wrong.exhaustive_coverage == 0.0


def test_exhaustive_coverage_over_certificate_units_and_multi_reason_units() -> None:
    """Averaged over every certificate unit (the §13.1 denominator), and also over the units
    with several sufficient reasons, where it measures exhaustiveness."""
    multi = scenario_case("req-life003-terminated-contractor-two-denials")
    single = scenario_case(APPENDIX_A)
    default = default_approval()
    assert isinstance(multi.gold, RequestGold)
    life001 = next(c for c in multi.gold.certificates if c.rule == "LIFE-001")
    partial = request_output(
        "DENY",
        rules=["LIFE-001"],
        evidence=[claim_dict(c.id, c.field, c.value) for c in life001.claim_sets[0]],
    )
    predictions = [
        predict(multi, partial),  # one of two reasons: 0.5
        predict(single, oracle_output(single)),  # its only reason: 1.0
        predict(default, request_output("APPROVE")),  # no reasons: outside both
    ]
    scores = score_cases([multi, single, default], predictions, settings=SETTINGS)
    assert scores.requests is not None
    g = scores.requests.grounding
    assert (g.n_certificate_units, g.n_multi_reason_units, g.n_default_units) == (2, 1, 1)
    assert g.exhaustive_coverage.point == pytest.approx(0.75)
    assert g.exhaustive_coverage.denominator == 2
    assert g.exhaustive_coverage_multi.point == 0.5
    assert g.exhaustive_coverage_multi.denominator == 1
    only_single = score_cases([single], predictions[1:2], settings=SETTINGS).requests
    assert only_single is not None
    assert only_single.grounding.exhaustive_coverage_multi.point is None  # n/a, not 0 or 1


# --------------------------------------------------------------------------------------------
# Default outcomes and denominators
# --------------------------------------------------------------------------------------------


def default_approval() -> Case:
    view = request_view(entitlements={"ent_1": entitlement()}, identities={"u_1": identity()})
    return request_case("default", "APPROVE", view=view)


def test_default_outcome_grounding() -> None:
    case = default_approval()
    plain = _grounding(case, request_output("APPROVE"))
    assert not plain.has_certificate and plain.reasons == 0
    assert plain.rule_recall is None and plain.rule_precision is None
    assert plain.exhaustive_coverage is None
    assert plain.citation_valid and plain.grounded
    true_claim = _grounding(
        case, request_output("APPROVE", evidence=[claim_dict("ent_1", "privileged", False)])
    )
    assert true_claim.grounded
    false_claim = _grounding(
        case, request_output("APPROVE", evidence=[claim_dict("ent_1", "privileged", True)])
    )
    assert false_claim.false_claims == 1 and not false_claim.grounded
    unknown_field = _grounding(
        case, request_output("APPROVE", evidence=[claim_dict("ent_1", "colour", "red")])
    )
    assert unknown_field.false_claims == 1
    unsupported = _grounding(case, request_output("APPROVE", rules=["TEMP-001"]))
    assert unsupported.unsupported_rules == ("TEMP-001",) and not unsupported.grounded
    capped = _grounding(case, request_output("APPROVE", cap=7))  # over-restriction: not correct
    assert not capped.citation_valid and not capped.grounded


def test_denominators_keep_certificate_and_default_cases_apart() -> None:
    appendix = scenario_case(APPENDIX_A)
    default = default_approval()
    spec = scenario(APPENDIX_A)
    output_b = next(o for o in spec.outputs if o.label == "B").output.model_dump()
    scores = score_cases(
        [appendix, default],
        [predict(appendix, output_b), predict(default, request_output("APPROVE"))],
        settings=SETTINGS,
    )
    assert scores.requests is not None
    g = scores.requests.grounding
    assert (g.n_certificate_units, g.n_default_units) == (1, 1)
    assert g.grounded_accuracy.point == 0.0 and g.grounded_accuracy.denominator == 1
    assert g.citation_valid_accuracy.point == 0.0
    assert g.rule_recall.point == 0.0 and g.rule_recall.denominator == 1
    assert g.right_for_wrong_reason.point == 0.0  # B is wrong, so not "right" for any reason
    assert g.default_outcome_grounding.point == 1.0 and g.default_outcome_grounding.denominator == 1
    assert g.no_citation_rate.point == 0.5  # the default case cites nothing
    assert g.rule_precision.point == 1.0 and g.rule_precision.denominator == 1


def test_right_for_the_wrong_reason_and_invalid_predictions() -> None:
    appendix = scenario_case(APPENDIX_A)
    bare_deny = predict(appendix, request_output("DENY", rules=["SOD-002"]))
    g = score_cases([appendix], [bare_deny], settings=SETTINGS).requests
    assert g is not None
    assert g.grounding.right_for_wrong_reason.point == 1.0  # correct, cited, but no claims
    assert g.grounding.citation_valid_accuracy.point == 1.0
    assert g.grounding.grounded_accuracy.point == 0.0
    invalid = score_cases([appendix], [predict(appendix, status="invalid")], settings=SETTINGS)
    assert invalid.requests is not None
    gi = invalid.requests.grounding
    assert gi.rule_recall.point == 0.0  # an INVALID output cites nothing
    assert gi.rule_precision.point is None
    assert gi.no_citation_rate.point is None  # only answered units can cite
    assert gi.grounded_accuracy.point == 0.0
    assert gi.false_claim_rate.point is None  # no claims at all


def test_unsupported_citation_counts() -> None:
    appendix = scenario_case(APPENDIX_A)
    output = request_output("DENY", rules=["SOD-002", "NOPE-1", "LIFE-001"])
    scores = score_cases([appendix], [predict(appendix, output)], settings=SETTINGS)
    assert scores.requests is not None
    g = scores.requests.grounding
    assert g.unsupported_citations == 2
    assert g.unsupported_citation_rate.point == 1.0
    assert g.rule_precision.point == pytest.approx(1 / 3)


# --------------------------------------------------------------------------------------------
# JML grounding
# --------------------------------------------------------------------------------------------


def _plan_output(effective: str, rules: list[str], *, claims: bool = True) -> dict[str, Any]:
    evidence = (
        [
            claim_dict("u_1", "termination_type", "voluntary"),
            claim_dict("u_1", "termination_date", LEAVE_DAY.isoformat()),
        ]
        if claims
        else []
    )
    return {
        "operations": [op.model_dump(exclude_none=True) for op in leaver_plan(effective)],
        "citations": {"rules": rules, "evidence": evidence},
    }


def test_jml_grounding_uses_the_certificate_and_exact_match() -> None:
    case = leaver_case("ev")
    on_time = LEAVE_DAY.isoformat()
    late = day(5).isoformat()  # two days after the termination date
    outputs = {
        "grounded": _plan_output(on_time, ["JML-003"]),
        "unsupported": _plan_output(on_time, ["JML-001"]),
        "late": _plan_output(late, ["JML-003"]),
        "no_claims": _plan_output(on_time, ["JML-003"], claims=False),
    }
    results: dict[str, UnitGrounding] = {}
    for name, output in outputs.items():
        (record,) = score_config([case], [predict(case, output)], settings=SETTINGS).records
        assert record.plan_grounding is not None
        results[name] = record.plan_grounding
    assert results["grounded"].grounded and results["grounded"].rule_recall == 1.0
    assert results["unsupported"].unsupported_rules == ("JML-001",)
    assert not results["unsupported"].citation_valid
    # The late plan cites its certificate in full, but the plan is wrong: no coverage.
    assert not results["late"].grounded and results["late"].reasons_satisfied == 1
    assert results["late"].exhaustive_coverage == 0.0
    assert results["no_claims"].citation_valid and not results["no_claims"].grounded


def test_jml_rule_precision_counts_mover_sod_findings_and_exceptions() -> None:
    """SPEC 0.4.1: a mover plan's findings include the SoD rules it judged, with MIT-001
    evaluated, so citing them is supported."""
    case = scenario_case("jml-002-mover-birthright-sod-overlap")
    assert isinstance(case.gold, JmlGold)
    findings = {f.rule for f in case.gold.findings}
    assert {"JML-002", "SOD-001"} <= findings
    output = oracle_output(case)
    output["citations"]["rules"] = sorted(findings | {x.rule for x in case.gold.exceptions})
    scores = score_cases([case], [predict(case, output)], settings=SETTINGS)
    assert scores.jml is not None
    g = scores.jml.grounding
    assert g.rule_precision.point == 1.0 and g.unsupported_citations == 0
    assert g.grounded_accuracy.point == 1.0
    assert not g.grounded_accuracy.primary  # JML's primary metric is the unsafe plan rate


def test_hand_built_certificate_is_grounded_by_its_claims() -> None:
    gold = RequestGold(
        decision=RequestDecision.DENY,
        risk_tier=RiskTier.low,
        findings=(Finding(rule="ELIG-004", effect=Effect.DENY),),
        certificates=(
            Certificate(
                rule="ELIG-004",
                claim_sets=((Claim(id="ent_1", field="requestable", value=False),),),
            ),
        ),
    )
    view = request_view(entitlements={"ent_1": entitlement(requestable=False)})
    case = request_case(
        "h",
        "DENY",
        view=view,
        risk=gold.risk_tier,
        findings=gold.findings,
        certificates=gold.certificates,
    )
    g = _grounding(
        case,
        request_output(
            "DENY", rules=["ELIG-004"], evidence=[claim_dict("ent_1", "requestable", False)]
        ),
    )
    assert g.grounded and g.n_claims == 1 and g.false_claims == 0


def test_rates_combine_across_units_as_the_spec_defines() -> None:
    """Two units with different claim and reason counts, worked by hand (SPEC §13.1).

    The false-claim rate pools claims ("a rate over all claims"); rule recall and rule
    precision average each unit's fraction over the units where it is defined. Pooling recall
    and precision, or averaging the per-unit false-claim fractions, gives different numbers.
    """
    view = request_view(
        entitlements={"ent_1": entitlement(requestable=False)}, identities={"u_1": identity()}
    )
    requestable = Certificate(
        rule="ELIG-004", claim_sets=((Claim(id="ent_1", field="requestable", value=False),),)
    )
    finance = Certificate(
        rule="LIFE-001", claim_sets=((Claim(id="u_1", field="department", value="Finance"),),)
    )
    # Unit 1: one reason (ELIG-004); LIFE-001 is a finding without a certificate.
    one = request_case(
        "u1",
        "DENY",
        view=view,
        findings=(
            Finding(rule="ELIG-004", effect=Effect.DENY),
            Finding(rule="LIFE-001", effect=Effect.DENY),
        ),
        certificates=(requestable,),
    )
    # Unit 2: two separate reasons, ELIG-004 and LIFE-001.
    two = request_case(
        "u2",
        "DENY",
        view=view,
        findings=(
            Finding(rule="ELIG-004", effect=Effect.DENY),
            Finding(rule="LIFE-001", effect=Effect.DENY),
        ),
        certificates=(requestable, finance),
    )
    predictions = [
        # Cites a finding that isn't a certificate rule: recall 0, precision 1. Its one claim
        # is false (ent_1 isn't requestable): 1 false claim of 1.
        predict(
            one,
            request_output(
                "DENY", rules=["LIFE-001"], evidence=[claim_dict("ent_1", "requestable", True)]
            ),
        ),
        # Cites one of its two certificate rules and a bogus rule: recall 1/2, precision 1/2.
        # Three true claims: 0 false of 3.
        predict(
            two,
            request_output(
                "DENY",
                rules=["ELIG-004", "BOGUS-001"],
                evidence=[
                    claim_dict("ent_1", "requestable", False),
                    claim_dict("ent_1", "privileged", False),
                    claim_dict("u_1", "department", "Finance"),
                ],
            ),
        ),
    ]
    scores = score_cases([one, two], predictions, settings=SETTINGS)
    assert scores.requests is not None
    g = scores.requests.grounding
    # (1 + 0) false claims over (1 + 3) claims, not the mean of 1/1 and 0/3 (0.5).
    assert g.false_claim_rate.point == pytest.approx(0.25)
    assert (g.false_claim_rate.numerator, g.false_claim_rate.denominator) == (1.0, 4.0)
    # Mean of 0 and 1/2 over two certificate units, not pooled reasons 1/3.
    assert g.rule_recall.point == pytest.approx(0.25)
    assert g.rule_recall.denominator == 2
    # Mean of 1 and 1/2 over two citing units, not pooled citations 2/3.
    assert g.rule_precision.point == pytest.approx(0.75)
    assert g.rule_precision.denominator == 2
    assert g.unsupported_citations == 1  # BOGUS-001
