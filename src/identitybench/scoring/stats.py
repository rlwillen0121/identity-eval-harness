"""Interval estimates, paired comparisons, agreement and multiplicity control (SPEC §10.3,
§12.8, §13.3).

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
* A paired bootstrap p-value is anti-conservative when few units carry the difference: its
  tail shrinks like e^-d in the number d of units that do, whatever n is. At n = 40 with every
  discordant unit favoring one side, d = 4 gives a bootstrap p near 0.03 where the exact test
  gives 0.125. :func:`paired_signflip_test` is the exact randomization test of the same
  difference over the resampling units: each unit keeps the size of its paired difference and
  its sign is flipped at random. Comparisons report the larger of the two p-values
  (``scoring.summary.compare_configs``). For 0/1 outcomes on independent units every
  difference is ±1 and the test is the exact two-sided sign (McNemar) test. A plain sign test
  would ignore sizes, so an NRC difference resting on four catastrophic approvals, with twenty
  cheap escalations pointing the other way, would pass on the twenty; the sign-flip test weighs
  each unit by its difference and leaves the four to carry their own weight.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Final, Literal

import numpy as np
from numpy.typing import NDArray
from scipy.stats import beta as _beta_dist  # pyright: ignore[reportMissingTypeStubs]
from scipy.stats import norm as _norm_dist  # pyright: ignore[reportMissingTypeStubs]

__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_N_BOOT",
    "DEFAULT_POWER",
    "MAX_EXACT_WORK",
    "RARE_EVENT_THRESHOLD",
    "AlphaMetric",
    "Discordance",
    "HolmResult",
    "Interval",
    "IntervalMethod",
    "PairedResult",
    "SignFlipTest",
    "binomial_ci",
    "clopper_pearson",
    "clopper_pearson_upper",
    "confusion_matrix",
    "discordance",
    "holm_bonferroni",
    "kappa_ci",
    "krippendorff_alpha",
    "krippendorff_alpha_ci",
    "mean_ci",
    "minimum_detectable_difference",
    "paired_bootstrap_diff",
    "paired_signflip_test",
    "quadratic_weighted_kappa",
    "ratio_ci",
    "ratio_difference_ci",
    "sign_test_p",
    "signflip_p",
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
"""Upper bound on elements in one array of resampling indices, sign draws or exact sign-flip
probabilities (~32 MB of 8-byte entries), so memory stays flat however many units there are."""

_TIE_TOLERANCE: Final = 1e-9
"""Two per-unit totals closer than this (relative to their size, or absolutely below 1) are a
tie in :func:`paired_signflip_test`. Totals are sums of loss-matrix entries or 0/1 flags, so
equal decisions give equal totals up to float rounding (a custom routing cost of 1.7 summed in
a different order), while a real difference is at least the smallest gap between two
loss-matrix entries, many orders of magnitude above this. The same tolerance decides whether a
difference sits on a lattice (:func:`signflip_p`)."""

_LATTICE_DENOMINATOR: Final = 10**6
"""Largest denominator :func:`signflip_p` tries when it reads a per-unit difference as a
fraction. Default loss-matrix costs are multiples of 0.5 (risk weights 0.5 to 4 times
half-integer cells), and a custom matrix written with a few decimals stays far below this."""

MAX_EXACT_WORK: Final = 2 * 10**9
"""Budget for an exact sign-flip null distribution (:func:`signflip_p`), counted as
d · (2·M + 1) for d differences whose sizes sum to M lattice steps (an upper bound on the array
updates). Default costs need about 10^7 at SPEC §10.3's size. At the edge of the budget one
comparison takes up to a few seconds (measured: 0.6 s for about 7,000 discordant request cases,
2.4 s at the widest span the budget admits). Past it the p-value is a seeded Monte Carlo
estimate instead."""

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
class SignFlipTest:
    """Paired sign-flip test on per-unit differences ``a − b`` (ties dropped).

    ``n_units`` counts the units (clusters, when clustered) that bear on the metric: a nonzero
    shared denominator or a nonzero numerator for either configuration. For the compared
    metrics a numerator implies its denominator, so these are the units the metric is a rate
    over. ``a_higher`` counts those where ``a``'s contribution
    exceeds ``b``'s (for WAR, units where ``a`` wrongfully approved more), ``b_higher`` the
    reverse; the rest are ties. ``p_value`` is the two-sided sign-flip p-value of the
    differences (:func:`signflip_p`), 1 when no unit differs. ``exact`` is False only when the
    differences sit on no lattice small enough to enumerate and ``p_value`` is a seeded Monte
    Carlo estimate.
    """

    n_units: int
    a_higher: int
    b_higher: int
    p_value: float
    exact: bool

    @property
    def n_discordant(self) -> int:
        """Units whose contributions differ: the d of the test."""
        return self.a_higher + self.b_higher

    @property
    def rate(self) -> float | None:
        """Share of the denominator's units that differ; ``None`` with no such unit."""
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


