"""Interval estimates, paired comparisons and multiplicity control (SPEC §10.3, §12.8, §13.3).

Every rate IdentityBench reports is a *ratio of sums* over units (cases, review items, events):
WAR is wrongful approvals over gold-non-APPROVE cases, NRC is total cost over cases, and so on.
When units are correlated (review items inside a campaign, twins inside a family) the honest
interval resamples whole clusters, sums numerators and denominators inside the resample, and
takes the ratio of those sums. Averaging per-cluster ratios instead would weight a 5-item
campaign like a 60-item one, which is a different estimand.

Conventions shared by the whole module:

* An estimate whose denominator is 0 is ``None`` and renders as ``n/a`` (SPEC §12.2), never as
  0 or 1. Bootstrap resamples whose denominator is 0 are dropped and counted, not imputed.
* Randomness only comes from ``numpy.random.default_rng(seed)``; the same inputs and seed give
  the same interval. The resample index stream depends only on ``(seed, n_boot, n_clusters)``,
  so a single-config interval and a paired comparison with the same seed and clusters see the
  same resamples.
* With fewer than two *effective* clusters (or units, for iid resampling) the bootstrap cannot
  see sampling variation at all. A cluster is effective when its denominator is nonzero: if
  only one cluster has a denominator, every usable resample repeats that cluster and
  reproduces the observed ratio. Rather than print a zero-width interval that looks like
  certainty, such intervals report the point with ``low``/``high`` (and a paired p-value) of
  ``None``.
* Bootstrap intervals collapse to [0, 0] when no events are observed. For independent units with
  fewer than ``RARE_EVENT_THRESHOLD`` events, callers should report :func:`binomial_ci` (exact
  Clopper–Pearson) instead, as SPEC §12.8 requires for request WAR and JML unsafe plan rate.
* SPEC §10.3 separates precision from comparison power. Precision is the width of a
  single-config interval (:func:`ratio_ci`, :func:`binomial_ci`). Power depends on how many units
  two configs disagree on, which :func:`discordance` counts and
  :func:`minimum_detectable_difference` turns into the smallest difference a release can claim
  to detect.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import numpy as np
from numpy.typing import NDArray
from scipy.stats import beta as _beta_dist  # pyright: ignore[reportMissingTypeStubs]
from scipy.stats import norm as _norm_dist  # pyright: ignore[reportMissingTypeStubs]

__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_N_BOOT",
    "DEFAULT_POWER",
    "RARE_EVENT_THRESHOLD",
    "Discordance",
    "HolmResult",
    "Interval",
    "IntervalMethod",
    "PairedResult",
    "binomial_ci",
    "clopper_pearson",
    "clopper_pearson_upper",
    "confusion_matrix",
    "discordance",
    "holm_bonferroni",
    "kappa_ci",
    "mean_ci",
    "minimum_detectable_difference",
    "paired_bootstrap_diff",
    "quadratic_weighted_kappa",
    "ratio_ci",
]

DEFAULT_N_BOOT: Final = 2000
"""Bootstrap resamples for every reported interval (SPEC §12.8: B = 2,000)."""

DEFAULT_ALPHA: Final = 0.05
"""Two-sided 95% intervals; one-sided 95% upper bounds."""

DEFAULT_POWER: Final = 0.8
"""Power at which a release states its minimum detectable difference (SPEC §10.3)."""

RARE_EVENT_THRESHOLD: Final = 5
"""Below this many events on independent units, report an exact Clopper–Pearson bound."""

_MAX_GATHER: Final = 1 << 22
"""Upper bound on elements in one bootstrap index chunk (~32 MB of int64), so memory stays flat
however many units are resampled."""

type FloatArray = NDArray[np.float64]
type IntArray = NDArray[np.intp]
type Numbers = Sequence[float] | NDArray[Any]
type Labels = Sequence[Hashable] | NDArray[Any]

IntervalMethod = Literal[
    "bootstrap_iid",
    "bootstrap_cluster",
    "clopper_pearson",
    "clopper_pearson_upper",
]


@dataclass(frozen=True, slots=True)
class Interval:
    """A point estimate with its interval and the bookkeeping a report needs to state it.

    ``point``, ``low`` and ``high`` are all ``None`` when the estimand is undefined (empty
    denominator). Only ``low`` and ``high`` are ``None`` when the point is defined but no interval
    can be computed: every bootstrap resample had to be dropped, or fewer than two clusters had a
    nonzero denominator. ``n_clusters`` still counts every cluster. For kappa,
    ``numerator_total`` and ``denominator_total`` are the weighted observed and chance-expected
    disagreement (κ = 1 − numerator/denominator). For
    one-sided bounds ``low`` is 0. ``alpha`` is the interval's error rate: a two-sided
    ``1 − alpha`` interval, or a one-sided ``1 − alpha`` upper bound.
    """

    point: float | None
    low: float | None
    high: float | None
    n_units: int
    n_clusters: int
    numerator_total: float
    denominator_total: float
    method: IntervalMethod
    dropped_resamples: int = 0
    alpha: float = DEFAULT_ALPHA

    @property
    def is_na(self) -> bool:
        """True when the estimand is undefined and must be reported as n/a."""
        return self.point is None

    def format(self, digits: int = 3, *, percent: bool = False) -> str:
        """Render for tables and logs: ``point [low, high]``, ``point`` alone, or ``n/a``.

        A one-sided bound renders as ``point (95% upper ≤ high)`` so a table can't pass it off
        as a two-sided interval (SPEC §12.8: every bound states its estimand).
        """
        if self.point is None:
            return "n/a"
        scale, suffix = (100.0, "%") if percent else (1.0, "")

        def fmt(x: float) -> str:
            return f"{x * scale:.{digits}f}{suffix}"

        if self.low is None or self.high is None:
            return fmt(self.point)
        if self.method == "clopper_pearson_upper":
            level = f"{round((1.0 - self.alpha) * 100.0, 6):g}"
            return f"{fmt(self.point)} ({level}% upper ≤ {fmt(self.high)})"
        return f"{fmt(self.point)} [{fmt(self.low)}, {fmt(self.high)}]"


@dataclass(frozen=True, slots=True)
class PairedResult:
    """Difference ``a − b`` between two configurations scored on the same units.

    ``diff`` is ``None`` only when either configuration's estimand is undefined, and then so are
    ``low``, ``high`` and ``p_value``. With both points defined, ``diff`` is always reported;
    ``low``, ``high`` and ``p_value`` are still ``None`` when no resample was usable or either
    configuration has fewer than two clusters with a nonzero denominator.
    """

    diff: float | None
    low: float | None
    high: float | None
    p_value: float | None
    point_a: float | None
    point_b: float | None
    n_units: int
    n_clusters: int
    dropped_resamples: int = 0


@dataclass(frozen=True, slots=True)
class Discordance:
    """How often two configurations disagree on a binary per-unit outcome (SPEC §10.3).

    ``a_only`` counts units flagged by config ``a`` but not ``b`` (for WAR, cases ``a``
    wrongfully approved and ``b`` did not); ``b_only`` the reverse.
    """

    n_units: int
    a_only: int
    b_only: int

    @property
    def n_discordant(self) -> int:
        """Units where the two configurations differ."""
        return self.a_only + self.b_only

    @property
    def rate(self) -> float | None:
        """Share of units where the configurations differ; ``None`` with no units."""
        return self.n_discordant / self.n_units if self.n_units else None


@dataclass(frozen=True, slots=True)
class HolmResult:
    """Holm-adjusted p-value and the step-down rejection decision at the family's alpha."""

    adjusted_p: float
    reject: bool


