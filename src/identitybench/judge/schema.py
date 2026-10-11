"""The judge's output contract: JSON schema, validated scores and response parsing (SPEC §13.2).

The schema follows the same strict-structured-output subset as ``schemas.json_schemas``: every
property required, no extra properties, no numeric bounds. The 1-4 range is enforced here, at
parse time, so a provider that ignores the schema can't slip a 0 or a 5 into the averages.

A judge reply that can't be validated is recorded as ``invalid`` and never coerced into a
score: a missing dimension, a string, a boolean or a fractional number all fail. The only
leniency is an integral float (``3.0``), which is the same number.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from identitybench.judge.rubric import DIMENSIONS
from identitybench.providers._json import extract_json_object
from identitybench.providers.base import ModelResponse
from identitybench.schemas.enums import Task
from identitybench.schemas.predictions import PredictionUsage

__all__ = [
    "JUDGE_OUTPUT_SCHEMA",
    "JUDGE_SCHEMA_NAME",
    "JudgeOutputError",
    "JudgeScore",
    "JudgeStatus",
    "JudgeVerdict",
    "RubricScores",
    "parse_judge_response",
    "parse_rubric_scores",
]

JUDGE_SCHEMA_NAME = "rubric_scores"

JUDGE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **{dim: {"type": "integer"} for dim in DIMENSIONS},
        "notes": {"type": "string"},
    },
    "required": [*DIMENSIONS, "notes"],
    "additionalProperties": False,
}

Score = Annotated[int, Field(strict=True, ge=1, le=4)]


class RubricScores(BaseModel):
    """One rater's scores for one rationale (a judge's verdict or a human label)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    faithfulness: Score
    decisive_coverage: Score
    actionability: Score
    auditability: Score
    notes: str = ""

    def value(self, dimension: str) -> int:
        """The score on ``dimension`` (one of ``DIMENSIONS``)."""
        if dimension not in DIMENSIONS:
            raise KeyError(f"unknown dimension {dimension!r}")
        return int(getattr(self, dimension))


class JudgeStatus(StrEnum):
    ok = "ok"
    invalid = "invalid"
    """The judge answered, but not with a valid score object."""
    refused = "refused"
    error = "error"
    """Transport, auth or provider failure after the provider's own retries."""


class JudgeOutputError(ValueError):
    """A judge reply (or a label row) that isn't a valid score object."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class JudgeVerdict(_Frozen):
    """What one judge call produced."""

    status: JudgeStatus
    scores: RubricScores | None = None
    error: str | None = None


def _score(dimension: str, value: object, problems: list[str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        problems.append(f"{dimension} must be an integer from 1 to 4, got {value!r}")
        return None
    if isinstance(value, float) and not value.is_integer():
        problems.append(f"{dimension} must be an integer from 1 to 4, got {value!r}")
        return None
    number = int(value)
    if not 1 <= number <= 4:
        problems.append(f"{dimension} must be from 1 to 4, got {number}")
        return None
    return number


def parse_rubric_scores(data: Mapping[str, Any]) -> RubricScores:
    """Validate a decoded score object; raise ``JudgeOutputError`` listing every problem.

    Keys beyond the four dimensions and ``notes`` are ignored (a judge that adds a field still
    answered the question); ``notes`` may be missing or null.
    """
    problems: list[str] = []
    values: dict[str, int] = {}
    for dim in DIMENSIONS:
        if dim not in data:
            problems.append(f"missing {dim}")
            continue
        score = _score(dim, data[dim], problems)
        if score is not None:
            values[dim] = score
    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        problems.append(f"notes must be a string, got {type(notes).__name__}")
    if problems:
        raise JudgeOutputError("; ".join(problems))
    return RubricScores(
        faithfulness=values["faithfulness"],
        decisive_coverage=values["decisive_coverage"],
        actionability=values["actionability"],
        auditability=values["auditability"],
        notes=notes if isinstance(notes, str) else "",
    )


def parse_judge_response(response: ModelResponse) -> JudgeVerdict:
    """Turn a judge's ``ModelResponse`` into a verdict.

    Native structured output (``response.parsed``) is used when present; otherwise the JSON
    object is extracted from the text with the same tolerant-but-never-repairing extractor the
    runner uses for model outputs, preferring the last object that carries every dimension.
    """
    if response.refused:
        return JudgeVerdict(status=JudgeStatus.refused, error="judge refused")
    if response.error is not None:
        return JudgeVerdict(status=JudgeStatus.error, error=response.error)
    data = (
        response.parsed
        if response.parsed is not None
        else extract_json_object(response.text, required_keys=DIMENSIONS)
    )
    if data is None:
        return JudgeVerdict(status=JudgeStatus.invalid, error="no JSON object in the reply")
    try:
        scores = parse_rubric_scores(data)
    except JudgeOutputError as exc:
        return JudgeVerdict(status=JudgeStatus.invalid, error=str(exc))
    return JudgeVerdict(status=JudgeStatus.ok, scores=scores)


class JudgeScore(_Frozen):
    """One judge's verdict on one rationale: the record ``judge_predictions`` writes per
    (prediction, item, judge).

    It carries the output's family, correctness and case family so every aggregate (the
    self-family rule, the correct/incorrect split, family residuals, clustered intervals) can be
    recomputed from these records alone, and the rubric hash so scores from different rubric
    versions are never pooled.
    """

    target_id: str
    """``targets.target_id_for`` for a prediction, or the label ID for a labeled rationale."""
    run_id: str
    config_id: str
    case_id: str
    family_id: str
    """``Case.family_id``: the unit every bootstrap over judge scores resamples (SPEC §12.8)."""
    task: Task
    sample_idx: int = 0
    item_id: str | None = None
    correct: bool
    output_family: str
    weight: float = 1.0
    """``JudgeTarget.weight``: the rationale's inverse inclusion probability under the
    campaign's sampling, which the correctness means are weighted by. 1 when unsampled."""
    judge: str
    """The judge's name (``Judge.name``)."""
    judge_family: str
    status: JudgeStatus
    scores: RubricScores | None = None
    error: str | None = None
    rubric_sha256: str
    """``Rubric.sha256``: the judge's whole instructions (system prompt, rubric, layout)."""
    rubric_file_sha256: str = ""
    """``Rubric.rubric_file_sha256``: the rubric file alone, which validation matches with the
    human labels' (empty on records written before it was recorded; validation refuses those)."""
    ignored_params: tuple[str, ...] = ()
    """Generation settings the judge's provider reported dropping for this call (its
    ``raw["ignored_params"]``), so a verdict produced without temperature 0 is flagged
    (SPEC §12.8)."""
    cache_hit: bool = False
    latency_s: float = 0.0
    usage: PredictionUsage = PredictionUsage()

    @model_validator(mode="after")
    def _check(self) -> JudgeScore:
        if (self.status is JudgeStatus.ok) != (self.scores is not None):
            raise ValueError("scores must be present exactly when status is ok")
        if self.weight <= 0:
            raise ValueError(f"weight must be positive, got {self.weight}")
        return self
