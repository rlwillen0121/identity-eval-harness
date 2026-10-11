"""SPEC §12.8 variance: disagreement across repeats on labels, durations and plan timelines."""

from __future__ import annotations

from typing import Any

import pytest

from identitybench.scoring.settings import ScoreSettings
from identitybench.scoring.summary import score_cases
from tests.scoring.builders import (
    LEAVE_DAY,
    leaver_case,
    leaver_plan,
    predict,
    request_case,
    request_output,
    review_case,
    review_output,
)

SETTINGS = ScoreSettings(n_boot=200)


def _plan() -> dict[str, Any]:
    return {
        "operations": [
            op.model_dump(exclude_none=True) for op in leaver_plan(LEAVE_DAY.isoformat())
        ]
    }


def test_disagreement_by_hand() -> None:
    r1 = request_case("r1", "APPROVE", duration=7)  # approved every time, durations differ
    r2 = request_case("r2", "DENY")  # labels differ
    r3 = request_case("r3", "DENY")  # one repeat only: not counted
    campaign = review_case("c1", [("i1", "CERTIFY"), ("i2", "REVOKE")])
    steady, flaky = leaver_case("ev1"), leaver_case("ev2")
    predictions = [
        predict(r1, request_output("APPROVE", cap=7), sample_idx=0),
        predict(r1, request_output("APPROVE", cap=7), sample_idx=1),
        predict(r1, request_output("APPROVE", cap=30), sample_idx=2),
        predict(r2, request_output("DENY"), sample_idx=0),
        predict(r2, request_output("ESCALATE"), sample_idx=1),
        predict(r3, request_output("DENY")),
        predict(campaign, review_output([("i1", "CERTIFY"), ("i2", "REVOKE")]), sample_idx=0),
        predict(campaign, review_output([("i1", "CERTIFY"), ("i2", "CERTIFY")]), sample_idx=1),
        predict(steady, _plan(), sample_idx=0),
        predict(steady, _plan(), sample_idx=1),
        predict(flaky, _plan(), sample_idx=0),
        predict(flaky, status="invalid", sample_idx=1),
    ]
    scores = score_cases([r1, r2, r3, campaign, steady, flaky], predictions, settings=SETTINGS)
    repeats = scores.repeats
    assert repeats is not None
    assert repeats.n_repeated_cases == 5 and repeats.max_repeats == 3
    assert repeats.request_label_disagreement.point == pytest.approx(1 / 2)  # r2 of r1, r2
    assert repeats.request_duration_disagreement.point == 1.0  # r1, the only all-approved case
    assert repeats.request_duration_disagreement.denominator == 1
    assert repeats.review_label_disagreement.point == pytest.approx(1 / 2)  # i2 of i1, i2
    assert repeats.review_label_disagreement.cluster == "campaign"
    assert repeats.plan_timeline_disagreement.point == pytest.approx(1 / 2)  # ev2
    # ev2's INVALID repeat differs from its valid one; nothing here is INVALID throughout.
    counts = (
        repeats.n_requests_all_invalid,
        repeats.n_items_all_invalid,
        repeats.n_events_all_invalid,
    )
    assert counts == (0, 0, 0)
    # Primary metrics read repeat 0 only.
    assert scores.requests is not None and scores.requests.metrics.accuracy.point == 1.0
    assert scores.n_predictions == 12


def test_units_whose_repeats_all_fail_are_left_out() -> None:
    """Repeats that all fail to parse neither agree nor disagree, as in robustness pairs: a
    configuration that never parses mustn't look perfectly stable."""
    dead, half, steady = (request_case(f"r_{n}", "DENY") for n in ("dead", "half", "steady"))
    campaign = review_case("c1", [("i1", "CERTIFY"), ("i2", "REVOKE")])
    dead_event, steady_event = leaver_case("ev_dead"), leaver_case("ev_steady")
    deny = request_output("DENY")
    predictions = [
        predict(dead, status="invalid", sample_idx=0),
        predict(dead, status="error", sample_idx=1),
        predict(half, status="invalid", sample_idx=0),  # then answered: a disagreement
        predict(half, deny, sample_idx=1),
        predict(steady, deny, sample_idx=0),
        predict(steady, deny, sample_idx=1),
        # i2 is missing from both outputs, so INVALID in both repeats; i1 agrees
        predict(campaign, review_output([("i1", "CERTIFY")]), sample_idx=0),
        predict(campaign, review_output([("i1", "CERTIFY")]), sample_idx=1),
        predict(dead_event, status="invalid", sample_idx=0),
        predict(dead_event, status="refused", sample_idx=1),
        predict(steady_event, _plan(), sample_idx=0),
        predict(steady_event, _plan(), sample_idx=1),
    ]
    cases = [dead, half, steady, campaign, dead_event, steady_event]
    repeats = score_cases(cases, predictions, settings=SETTINGS).repeats
    assert repeats is not None
    assert repeats.n_requests_all_invalid == 1
    assert repeats.request_label_disagreement.point == pytest.approx(1 / 2)  # half of 2
    assert repeats.request_label_disagreement.denominator == 2
    assert repeats.n_items_all_invalid == 1
    assert repeats.review_label_disagreement.point == 0.0  # i1 only
    assert repeats.review_label_disagreement.denominator == 1
    assert repeats.n_events_all_invalid == 1
    assert repeats.plan_timeline_disagreement.point == 0.0  # ev_steady only
    assert repeats.plan_timeline_disagreement.denominator == 1
    only_dead = score_cases([dead], predictions[:2], settings=SETTINGS).repeats
    assert only_dead is not None
    assert only_dead.request_label_disagreement.point is None  # n/a, not perfectly stable


def test_identical_plans_written_differently_agree() -> None:
    event = leaver_case("ev")
    reordered = {"operations": list(reversed(_plan()["operations"]))}
    predictions = [predict(event, _plan(), sample_idx=0), predict(event, reordered, sample_idx=1)]
    repeats = score_cases([event], predictions, settings=SETTINGS).repeats
    assert repeats is not None
    assert repeats.plan_timeline_disagreement.point == 0.0
    assert repeats.request_label_disagreement.point is None  # no repeated requests


def test_no_repeats_means_no_section() -> None:
    case = request_case("r", "DENY")
    assert (
        score_cases([case], [predict(case, request_output("DENY"))], settings=SETTINGS).repeats
        is None
    )
