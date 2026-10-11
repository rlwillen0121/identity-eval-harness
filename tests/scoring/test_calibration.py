"""Tests for ``identitybench.scoring.calibration``: ECE/Brier worked examples and the selective
auto-decision curve on a hand-checkable case set."""

from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from identitybench.schemas.enums import Label
from identitybench.scoring.calibration import (
    Certification,
    SelectiveCase,
    SelectivePoint,
    brier,
    certify_threshold,
    choose_threshold,
    ece,
    operating_point,
    selective_curve,
)
from identitybench.scoring.stats import clopper_pearson_upper

A, N, E, INVALID = Label.A, Label.N, Label.E, Label.INVALID

# --------------------------------------------------------------------------------------------
# ECE and Brier
# --------------------------------------------------------------------------------------------


def test_ece_worked_example_both_strategies() -> None:
    """Bins {0.6, 0.6} (accuracy 1.0) and {0.9, 0.9} (accuracy 0.5): ECE = ½·0.4 + ½·0.4."""
    conf = [0.9, 0.9, 0.6, 0.6]
    correct = [True, False, True, True]
    assert ece(conf, correct, n_bins=2) == pytest.approx(0.4)
    assert ece(conf, correct, n_bins=10, strategy="equal_width") == pytest.approx(0.4)


def test_ece_is_zero_when_calibrated() -> None:
    conf = [0.8] * 5 + [0.5] * 4
    correct = [True, True, True, True, False, True, False, True, False]
    assert ece(conf, correct) == pytest.approx(0.0)
    assert ece(conf, correct, strategy="equal_width") == pytest.approx(0.0)


def test_equal_mass_bins_have_equal_counts_without_ties() -> None:
    """20 distinct confidences, 10 bins: pairs (0,1), (2,3), … sorted by confidence."""
    rng = np.random.default_rng(0)
    conf = rng.permutation(np.linspace(0.05, 0.99, 20))
    correct = rng.random(20) < conf
    order = np.argsort(conf)
    expected = sum(
        abs(correct[order[i : i + 2]].mean() - conf[order[i : i + 2]].mean()) * 2 / 20
        for i in range(0, 20, 2)
    )
    assert ece(conf, correct) == pytest.approx(expected)


def test_equal_mass_ece_keeps_ties_together_and_ignores_order() -> None:
    conf = [0.5, 0.5, 0.5, 0.9]
    correct = [True, False, False, True]
    # Ties share a bin: {0.5 ×3} acc 1/3 and {0.9} acc 1 → (3·|1/3 − 0.5| + |1 − 0.9|) / 4
    expected = (3 * abs(1 / 3 - 0.5) + 0.1) / 4
    assert ece(conf, correct, n_bins=2) == pytest.approx(expected)
    assert ece(conf[::-1], correct[::-1], n_bins=2) == pytest.approx(expected)


def _per_value_ece(conf: list[float], correct: list[bool]) -> float:
    """ECE with every distinct confidence value in a bin of its own."""
    c, y = np.array(conf), np.array(correct, dtype=float)
    return float(sum(abs(y[c == v].sum() - c[c == v].sum()) for v in np.unique(c)) / c.size)


def test_large_late_tie_group_does_not_swallow_a_small_one() -> None:
    """Regression: 9 cases at 0.3 (all correct) and 91 at 0.95 (80 correct).

    Binning each tie group by its *first* position put both groups in bin 0, where their errors
    cancelled (ECE 0.0015). Each group holds more than n/B cases, so each gets its own bin:
    (|9 − 2.7| + |80 − 86.45|) / 100 = 0.1275.
    """
    conf = [0.3] * 9 + [0.95] * 91
    correct = [True] * 9 + [True] * 80 + [False] * 11
    assert ece(conf, correct) == pytest.approx(0.1275)
    assert ece(conf[::-1], correct[::-1]) == pytest.approx(0.1275)
    # The mirror image (small group on top) was already separated and must not change.
    conf_hi = [0.95] * 91 + [0.99] * 9
    correct_hi = [True] * 80 + [False] * 11 + [True] * 9
    assert ece(conf_hi, correct_hi) == pytest.approx(0.0654)


