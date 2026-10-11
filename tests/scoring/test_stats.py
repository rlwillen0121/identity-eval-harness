"""Tests for ``identitybench.scoring.stats``: textbook values, reference implementations and
simulation sanity checks (kept small so the whole file runs in a few seconds)."""

from __future__ import annotations

import time

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy.stats import binom, norm
from sklearn.metrics import cohen_kappa_score

from identitybench.scoring.stats import (
    DEFAULT_N_BOOT,
    Discordance,
    HolmResult,
    Interval,
    binomial_ci,
    clopper_pearson,
    clopper_pearson_upper,
    confusion_matrix,
    discordance,
    holm_bonferroni,
    kappa_ci,
    mean_ci,
    minimum_detectable_difference,
    paired_bootstrap_diff,
    quadratic_weighted_kappa,
    ratio_ci,
)

# --------------------------------------------------------------------------------------------
# Clopper–Pearson
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("k", "n", "low", "high"),
    [
        (5, 10, 0.1871, 0.8129),  # standard table value
        (3, 20, 0.0321, 0.3789),
        (0, 10, 0.0, 1 - 0.025 ** (1 / 10)),  # closed form at k = 0
        (10, 10, 0.025 ** (1 / 10), 1.0),  # closed form at k = n
    ],
)
def test_clopper_pearson_textbook_values(k: int, n: int, low: float, high: float) -> None:
    lo, hi = clopper_pearson(k, n)
    assert lo == pytest.approx(low, abs=1e-4)
    assert hi == pytest.approx(high, abs=1e-4)


def test_clopper_pearson_is_symmetric_in_k() -> None:
    for k in range(13):
        lo, hi = clopper_pearson(k, 12)
        lo_m, hi_m = clopper_pearson(12 - k, 12)
        assert lo == pytest.approx(1 - hi_m)
        assert hi == pytest.approx(1 - lo_m)


def test_clopper_pearson_exact_coverage_never_below_nominal() -> None:
    """CP is conservative: its true coverage is at least 95% for every p."""
    n = 40
    bounds = [clopper_pearson(k, n) for k in range(n + 1)]
    pmf_k = np.arange(n + 1)
    for p in np.linspace(0.005, 0.995, 199):
        covered = np.array([lo <= p <= hi for lo, hi in bounds])
        assert float(binom.pmf(pmf_k, n, p)[covered].sum()) >= 0.95 - 1e-12


def test_clopper_pearson_upper_matches_spec_limits() -> None:
    """SPEC §10.3: zero events need n ≥ 299 for a 95% upper bound below 1%."""
    assert clopper_pearson_upper(0, 200) == pytest.approx(0.014867, abs=1e-6)
    assert clopper_pearson_upper(0, 200) == pytest.approx(1 - 0.05 ** (1 / 200))
    assert clopper_pearson_upper(0, 299) < 0.01
    assert clopper_pearson_upper(0, 298) > 0.01


def test_one_sided_upper_equals_two_sided_at_double_alpha() -> None:
    for k, n in [(0, 50), (1, 4), (7, 120), (30, 31)]:
        assert clopper_pearson_upper(k, n, 0.05) == pytest.approx(clopper_pearson(k, n, 0.10)[1])
    assert clopper_pearson_upper(9, 9) == 1.0


@pytest.mark.parametrize(
    ("k", "n", "alpha"), [(0, 0, 0.05), (-1, 5, 0.05), (6, 5, 0.05), (1, 5, 1.0)]
)
def test_clopper_pearson_rejects_bad_arguments(k: int, n: int, alpha: float) -> None:
    with pytest.raises(ValueError):
        clopper_pearson(k, n, alpha)
    with pytest.raises(ValueError):
        clopper_pearson_upper(k, n, alpha)


def test_binomial_ci_wraps_bounds_as_intervals() -> None:
    two = binomial_ci(3, 20)
    assert two.method == "clopper_pearson"
    assert (two.low, two.high) == clopper_pearson(3, 20)
    assert two.point == pytest.approx(0.15)
    assert (two.n_units, two.numerator_total, two.denominator_total) == (20, 3.0, 20.0)

    one = binomial_ci(0, 200, one_sided=True)
    assert one.method == "clopper_pearson_upper"
    assert one.low == 0.0
    assert one.high == pytest.approx(clopper_pearson_upper(0, 200))
    assert one.point == 0.0

    empty = binomial_ci(0, 0)
    assert empty.is_na
    assert empty.format() == "n/a"
    assert (empty.numerator_total, empty.denominator_total) == (0.0, 0.0)


