"""Prediction record contract: per-task output validation and status consistency."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from identitybench.schemas.enums import Task
from identitybench.schemas.outputs import JmlOutput, RequestOutput, ReviewOutput
from identitybench.schemas.predictions import Prediction, PredictionStatus


def _base(**kw: object) -> dict[str, object]:
    return {"run_id": "r", "config_id": "c", "case_id": "k", **kw}


def test_output_is_validated_against_the_task_model() -> None:
    p = Prediction.model_validate(
        _base(task="access_request", status="ok", output={"decision": "DENY"})
    )
    assert isinstance(p.output, RequestOutput)
    r = Prediction.model_validate(_base(task="access_review", status="ok", output={"items": []}))
    assert isinstance(r.output, ReviewOutput)
    j = Prediction.model_validate(_base(task="jml", status="ok", output={"operations": []}))
    assert isinstance(j.output, JmlOutput)


def test_wrong_shape_for_task_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Prediction.model_validate(_base(task="jml", status="ok", output={"decision": "DENY"}))


def test_status_and_output_must_agree() -> None:
    with pytest.raises(ValidationError):
        Prediction.model_validate(_base(task="access_request", status="ok"))
    with pytest.raises(ValidationError):
        Prediction.model_validate(
            _base(task="access_request", status="invalid", output={"decision": "DENY"})
        )
    p = Prediction.model_validate(
        _base(task="access_request", status="refused", invalid_reason="refusal")
    )
    assert p.is_invalid and p.output is None


def test_jsonl_round_trip() -> None:
    p = Prediction.model_validate(
        _base(
            task=Task.access_review,
            status=PredictionStatus.ok,
            output={"items": [{"item_id": "rv_1", "decision": "REVOKE"}]},
            item_errors={"#3": "decision: invalid enum"},
        )
    )
    assert Prediction.model_validate_json(p.model_dump_json()) == p