# --------------------------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------------------------


def _as_vector(values: Numbers, name: str, length: int | None = None) -> FloatArray:
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional, got shape {arr.shape}")
    if length is not None and arr.size != length:
        raise ValueError(f"{name} has {arr.size} entries, expected {length}")
    if not np.all(np.isfinite(arr)):
        raise ValueError(f"{name} contains non-finite values")
    return arr


def _as_denominators(values: Numbers, name: str, length: int) -> FloatArray:
    """Denominators are counts or non-negative weights; a negative one has no meaning in a rate."""
    arr = _as_vector(values, name, length)
    if np.any(arr < 0.0):
        raise ValueError(f"{name} must be non-negative")
    return arr


def _check_alpha(alpha: float) -> None:
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")


def _check_boot(n_boot: int, alpha: float) -> None:
    if n_boot < 1:
        raise ValueError(f"n_boot must be at least 1, got {n_boot}")
    _check_alpha(alpha)


def _cluster_codes(clusters: Labels | None, n: int) -> tuple[IntArray, int]:
    """Integer cluster codes in first-appearance order; each unit is its own cluster if None."""
    if clusters is None:
        return np.arange(n, dtype=np.intp), n
    if len(clusters) != n:
        raise ValueError(f"clusters has {len(clusters)} entries, expected {n}")
    index: dict[Hashable, int] = {}
    codes = np.fromiter((index.setdefault(c, len(index)) for c in clusters), dtype=np.intp, count=n)
    return codes, len(index)