@pytest.mark.parametrize(("k", "n"), [(5, 0), (-1, 0), (0, -3), (4, 3)])
def test_binomial_ci_rejects_impossible_counts(k: int, n: int) -> None:
    with pytest.raises(ValueError):
        binomial_ci(k, n)
    with pytest.raises(ValueError):
        binomial_ci(k, n, one_sided=True)


# --------------------------------------------------------------------------------------------
# Interval rendering
# --------------------------------------------------------------------------------------------


def test_interval_format() -> None:
    iv = Interval(0.5, 0.4, 0.6, 10, 10, 5.0, 10.0, "bootstrap_iid")
    assert iv.format() == "0.500 [0.400, 0.600]"
    assert iv.format(1, percent=True) == "50.0% [40.0%, 60.0%]"
    assert Interval(0.5, None, None, 10, 10, 5.0, 10.0, "bootstrap_iid").format(2) == "0.50"
    na = Interval(None, None, None, 0, 0, 0.0, 0.0, "bootstrap_iid")
    assert na.is_na
    assert na.format(percent=True) == "n/a"


def test_one_sided_bound_never_renders_like_a_two_sided_interval() -> None:
    """SPEC §12.8: a bound states what it is; ``[0, u]`` would read as a two-sided interval."""
    upper = binomial_ci(0, 200, one_sided=True)
    assert upper.alpha == 0.05
    assert upper.format(4) == "0.0000 (95% upper ≤ 0.0149)"
    assert upper.format(1, percent=True) == "0.0% (95% upper ≤ 1.5%)"
    assert binomial_ci(1, 10, one_sided=True, alpha=0.1).format(2) == "0.10 (90% upper ≤ 0.34)"
    assert binomial_ci(1, 10).format(2) == "0.10 [0.00, 0.45]"


def test_intervals_record_their_alpha() -> None:
    num, den = [1, 0, 1, 1, 0, 0], [1] * 6
    assert ratio_ci(num, den, n_boot=200).alpha == 0.05
    assert ratio_ci(num, den, n_boot=200, alpha=0.1).alpha == 0.1
    assert binomial_ci(2, 9, alpha=0.01).alpha == 0.01
    assert kappa_ci([1, 2, 1, 2], [1, 2, 2, 2], [1, 2], 200, alpha=0.2).alpha == 0.2


# --------------------------------------------------------------------------------------------
# Ratio and mean intervals
# --------------------------------------------------------------------------------------------


def _bernoulli(rng: np.random.Generator, p: float | np.ndarray, size: int) -> np.ndarray:
    return (rng.random(size) < p).astype(np.float64)


def _reference_ratio_bootstrap(
    num: np.ndarray, den: np.ndarray, clusters: np.ndarray, n_boot: int, seed: int, alpha: float
) -> tuple[float, float]:
    """Textbook percentile cluster bootstrap, written out naively.

    Draws an ``(n_boot, n_clusters)`` index matrix from ``default_rng(seed)``, sums whole
    clusters per resample and takes the ``alpha/2`` and ``1 − alpha/2`` quantiles of the
    resampled ratios. ``clusters`` must be labelled 0..k−1 in first-appearance order, which is
    the coding ``ratio_ci`` uses.
    """
    n_clusters = int(clusters.max()) + 1
    cluster_num = np.array([num[clusters == c].sum() for c in range(n_clusters)])
    cluster_den = np.array([den[clusters == c].sum() for c in range(n_clusters)])
    idx = np.random.default_rng(seed).integers(0, n_clusters, size=(n_boot, n_clusters))
    ratios = cluster_num[idx].sum(axis=1) / cluster_den[idx].sum(axis=1)
    low, high = np.quantile(ratios, [alpha / 2, 1 - alpha / 2])
    return float(low), float(high)


@pytest.mark.parametrize("alpha", [0.05, 0.10])
def test_ratio_ci_matches_reference_percentile_bootstrap(alpha: float) -> None:
    """Pins the interval level and tails: 95% means the 2.5% and 97.5% resample quantiles."""
    rng = np.random.default_rng(21)
    sizes = rng.integers(1, 9, 40)  # unequal cluster sizes
    clusters = np.repeat(np.arange(40), sizes)
    num = _bernoulli(rng, 0.25, clusters.size)
    den = np.ones(clusters.size)
    iv = ratio_ci(num, den, clusters, n_boot=DEFAULT_N_BOOT, alpha=alpha, seed=13)
    low, high = _reference_ratio_bootstrap(num, den, clusters, DEFAULT_N_BOOT, 13, alpha)
    assert iv.low == pytest.approx(low, abs=1e-12)
    assert iv.high == pytest.approx(high, abs=1e-12)
    assert iv.dropped_resamples == 0


