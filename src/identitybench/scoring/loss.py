"""Risk cost: the SPEC §12.3 loss matrix, normalized risk cost and its sensitivity grid.

The unit of cost is one human review. A case's cost has two parts (SPEC §12.3):

- **routing**: every case that goes to a human costs ``routing`` (1 by default). That is every
  ESCALATE, correct or not, and every INVALID, which falls back to a human;
- **error cost** on top: the off-diagonal cells of the matrix, plus over-duration (3·w) and
  over-restriction (0.5) on approvals, plus 0.5 of overhead for a failed automation (INVALID).

With the defaults this reproduces the printed matrix exactly, routing included:

| gold ↓ / pred → | APPROVE | DENY | ESCALATE | INVALID |
|---|---:|---:|---:|---:|
| APPROVE | 0 | 2 | 1 | 1.5 |
| DENY | 10·w | 0 | 1 | 1.5 |
| ESCALATE | 5·w | 2 | 1 | 1.5 |

Review items read CERTIFY, REVOKE and ESCALATE in place of APPROVE, DENY and ESCALATE, which
``Label`` already does. ``always_escalate`` routes every case and makes no error, so its mean
cost is the routing cost and NRC (mean cost over ``always_escalate``'s mean cost under the same
settings) is 1 for it by construction.

**Sensitivity.** The report recomputes mean cost and NRC under every combination of three
wrongful-approval multipliers, three routing costs and two case mixes (SPEC §12.3). The
multiplier is the DENY→APPROVE cell's multiple of w (10 by default). The ESCALATE→APPROVE cell
is the other wrongful approval, so it scales with it and keeps its configured ratio (5:10 by
default). The production-like mix reweights gold classes to fixed shares (60% A / 25% N / 15% E
for requests by default) instead of the dataset's own shares.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from identitybench.schemas.enums import Label

__all__ = [
    "DEFAULT_PRODUCTION_MIX",
    "CostSetting",
    "LossMatrix",
    "Mix",
    "mix_weights",
]

Mix = Literal["dataset", "production"]

DEFAULT_PRODUCTION_MIX: Mapping[Label, float] = {Label.A: 0.60, Label.N: 0.25, Label.E: 0.15}
"""SPEC §12.3: the production-like request mix, as gold-class shares."""


def _real_number(value: Any) -> Any:
    """Accept an int or float but not a bool: ``True`` would silently become a cost of 1."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"expected a number, got {type(value).__name__}")
    return value


Cost = Annotated[float, BeforeValidator(_real_number)]


class LossMatrix(BaseModel):
    """SPEC §12.3 costs, in units of one human review. ``× w`` cells scale with risk weight.

    This is also the shape of an inline ``loss_matrix:`` in an experiment config: a mapping of
    any of these field names to numbers. Unknown keys are rejected so a typo can't silently
    leave a default in place.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    routing: Cost = Field(default=1.0, gt=0)
    """Cost of sending a case to a human: every ESCALATE and every INVALID. Must be positive,
    because it is the unit NRC is measured in."""
    invalid_overhead: Cost = Field(default=0.5, ge=0)
    """Added to routing for an INVALID output (the failed automation)."""
    wrongful_approval: Cost = Field(default=10.0, gt=0)
    """× w: gold DENY (REVOKE) predicted APPROVE (CERTIFY). Positive, because the sensitivity
    grid scales the approval cells relative to it."""
    escalation_approval: Cost = Field(default=5.0, ge=0)
    """× w: gold ESCALATE predicted APPROVE (CERTIFY)."""
    wrongful_denial: Cost = Field(default=2.0, ge=0)
    """Gold APPROVE (CERTIFY) predicted DENY (REVOKE)."""
    escalation_denial: Cost = Field(default=2.0, ge=0)
    """Gold ESCALATE predicted DENY (REVOKE)."""
    over_duration: Cost = Field(default=3.0, ge=0)
    """× w: a correct-label approval whose effective duration is longer than gold's."""
    over_restriction: Cost = Field(default=0.5, ge=0)
    """A correct-label approval whose effective duration is shorter than gold's."""

    @classmethod
    def from_config(cls, value: str | Mapping[str, Any]) -> LossMatrix:
        """The matrix an experiment config names: ``default`` or an inline mapping."""
        if isinstance(value, str):
            if value != "default":
                raise ValueError(f"unknown loss matrix {value!r}; use 'default' or a mapping")
            return cls()
        return cls.model_validate(dict(value))

    def cost(
        self,
        gold: Label,
        pred: Label,
        weight: float,
        *,
        over_duration: bool = False,
        over_restriction: bool = False,
    ) -> float:
        """Total cost of one decision, routing included.

        ``over_duration`` and ``over_restriction`` only apply when gold and prediction are both
        APPROVE; the caller derives them from effective durations (SPEC §12.1).
        """
        if gold is Label.INVALID:
            raise ValueError("gold labels are A, N or E")
        if over_duration and over_restriction:
            raise ValueError("a duration can't be both too long and too short")
        if (over_duration or over_restriction) and not (gold is Label.A and pred is Label.A):
            raise ValueError("duration errors only apply to an approval of a gold approval")
        if pred is Label.INVALID:
            return self.routing + self.invalid_overhead
        if pred is Label.E:
            return self.routing
        if pred is Label.N:
            return {Label.A: self.wrongful_denial, Label.E: self.escalation_denial}.get(gold, 0.0)
        if gold is Label.N:
            return self.wrongful_approval * weight
        if gold is Label.E:
            return self.escalation_approval * weight
        if over_duration:
            return self.over_duration * weight
        if over_restriction:
            return self.over_restriction
        return 0.0

    def with_setting(self, setting: CostSetting) -> LossMatrix:
        """This matrix under a sensitivity setting: the wrongful-approval multiplier replaces
        ``wrongful_approval`` (the ESCALATE→APPROVE cell keeps its ratio to it) and the routing
        cost replaces ``routing``. The mix is applied to cases, not to the matrix."""
        scale = setting.wrongful_approval_multiplier / self.wrongful_approval
        return LossMatrix.model_validate(
            self.model_dump()
            | {
                "wrongful_approval": setting.wrongful_approval_multiplier,
                "escalation_approval": self.escalation_approval * scale,
                "routing": setting.routing,
            }
        )


@dataclass(frozen=True, slots=True)
class CostSetting:
    """One cell of the sensitivity grid (SPEC §12.3)."""

    wrongful_approval_multiplier: float
    routing: float
    mix: Mix

    @property
    def key(self) -> str:
        """Stable identifier, e.g. ``wa=10|routing=1|mix=dataset``."""
        return f"wa={self.wrongful_approval_multiplier:g}|routing={self.routing:g}|mix={self.mix}"


def mix_weights(golds: Sequence[Label], target: Mapping[Label, float] | None) -> list[float] | None:
    """Per-unit weights that reweight gold classes to the ``target`` shares.

    ``target`` None is the dataset mix: every unit weighs 1. Otherwise unit ``i`` weighs
    ``target[g_i] · n / count(g_i)``, so a weighted mean equals ``Σ_c target[c] · mean(x | c)``
    and the weights still sum to ``n``. Returns None when a class with a positive target share
    has no units: that mix can't be formed from these cases, and the result is n/a rather than
    a quietly different mix.
    """
    if target is None:
        return [1.0] * len(golds)
    counts = Counter(golds)
    if any(share > 0 and counts[label] == 0 for label, share in target.items()):
        return None
    n = len(golds)
    return [target.get(g, 0.0) * n / counts[g] for g in golds]
