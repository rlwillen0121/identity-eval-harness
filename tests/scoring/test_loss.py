"""SPEC §12.3 loss matrix, sensitivity settings, case-mix weights and scoring settings."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from identitybench.schemas.enums import Label, Task
from identitybench.scoring.loss import DEFAULT_PRODUCTION_MIX, CostSetting, LossMatrix, mix_weights
from identitybench.scoring.settings import ScoreSettings

A, N, E, INVALID = Label.A, Label.N, Label.E, Label.INVALID

PRINTED_MATRIX: dict[tuple[Label, Label], tuple[float, float]] = {
    (A, A): (0.0, 0.0),
    (A, N): (2.0, 0.0),
    (A, E): (1.0, 0.0),
    (A, INVALID): (1.5, 0.0),
    (N, A): (0.0, 10.0),
    (N, N): (0.0, 0.0),
    (N, E): (1.0, 0.0),
    (N, INVALID): (1.5, 0.0),
    (E, A): (0.0, 5.0),
    (E, N): (2.0, 0.0),
    (E, E): (1.0, 0.0),
    (E, INVALID): (1.5, 0.0),
}
"""SPEC §12.3's table as (gold, pred) -> (constant, multiple of w), routing included."""


@pytest.mark.parametrize("weight", [0.5, 1.0, 2.0, 4.0])
@pytest.mark.parametrize(("gold", "pred"), list(PRINTED_MATRIX))
def test_default_matrix_is_the_printed_table(gold: Label, pred: Label, weight: float) -> None:
    constant, multiple = PRINTED_MATRIX[(gold, pred)]
    assert LossMatrix().cost(gold, pred, weight) == constant + multiple * weight


def test_duration_errors_on_approvals() -> None:
    loss = LossMatrix()
    assert loss.cost(A, A, 2.0, over_duration=True) == 6.0  # 3·w
    assert loss.cost(A, A, 2.0, over_restriction=True) == 0.5
    assert loss.cost(A, A, 4.0, over_restriction=True) == 0.5  # not weighted


def test_duration_flags_are_validated() -> None:
    loss = LossMatrix()
    with pytest.raises(ValueError, match="both"):
        loss.cost(A, A, 1.0, over_duration=True, over_restriction=True)
    with pytest.raises(ValueError, match="gold approval"):
        loss.cost(N, A, 1.0, over_duration=True)
    with pytest.raises(ValueError, match="gold labels"):
        loss.cost(INVALID, A, 1.0)


def test_invalid_costs_routing_plus_overhead_under_any_routing() -> None:
    loss = LossMatrix(routing=2.0)
    assert loss.cost(N, INVALID, 4.0) == 2.5
    assert loss.cost(A, E, 4.0) == 2.0
    assert loss.cost(N, A, 1.0) == 10.0  # error costs don't change with routing


def test_sensitivity_setting_scales_both_wrongful_approval_cells() -> None:
    loss = LossMatrix().with_setting(CostSetting(30.0, 0.5, "dataset"))
    assert loss.wrongful_approval == 30.0
    assert loss.escalation_approval == 15.0  # keeps the 5:10 ratio
    assert loss.routing == 0.5
    assert loss.cost(E, A, 2.0) == 30.0
    custom = LossMatrix(wrongful_approval=12.0, escalation_approval=4.0)
    assert custom.with_setting(CostSetting(3.0, 1.0, "dataset")).escalation_approval == 1.0


def test_setting_key_is_stable() -> None:
    assert CostSetting(10.0, 1.0, "production").key == "wa=10|routing=1|mix=production"
    assert CostSetting(0.5, 0.5, "dataset").key == "wa=0.5|routing=0.5|mix=dataset"


def test_from_config() -> None:
    assert LossMatrix.from_config("default") == LossMatrix()
    assert LossMatrix.from_config({"routing": 2, "over_duration": 4.5}) == LossMatrix(
        routing=2.0, over_duration=4.5
    )
    with pytest.raises(ValueError, match="unknown loss matrix"):
        LossMatrix.from_config("strict")
    with pytest.raises(ValidationError):
        LossMatrix.from_config({"routnig": 2})  # typo: unknown key
    with pytest.raises(ValidationError):
        LossMatrix.from_config({"routing": 0})  # NRC's unit must be positive
    with pytest.raises(ValidationError):
        LossMatrix.from_config({"wrongful_denial": -1})
    with pytest.raises(ValidationError):
        LossMatrix.from_config({"invalid_overhead": True})  # a bool is not a cost


def test_mix_weights_reweight_gold_classes() -> None:
    golds = [A, A, A, A, N, N, N, E, E, E]
    weights = mix_weights(golds, DEFAULT_PRODUCTION_MIX)
    assert weights is not None
    assert weights[:4] == [pytest.approx(1.5)] * 4  # 0.6 · 10 / 4
    assert weights[4:7] == [pytest.approx(0.25 * 10 / 3)] * 3
    assert weights[7:] == [pytest.approx(0.5)] * 3
    assert math.isclose(sum(weights), 10.0)
    assert mix_weights(golds, None) == [1.0] * 10


def test_mix_weights_are_none_when_a_weighted_class_is_absent() -> None:
    assert mix_weights([A, N], DEFAULT_PRODUCTION_MIX) is None
    assert mix_weights([A, N], {A: 0.5, N: 0.5, E: 0.0}) == [1.0, 1.0]


def test_settings_validation_and_grid() -> None:
    settings = ScoreSettings()
    grid = settings.grid(Task.access_request)
    assert len(grid) == 3 * 3 * 2
    assert grid[0] == CostSetting(3.0, 0.5, "dataset")
    assert {s.mix for s in settings.grid(Task.access_review)} == {"dataset"}
    with pytest.raises(ValidationError, match="sum to 1"):
        ScoreSettings(production_mix={Task.access_request: {A: 0.5, N: 0.25}})
    with pytest.raises(ValidationError, match="gold labels"):
        ScoreSettings(production_mix={Task.access_request: {A: 0.5, INVALID: 0.5}})
    with pytest.raises(ValidationError, match="positive"):
        ScoreSettings(routing_costs=(0.0,))
    with pytest.raises(ValidationError, match="positive"):
        ScoreSettings(wrongful_approval_multipliers=(-3.0,))
    with pytest.raises(ValidationError, match="at least one"):
        ScoreSettings(routing_costs=())
    with pytest.raises(ValidationError, match="non-negative"):
        ScoreSettings(production_mix={Task.access_request: {A: 1.5, N: -0.5}})
    assert settings.resampling.n_boot == settings.n_boot