def test_ratio_ci_precision_matches_spec_example() -> None:
    """SPEC §10.3: ~200 non-APPROVE cases and WAR near 10% give a 95% interval of about ±4.5
    points (a 90% interval would be about ±3.5)."""
    num = np.zeros(200)
    num[:20] = 1.0
    iv = ratio_ci(num, np.ones(200))
    assert iv.point is not None and iv.low is not None and iv.high is not None
    assert iv.point - iv.low == pytest.approx(0.040, abs=0.003)
    assert iv.high - iv.point == pytest.approx(0.045, abs=0.003)


def test_ratio_ci_point_is_ratio_of_sums() -> None:
    # Clusters of unequal size: the pooled ratio, not the mean of per-cluster ratios.
    num = [1, 0, 0, 0, 1, 1]
    den = [1, 1, 1, 1, 1, 1]
    clusters = ["a", "a", "a", "a", "b", "b"]
    iv = ratio_ci(num, den, clusters, n_boot=500)
    assert iv.point == pytest.approx(0.5)
    assert iv.n_units == 6
    assert iv.n_clusters == 2
    assert (iv.numerator_total, iv.denominator_total) == (3.0, 6.0)
    assert iv.method == "bootstrap_cluster"
    assert iv.point is not None and iv.low is not None and iv.high is not None
    assert iv.low <= iv.point <= iv.high


def test_ratio_ci_is_deterministic_per_seed() -> None:
    rng = np.random.default_rng(3)
    num = _bernoulli(rng, 0.2, 300)
    den = np.ones(300)
    assert ratio_ci(num, den, seed=11) == ratio_ci(num, den, seed=11)
    assert ratio_ci(num, den, seed=11) != ratio_ci(num, den, seed=12)


def test_singleton_clusters_reproduce_iid_bootstrap() -> None:
    rng = np.random.default_rng(4)
    num = _bernoulli(rng, 0.3, 120)
    den = np.ones(120)
    iid = ratio_ci(num, den, seed=5)
    singleton = ratio_ci(num, den, list(range(120)), seed=5)
    assert (iid.low, iid.high) == (singleton.low, singleton.high)
    assert iid.method == "bootstrap_iid"
    assert singleton.method == "bootstrap_cluster"


def test_ratio_ci_zero_denominator_is_na() -> None:
    iv = ratio_ci([0, 0, 0], [0, 0, 0], ["x", "y", "z"])
    assert iv.point is None and iv.low is None and iv.high is None
    assert iv.is_na
    assert iv.format() == "n/a"
    assert iv.dropped_resamples == 0
    assert iv.n_clusters == 3

    empty = ratio_ci([], [])
    assert empty.is_na
    assert empty.n_units == 0


def test_single_cluster_reports_point_without_interval() -> None:
    """One cluster gives every resample the same data: a zero-width interval would be false
    certainty, so only the point is reported."""
    iv = ratio_ci([1, 0, 1, 0, 0], [1] * 5, ["camp"] * 5)
    assert iv.point == pytest.approx(0.4)
    assert (iv.low, iv.high, iv.n_clusters, iv.dropped_resamples) == (None, None, 1, 0)
    assert iv.format() == "0.400"
    single_unit = mean_ci([3.0])
    assert (single_unit.point, single_unit.low, single_unit.high) == (3.0, None, None)
    k = kappa_ci([1, 2, 3, 1], [1, 3, 3, 2], [1, 2, 3], clusters=["case"] * 4)
    assert k.point == pytest.approx(9 / 13)
    assert (k.low, k.high) == (None, None)
    paired = paired_bootstrap_diff([1, 1], [1, 1], [0, 1], [1, 1], ["camp", "camp"])
    assert paired.diff == pytest.approx(0.5)
    assert (paired.low, paired.high, paired.p_value) == (None, None, None)


