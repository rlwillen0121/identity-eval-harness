"""SPEC §12.4 JML plan aggregation on hand-built leaver events.

Every event is the same voluntary leaver (one active account holding one high-risk assignment,
terminated on day +3; gold revokes everything and disables the account that day; the horizon
ends on day +17, 15 days from the termination date inclusive). Only the predicted plans differ.
"""

from __future__ import annotations

from typing import Any

import pytest

from identitybench.policy.jml_state import score_plan
from identitybench.schemas.enums import Tier
from identitybench.schemas.gold import JmlGold
from identitybench.schemas.outputs import JmlOutput
from identitybench.scoring.plans import plan_unit
from identitybench.scoring.settings import ScoreSettings
from identitybench.scoring.stats import clopper_pearson_upper
from identitybench.scoring.summary import score_cases, score_config
from tests.scoring.builders import (
    LEAVE_DAY,
    day,
    leaver_case,
    leaver_plan,
    oracle_output,
    predict,
    request_case,
    scenario_case,
)

SETTINGS = ScoreSettings(n_boot=200)
ON_TIME = LEAVE_DAY.isoformat()


def _plan(operations: list[dict[str, Any]]) -> dict[str, Any]:
    return {"operations": operations, "citations": {"rules": ["JML-003"], "evidence": []}}


def _ops(effective: str) -> list[dict[str, Any]]:
    return [op.model_dump(exclude_none=True) for op in leaver_plan(effective)]


def five_events() -> tuple[list[Any], list[Any]]:
    """exact, empty, two days late, INVALID, and exact plus three skipped operations."""
    cases = [leaver_case(f"ev{i}", tier=Tier.L1 if i < 3 else Tier.L2) for i in range(5)]
    skipped = [
        {"op": "DISABLE_ACCOUNT", "account_id": "acct_ghost", "effective": ON_TIME},  # fabricated
        {"op": "DISABLE_ACCOUNT", "account_id": "acct_1", "effective": day(-1).isoformat()},
        {"op": "REVOKE_ALL", "identity_id": "u_1", "effective": day(40).isoformat()},
    ]
    predictions = [
        predict(cases[0], _plan(_ops(ON_TIME))),
        predict(cases[1], _plan([])),
        predict(cases[2], _plan(_ops(day(5).isoformat()))),
        predict(cases[3], status="invalid"),
        predict(cases[4], _plan(_ops(ON_TIME) + skipped)),
    ]
    return cases, predictions


def test_per_event_comparisons_come_from_score_plan() -> None:
    cases, predictions = five_events()
    scored = score_config(cases, predictions, settings=SETTINGS)
    for case, prediction, record in zip(cases, predictions, scored.records, strict=True):
        assert isinstance(case.gold, JmlGold)
        output = prediction.output
        ops = output.operations if isinstance(output, JmlOutput) else None
        assert record.plan == score_plan(case.view, case.gold, ops)
        assert record.units == [] and record.cost is None


def test_plan_aggregates_by_hand() -> None:
    cases, predictions = five_events()
    scores = score_cases(cases, predictions, settings=SETTINGS)
    jml = scores.jml
    assert jml is not None
    assert (jml.n_events, jml.n_invalid, jml.n_no_prediction) == (5, 1, 0)
    # Unsafe: the empty, late and INVALID plans leave access in place past day +3.
    assert jml.unsafe_plan_rate.point == pytest.approx(3 / 5)
    assert jml.unsafe_plan_rate.primary
    assert jml.exact_timeline_match.point == pytest.approx(1 / 5)  # skipped ops block event 5
    assert jml.invalid_rate.point == pytest.approx(1 / 5)
    # Residual source-days: empty and INVALID keep as_1 for days +3..+17 (15), late for 2 days.
    assert jml.residual_source_days.point == pytest.approx((15 + 2 + 15) / 5)
    assert jml.account_state_errors_more_permissive.point == pytest.approx((15 + 2 + 15) / 5)
    assert jml.account_state_errors_less_permissive.point == 0.0
    # Excess access-days are risk weighted: the entitlement is high risk (w = 2).
    assert jml.excess_access_days.point == pytest.approx(2 * (15 + 2 + 15) / 5)
    assert jml.missing_access_days.point == 0.0
    assert jml.ownership_errors.point == 0.0
    assert jml.residual_source_days.units == "JML event"
    assert jml.residual_source_days.method == "bootstrap_iid"