def _method(clusters: Labels | None) -> IntervalMethod:
    return "bootstrap_iid" if clusters is None else "bootstrap_cluster"


# --------------------------------------------------------------------------------------------
# Bootstrap core
# --------------------------------------------------------------------------------------------


def _cluster_sums(columns: FloatArray, codes: IntArray, n_clusters: int) -> FloatArray:
    """Per-cluster column sums: ``(n_units, m)`` -> ``(n_clusters, m)``."""
    return np.stack(
        [
            np.bincount(codes, weights=columns[:, j], minlength=n_clusters)
            for j in range(columns.shape[1])
        ],
        axis=1,
    )


def _resampled_sums(cluster_sums: FloatArray, n_boot: int, seed: int) -> FloatArray:
    """Column totals over ``n_boot`` resamples of whole clusters drawn with replacement.

    Each resample draws ``n_clusters`` cluster indices (one row of an index matrix) and sums the
    per-cluster totals it selected. Chunk boundaries depend only on ``n_boot`` and the cluster
    count, so the index stream for a given seed is the same whatever columns are being summed.
    """
    rng = np.random.default_rng(seed)
    n_clusters, n_cols = cluster_sums.shape
    out = np.empty((n_boot, n_cols), dtype=np.float64)
    rows_per_chunk = max(1, _MAX_GATHER // n_clusters)
    for start in range(0, n_boot, rows_per_chunk):
        rows = min(rows_per_chunk, n_boot - start)
        idx = rng.integers(0, n_clusters, size=(rows, n_clusters))
        for j in range(n_cols):
            out[start : start + rows, j] = cluster_sums[:, j][idx].sum(axis=1)
    return out


def _percentile_ci(samples: FloatArray, alpha: float) -> tuple[float | None, float | None]:
    if samples.size == 0:
        return None, None
    low, high = np.quantile(samples, [alpha / 2.0, 1.0 - alpha / 2.0])
    return float(low), float(high)


# --------------------------------------------------------------------------------------------
# Ratio and mean intervals
# --------------------------------------------------------------------------------------------


def ratio_ci(
    numerators: Numbers,
    denominators: Numbers,
    clusters: Labels | None = None,
    *,
    n_boot: int = DEFAULT_N_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = 0,
) -> Interval:
    """Percentile bootstrap interval for ``sum(numerators) / sum(denominators)``.

    Each unit contributes a numerator and a denominator (for WAR: 1/1 for a wrongful approval,
    0/1 for a correct non-approval, 0/0 for a gold-APPROVE case). With ``clusters`` given, whole
    clusters are resampled (SPEC §12.8: campaigns for reviews, families for robustness);
    otherwise units are resampled iid. Resamples whose denominator sums to 0 are dropped and
    counted in ``dropped_resamples``. When the observed denominator is 0 the estimate is n/a and
    no bootstrap is run. When fewer than two clusters have a nonzero denominator only the point
    is reported (see the module docstring). Denominators must be non-negative.
    """
    num = _as_vector(numerators, "numerators")
    den = _as_denominators(denominators, "denominators", num.size)
    _check_boot(n_boot, alpha)
    codes, n_clusters = _cluster_codes(clusters, num.size)
    num_total, den_total = float(num.sum()), float(den.sum())
    method = _method(clusters)
    if den_total == 0.0:
        return Interval(
            None, None, None, num.size, n_clusters, num_total, den_total, method, alpha=alpha
        )
    sums = _cluster_sums(np.column_stack([num, den]), codes, n_clusters)
    if np.count_nonzero(sums[:, 1]) < 2:
        return Interval(
            num_total / den_total,
            None,
            None,
            num.size,
            n_clusters,
            num_total,
            den_total,
            method,
            alpha=alpha,
        )
    boot = _resampled_sums(sums, n_boot, seed)
    valid = boot[:, 1] != 0.0
    low, high = _percentile_ci(boot[valid, 0] / boot[valid, 1], alpha)
    return Interval(
        point=num_total / den_total,
        low=low,
        high=high,
        n_units=num.size,
        n_clusters=n_clusters,
        numerator_total=num_total,
        denominator_total=den_total,
        method=method,
        dropped_resamples=int(n_boot - np.count_nonzero(valid)),
        alpha=alpha,
    )


def mean_ci(
    values: Numbers,
    clusters: Labels | None = None,
    *,
    n_boot: int = DEFAULT_N_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = 0,
) -> Interval:
    """Bootstrap interval for a per-unit mean (e.g. mean risk cost per case behind NRC).

    With clusters this is the ratio estimator ``sum(values) / n_units`` over resampled clusters,
    so unequal cluster sizes are weighted by their unit counts. Empty input is n/a.
    """
    arr = _as_vector(values, "values")
    return ratio_ci(arr, np.ones_like(arr), clusters, n_boot=n_boot, alpha=alpha, seed=seed)


# --------------------------------------------------------------------------------------------
# Exact binomial bounds
# --------------------------------------------------------------------------------------------


def _check_binomial(k: int, n: int, alpha: float) -> None:
    if n < 1:
        raise ValueError(f"n must be at least 1, got {n}; an empty denominator is n/a")
    if not 0 <= k <= n:
        raise ValueError(f"k must be in [0, n], got k={k}, n={n}")
    _check_alpha(alpha)


def _beta_ppf(q: float, a: float, b: float) -> float:
    return float(_beta_dist.ppf(q, a, b))  # pyright: ignore[reportUnknownMemberType]


def _norm_ppf(q: float) -> float:
    return float(_norm_dist.ppf(q))  # pyright: ignore[reportUnknownMemberType]


def clopper_pearson(k: int, n: int, alpha: float = DEFAULT_ALPHA) -> tuple[float, float]:
    """Exact two-sided ``1 − alpha`` Clopper–Pearson interval for ``k`` events in ``n`` trials."""
    _check_binomial(k, n, alpha)
    low = 0.0 if k == 0 else _beta_ppf(alpha / 2.0, k, n - k + 1)
    high = 1.0 if k == n else _beta_ppf(1.0 - alpha / 2.0, k + 1, n - k)
    return low, high


def clopper_pearson_upper(k: int, n: int, alpha: float = DEFAULT_ALPHA) -> float:
    """Exact one-sided ``1 − alpha`` Clopper–Pearson upper bound.

    This is the bound used to certify a WAR target (SPEC §12.7). With zero events it equals
    ``1 − alpha^(1/n)``: about 0.0149 for n = 200, and below 0.01 only from n = 299 (§10.3).
    """
    _check_binomial(k, n, alpha)
    return 1.0 if k == n else _beta_ppf(1.0 - alpha, k + 1, n - k)


def binomial_ci(
    k: int, n: int, *, alpha: float = DEFAULT_ALPHA, one_sided: bool = False
) -> Interval:
    """Clopper–Pearson bound wrapped as an :class:`Interval` so reports render it uniformly.

    Only valid for independent units (base request cases, JML events, whole campaigns). ``n == 0``
    (with ``k == 0``) gives an n/a interval rather than an error, matching the
    empty-denominator convention.
    """
    method: IntervalMethod = "clopper_pearson_upper" if one_sided else "clopper_pearson"
    if n == 0:
        _check_alpha(alpha)
        if k != 0:
            raise ValueError(f"k must be 0 when n is 0, got k={k}")
        return Interval(None, None, None, 0, 0, 0.0, 0.0, method, alpha=alpha)
    if one_sided:
        low, high = 0.0, clopper_pearson_upper(k, n, alpha)
    else:
        low, high = clopper_pearson(k, n, alpha)
    return Interval(k / n, low, high, n, n, float(k), float(n), method, alpha=alpha)


# --------------------------------------------------------------------------------------------
# Paired comparison and multiplicity
# --------------------------------------------------------------------------------------------


def paired_bootstrap_diff(
    num_a: Numbers,
    den_a: Numbers,
    num_b: Numbers,
    den_b: Numbers,
    clusters: Labels | None,
    *,
    n_boot: int = DEFAULT_N_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = 0,
) -> PairedResult:
    """Paired cluster bootstrap for ``ratio_a − ratio_b`` over the same units (SPEC §12.8).

    Entry ``i`` of all four arrays (and of ``clusters``) must describe the same unit, so each
    resample applies the same clusters to both configurations and the case-mix noise they share
    cancels. Resamples where either denominator is 0 are dropped. The two-sided p-value is
    ``2 × min(P*(d ≤ 0), P*(d ≥ 0))`` over the usable resamples, capped at 1 and floored at
    ``1 / (usable + 1)`` because a finite bootstrap cannot resolve smaller p-values. When either
    configuration has fewer than two clusters with a nonzero denominator only the point
    difference is reported. Denominators must be non-negative.
    """
    na = _as_vector(num_a, "num_a")
    n = na.size
    da = _as_denominators(den_a, "den_a", n)
    nb = _as_vector(num_b, "num_b", n)
    db = _as_denominators(den_b, "den_b", n)
    _check_boot(n_boot, alpha)
    codes, n_clusters = _cluster_codes(clusters, n)
    den_a_total, den_b_total = float(da.sum()), float(db.sum())
    point_a = float(na.sum()) / den_a_total if den_a_total != 0.0 else None
    point_b = float(nb.sum()) / den_b_total if den_b_total != 0.0 else None
    if point_a is None or point_b is None:
        return PairedResult(None, None, None, None, point_a, point_b, n, n_clusters)
    sums = _cluster_sums(np.column_stack([na, da, nb, db]), codes, n_clusters)
    if np.count_nonzero(sums[:, 1]) < 2 or np.count_nonzero(sums[:, 3]) < 2:
        return PairedResult(point_a - point_b, None, None, None, point_a, point_b, n, n_clusters)
    boot = _resampled_sums(sums, n_boot, seed)
    valid = (boot[:, 1] != 0.0) & (boot[:, 3] != 0.0)
    kept = boot[valid]
    diffs = kept[:, 0] / kept[:, 1] - kept[:, 2] / kept[:, 3]
    dropped = int(n_boot - diffs.size)
    low, high = _percentile_ci(diffs, alpha)
    p_value: float | None = None
    if diffs.size:
        tail = int(min(np.count_nonzero(diffs <= 0.0), np.count_nonzero(diffs >= 0.0)))
        p_value = max(min(1.0, 2.0 * tail / diffs.size), 1.0 / (diffs.size + 1))
    return PairedResult(
        diff=point_a - point_b,
        low=low,
        high=high,
        p_value=p_value,
        point_a=point_a,
        point_b=point_b,
        n_units=n,
        n_clusters=n_clusters,
        dropped_resamples=dropped,
    )


def _as_flags(
    values: Sequence[bool] | Sequence[int] | NDArray[Any], name: str
) -> NDArray[np.bool_]:
    arr = np.asarray(values)
    if arr.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional, got shape {arr.shape}")
    if not np.all((arr == 0) | (arr == 1)):
        raise ValueError(f"{name} must contain only booleans (or 0/1)")
    return arr.astype(np.bool_)


def discordance(
    flags_a: Sequence[bool] | Sequence[int] | NDArray[Any],
    flags_b: Sequence[bool] | Sequence[int] | NDArray[Any],
) -> Discordance:
    """Count the units on which two configurations' binary outcomes differ (SPEC §10.3).

    Entry ``i`` of both arrays must be the same unit, restricted to the metric's denominator
    (for WAR: the gold-non-APPROVE cases, flagged when the config wrongfully approved).
    """
    a = _as_flags(flags_a, "flags_a")
    b = _as_flags(flags_b, "flags_b")
    if a.size != b.size:
        raise ValueError(f"flags_b has {b.size} entries, expected {a.size}")
    return Discordance(
        n_units=int(a.size),
        a_only=int(np.count_nonzero(a & ~b)),
        b_only=int(np.count_nonzero(b & ~a)),
    )


def minimum_detectable_difference(
    discordance_rate: float,
    n_units: int,
    *,
    alpha: float = DEFAULT_ALPHA,
    power: float = DEFAULT_POWER,
) -> float | None:
    """Smallest paired difference in a rate detectable at ``power`` (two-sided ``alpha``).

    SPEC §10.3: power depends on how many units two configs disagree on, not on ``n_units``
    alone. This solves Connor's (1987) paired-proportion sample-size equation
    ``√n · δ = z₁₋α/₂ · √ψ + z_power · √(ψ − δ²)`` for ``δ``, where ``ψ`` is the
    discordance rate (from :func:`discordance` on a pilot, e.g. M2) and ``δ`` is the difference
    in rates over the same ``n_units`` denominator units. Squaring gives a quadratic whose
    larger root is the answer, but squaring also admits a spurious root: the larger root solves
    the unsquared equation only when ``n_units ≥ z₁₋α/₂²`` (the left side ``√n · δ − z · √ψ``
    must be non-negative). Below that size (3.84 at alpha 0.05, 6.63 at 0.01) no ``δ``
    reaches the target power.

    The approximation assumes independent units (base request cases, JML events). For
    clustered units (review items within campaigns) it is optimistic; simulate with
    :func:`paired_bootstrap_diff` instead. Returns ``None`` when no difference is detectable at
    that size and discordance: ``n_units`` is below ``z₁₋α/₂²``, or the solution exceeds ``ψ``
    (``δ`` cannot exceed ``ψ``, every disagreement favoring one config).
    """
    _check_alpha(alpha)
    if not 0.5 <= power < 1.0:
        raise ValueError(f"power must be in [0.5, 1), got {power}")
    if not 0.0 <= discordance_rate <= 1.0:
        raise ValueError(f"discordance_rate must be in [0, 1], got {discordance_rate}")
    if n_units < 1:
        raise ValueError(f"n_units must be at least 1, got {n_units}")
    z_a, z_b = _norm_ppf(1.0 - alpha / 2.0), _norm_ppf(power)
    # (n + z_b²)·δ² − 2·√n·z_a·√ψ·δ + (z_a² − z_b²)·ψ = 0, discriminant ∝ n − z_a² + z_b².
    # n ≥ z_a² rejects the spurious root and keeps spread ≥ z_b² ≥ 0 (power ≥ 0.5).
    if discordance_rate == 0.0 or n_units < z_a**2:
        return None
    spread = n_units - z_a**2 + z_b**2
    delta = (
        math.sqrt(discordance_rate)
        * (math.sqrt(n_units) * z_a + z_b * math.sqrt(spread))
        / (n_units + z_b**2)
    )
    return delta if delta <= discordance_rate else None


def holm_bonferroni[K: Hashable](
    pvalues: Mapping[K, float], alpha: float = DEFAULT_ALPHA
) -> dict[K, HolmResult]:
    """Holm step-down adjustment across a family of comparisons (SPEC §12.8).

    The adjusted p-value of the i-th smallest raw p-value is
    ``max_{j ≤ i} min(1, (m − j + 1) · p_(j))``; a hypothesis is rejected iff its adjusted value
    is at most ``alpha``, which is exactly Holm's sequential procedure. Ties keep input order.
    The result preserves the input's key order.
    """
    _check_alpha(alpha)
    items = list(pvalues.items())
    for key, p in items:
        if not 0.0 <= p <= 1.0:
            raise ValueError(f"p-value for {key!r} must be in [0, 1], got {p}")
    m = len(items)
    order = sorted(range(m), key=lambda i: items[i][1])
    adjusted = [0.0] * m
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * items[i][1]))
        adjusted[i] = running
    return {key: HolmResult(adjusted[i], adjusted[i] <= alpha) for i, (key, _) in enumerate(items)}