def test_point_survives_when_every_resample_is_dropped() -> None:
    """Only cluster 0 has a denominator; a lone resample that misses it leaves no interval."""
    clusters = np.repeat(np.arange(10), 2)
    den = np.where(clusters == 0, 1.0, 0.0)
    num = den.copy()
    seed = next(
        s for s in range(100) if 0 not in np.random.default_rng(s).integers(0, 10, size=(1, 10))
    )
    iv = ratio_ci(num, den, clusters, n_boot=1, seed=seed)
    assert (iv.point, iv.low, iv.high, iv.dropped_resamples) == (1.0, None, None, 1)
    assert not iv.is_na
    assert iv.format() == "1.000"


def test_ratio_ci_drops_and_counts_zero_denominator_resamples() -> None:
    """Only cluster 0 has a nonzero denominator; resamples missing it are dropped."""
    clusters = np.repeat(np.arange(10), 5)
    den = np.where(clusters == 0, 1.0, 0.0)
    num = np.where((clusters == 0) & (np.arange(50) % 2 == 0), 1.0, 0.0)
    iv = ratio_ci(num, den, clusters, n_boot=2000, seed=1)
    # P(cluster 0 never drawn in 10 draws) = 0.9^10 ≈ 0.349
    assert 550 < iv.dropped_resamples < 850
    # Every usable resample repeats cluster 0, so its ratio is exactly the point estimate.
    assert iv.point == pytest.approx(3 / 5)
    assert iv.low == pytest.approx(iv.point)
    assert iv.high == pytest.approx(iv.point)


def test_cluster_bootstrap_wider_than_iid_under_within_cluster_correlation() -> None:
    rng = np.random.default_rng(10)
    n_clusters, size = 30, 20
    cluster_p = np.where(np.arange(n_clusters) % 2 == 0, 0.1, 0.6)
    num = _bernoulli(rng, np.repeat(cluster_p, size), n_clusters * size)
    den = np.ones_like(num)
    clusters = np.repeat(np.arange(n_clusters), size)
    iid = ratio_ci(num, den)
    clustered = ratio_ci(num, den, clusters)
    assert iid.point == clustered.point
    assert iid.low is not None and iid.high is not None
    assert clustered.low is not None and clustered.high is not None
    assert (clustered.high - clustered.low) > 1.5 * (iid.high - iid.low)


def test_bootstrap_coverage_on_simulated_binomial_data() -> None:
    """95% percentile intervals cover a WAR-like rate (p = 0.1, n = 200) about 95% of the time."""
    rng = np.random.default_rng(2024)
    p, n, reps = 0.1, 200, 300
    covered = 0
    for rep in range(reps):
        iv = ratio_ci(_bernoulli(rng, p, n), np.ones(n), n_boot=400, seed=rep)
        assert iv.low is not None and iv.high is not None
        covered += iv.low <= p <= iv.high
    assert 0.92 <= covered / reps <= 0.985


def test_cluster_bootstrap_keeps_coverage_where_iid_undercovers() -> None:
    rng = np.random.default_rng(7)
    n_clusters, size, reps, truth = 40, 10, 200, 0.2  # Beta(1, 4) cluster rates, mean 0.2
    clusters = np.repeat(np.arange(n_clusters), size)
    cover_cluster = cover_iid = 0
    for rep in range(reps):
        num = _bernoulli(rng, np.repeat(rng.beta(1, 4, n_clusters), size), n_clusters * size)
        den = np.ones_like(num)
        c = ratio_ci(num, den, clusters, n_boot=400, seed=rep)
        i = ratio_ci(num, den, n_boot=400, seed=rep)
        assert c.low is not None and c.high is not None
        assert i.low is not None and i.high is not None
        cover_cluster += c.low <= truth <= c.high
        cover_iid += i.low <= truth <= i.high
    assert cover_cluster / reps >= 0.88
    assert cover_iid / reps < cover_cluster / reps - 0.05


@pytest.mark.parametrize("clustered", [False, True])
def test_bootstrap_2000_by_2000_runs_under_a_second(clustered: bool) -> None:
    rng = np.random.default_rng(0)
    num = _bernoulli(rng, 0.1, 2000)
    den = np.ones(2000)
    clusters = list(range(2000)) if clustered else None
    ratio_ci(num, den, clusters, n_boot=50)  # warm up imports and caches
    start = time.perf_counter()
    iv = ratio_ci(num, den, clusters, n_boot=DEFAULT_N_BOOT)
    assert time.perf_counter() - start < 1.0
    assert iv.n_clusters == 2000