def test_operation_diagnostics() -> None:
    cases, predictions = five_events()
    jml = score_cases(cases, predictions, settings=SETTINGS).jml
    assert jml is not None
    counts = jml.operations
    assert (counts.fabricated, counts.retroactive, counts.out_of_horizon) == (1, 1, 1)
    assert (counts.failed, counts.conflicts) == (0, 0)
    assert (counts.plans_with_fabricated, counts.plans_with_retroactive) == (1, 1)
    # Precision is defined where a plan changes state: exact 1, late 0, exact-plus-skipped 1.
    assert jml.op_precision.point == pytest.approx(2 / 3)
    assert jml.op_precision.denominator == 3
    # Recall is over every event (gold always changes state): 1, 0, 0, 0, 1.
    assert jml.op_recall.point == pytest.approx(2 / 5)


def test_unsafe_plan_rate_carries_an_exact_bound() -> None:
    cases, predictions = five_events()
    jml = score_cases(cases, predictions, settings=SETTINGS).jml
    assert jml is not None
    bound = jml.unsafe_plan_rate.bound
    assert bound is not None
    assert (bound.k, bound.n) == (3, 5)
    assert bound.upper == pytest.approx(clopper_pearson_upper(3, 5))
    assert bound.units == "JML event"
    perfect = score_cases(cases[:1], predictions[:1], settings=SETTINGS).jml
    assert perfect is not None and perfect.unsafe_plan_rate.point == 0.0
    assert perfect.unsafe_plan_rate.bound is not None  # bounded even with zero events
    assert perfect.unsafe_plan_rate.bound.upper == pytest.approx(0.95)  # 1 − 0.05^(1/1)


def test_missing_jml_prediction_is_simulated_as_empty_and_invalid() -> None:
    case = leaver_case("ev")
    jml = score_cases([case], [], settings=SETTINGS, config_id="cfg").jml
    assert jml is not None
    assert (jml.n_invalid, jml.n_no_prediction) == (1, 1)
    assert jml.unsafe_plan_rate.point == 1.0
    assert jml.exact_timeline_match.point == 0.0
    assert jml.grounding.grounded_accuracy.point == 0.0


def test_breakdowns_by_event_type_tier_and_tag() -> None:
    cases, predictions = five_events()
    jml = score_cases(cases, predictions, settings=SETTINGS).jml
    assert jml is not None
    assert list(jml.by_event_type) == ["leaver"]
    assert jml.by_event_type["leaver"].n_events == 5 and jml.by_event_type["leaver"].small_n
    assert jml.by_tier[Tier.L1].unsafe_plan_rate.point == pytest.approx(2 / 3)  # events 0-2
    assert jml.by_tier[Tier.L2].unsafe_plan_rate.point == pytest.approx(1 / 2)  # events 3-4
    assert jml.by_tag == {}


def test_oracle_plans_on_conformance_events_are_exact() -> None:
    ids = ["jml-003-voluntary-future-dated-leaver", "jml-002-mover-birthright-sod-overlap"]
    cases = [scenario_case(i) for i in ids]
    jml = score_cases(cases, [predict(c, oracle_output(c)) for c in cases], settings=SETTINGS).jml
    assert jml is not None
    assert jml.unsafe_plan_rate.point == 0.0 and jml.exact_timeline_match.point == 1.0
    assert jml.op_precision.point == 1.0 and jml.op_recall.point == 1.0
    assert jml.grounding.grounded_accuracy.point == 1.0


def test_plan_unit_refuses_decision_cases() -> None:
    with pytest.raises(ValueError, match="not a JML case"):
        plan_unit(request_case("r", "DENY"), None)