# --------------------------------------------------------------------------------------------
# Agreement (judge validation, SPEC §13.3)
# --------------------------------------------------------------------------------------------


def _category_codes[T: Hashable](
    a: Sequence[T], b: Sequence[T], categories: Sequence[T]
) -> tuple[IntArray, IntArray, int]:
    index = {c: i for i, c in enumerate(categories)}
    if len(index) != len(categories):
        raise ValueError("categories must be distinct")
    if len(index) < 2:
        raise ValueError("at least two categories are needed")
    if len(a) != len(b):
        raise ValueError(f"raters have different lengths: {len(a)} vs {len(b)}")
    try:
        ia = np.fromiter((index[x] for x in a), dtype=np.intp, count=len(a))
        ib = np.fromiter((index[x] for x in b), dtype=np.intp, count=len(b))
    except KeyError as exc:
        raise ValueError(f"rating {exc.args[0]!r} is not one of the categories") from exc
    return ia, ib, len(index)


def confusion_matrix[T: Hashable](
    a: Sequence[T], b: Sequence[T], categories: Sequence[T]
) -> NDArray[np.int64]:
    """``k × k`` count matrix with rater ``a`` on rows and rater ``b`` on columns.

    Rows and columns follow the order of ``categories``; unused categories get zero rows.
    """
    ia, ib, k = _category_codes(a, b, categories)
    counts = np.bincount(ia * k + ib, minlength=k * k).astype(np.int64)
    return counts.reshape(k, k)