def test_ratio_ci_rejects_malformed_input() -> None:
    with pytest.raises(ValueError, match="denominators"):
        ratio_ci([1, 0], [1])
    with pytest.raises(ValueError, match="one-dimensional"):
        ratio_ci(np.array([[1, 0]]), np.array([[1, 1]]))
    with pytest.raises(ValueError, match="non-finite"):
        ratio_ci([1, float("nan")], [1, 1])
    with pytest.raises(ValueError, match="clusters"):
        ratio_ci([1, 0], [1, 1], ["a"])
    with pytest.raises(ValueError, match="n_boot"):
        ratio_ci([1, 0], [1, 1], n_boot=0)
    with pytest.raises(ValueError, match="alpha"):
        ratio_ci([1, 0], [1, 1], alpha=0.0)
    with pytest.raises(ValueError, match="non-negative"):
        ratio_ci([1, 1], [1, -0.5])


def test_mean_ci_is_ratio_over_unit_counts() -> None:
    costs = [0.0, 1.0, 1.0, 10.0, 2.0, 0.5]
    clusters = ["c1", "c1", "c2", "c2", "c2", "c3"]
    iv = mean_ci(costs, clusters, n_boot=500, seed=9)
    assert iv.point == pytest.approx(np.mean(costs))
    assert iv.denominator_total == 6.0
    assert iv == ratio_ci(costs, np.ones(6), clusters, n_boot=500, seed=9)
    assert mean_ci([]).is_na


# --------------------------------------------------------------------------------------------
# Paired bootstrap
# --------------------------------------------------------------------------------------------


def test_paired_identical_configs_have_zero_diff_and_p_one() -> None:
    rng = np.random.default_rng(1)
    num = _bernoulli(rng, 0.3, 100)
    den = np.ones(100)
    res = paired_bootstrap_diff(num, den, num, den, None, n_boot=500)
    assert res.diff == 0.0
    assert (res.low, res.high) == (0.0, 0.0)
    assert res.p_value == 1.0


def test_paired_large_difference_hits_p_floor() -> None:
    den = np.ones(60)
    res = paired_bootstrap_diff(np.ones(60), den, np.zeros(60), den, None, n_boot=999)
    assert res.diff == 1.0
    assert res.p_value == pytest.approx(1 / 1000)
    assert res.low == res.high == 1.0


def test_paired_p_value_matches_reference_in_mid_range() -> None:
    """Pins the two-sided p = 2 · min tail. Here a one-sided p (≈ 0.04) would wrongly reject at
    0.05; the two-sided one does not."""
    n, n_boot, seed = 300, 2000, 4
    num_a = np.zeros(n)
    num_a[:36] = 1.0
    num_b = num_a.copy()
    num_b[:2] = 0.0
    num_b[36:44] = 1.0  # b is worse on 8 cases and better on 2
    den = np.ones(n)
    res = paired_bootstrap_diff(num_a, den, num_b, den, None, n_boot=n_boot, seed=seed)

    idx = np.random.default_rng(seed).integers(0, n, size=(n_boot, n))
    diffs = num_a[idx].mean(axis=1) - num_b[idx].mean(axis=1)
    tail = min(np.mean(diffs <= 0), np.mean(diffs >= 0))
    expected = max(min(1.0, 2 * tail), 1 / (n_boot + 1))
    assert res.diff == pytest.approx(-6 / 300)
    assert res.p_value == pytest.approx(expected, abs=1e-12)
    assert res.p_value == pytest.approx(0.079)
    assert res.low == pytest.approx(float(np.quantile(diffs, 0.025)), abs=1e-12)
    assert res.high == pytest.approx(float(np.quantile(diffs, 0.975)), abs=1e-12)


def test_paired_uses_the_same_resamples_as_ratio_ci() -> None:
    """With config b fixed at 0, every paired difference equals config a's resampled ratio."""
    rng = np.random.default_rng(5)
    clusters = np.repeat(np.arange(25), 4)
    num_a = _bernoulli(rng, 0.4, 100)
    den = np.ones(100)
    single = ratio_ci(num_a, den, clusters, seed=17)
    paired = paired_bootstrap_diff(num_a, den, np.zeros(100), den, clusters, seed=17)
    assert paired.diff == pytest.approx(single.point)
    assert paired.low == pytest.approx(single.low)
    assert paired.high == pytest.approx(single.high)
    assert paired.n_clusters == 25


def test_pairing_cancels_shared_case_noise() -> None:
    rng = np.random.default_rng(6)
    num_a = _bernoulli(rng, 0.3, 300)
    num_b = num_a.copy()
    num_b[:6] = 1.0 - num_b[:6]  # b disagrees with a on 2% of cases
    den = np.ones(300)
    paired = paired_bootstrap_diff(num_a, den, num_b, den, None)
    single = ratio_ci(num_a, den)
    assert paired.low is not None and paired.high is not None
    assert single.low is not None and single.high is not None
    assert paired.high - paired.low < 0.5 * (single.high - single.low)