def sign_test_p(n_positive: int, n_negative: int) -> float:
    """Exact two-sided sign test p-value for ``n_positive`` and ``n_negative`` nonzero signs.

    Under the null each discordant unit is equally likely to go either way, so the split is
    Binomial(d, 1/2) with d = ``n_positive + n_negative``; the p-value is twice the smaller
    tail, capped at 1. It is computed in exact integer arithmetic (no floating-point tail sums),
    and is 1 when d = 0. With d discordant units the smallest attainable value is 2/2^d, so at
    alpha 0.05 at least six units must differ, all in one direction, before anything can be
    rejected. For binary outcomes this is the exact McNemar test, and it is :func:`signflip_p`
    of differences that all have one size, which that function computes through here.

    Past about 1,075 one-way units the exact value is below the smallest positive float, and
    the result is floored there rather than rounded to 0: a p-value is never published as
    exactly zero, and rounding up only makes the test more conservative.
    """
    if n_positive < 0 or n_negative < 0:
        raise ValueError(f"sign counts must be non-negative, got {n_positive}, {n_negative}")
    d = n_positive + n_negative
    if d == 0:
        return 1.0
    # Σ_{i ≤ k} C(d, i), each term from the last: C(d, i + 1) = C(d, i) · (d − i) / (i + 1).
    term = tail = 1
    for i in range(min(n_positive, n_negative)):
        term = term * (d - i) // (i + 1)
        tail += term
    return max(math.ulp(0.0), min(1.0, 2 * tail / 2**d))