def test_realistic_tied_confidence_mix_keeps_levels_apart() -> None:
    """5% at 0.8, 40% at 0.9, 55% at 0.95 (typical LLM self-reports): three bins, not two."""
    conf = [0.8] * 5 + [0.9] * 40 + [0.95] * 55
    correct = [True] * 5 + [True] * 32 + [False] * 8 + [True] * 50 + [False] * 5
    # (|5 − 4| + |32 − 36| + |50 − 52.25|) / 100
    assert ece(conf, correct) == pytest.approx(0.0725)
    assert ece(conf, correct) == pytest.approx(_per_value_ece(conf, correct))


@settings(max_examples=150, deadline=None, derandomize=True)
@given(
    st.lists(st.integers(min_value=0, max_value=99), min_size=1, max_size=5, unique=True),
    st.integers(min_value=1, max_value=20),
    st.data(),
)
def test_values_holding_a_bin_worth_of_cases_are_never_merged(
    levels: list[int], base: int, data: st.DataObject
) -> None:
    """With at most 5 distinct values each held by c..2c cases, every value holds at least
    n/10 cases, so 10 equal-mass bins must keep every value in its own bin."""
    conf: list[float] = []
    correct: list[bool] = []
    for level in levels:
        count = data.draw(st.integers(min_value=base, max_value=2 * base))
        conf += [level / 100] * count
        correct += data.draw(st.lists(st.booleans(), min_size=count, max_size=count))
    assert ece(conf, correct) == pytest.approx(_per_value_ece(conf, correct), abs=1e-12)


def test_equal_width_edges_go_to_upper_bin() -> None:
    # 0.3 belongs to [0.3, 0.4) and 1.0 to the last bin; each is alone in its bin.
    assert ece([0.3, 1.0], [False, True], strategy="equal_width") == pytest.approx(0.15)
    assert ece([0.0], [False], strategy="equal_width") == 0.0


def test_one_bin_ece_is_absolute_overconfidence() -> None:
    conf = [0.9, 0.8, 0.7, 0.6]
    correct = [True, False, False, True]
    assert ece(conf, correct, n_bins=1) == pytest.approx(abs(0.5 - 0.75))


def test_brier_worked_example() -> None:
    assert brier([1.0, 0.0, 0.5], [True, True, False]) == pytest.approx((0 + 1 + 0.25) / 3)
    assert brier([0.7], [1]) == pytest.approx(0.09)


def test_calibration_empty_is_na() -> None:
    assert ece([], []) is None
    assert brier([], []) is None


