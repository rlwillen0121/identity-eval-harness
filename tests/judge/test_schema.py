"""Parsing and validating judge outputs, and the JudgeScore record."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from identitybench.judge.rubric import DIMENSIONS
from identitybench.judge.schema import (
    JUDGE_OUTPUT_SCHEMA,
    JudgeOutputError,
    JudgeScore,
    JudgeStatus,
    RubricScores,
    parse_judge_response,
    parse_rubric_scores,
)
from identitybench.providers.base import ModelResponse
from identitybench.schemas.enums import Task

from .helpers import scores_json

VALID: dict[str, Any] = {
    "faithfulness": 4,
    "decisive_coverage": 3,
    "actionability": 2,
    "auditability": 1,
    "notes": "ok",
}


def test_schema_is_strict_and_unbounded() -> None:
    assert JUDGE_OUTPUT_SCHEMA["additionalProperties"] is False
    assert set(JUDGE_OUTPUT_SCHEMA["required"]) == {*DIMENSIONS, "notes"}
    for dim in DIMENSIONS:
        assert JUDGE_OUTPUT_SCHEMA["properties"][dim] == {"type": "integer"}


def test_valid_scores_parse() -> None:
    scores = parse_rubric_scores(VALID)
    assert [scores.value(d) for d in DIMENSIONS] == [4, 3, 2, 1]
    assert scores.notes == "ok"


def test_integral_float_and_missing_notes_are_accepted() -> None:
    data = {dim: 2.0 for dim in DIMENSIONS}
    scores = parse_rubric_scores(data)
    assert scores.faithfulness == 2 and isinstance(scores.faithfulness, int)
    assert scores.notes == ""
    assert parse_rubric_scores({**data, "notes": None}).notes == ""


def test_extra_keys_are_ignored() -> None:
    assert parse_rubric_scores({**VALID, "overall": 9}).faithfulness == 4


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (True, "must be an integer"),
        ("3", "must be an integer"),
        (2.5, "must be an integer"),
        (0, "from 1 to 4, got 0"),
        (5, "from 1 to 4, got 5"),
        (None, "must be an integer"),
    ],
)
def test_bad_scores_are_rejected(value: object, message: str) -> None:
    with pytest.raises(JudgeOutputError, match=message):
        parse_rubric_scores({**VALID, "actionability": value})


def test_every_problem_is_listed() -> None:
    with pytest.raises(JudgeOutputError) as info:
        parse_rubric_scores({"faithfulness": 9, "notes": 3})
    text = str(info.value)
    for needle in ("faithfulness", "missing decisive_coverage", "missing auditability", "notes"):
        assert needle in text


def test_rubric_scores_model_is_strict() -> None:
    with pytest.raises(ValidationError):
        RubricScores(**{**VALID, "faithfulness": True})
    with pytest.raises(KeyError):
        parse_rubric_scores(VALID).value("tone")


def test_response_parsed_dict_wins() -> None:
    response = ModelResponse(text="not json", parsed=dict(VALID))
    verdict = parse_judge_response(response)
    assert verdict.status is JudgeStatus.ok
    assert verdict.scores is not None and verdict.scores.decisive_coverage == 3


def test_response_text_in_a_fence_is_extracted() -> None:
    verdict = parse_judge_response(ModelResponse(text=f"Here:\n```json\n{scores_json()}\n```"))
    assert verdict.status is JudgeStatus.ok


def test_verdict_after_an_echoed_object_is_chosen() -> None:
    text = '{"note": "draft"} then the verdict ' + scores_json(faithfulness=1)
    verdict = parse_judge_response(ModelResponse(text=text))
    assert verdict.scores is not None and verdict.scores.faithfulness == 1


@pytest.mark.parametrize(
    ("response", "status", "error"),
    [
        (ModelResponse(refused=True), JudgeStatus.refused, "refused"),
        (ModelResponse(error="timeout"), JudgeStatus.error, "timeout"),
        (ModelResponse(text="I give it a 4"), JudgeStatus.invalid, "no JSON object"),
        (ModelResponse(text=scores_json(auditability=7)), JudgeStatus.invalid, "auditability"),
    ],
)
def test_failed_responses(response: ModelResponse, status: JudgeStatus, error: str) -> None:
    verdict = parse_judge_response(response)
    assert verdict.status is status
    assert verdict.scores is None
    assert verdict.error is not None and error in verdict.error


def _score(status: JudgeStatus, scores: RubricScores | None) -> JudgeScore:
    return JudgeScore(
        target_id="t",
        run_id="r",
        config_id="c",
        case_id="k",
        family_id="f",
        task=Task.access_request,
        correct=True,
        output_family="openai",
        judge="j",
        judge_family="anthropic",
        status=status,
        scores=scores,
        rubric_sha256="0" * 64,
    )


def test_judge_score_requires_scores_exactly_when_ok() -> None:
    scores = parse_rubric_scores(VALID)
    assert _score(JudgeStatus.ok, scores).scores == scores
    assert _score(JudgeStatus.error, None).scores is None
    with pytest.raises(ValidationError):
        _score(JudgeStatus.ok, None)
    with pytest.raises(ValidationError):
        _score(JudgeStatus.invalid, scores)