def test_paired_p_value_null_and_alternative() -> None:
    rng = np.random.default_rng(8)
    den = np.ones(200)
    null_rejections = 0
    for rep in range(200):
        a, b = _bernoulli(rng, 0.3, 200), _bernoulli(rng, 0.3, 200)
        res = paired_bootstrap_diff(a, den, b, den, None, n_boot=400, seed=rep)
        assert res.p_value is not None
        null_rejections += res.p_value < 0.05
    assert null_rejections / 200 <= 0.10

    a = _bernoulli(rng, 0.45, 200)
    b = a * _bernoulli(rng, 0.5, 200)  # b approves a subset of a's approvals
    res = paired_bootstrap_diff(a, den, b, den, None, n_boot=2000)
    assert res.p_value is not None and res.p_value < 0.01
    assert res.low is not None and res.low > 0.0


def test_paired_handles_empty_denominators() -> None:
    res = paired_bootstrap_diff([1, 0], [1, 1], [0, 0], [0, 0], None)
    assert res.diff is None and res.p_value is None and res.low is None
    assert res.point_a == 0.5 and res.point_b is None

    clusters = np.repeat(np.arange(10), 3)
    den_a = np.where(clusters == 0, 1.0, 0.0)
    res = paired_bootstrap_diff(den_a, den_a, np.zeros(30), np.ones(30), clusters, seed=2)
    assert 550 < res.dropped_resamples < 850
    assert res.p_value == pytest.approx(1 / (DEFAULT_N_BOOT - res.dropped_resamples + 1))


def test_paired_rejects_misaligned_units() -> None:
    with pytest.raises(ValueError, match="num_b"):
        paired_bootstrap_diff([1, 0], [1, 1], [1], [1, 1], None)
    with pytest.raises(ValueError, match="clusters"):
        paired_bootstrap_diff([1, 0], [1, 1], [1, 0], [1, 1], ["a", "b", "c"])
    with pytest.raises(ValueError, match="den_b"):
        paired_bootstrap_diff([1, 0], [1, 1], [1, 0], [1, -1], None)


# --------------------------------------------------------------------------------------------
# Comparison power (SPEC §10.3)
# --------------------------------------------------------------------------------------------


def test_discordance_counts_each_direction() -> None:
    a = [True, True, False, False, True, False]
    b = [True, False, True, False, False, False]
    d = discordance(a, b)
    assert d == Discordance(n_units=6, a_only=2, b_only=1)
    assert d.n_discordant == 3
    assert d.rate == pytest.approx(0.5)
    assert discordance([], []).rate is None
    assert discordance(np.array([1, 0]), np.array([1, 0])).n_discordant == 0
    with pytest.raises(ValueError, match="flags_b"):
        discordance([True], [True, False])
    with pytest.raises(ValueError, match="booleans"):
        discordance([0, 2], [0, 1])


def _connor_sample_size(delta: float, psi: float, alpha: float, power: float) -> float:
    z_a, z_b = norm.ppf(1 - alpha / 2), norm.ppf(power)
    return float((z_a * np.sqrt(psi) + z_b * np.sqrt(psi - delta**2)) ** 2 / delta**2)


@pytest.mark.parametrize(
    ("psi", "n", "alpha", "power"),
    [(0.1, 200, 0.05, 0.8), (0.05, 300, 0.05, 0.8), (0.2, 1000, 0.01, 0.9), (0.1, 300, 0.05, 0.5)],
)
def test_mdd_inverts_connor_sample_size(psi: float, n: int, alpha: float, power: float) -> None:
    delta = minimum_detectable_difference(psi, n, alpha=alpha, power=power)
    assert delta is not None and 0.0 < delta <= psi
    assert _connor_sample_size(delta, psi, alpha, power) == pytest.approx(n, rel=1e-9)


