"""The JSON form of a reported number: point, n, interval, method and units (SPEC §1, §12.8).

SPEC §12.8 promises that every number ships with its n and a 95% interval suited to its
sampling structure. :class:`Metric` is that promise as a record a report can render without
re-deriving anything: the ratio of sums behind the point (``numerator`` / ``denominator``),
what one unit is and which cluster was resampled (``units``, ``cluster``), how the interval was
computed (``method``), and whether the metric is one of the predeclared primary metrics or
exploratory (``primary``).

Rare-event bounds (SPEC §12.8) ride along as :class:`Bound`. A bootstrap interval collapses to
[0, 0] when no events are observed, so where the spec prescribes an exact Clopper–Pearson bound
(request WAR with fewer than five events, the share of campaigns with any wrongful
certification, the JML unsafe plan rate) the bound states its estimand, units and failure event
next to the bootstrap interval instead of replacing it.

Every builder here takes its units in a canonical order chosen by the caller (cases sorted by
ID, items by campaign position). The bootstrap's resample stream depends on that order, so a
fixed order is what makes intervals identical however the predictions were ordered on disk.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from identitybench.scoring.stats import (
    DEFAULT_ALPHA,
    DEFAULT_N_BOOT,
    Interval,
    IntervalMethod,
    binomial_ci,
    ratio_ci,
)

__all__ = [
    "CAMPAIGN_UNITS",
    "EVENT_UNITS",
    "REQUEST_UNITS",
    "REVIEW_UNITS",
    "Bound",
    "Metric",
    "Resampling",
    "UnitSpec",
    "cp_upper_bound",
    "mean_metric",
    "ratio_metric",
]


@dataclass(frozen=True, slots=True)
class Resampling:
    """Bootstrap settings shared by every interval in one scoring pass (SPEC §12.8: B = 2,000)."""

    n_boot: int = DEFAULT_N_BOOT
    alpha: float = DEFAULT_ALPHA
    seed: int = 0


@dataclass(frozen=True, slots=True)
class UnitSpec:
    """What one unit of a metric is, and which cluster the bootstrap resamples (SPEC §12.8).

    ``cluster`` is None when units are independent and resampled one by one.
    """

    units: str
    cluster: str | None


REQUEST_UNITS = UnitSpec("base request case", None)
"""Base request cases are independent (SPEC §12.8)."""

REVIEW_UNITS = UnitSpec("review item", "campaign")
"""Review items are resampled by campaign; campaigns don't share identities (SPEC §8.2)."""

CAMPAIGN_UNITS = UnitSpec("campaign", None)
"""Whole campaigns, for per-campaign estimands; campaigns are independent units."""

EVENT_UNITS = UnitSpec("JML event", None)
"""JML events are independent (SPEC §12.8)."""


class Bound(BaseModel):
    """An exact one-sided Clopper–Pearson upper bound on a rate over independent units.

    SPEC §12.8: every bound states its estimand, its units and its failure event. ``k`` failure
    events were observed among ``n`` units; ``upper`` is the ``1 − alpha`` upper bound.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: IntervalMethod = "clopper_pearson_upper"
    estimand: str
    units: str
    failure_event: str
    k: int
    n: int
    upper: float
    alpha: float


class Metric(BaseModel):
    """One reported rate or mean, with its n and interval.

    ``point = numerator / denominator`` and is None (rendered n/a) when the denominator is 0,
    never 0 or 1 (SPEC §12.2). The metric's n is ``denominator``; ``n_units`` counts every unit
    the bootstrap resampled, including units outside the denominator (a gold-APPROVE case for
    WAR), because resampling the whole population keeps the cluster structure intact.
    ``low``/``high`` are None when the point is n/a or no interval could be computed (fewer than
    two clusters with a nonzero denominator; see ``scoring.stats``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    point: float | None
    low: float | None
    high: float | None
    numerator: float
    denominator: float
    n_units: int
    n_clusters: int
    units: str
    cluster: str | None
    method: IntervalMethod
    alpha: float
    dropped_resamples: int = 0
    primary: bool = False
    """SPEC §12.8 predeclared primary metric; every other metric is exploratory."""
    bound: Bound | None = None

    @property
    def is_na(self) -> bool:
        return self.point is None


def _from_interval(
    interval: Interval, spec: UnitSpec, *, primary: bool, bound: Bound | None
) -> Metric:
    return Metric(
        point=interval.point,
        low=interval.low,
        high=interval.high,
        numerator=interval.numerator_total,
        denominator=interval.denominator_total,
        n_units=interval.n_units,
        n_clusters=interval.n_clusters,
        units=spec.units,
        cluster=spec.cluster,
        method=interval.method,
        alpha=interval.alpha,
        dropped_resamples=interval.dropped_resamples,
        primary=primary,
        bound=bound,
    )


def ratio_metric(
    numerators: Sequence[float],
    denominators: Sequence[float],
    clusters: Sequence[str] | None,
    spec: UnitSpec,
    resampling: Resampling,
    *,
    primary: bool = False,
    bound: Bound | None = None,
) -> Metric:
    """A ratio of sums over units with its bootstrap interval at ``spec``'s units.

    ``clusters`` must be given exactly when ``spec`` names a cluster, so a metric can't claim
    campaign resampling while resampling items (or the reverse).
    """
    if (clusters is None) != (spec.cluster is None):
        raise ValueError(f"clusters must be given iff the units are clustered ({spec})")
    interval = ratio_ci(
        numerators,
        denominators,
        clusters,
        n_boot=resampling.n_boot,
        alpha=resampling.alpha,
        seed=resampling.seed,
    )
    return _from_interval(interval, spec, primary=primary, bound=bound)


def mean_metric(
    values: Sequence[float],
    clusters: Sequence[str] | None,
    spec: UnitSpec,
    resampling: Resampling,
    *,
    primary: bool = False,
) -> Metric:
    """A per-unit mean (``sum / n_units``) with its bootstrap interval; n/a for no units."""
    return ratio_metric(values, [1.0] * len(values), clusters, spec, resampling, primary=primary)


def cp_upper_bound(
    k: int, n: int, *, estimand: str, units: str, failure_event: str, alpha: float
) -> Bound | None:
    """The exact one-sided Clopper–Pearson bound for ``k`` events in ``n`` independent units.

    None when ``n == 0``: an empty denominator has no bound, only n/a.
    """
    if n == 0:
        return None
    interval = binomial_ci(k, n, alpha=alpha, one_sided=True)
    assert interval.high is not None  # n > 0 always yields a bound
    return Bound(
        estimand=estimand,
        units=units,
        failure_event=failure_event,
        k=k,
        n=n,
        upper=interval.high,
        alpha=alpha,
    )
