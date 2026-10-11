"""Decision units: correctness with the duration rule, the INVALID rules, prediction indexing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from identitybench.providers.base import ModelResponse
from identitybench.runner.parse import parse_response
from identitybench.schemas.enums import Label
from identitybench.schemas.predictions import Prediction, PredictionStatus
from identitybench.scoring.settings import ScoreSettings
from identitybench.scoring.summary import score_config
from identitybench.scoring.units import (
    NO_PREDICTION,
    PredictionIndex,
    decision_units,
    longer,
)
from tests.scoring.builders import (
    leaver_case,
    predict,
    request_case,
    request_output,
    review_case,
    review_output,
)


def test_longer_treats_none_as_standing_access() -> None:
    assert longer(None, 7)
    assert not longer(7, None)
    assert not longer(None, None)
    assert longer(8, 7)
    assert not longer(7, 7)


@pytest.mark.parametrize(
    ("requested", "gold", "cap", "effective", "over", "under"),
    [
        (None, None, None, None, False, False),  # standing approval of a standing gold
        (None, 7, None, None, True, False),  # standing where a cap binds: over-duration
        (None, 7, 7, 7, False, False),
        (None, 7, 30, 30, True, False),
        (30, 30, None, 30, False, False),  # request-bound: no cap needed
        (30, 30, 45, 30, False, False),  # a cap can't extend a request
        (30, 30, 7, 7, False, True),  # over-restriction
        (8, 7, 7, 7, False, False),
        (None, None, 90, 90, False, True),  # capping standing access gold didn't cap
    ],
)
def test_effective_duration_is_min_of_request_and_cap(
    requested: int | None,
    gold: int | None,
    cap: int | None,
    effective: int | None,
    over: bool,
    under: bool,
) -> None:
    case = request_case("r", "APPROVE", duration=gold, requested=requested)
    (unit,) = decision_units(case, predict(case, request_output("APPROVE", cap=cap)))
    assert unit.pred_duration == effective
    assert (unit.over_duration, unit.over_restriction) == (over, under)
    assert unit.correct is not (over or under)
    assert unit.gold_duration == gold


@pytest.mark.parametrize("cap", [0, -3])
def test_a_non_positive_cap_is_invalid_at_parse_time(cap: int) -> None:
    """A cap of 0 or less grants no usable access, so it can't be carried out as an approval:
    the parser makes the output INVALID (``schemas.outputs.Conditions``), and the scorer never
    sees it as an over-restricted or wrongful approval."""
    case = request_case("r", "APPROVE", duration=7, requested=30)
    result = parse_response(case, ModelResponse(parsed=request_output("APPROVE", cap=cap)))
    assert result.status is PredictionStatus.invalid
    assert result.invalid_reason is not None and "max_duration_days" in result.invalid_reason
    # Nor can such an output be recorded as a valid prediction.
    with pytest.raises(ValidationError, match="max_duration_days"):
        predict(case, request_output("APPROVE", cap=cap))
    prediction = Prediction(
        run_id="run_1",
        config_id="cfg",
        case_id=case.case_id,
        task=case.task,
        status=result.status,
        invalid_reason=result.invalid_reason,
    )
    scored = score_config([case], [prediction], settings=ScoreSettings(n_boot=50))
    (record,) = scored.records
    (unit,) = record.units
    assert unit.pred is Label.INVALID and not unit.over_restriction
    assert unit.cost == 1.5  # routing plus the failed automation's overhead
    requests = scored.scores.requests
    assert requests is not None
    assert requests.metrics.unsafe_approval_share.point is None  # no approval was made


def test_a_one_day_cap_is_the_smallest_valid_cap() -> None:
    case = request_case("r", "APPROVE", duration=1, requested=30)
    result = parse_response(case, ModelResponse(parsed=request_output("APPROVE", cap=1)))
    assert result.status is PredictionStatus.ok
    (unit,) = decision_units(case, predict(case, request_output("APPROVE", cap=1)))
    assert unit.pred_duration == 1 and unit.correct


def test_durations_only_matter_for_gold_approvals() -> None:
    case = request_case("r", "DENY")
    (unit,) = decision_units(case, predict(case, request_output("APPROVE", cap=3)))
    assert unit.pred_duration == 3
    assert not unit.over_duration and not unit.over_restriction
    assert unit.wrongful_approval and not unit.correct


@pytest.mark.parametrize("status", ["invalid", "refused", "error", "over_budget"])
def test_non_ok_prediction_is_invalid(status: str) -> None:
    case = request_case("r", "APPROVE")
    (unit,) = decision_units(case, predict(case, status=status))
    assert unit.pred is Label.INVALID
    assert unit.status == status
    assert not unit.correct and not unit.answered and not unit.wrongful_approval


def test_missing_prediction_is_invalid() -> None:
    case = request_case("r", "DENY")
    (unit,) = decision_units(case, None)
    assert unit.pred is Label.INVALID
    assert unit.status == NO_PREDICTION


def test_review_items_missing_or_duplicated_are_invalid() -> None:
    case = review_case("c", [("i1", "CERTIFY"), ("i2", "REVOKE"), ("i3", "ESCALATE")])
    output = review_output([("i2", "REVOKE"), ("i3", "ESCALATE"), ("i3", "ESCALATE")])
    units = decision_units(case, predict(case, output))
    assert [u.item_id for u in units] == ["i1", "i2", "i3"]
    assert [u.status for u in units] == ["missing_item", "ok", "duplicate_item"]
    assert [u.pred for u in units] == [Label.INVALID, Label.N, Label.INVALID]
    assert [u.correct for u in units] == [False, True, False]


def test_item_dropped_at_parse_time_next_to_a_valid_copy_is_duplicated() -> None:
    case = review_case("c", [("i1", "CERTIFY"), ("i2", "CERTIFY")])
    output = review_output([("i1", "CERTIFY"), ("i2", "CERTIFY")])
    prediction = predict(case, output, item_errors={"i1": "decision: invalid enum"})
    units = decision_units(case, prediction)
    assert [u.status for u in units] == ["duplicate_item", "ok"]


def test_unknown_items_in_the_output_are_ignored() -> None:
    case = review_case("c", [("i1", "CERTIFY")])
    output = review_output([("i1", "CERTIFY"), ("i9", "REVOKE")])
    (unit,) = decision_units(case, predict(case, output))
    assert unit.correct


def test_invalid_review_prediction_makes_every_item_invalid() -> None:
    case = review_case("c", [("i1", "CERTIFY"), ("i2", "REVOKE")])
    units = decision_units(case, predict(case, status="refused"))
    assert {u.pred for u in units} == {Label.INVALID}
    assert {u.status for u in units} == {"refused"}


def test_review_units_follow_campaign_positions() -> None:
    case = review_case("c", [("b", "CERTIFY"), ("a", "REVOKE"), ("c", "CERTIFY")])
    units = decision_units(case, None)
    assert [(u.item_id, u.position, u.campaign_size) for u in units] == [
        ("b", 0, 3),
        ("a", 1, 3),
        ("c", 2, 3),
    ]
    assert all(u.cluster == "c" for u in units)


def test_decision_units_refuse_jml_cases() -> None:
    with pytest.raises(ValueError, match="not a decision case"):
        decision_units(leaver_case("j"), None)


def test_prediction_index_validation() -> None:
    a, b = request_case("a", "APPROVE"), request_case("b", "DENY")
    cases = {"a": a, "b": b}
    p = predict(a, request_output("APPROVE"))
    index = PredictionIndex([p, p, predict(a, request_output("DENY"), sample_idx=1)], cases)
    assert len(index) == 2  # identical duplicates collapse
    assert index.config_id == "cfg"
    assert index.get("a") == p and index.get("b") is None
    assert [x.sample_idx for x in index.samples("a")] == [0, 1]
    assert index.repeated_cases() == ["a"]
    with pytest.raises(ValueError, match="conflicting"):
        PredictionIndex([p, predict(a, request_output("DENY"))], cases)
    with pytest.raises(ValueError, match="unknown case"):
        PredictionIndex([p], {"b": b})
    with pytest.raises(ValueError, match="several configurations"):
        PredictionIndex([p, predict(b, request_output("DENY"), config_id="other")], cases)
    with pytest.raises(ValueError, match="several configurations"):
        PredictionIndex([p], cases, config_id="other")
    with pytest.raises(ValueError, match="nothing names"):
        PredictionIndex([], cases)
    assert PredictionIndex([], cases, config_id="x").config_id == "x"
    review = review_case("r", [("i1", "CERTIFY")])
    wrong_task = predict(a, request_output("APPROVE")).model_copy(update={"case_id": "r"})
    with pytest.raises(ValueError, match="the case is access_review"):
        PredictionIndex([wrong_task], {"r": review})


def test_malformed_decision_cases_are_refused() -> None:
    request = request_case("r", "DENY")
    without_request = request.model_copy(
        update={"view": request.view.model_copy(update={"request": None})}
    )
    with pytest.raises(ValueError, match="no request"):
        decision_units(without_request, None)
    review = review_case("c", [("i1", "CERTIFY")])
    without_campaign = review.model_copy(
        update={"view": review.view.model_copy(update={"campaign": None})}
    )
    with pytest.raises(ValueError, match="no campaign"):
        decision_units(without_campaign, None)
    other = review_case("c2", [("i2", "CERTIFY")])
    mismatched = review.model_copy(update={"gold": other.gold})
    with pytest.raises(ValueError, match="don't match"):
        decision_units(mismatched, None)