def test_mdd_reaches_nominal_power_for_mcnemar() -> None:
    """Simulated paired binary outcomes at the MDD: McNemar's test rejects ~80% of the time."""
    n, psi = 300, 0.1
    delta = minimum_detectable_difference(psi, n)
    assert delta is not None
    assert delta == pytest.approx(0.0509, abs=1e-4)
    probs = [(psi + delta) / 2, (psi - delta) / 2, 1 - psi]  # a-only, b-only, agree
    counts = np.random.default_rng(0).multinomial(n, probs, size=20_000)
    a_only, b_only = counts[:, 0], counts[:, 1]
    disc = np.maximum(a_only + b_only, 1)
    rejected = np.abs(a_only - b_only) / np.sqrt(disc) > norm.ppf(0.975)
    assert 0.78 <= float(rejected.mean()) <= 0.87


def _mdd(psi: float, n: int) -> float:
    delta = minimum_detectable_difference(psi, n)
    assert delta is not None
    return delta


def test_mdd_shrinks_with_size_and_grows_with_discordance() -> None:
    by_n = [_mdd(0.1, n) for n in (200, 400, 800)]
    by_psi = [_mdd(psi, 300) for psi in (0.05, 0.1, 0.2)]
    assert by_n == sorted(by_n, reverse=True) and len(set(by_n)) == 3
    assert by_psi == sorted(by_psi) and len(set(by_psi)) == 3


def test_mdd_undetectable_and_bad_input() -> None:
    # 4 disagreements in 200 cases: even a 4-0 split can't reach 80% power.
    assert minimum_detectable_difference(0.02, 200) is None
    assert minimum_detectable_difference(0.0, 200) is None
    assert minimum_detectable_difference(0.5, 2) is None  # n below z_a² − z_b²
    with pytest.raises(ValueError, match="alpha"):
        minimum_detectable_difference(0.1, 200, alpha=0.0)
    for power in (0.3, 1.0):
        with pytest.raises(ValueError, match="power"):
            minimum_detectable_difference(0.1, 200, power=power)
    with pytest.raises(ValueError, match="discordance_rate"):
        minimum_detectable_difference(1.5, 200)
    with pytest.raises(ValueError, match="n_units"):
        minimum_detectable_difference(0.1, 0)


# --------------------------------------------------------------------------------------------
# Holm–Bonferroni
# --------------------------------------------------------------------------------------------


def test_holm_textbook_example() -> None:
    """Four hypotheses at alpha = 0.05 (the standard worked example)."""
    res = holm_bonferroni({"H1": 0.01, "H2": 0.04, "H3": 0.03, "H4": 0.005})
    assert list(res) == ["H1", "H2", "H3", "H4"]
    adjusted = {key: r.adjusted_p for key, r in res.items()}
    assert adjusted == pytest.approx({"H1": 0.03, "H2": 0.06, "H3": 0.06, "H4": 0.02})
    assert {key for key, r in res.items() if r.reject} == {"H1", "H4"}


def test_holm_edge_cases() -> None:
    assert holm_bonferroni({}) == {}
    assert holm_bonferroni({"only": 0.04}) == {"only": HolmResult(0.04, True)}
    capped = holm_bonferroni({1: 0.5, 2: 0.9})
    assert capped[1].adjusted_p == 1.0 and capped[2].adjusted_p == 1.0
    tuple_keys = holm_bonferroni({("cfg_a", "cfg_b", "WAR"): 0.001, ("cfg_a", "cfg_b", "NRC"): 0.2})
    assert tuple_keys[("cfg_a", "cfg_b", "WAR")].reject
    with pytest.raises(ValueError):
        holm_bonferroni({"bad": 1.5})
    with pytest.raises(ValueError):
        holm_bonferroni({"bad": float("nan")})


def _holm_step_down(pvalues: list[float], alpha: float) -> list[bool]:
    """Holm's procedure as originally stated: stop at the first non-rejection.

    Compares ``(m − rank) · p`` with alpha (rather than ``p`` with ``alpha / (m − rank)``) so the
    oracle and the implementation round identically at the boundary.
    """
    m = len(pvalues)
    reject = [False] * m
    for rank, i in enumerate(sorted(range(m), key=lambda j: pvalues[j])):
        if (m - rank) * pvalues[i] > alpha:
            break
        reject[i] = True
    return reject


@settings(max_examples=200, deadline=None, derandomize=True)
@given(st.lists(st.floats(min_value=0.0, max_value=1.0), min_size=1, max_size=12))
def test_holm_matches_sequential_procedure(pvalues: list[float]) -> None:
    res = holm_bonferroni(dict(enumerate(pvalues)))
    assert [res[i].reject for i in range(len(pvalues))] == _holm_step_down(pvalues, 0.05)
    for i, p in enumerate(pvalues):
        assert p <= res[i].adjusted_p <= 1.0


