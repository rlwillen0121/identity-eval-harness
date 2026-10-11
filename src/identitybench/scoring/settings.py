"""Scoring settings: the loss matrix, the sensitivity grid and the statistics knobs.

Every scored summary records the settings that produced it, so a published number can always be
traced to its loss matrix, case mix and bootstrap seed (SPEC §12.3: costs live in a published
loss matrix that anyone can change).
"""

from __future__ import annotations

import math
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from identitybench.schemas.enums import Label, Task
from identitybench.scoring.calibration import DEFAULT_WAR_TARGET
from identitybench.scoring.loss import DEFAULT_PRODUCTION_MIX, CostSetting, LossMatrix, Mix
from identitybench.scoring.metrics import Resampling
from identitybench.scoring.stats import DEFAULT_ALPHA, DEFAULT_N_BOOT

__all__ = ["SMALL_N", "ScoreSettings"]

SMALL_N = 30
"""SPEC §10.2: per-tag results with n below this are flagged."""


def _default_mix() -> dict[Task, dict[Label, float]]:
    return {Task.access_request: dict(DEFAULT_PRODUCTION_MIX)}


class ScoreSettings(BaseModel):
    """How one scoring pass prices decisions and computes intervals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    loss: LossMatrix = Field(default_factory=LossMatrix)
    production_mix: dict[Task, dict[Label, float]] = Field(default_factory=_default_mix)
    """Gold-class shares of the production-like case mix, per task (SPEC §12.3). A task without
    an entry is analysed under its dataset mix only: the spec defines a production-like mix for
    requests, and the review target mix (~70/20/10) is already the realistic shape (§8.2)."""
    wrongful_approval_multipliers: tuple[float, ...] = (3.0, 10.0, 30.0)
    routing_costs: tuple[float, ...] = (0.5, 1.0, 2.0)
    war_target: float = Field(default=DEFAULT_WAR_TARGET, gt=0.0, lt=1.0)
    """SPEC §12.7: the WAR target an operating point is certified against."""
    small_n: int = Field(default=SMALL_N, ge=1)
    n_boot: int = Field(default=DEFAULT_N_BOOT, ge=1)
    alpha: float = Field(default=DEFAULT_ALPHA, gt=0.0, lt=1.0)
    seed: int = 0

    @model_validator(mode="after")
    def _check(self) -> Self:
        for task, shares in self.production_mix.items():
            if Label.INVALID in shares:
                raise ValueError(f"{task}: a case mix is over gold labels A, N and E")
            if any(share < 0 for share in shares.values()):
                raise ValueError(f"{task}: case-mix shares must be non-negative")
            if not math.isclose(sum(shares.values()), 1.0, abs_tol=1e-9):
                raise ValueError(f"{task}: case-mix shares must sum to 1")
        if not self.wrongful_approval_multipliers or not self.routing_costs:
            raise ValueError("the sensitivity grid needs at least one multiplier and routing cost")
        if any(m <= 0 for m in self.wrongful_approval_multipliers):
            raise ValueError("wrongful-approval multipliers must be positive")
        if any(r <= 0 for r in self.routing_costs):
            raise ValueError("routing costs must be positive")
        return self

    @property
    def resampling(self) -> Resampling:
        return Resampling(n_boot=self.n_boot, alpha=self.alpha, seed=self.seed)

    def grid(self, task: Task) -> list[CostSetting]:
        """Every sensitivity setting for ``task``, in a fixed order (multiplier, routing, mix)."""
        mixes: list[Mix] = ["dataset", "production"] if task in self.production_mix else ["dataset"]
        return [
            CostSetting(multiplier, routing, mix)
            for multiplier in self.wrongful_approval_multipliers
            for routing in self.routing_costs
            for mix in mixes
        ]