@pytest.mark.parametrize(
    ("conf", "correct", "message"),
    [
        ([0.5, 1.2], [True, False], r"\[0, 1\]"),
        ([0.5, float("nan")], [True, False], r"\[0, 1\]"),
        ([0.5], [True, False], "correctness flags"),
        ([0.5, 0.6], [1, 2], "booleans"),
    ],
)
def test_calibration_rejects_bad_input(conf: list[float], correct: list[int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ece(conf, correct)
    with pytest.raises(ValueError, match=message):
        brier(conf, correct)


def test_ece_rejects_bad_binning() -> None:
    with pytest.raises(ValueError, match="n_bins"):
        ece([0.5], [True], n_bins=0)
    with pytest.raises(ValueError, match="strategy"):
        ece([0.5], [True], strategy="quantile")  # type: ignore[arg-type]


@settings(max_examples=100, deadline=None, derandomize=True)
@given(
    st.lists(
        st.tuples(st.floats(min_value=0.0, max_value=1.0), st.booleans()), min_size=1, max_size=60
    ),
    st.integers(min_value=1, max_value=15),
)
def test_ece_bounds(pairs: list[tuple[float, bool]], n_bins: int) -> None:
    conf = [c for c, _ in pairs]
    correct = [k for _, k in pairs]
    for strategy in ("equal_mass", "equal_width"):
        value = ece(conf, correct, n_bins=n_bins, strategy=strategy)
        assert value is not None
        # Binning can only hide miscalibration: ECE never exceeds the per-case mean |c − y|.
        per_case = float(np.mean(np.abs(np.array(conf) - np.array(correct, dtype=float))))
        assert 0.0 <= value <= per_case + 1e-12


# --------------------------------------------------------------------------------------------
# Selective auto-decision
# --------------------------------------------------------------------------------------------

# Hand example (confidence, pred, gold, correct). Gold ≠ A: cases 2, 3, 4, 5 (four negatives).
HAND = [
    SelectiveCase(0.95, A, A, True),
    SelectiveCase(0.90, A, N, False),  # wrongful approval
    SelectiveCase(0.80, N, N, True),
    SelectiveCase(0.70, A, E, False),  # wrongful approval
    SelectiveCase(0.60, E, E, True),  # model escalates: never coverage
    SelectiveCase(0.60, N, A, False),  # wrongful denial
]


def test_selective_curve_hand_example() -> None:
    curve = selective_curve(HAND)
    rows = [
        (p.threshold, p.n_above, p.n_auto, p.n_wrongful_approvals, p.n_correct_auto) for p in curve
    ]
    assert rows == [
        (0.60, 6, 5, 2, 2),
        (0.70, 4, 4, 2, 2),
        (0.80, 3, 3, 1, 2),
        (0.90, 2, 2, 1, 1),
        (0.95, 1, 1, 0, 1),
    ]
    assert all(p.n_cases == 6 and p.n_negatives == 4 for p in curve)
    assert [p.coverage for p in curve] == pytest.approx([5 / 6, 4 / 6, 3 / 6, 2 / 6, 1 / 6])
    assert [p.war for p in curve] == pytest.approx([0.5, 0.5, 0.25, 0.25, 0.0])
    assert [p.correct_automation for p in curve] == pytest.approx(
        [2 / 6, 2 / 6, 2 / 6, 1 / 6, 1 / 6]
    )


def test_selective_curve_is_order_invariant_and_matches_operating_point() -> None:
    shuffled = [HAND[i] for i in (3, 0, 5, 1, 4, 2)]
    assert selective_curve(shuffled) == selective_curve(HAND)
    for point in selective_curve(HAND):
        assert operating_point(HAND, point.threshold) == point


def test_invalid_predictions_are_never_automated() -> None:
    cases = [SelectiveCase(0.99, INVALID, N, False), SelectiveCase(0.5, N, N, True)]
    top = selective_curve(cases)[-1]
    assert (top.threshold, top.n_above, top.n_auto) == (0.99, 1, 0)
    assert top.war == 0.0


def test_selective_curve_edge_cases() -> None:
    assert selective_curve([]) == []
    only_approvals = selective_curve([SelectiveCase(0.9, A, A, True)])
    assert only_approvals[0].war is None  # no gold ≠ A cases: n/a, not 0
    empty_point = SelectivePoint(0.5, 0, 0, 0, 0, 0, 0)
    assert empty_point.coverage is None and empty_point.correct_automation is None
    with pytest.raises(ValueError, match="gold"):
        selective_curve([SelectiveCase(0.9, A, INVALID, False)])
    with pytest.raises(ValueError, match="finite"):
        selective_curve([SelectiveCase(float("nan"), A, A, True)])


def test_operating_point_between_curve_thresholds() -> None:
    point = operating_point(HAND, 0.85)
    assert (point.n_above, point.n_auto, point.n_wrongful_approvals) == (2, 2, 1)
    assert point.threshold == 0.85
    nothing = operating_point(HAND, 1.5)
    assert (nothing.n_auto, nothing.war, nothing.coverage) == (0, 0.0, 0.0)


@pytest.mark.parametrize(
    ("target", "expected"),
    [(0.5, 0.60), (0.3, 0.80), (0.25, 0.80), (0.1, 0.95), (0.0, 0.95)],
)
def test_choose_threshold_maximizes_coverage_under_target(target: float, expected: float) -> None:
    assert choose_threshold(selective_curve(HAND), target) == expected


def test_choose_threshold_tie_break_and_no_solution() -> None:
    # Raising t from 0.5 to 0.7 only drops a model escalation: same automated set and coverage,
    # so the higher (more conservative) threshold is chosen.
    cases = [
        SelectiveCase(0.9, N, N, True),
        SelectiveCase(0.7, A, A, True),
        SelectiveCase(0.5, E, N, False),
    ]
    assert choose_threshold(selective_curve(cases), 0.0) == 0.7
    unsafe = [SelectiveCase(0.9, A, N, False), SelectiveCase(0.8, A, N, False)]
    assert choose_threshold(selective_curve(unsafe), 0.02) is None
    no_negatives = [SelectiveCase(0.9, A, A, True)]
    assert choose_threshold(selective_curve(no_negatives), 0.02) is None
    assert choose_threshold([], 0.02) is None


def test_certify_threshold_hand_example() -> None:
    cert = certify_threshold(HAND, 0.8, 0.02)
    assert (cert.k, cert.n) == (1, 4)
    assert cert.upper == pytest.approx(clopper_pearson_upper(1, 4))
    assert cert.upper == pytest.approx(0.7514, abs=1e-4)
    assert not cert.meets
    assert cert.status == "empirical"
    k, n, upper, meets = cert  # unpacks as (k, n, upper, meets)
    assert (k, n, upper, meets) == (1, 4, cert.upper, False)


def _clean_test_set(n_negatives: int) -> list[SelectiveCase]:
    """Every gold-non-APPROVE case correctly denied with high confidence, plus some approvals."""
    negatives = [SelectiveCase(0.95, N, N, True)] * n_negatives
    approvals = [SelectiveCase(0.97, A, A, True)] * 50
    return negatives + approvals


def test_certify_threshold_spec_sample_size_limits() -> None:
    """SPEC §10.3/§12.7: zero errors on 299 negatives certifies 1%; on 200 it certifies only 2%."""
    at_299 = certify_threshold(_clean_test_set(299), 0.9, 0.01)
    assert (at_299.k, at_299.n, at_299.meets) == (0, 299, True)
    assert at_299.upper == pytest.approx(clopper_pearson_upper(0, 299))
    at_200 = certify_threshold(_clean_test_set(200), 0.9, 0.01)
    assert (at_200.k, at_200.n, at_200.meets) == (0, 200, False)
    assert certify_threshold(_clean_test_set(200), 0.9).meets  # default target is 2%
    assert certify_threshold(_clean_test_set(200), 0.9).status == "meets"


def test_certify_threshold_meets_target_at_equality() -> None:
    """SPEC §12.7: the bound must be *at or below* the target, so equality certifies."""
    target = clopper_pearson_upper(0, 200)
    cert = certify_threshold(_clean_test_set(200), 0.9, target)
    assert cert.upper == target
    assert cert.meets
    assert not certify_threshold(_clean_test_set(200), 0.9, np.nextafter(target, 0.0)).meets


def test_certify_threshold_without_negatives_is_not_met() -> None:
    cert = certify_threshold([SelectiveCase(0.9, A, A, True)], 0.5, 0.02)
    assert cert == Certification(0, 0, None, False)


def test_certify_threshold_validates_alpha_and_target_even_without_negatives() -> None:
    no_negatives = [SelectiveCase(0.9, A, A, True)]
    with pytest.raises(ValueError, match="alpha"):
        certify_threshold(no_negatives, 0.5, 0.02, alpha=1.5)
    with pytest.raises(ValueError, match="target"):
        certify_threshold(no_negatives, 0.5, 2.0)