def _qwk_terms(counts: FloatArray) -> tuple[FloatArray, FloatArray]:
    """Weighted observed and chance-expected disagreement for ``(..., k, k)`` count tensors.

    Weights are ``(i − j)²``; the usual ``(k − 1)²`` normalization cancels in the ratio.
    """
    k = counts.shape[-1]
    pos = np.arange(k, dtype=np.float64)
    weights = (pos[:, None] - pos[None, :]) ** 2
    total = counts.sum(axis=(-2, -1))
    rows = counts.sum(axis=-1)
    cols = counts.sum(axis=-2)
    observed = (counts * weights).sum(axis=(-2, -1))
    with np.errstate(invalid="ignore", divide="ignore"):
        expected = np.einsum("...i,ij,...j->...", rows, weights, cols) / total
    return observed, np.nan_to_num(expected, nan=0.0)


def quadratic_weighted_kappa[T: Hashable](
    a: Sequence[T], b: Sequence[T], categories: Sequence[T]
) -> float | None:
    """Quadratic-weighted Cohen's κ between two raters over ordered ``categories``.

    Matches ``sklearn.metrics.cohen_kappa_score(a, b, labels=categories, weights="quadratic")``.
    Returns ``None`` when κ is undefined: no items, or both raters put every item in one
    category (zero chance-expected disagreement).
    """
    observed, expected = _qwk_terms(confusion_matrix(a, b, categories).astype(np.float64))
    exp = float(expected)
    return None if exp == 0.0 else 1.0 - float(observed) / exp


