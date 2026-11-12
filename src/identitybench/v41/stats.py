"""Family-clustered BCa confidence intervals for v4.1 reports."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from statistics import NormalDist


def family_cluster_bca_bootstrap_ci(
    values_by_family: Mapping[str, Sequence[float]],
    *,
    n_resamples: int = 10000,
    alpha: float = 0.05,
    seed: int = 20260401,
) -> tuple[float, float, float]:
    """Compute family-clustered BCa bootstrap (point, ci_low, ci_high) (§4.5, R2-24)."""
    family_keys = sorted(values_by_family.keys())
    m = len(family_keys)
    if m == 0:
        return 0.0, 0.0, 0.0

    cluster_means = [
        sum(values_by_family[k]) / float(len(values_by_family[k]))
        for k in family_keys
        if values_by_family[k]
    ]
    m = len(cluster_means)
    if m == 0:
        return 0.0, 0.0, 0.0

    theta_hat = sum(cluster_means) / float(m)
    if m == 1 or max(cluster_means) == min(cluster_means):
        return round(theta_hat, 6), round(theta_hat, 6), round(theta_hat, 6)

    rng = random.Random(seed)
    boot_means: list[float] = []
    for _ in range(n_resamples):
        sample = [cluster_means[rng.randrange(m)] for _ in range(m)]
        boot_means.append(sum(sample) / float(m))
    boot_means.sort()

    norm = NormalDist()
    prop_less = sum(1 for b in boot_means if b < theta_hat) / float(n_resamples)
    prop_less = min(max(prop_less, 1e-6), 1.0 - 1e-6)
    z0 = norm.inv_cdf(prop_less)

    # Jackknife acceleration factor a
    total_sum = sum(cluster_means)
    jack_means = [(total_sum - x) / float(m - 1) for x in cluster_means]
    jack_avg = sum(jack_means) / float(m)
    num = sum((jack_avg - j) ** 3 for j in jack_means)
    den = 6.0 * (sum((jack_avg - j) ** 2 for j in jack_means) ** 1.5)
    acc = num / den if den > 1e-12 else 0.0

    def _bca_quantile(p: float) -> float:
        zp = norm.inv_cdf(p)
        denom = 1.0 - acc * (z0 + zp)
        if abs(denom) < 1e-9:
            adj_p = p
        else:
            adj_z = z0 + (z0 + zp) / denom
            adj_p = norm.cdf(adj_z)
        idx = min(max(round(adj_p * (n_resamples - 1)), 0), n_resamples - 1)
        return boot_means[idx]

    lo = _bca_quantile(0.5 * alpha)
    hi = _bca_quantile(1.0 - 0.5 * alpha)
    return round(theta_hat, 6), round(lo, 6), round(hi, 6)