# --------------------------------------------------------------------------------------------
# Agreement
# --------------------------------------------------------------------------------------------


def test_confusion_matrix_counts() -> None:
    cm = confusion_matrix([1, 2, 3, 1], [1, 3, 3, 2], [1, 2, 3, 4])
    assert cm.dtype == np.int64
    assert cm.tolist() == [[1, 1, 0, 0], [0, 0, 1, 0], [0, 0, 1, 0], [0, 0, 0, 0]]


def test_quadratic_kappa_hand_computation() -> None:
    """Rows a, cols b: O = [[1,1,0],[0,0,1],[0,0,1]]; weighted observed 2, expected 26/4."""
    a, b = [1, 2, 3, 1], [1, 3, 3, 2]
    assert quadratic_weighted_kappa(a, b, [1, 2, 3]) == pytest.approx(9 / 13)
    assert quadratic_weighted_kappa(a, b, [1, 2, 3]) == pytest.approx(
        cohen_kappa_score(a, b, labels=[1, 2, 3], weights="quadratic")
    )


def test_quadratic_kappa_extremes() -> None:
    assert quadratic_weighted_kappa([1, 2, 3, 4], [1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)
    assert quadratic_weighted_kappa(["lo", "hi"], ["hi", "lo"], ["lo", "hi"]) == pytest.approx(-1.0)
    # Both raters use one category: chance-expected disagreement is 0 and κ is undefined.
    assert quadratic_weighted_kappa([2, 2, 2], [2, 2, 2], [1, 2, 3, 4]) is None
    assert quadratic_weighted_kappa([], [], [1, 2]) is None


def test_quadratic_kappa_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="not one of the categories"):
        quadratic_weighted_kappa([1, 5], [1, 2], [1, 2, 3, 4])
    with pytest.raises(ValueError, match="distinct"):
        quadratic_weighted_kappa([1], [1], [1, 1, 2])
    with pytest.raises(ValueError, match="two categories"):
        quadratic_weighted_kappa([1], [1], [1])
    with pytest.raises(ValueError, match="lengths"):
        quadratic_weighted_kappa([1, 2], [1], [1, 2])


@settings(max_examples=150, deadline=None, derandomize=True)
@given(
    st.lists(st.tuples(st.integers(1, 4), st.integers(1, 4)), min_size=1, max_size=80),
)
def test_quadratic_kappa_matches_sklearn(pairs: list[tuple[int, int]]) -> None:
    a = [x for x, _ in pairs]
    b = [y for _, y in pairs]
    ours = quadratic_weighted_kappa(a, b, [1, 2, 3, 4])
    if ours is None:
        assert len(set(a) | set(b)) == 1
        return
    ref = cohen_kappa_score(a, b, labels=[1, 2, 3, 4], weights="quadratic")
    assert ours == pytest.approx(ref, abs=1e-12)


def test_kappa_ci_brackets_point_and_is_deterministic() -> None:
    rng = np.random.default_rng(12)
    a = rng.integers(1, 5, 150).tolist()
    b = np.clip(np.array(a) + rng.integers(-1, 2, 150), 1, 4).tolist()
    iv = kappa_ci(a, b, [1, 2, 3, 4], n_boot=1000, seed=3)
    assert iv.point == pytest.approx(quadratic_weighted_kappa(a, b, [1, 2, 3, 4]))
    assert iv.point == pytest.approx(1 - iv.numerator_total / iv.denominator_total)
    assert iv.point is not None and iv.low is not None and iv.high is not None
    assert iv.low < iv.point < iv.high
    assert iv.method == "bootstrap_iid"
    assert iv == kappa_ci(a, b, [1, 2, 3, 4], 1000, 3)

    groups = [i // 3 for i in range(150)]
    clustered = kappa_ci(a, b, [1, 2, 3, 4], n_boot=1000, seed=3, clusters=groups)
    assert clustered.method == "bootstrap_cluster"
    assert clustered.n_clusters == 50
    assert clustered.point == iv.point


def test_kappa_ci_undefined_and_dropped_resamples() -> None:
    assert kappa_ci([3, 3, 3], [3, 3, 3], [1, 2, 3, 4]).is_na
    # Only one disagreeing item among many identical ones: resamples that miss it are
    # degenerate and dropped.
    a = [2] * 19 + [1]
    b = [2] * 19 + [2]
    iv = kappa_ci(a, b, [1, 2, 3], n_boot=1000, seed=0)
    assert iv.point is not None
    assert iv.dropped_resamples > 0