def kappa_ci[T: Hashable](
    a: Sequence[T],
    b: Sequence[T],
    categories: Sequence[T],
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = 0,
    *,
    alpha: float = DEFAULT_ALPHA,
    clusters: Labels | None = None,
) -> Interval:
    """Percentile bootstrap interval for quadratic-weighted κ, resampling rated items.

    Pass ``clusters`` to resample groups of items together (e.g. rationales from the same case).
    Resamples where κ is undefined are dropped and counted. With a single cluster only the point
    is reported.
    """
    _check_boot(n_boot, alpha)
    ia, ib, k = _category_codes(a, b, categories)
    n = ia.size
    codes, n_clusters = _cluster_codes(clusters, n)
    method = _method(clusters)
    cells = np.zeros((n, k * k), dtype=np.float64)
    cells[np.arange(n), ia * k + ib] = 1.0
    observed, expected = _qwk_terms(cells.sum(axis=0).reshape(k, k))
    obs, exp = float(observed), float(expected)
    if exp == 0.0:
        return Interval(None, None, None, n, n_clusters, obs, exp, method, alpha=alpha)
    if n_clusters < 2:
        return Interval(1.0 - obs / exp, None, None, n, n_clusters, obs, exp, method, alpha=alpha)
    boot = _resampled_sums(_cluster_sums(cells, codes, n_clusters), n_boot, seed)
    b_obs, b_exp = _qwk_terms(boot.reshape(n_boot, k, k))
    valid = b_exp != 0.0
    low, high = _percentile_ci(1.0 - b_obs[valid] / b_exp[valid], alpha)
    return Interval(
        point=1.0 - obs / exp,
        low=low,
        high=high,
        n_units=n,
        n_clusters=n_clusters,
        numerator_total=obs,
        denominator_total=exp,
        method=method,
        dropped_resamples=int(n_boot - np.count_nonzero(valid)),
        alpha=alpha,
    )