def _lattice(magnitudes: FloatArray) -> list[int] | None:
    """Each magnitude as a whole number of one common step, or None when they share none.

    Each distinct magnitude is read as the nearest fraction whose denominator is at most
    ``_LATTICE_DENOMINATOR`` and must match it to ``_TIE_TOLERANCE``; the step is the largest
    one that all those fractions are whole multiples of. Reading through fractions also puts
    float noise back on the lattice: 1.7 + 1.7 + 1.7 and 5.1 are both 51 tenths, so a signed sum
    that differs from the observed one only by rounding counts as reaching it.
    """
    values, inverse = np.unique(magnitudes, return_inverse=True)
    fractions: list[Fraction] = []
    for value in (float(v) for v in values):
        fraction = Fraction(value).limit_denominator(_LATTICE_DENOMINATOR)
        if abs(float(fraction) - value) > _TIE_TOLERANCE * max(1.0, value):
            return None
        fractions.append(fraction)
    scale = math.lcm(*(f.denominator for f in fractions))
    whole = [f.numerator * (scale // f.denominator) for f in fractions]
    step = math.gcd(*whole)
    return [whole[int(i)] // step for i in inverse]


def _signflip_exact(steps: Sequence[int], observed: int) -> float:
    """``P(|Σ ε_i · steps_i| ≥ observed)`` for independent fair signs ε_i, by dynamic programming.

    The signed sum lives on the integers in ``[−Σ steps, Σ steps]``; each step splits every
    reachable sum's probability evenly between ``sum − step`` and ``sum + step``. Steps are
    taken smallest first so the array stays short for as long as possible. Every entry is
    non-negative, so the additions lose no relative precision and a small tail stays accurate.
    """
    dist = np.ones(1, dtype=np.float64)
    for step in sorted(steps):
        grown = np.zeros(dist.size + 2 * step, dtype=np.float64)
        grown[: dist.size] = dist
        grown[2 * step :] += dist
        grown *= 0.5
        dist = grown
    distance = np.abs(np.arange(dist.size) - dist.size // 2)
    return float(dist[distance >= observed].sum())


def _signflip_monte_carlo(
    magnitudes: FloatArray, observed: float, n_draws: int, seed: int
) -> float:
    """Seeded Monte Carlo sign-flip p-value, ``(k + 1) / (n_draws + 1)``.

    ``k`` counts the draws of independent fair signs whose signed sum lands at least as far from
    0 as the observed one, within float rounding. Counting the observed assignment itself (the
    +1) keeps it a valid p-value at any ``n_draws`` (Phipson and Smyth 2010) and never below
    ``1 / (n_draws + 1)``. Chunks are sized like the bootstrap's, so memory stays flat.
    """
    rng = np.random.default_rng(seed)
    threshold = observed - _TIE_TOLERANCE * max(1.0, float(magnitudes.sum()))
    rows_per_chunk = max(1, _MAX_GATHER // magnitudes.size)
    hits = 0
    for start in range(0, n_draws, rows_per_chunk):
        rows = min(rows_per_chunk, n_draws - start)
        signs = rng.integers(0, 2, size=(rows, magnitudes.size)) * 2 - 1
        hits += int(np.count_nonzero(np.abs(signs @ magnitudes) >= threshold))
    return (hits + 1) / (n_draws + 1)


def _signflip(
    differences: FloatArray, *, n_draws: int, seed: int, max_exact_work: int
) -> tuple[float, bool]:
    """:func:`signflip_p` and whether it was computed exactly."""
    if n_draws < 1:
        raise ValueError(f"n_draws must be at least 1, got {n_draws}")
    if max_exact_work < 0:
        raise ValueError(f"max_exact_work must be non-negative, got {max_exact_work}")
    nonzero = differences[np.abs(differences) > _TIE_TOLERANCE]
    if nonzero.size == 0:
        return 1.0, True
    magnitudes = np.abs(nonzero)
    largest = float(magnitudes.max())
    if float(np.ptp(magnitudes)) <= _TIE_TOLERANCE * max(1.0, largest):
        # Every difference has one size: the exact sign test, whatever that size is.
        positive = int(np.count_nonzero(nonzero > 0.0))
        return sign_test_p(positive, nonzero.size - positive), True
    steps = _lattice(magnitudes)
    if steps is not None:
        span = 2 * sum(steps) + 1
        if span <= _MAX_GATHER and len(steps) * span <= max_exact_work:
            up = (nonzero > 0.0).tolist()
            observed = abs(sum(s if u else -s for s, u in zip(steps, up, strict=True)))
            # The float64 sum of 2^-d multiples can round past 1 when the observed sum is the
            # smallest reachable one (the whole distribution is summed), so cap it: Holm
            # rejects p-values outside [0, 1].
            return min(1.0, max(math.ulp(0.0), _signflip_exact(steps, observed))), True
    return _signflip_monte_carlo(magnitudes, abs(float(nonzero.sum())), n_draws, seed), False


def signflip_p(
    differences: Numbers,
    *,
    n_draws: int = DEFAULT_N_BOOT,
    seed: int = 0,
    max_exact_work: int = MAX_EXACT_WORK,
) -> float:
    """Two-sided paired sign-flip p-value: ``P(|Σ ε_i · δ_i| ≥ |Σ δ_i|)`` for fair signs ε_i.

    This is the exact randomization test (Fisher's, also called Pitman's) of a sum of paired
    differences. Under the null that two configurations are exchangeable on every unit, each
    unit's difference δ_i was as likely to come out −δ_i, so its sign is flipped at random while
    its size is kept. When every δ_i has one size this is the exact two-sided sign test
    (:func:`sign_test_p`): d = 4 one-way differences give 2/2^4 = 0.125. Unlike the sign test it
    weighs each unit by its difference: four differences of −40 against twenty of +1 give 0.125,
    where the sign test (20 against 4) would give 0.0015.

    Differences within ``_TIE_TOLERANCE`` of 0 are ties and are dropped (a zero difference is
    the same under either sign); with none left the p-value is 1. Differences of one size
    always take the exact sign-test path. Otherwise the value is exact when the differences are
    whole multiples of one small common step (any loss matrix whose costs have a few decimals),
    the null distribution's 2·M + 1 lattice points for M total steps fit in one array of
    ``_MAX_GATHER`` entries, and d · (2·M + 1) is at most ``max_exact_work``. Costs that share
    no small common step take the Monte Carlo path at any size: ``n_draws`` sign draws seeded
    with ``seed``, ``(k + 1) / (n_draws + 1)``, still a valid p-value, never below
    ``1 / (n_draws + 1)``, and the same for the same inputs. An exact value below the smallest
    positive float is floored there, as in :func:`sign_test_p`, and float rounding never takes
    it above 1.
    """
    diffs = _as_vector(differences, "differences")
    return _signflip(diffs, n_draws=n_draws, seed=seed, max_exact_work=max_exact_work)[0]


def paired_signflip_test(
    num_a: Numbers,
    den_a: Numbers,
    num_b: Numbers,
    den_b: Numbers,
    clusters: Labels | None,
    *,
    n_draws: int = DEFAULT_N_BOOT,
    seed: int = 0,
    max_exact_work: int = MAX_EXACT_WORK,
) -> SignFlipTest:
    """Paired sign-flip test on each unit's contribution to ``ratio_a − ratio_b``.

    For the metrics compared here the denominator is defined by gold alone (WAR's gold
    non-APPROVE cases, NRC's ``always_escalate`` cost), so both configurations share it, and
    the difference of the two ratios is ``Σ (num_a − num_b) / Σ den``: each unit (each cluster,
    when clustered) moves the difference by its own numerator difference δ_i. The test drops
    ties and computes :func:`signflip_p` of the δ_i, which is the randomization distribution of
    the metric difference itself. For a 0/1 outcome on independent units every δ_i is ±1 and
    this is the exact sign (McNemar) test. For a cost (NRC), or a per-campaign total of review
    items, each unit counts by its size, so a difference resting on four catastrophic approvals
    gets the p-value of four units, however many cheap escalations point the other way. It is
    the conservative companion to :func:`paired_bootstrap_diff`, whose p-value is
    anti-conservative when only a few units carry the difference (see the module docstring).

    Entry ``i`` of every array describes the same unit. ``den_a`` and ``den_b`` must be equal,
    or the numerator difference isn't the metric difference and ``ValueError`` is raised.
    Per-unit totals within ``_TIE_TOLERANCE`` of each other are ties. ``n_draws``, ``seed``
    and ``max_exact_work`` only matter when the p-value can't be computed exactly
    (:func:`signflip_p`).
    """
    na = _as_vector(num_a, "num_a")
    n = na.size
    da = _as_denominators(den_a, "den_a", n)
    nb = _as_vector(num_b, "num_b", n)
    db = _as_denominators(den_b, "den_b", n)
    if not np.array_equal(da, db):
        raise ValueError("a sign-flip test needs a shared denominator: den_a and den_b differ")
    codes, n_clusters = _cluster_codes(clusters, n)
    sums = _cluster_sums(np.column_stack([na, nb, da]), codes, n_clusters)
    a_sum, b_sum, den_sum = sums[:, 0], sums[:, 1], sums[:, 2]
    tied = np.isclose(a_sum, b_sum, rtol=_TIE_TOLERANCE, atol=_TIE_TOLERANCE)
    # Units that bear on the metric: in its denominator, or (never, for a rate whose numerator
    # implies its denominator) contributing to a numerator from outside it.
    bearing = (den_sum != 0.0) | (a_sum != 0.0) | (b_sum != 0.0)
    p_value, exact = _signflip(
        (a_sum - b_sum)[~tied], n_draws=n_draws, seed=seed, max_exact_work=max_exact_work
    )
    return SignFlipTest(
        n_units=int(np.count_nonzero(bearing)),
        a_higher=int(np.count_nonzero(~tied & (a_sum > b_sum))),
        b_higher=int(np.count_nonzero(~tied & (a_sum < b_sum))),
        p_value=p_value,
        exact=exact,
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


AlphaMetric = Literal["nominal", "ordinal", "interval"]


def _unit_coincidences(
    units: Sequence[Sequence[Hashable]], categories: Sequence[Hashable]
) -> tuple[FloatArray, int]:
    """Per-unit coincidence counts, ``(n_units, k * k)``, and ``k``.

    A unit with m ≥ 2 values adds every ordered pair of two different values' categories,
    weighted 1 / (m − 1) (Krippendorff 2011); a unit with fewer values pairs nothing.
    """
    index = {c: i for i, c in enumerate(categories)}
    if len(index) != len(categories):
        raise ValueError("categories must be distinct")
    if len(index) < 2:
        raise ValueError("at least two categories are needed")
    k = len(index)
    rows = np.zeros((len(units), k * k), dtype=np.float64)
    for row, values in enumerate(units):
        m = len(values)
        if m < 2:
            continue
        try:
            codes = np.fromiter((index[v] for v in values), dtype=np.intp, count=m)
        except KeyError as exc:
            raise ValueError(f"rating {exc.args[0]!r} is not one of the categories") from exc
        counts = np.bincount(codes, minlength=k).astype(np.float64)
        pairs = np.outer(counts, counts) - np.diag(counts)
        rows[row] = (pairs / (m - 1)).ravel()
    return rows, k


def _alpha_terms(
    coincidences: FloatArray, metric: AlphaMetric, values: FloatArray
) -> tuple[FloatArray, FloatArray]:
    """Observed and expected disagreement for ``(..., k, k)`` coincidence tensors, scaled so
    that α = 1 − observed / expected: Σ o·δ² and Σ n_c·n_k·δ² / (n − 1)."""
    k = coincidences.shape[-1]
    marginals = coincidences.sum(axis=-1)
    total = marginals.sum(axis=-1)
    if metric == "nominal":
        delta = np.broadcast_to(1.0 - np.eye(k), coincidences.shape)
    elif metric == "interval":
        delta = np.broadcast_to((values[:, None] - values[None, :]) ** 2, coincidences.shape)
    else:
        # Ordinal: δ(c, k) = Σ_{g=c..k} n_g − (n_c + n_k) / 2, from the pooled marginals.
        positions = np.arange(k, dtype=np.intp)
        low = np.minimum(positions[:, None], positions[None, :])
        high = np.maximum(positions[:, None], positions[None, :])
        cumulative = np.cumsum(marginals, axis=-1)
        between = (
            cumulative[..., high] - cumulative[..., low] + marginals[..., low]
        )  # Σ n_g for g from min(c, k) to max(c, k)
        half = (marginals[..., :, None] + marginals[..., None, :]) / 2.0
        delta = (between - half) ** 2
    observed = (coincidences * delta).sum(axis=(-2, -1))
    pairs = marginals[..., :, None] * marginals[..., None, :]
    with np.errstate(invalid="ignore", divide="ignore"):
        expected = (pairs * delta).sum(axis=(-2, -1)) / (total - 1.0)
    return observed, np.nan_to_num(expected, nan=0.0, posinf=0.0)


def krippendorff_alpha_ci(
    units: Sequence[Sequence[Hashable]],
    categories: Sequence[Hashable],
    metric: AlphaMetric = "ordinal",
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = 0,
    *,
    alpha: float = DEFAULT_ALPHA,
    clusters: Labels | None = None,
) -> Interval:
    """Krippendorff's α across any number of raters, with a percentile bootstrap interval.

    ``units`` holds, per rated item, the values the raters gave it (missing ratings simply
    left out); ``categories`` lists every possible value in order (for ``interval`` they must
    be numbers). Items with fewer than two values pair nothing and don't count toward
    ``n_units``. Pass ``clusters`` (one per item) to resample correlated items together, as
    :func:`kappa_ci` does; the bootstrap resamples whole items or clusters and recomputes α,
    marginals included. α is undefined (``n/a``) when no disagreement is expected: fewer than
    two pairable values, or all of them in one category.
    """
    _check_boot(n_boot, alpha)
    rows, k = _unit_coincidences(units, categories)
    values = (
        np.asarray(categories, dtype=np.float64)
        if metric == "interval"
        else np.arange(k, dtype=np.float64)
    )
    paired = rows.sum(axis=1) > 0
    codes, _ = _cluster_codes(clusters, len(units))
    rows, codes = rows[paired], codes[paired]
    n = int(paired.sum())
    effective = len(np.unique(codes))
    method = _method(clusters)
    observed, expected = _alpha_terms(rows.sum(axis=0).reshape(k, k), metric, values)
    obs, exp = float(observed), float(expected)
    if exp == 0.0:
        return Interval(None, None, None, n, effective, obs, exp, method, alpha=alpha)
    point = 1.0 - obs / exp
    if effective < 2:
        return Interval(point, None, None, n, effective, obs, exp, method, alpha=alpha)
    _, dense = np.unique(codes, return_inverse=True)
    sums = _cluster_sums(rows, dense.astype(np.intp), effective)
    boot = _resampled_sums(sums, n_boot, seed).reshape(n_boot, k, k)
    b_obs, b_exp = _alpha_terms(boot, metric, values)
    valid = b_exp != 0.0
    low, high = _percentile_ci(1.0 - b_obs[valid] / b_exp[valid], alpha)
    return Interval(
        point=point,
        low=low,
        high=high,
        n_units=n,
        n_clusters=effective,
        numerator_total=obs,
        denominator_total=exp,
        method=method,
        dropped_resamples=int(n_boot - np.count_nonzero(valid)),
        alpha=alpha,
    )


def krippendorff_alpha(
    units: Sequence[Sequence[Hashable]],
    categories: Sequence[Hashable],
    metric: AlphaMetric = "ordinal",
) -> float | None:
    """Krippendorff's α (no interval); ``None`` when undefined. See
    :func:`krippendorff_alpha_ci`."""
    rows, k = _unit_coincidences(units, categories)
    values = (
        np.asarray(categories, dtype=np.float64)
        if metric == "interval"
        else np.arange(k, dtype=np.float64)
    )
    observed, expected = _alpha_terms(rows.sum(axis=0).reshape(k, k), metric, values)
    exp = float(expected)
    return None if exp == 0.0 else 1.0 - float(observed) / exp


def ratio_difference_ci(
    numerators_a: Numbers,
    denominators_a: Numbers,
    numerators_b: Numbers,
    denominators_b: Numbers,
    clusters: Labels | None = None,
    *,
    n_boot: int = DEFAULT_N_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = 0,
) -> Interval:
    """Percentile bootstrap interval for ``Σnum_a / Σden_a − Σnum_b / Σden_b``, resampling
    units (or ``clusters``) jointly, so both ratios move together in every resample.

    Each unit contributes to either side or both (zeros elsewhere). n/a when either observed
    denominator is 0; only the point when fewer than two clusters have a nonzero denominator on
    either side; resamples where either is 0 are dropped and counted.
    """
    num_a = _as_vector(numerators_a, "numerators_a")
    n = num_a.size
    den_a = _as_denominators(denominators_a, "denominators_a", n)
    num_b = _as_vector(numerators_b, "numerators_b", n)
    den_b = _as_denominators(denominators_b, "denominators_b", n)
    _check_boot(n_boot, alpha)
    columns = [num_a, den_a, num_b, den_b]
    stacked = np.column_stack(columns)
    codes, n_clusters = _cluster_codes(clusters, n)
    method = _method(clusters)
    totals = stacked.sum(axis=0)
    if totals[1] == 0.0 or totals[3] == 0.0:
        return Interval(None, None, None, n, n_clusters, 0.0, 0.0, method, alpha=alpha)
    point = float(totals[0] / totals[1] - totals[2] / totals[3])
    sums = _cluster_sums(stacked, codes, n_clusters)
    if min(np.count_nonzero(sums[:, 1]), np.count_nonzero(sums[:, 3])) < 2:
        # Fewer than two clusters on a side: the bootstrap can't see that side's variation.
        return Interval(point, None, None, n, n_clusters, 0.0, 0.0, method, alpha=alpha)
    boot = _resampled_sums(sums, n_boot, seed)
    valid = (boot[:, 1] != 0.0) & (boot[:, 3] != 0.0)
    diffs = boot[valid, 0] / boot[valid, 1] - boot[valid, 2] / boot[valid, 3]
    low, high = _percentile_ci(diffs, alpha)
    return Interval(
        point=point,
        low=low,
        high=high,
        n_units=n,
        n_clusters=n_clusters,
        numerator_total=0.0,
        denominator_total=0.0,
        method=method,
        dropped_resamples=int(n_boot - np.count_nonzero(valid)),
        alpha=alpha,
    )
