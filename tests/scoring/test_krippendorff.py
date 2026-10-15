"""Krippendorff's alpha against the published worked example, and its bootstrap interval."""

from __future__ import annotations

import pytest

from identitybench.scoring.stats import AlphaMetric, krippendorff_alpha, krippendorff_alpha_ci

N = None
# Krippendorff (2011), "Computing Krippendorff's Alpha-Reliability": 4 observers, 12 units.
OBSERVERS = (
    (1, 2, 3, 3, 2, 1, 4, 1, 2, N, N, N),
    (1, 2, 3, 3, 2, 2, 4, 1, 2, 5, N, 3),
    (N, 3, 3, 3, 2, 3, 4, 2, 2, 5, 1, N),
    (1, 2, 3, 3, 2, 4, 4, 1, 2, 5, 1, N),
)
UNITS = [[v for v in column if v is not None] for column in zip(*OBSERVERS, strict=True)]
CATEGORIES = (1, 2, 3, 4, 5)


@pytest.mark.parametrize(
    ("metric", "expected"), [("nominal", 0.743), ("ordinal", 0.815), ("interval", 0.849)]
)
def test_published_example(metric: AlphaMetric, expected: float) -> None:
    value = krippendorff_alpha(UNITS, CATEGORIES, metric)
    assert value == pytest.approx(expected, abs=5e-4)
    interval = krippendorff_alpha_ci(UNITS, CATEGORIES, metric, n_boot=200)
    assert interval.point == pytest.approx(value)
    assert interval.n_units == 11  # the unit with one value pairs nothing
    assert interval.low is not None and interval.high is not None
    assert interval.low <= interval.point <= interval.high


def test_perfect_agreement_and_undefined_cases() -> None:
    assert krippendorff_alpha([[1, 1], [2, 2, 2], [4, 4]], (1, 2, 3, 4)) == 1.0
    assert krippendorff_alpha([[3, 3], [3, 3]], (1, 2, 3, 4)) is None  # no expected disagreement
    assert krippendorff_alpha([[1], [2]], (1, 2, 3, 4)) is None
    with pytest.raises(ValueError, match="not one of the categories"):
        krippendorff_alpha([[1, 7]], (1, 2, 3, 4))


def test_ordinal_weighs_distance() -> None:
    near = [[1, 2], [3, 4], [1, 1], [4, 4], [2, 2], [3, 3]]
    far = [[1, 4], [4, 1], [1, 1], [4, 4], [2, 2], [3, 3]]
    a_near = krippendorff_alpha(near, (1, 2, 3, 4))
    a_far = krippendorff_alpha(far, (1, 2, 3, 4))
    assert a_near is not None and a_far is not None and a_near > a_far


def test_clusters_resample_together_and_are_deterministic() -> None:
    units = [[1, 1, 2], [2, 2, 2], [3, 4, 3], [4, 4, 4], [1, 2, 1], [3, 3, 3]]
    clusters = ["a", "a", "b", "b", "c", "c"]
    one = krippendorff_alpha_ci(units, (1, 2, 3, 4), n_boot=300, seed=3, clusters=clusters)
    two = krippendorff_alpha_ci(units, (1, 2, 3, 4), n_boot=300, seed=3, clusters=clusters)
    assert one == two
    assert one.n_clusters == 3 and one.method == "bootstrap_cluster"
    single = krippendorff_alpha_ci(units, (1, 2, 3, 4), n_boot=50, clusters=["x"] * 6)
    assert single.point is not None and single.low is None
