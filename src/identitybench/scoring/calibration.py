"""Calibration and selective auto-decision operating points (SPEC §12.7).

These apply when a model returns a confidence with its decision. Calibration (ECE, Brier) is
measured against *correctness* (label and, for APPROVE, duration). The selective analysis asks
a deployment question: if every decision below confidence ``t`` were sent to a human instead,
how much of the workload would still be automated, and how often would a case that must not
be auto-approved get approved anyway? A threshold is chosen on ``dev`` with
:func:`choose_threshold` and then evaluated on ``test`` with :func:`operating_point` and
:func:`certify_threshold`.

Empty inputs give ``None`` (reported n/a), never 0, following SPEC §12.2.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, NamedTuple, Protocol

import numpy as np
from numpy.typing import NDArray

from identitybench.schemas.enums import Label
from identitybench.scoring.stats import DEFAULT_ALPHA, clopper_pearson_upper

__all__ = [
    "DEFAULT_N_BINS",
    "DEFAULT_WAR_TARGET",
    "BinStrategy",
    "Certification",
    "ConfidenceCase",
    "SelectiveCase",
    "SelectivePoint",
    "brier",
    "certify_threshold",
    "choose_threshold",
    "ece",
    "operating_point",
    "selective_curve",
]

DEFAULT_N_BINS: Final = 10
"""SPEC §12.7: ECE over 10 equal-mass bins."""

DEFAULT_WAR_TARGET: Final = 0.02
"""SPEC §12.7: default WAR target; 1% cannot be certified at v1's size (§10.3)."""

BinStrategy = Literal["equal_mass", "equal_width"]

type FloatArray = NDArray[np.float64]
type Numbers = Sequence[float] | NDArray[Any]
type Flags = Sequence[bool] | Sequence[int] | NDArray[Any]

_AUTO_LABELS: Final = frozenset({Label.A, Label.N})


# --------------------------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------------------------


def _calibration_inputs(confidences: Numbers, correct: Flags) -> tuple[FloatArray, FloatArray]:
    conf = np.asarray(confidences, dtype=np.float64)
    hits = np.asarray(correct, dtype=np.float64)
    if conf.ndim != 1 or hits.ndim != 1:
        raise ValueError("confidences and correct must be one-dimensional")
    if conf.size != hits.size:
        raise ValueError(f"{conf.size} confidences but {hits.size} correctness flags")
    if not np.all((conf >= 0.0) & (conf <= 1.0)):
        raise ValueError("confidences must lie in [0, 1]")
    if not np.all((hits == 0.0) | (hits == 1.0)):
        raise ValueError("correct must contain only booleans (or 0/1)")
    return conf, hits


def _equal_mass_bins(conf: FloatArray, n_bins: int) -> NDArray[np.intp]:
    """Assign sorted positions to ``n_bins`` nearly equal-count bins, keeping ties together.

    Model confidences are heavily tied (0.9, 0.95, ...). Splitting a tie group across bins would
    make ECE depend on input order, so each tie group is placed as a whole by the *midpoint* of
    the sorted positions it occupies: sorted position ``p`` of ``n`` falls in bin
    ``⌊p · B / n⌋``. Placing a group by its first position instead would let a large group that
    starts late in a segment swallow the earlier, smaller group there, and their opposite-sign
    errors would cancel. With midpoints, two groups share a bin only if together they hold fewer
    than ``2n / B`` cases, so two values *each* held by at least ``n / B`` cases never share a
    bin. A value with ``n / B`` cases can still absorb a small neighbour: with ``n = 100`` and
    ``B = 10``, ``[0.1]×6 + [0.5]×10 + [0.6]×1 + [0.9]×83`` puts 0.5 and 0.6 together in bin 1.
    That merge is acceptable, since a shared bin stays under ``2n / B`` cases. Without ties this
    is a plain equal-count split.
    """
    n = conf.size
    order = np.argsort(conf, kind="stable")
    ordered = conf[order]
    first = np.searchsorted(ordered, ordered, side="left")
    last = np.searchsorted(ordered, ordered, side="right") - 1
    bins = np.empty(n, dtype=np.intp)
    bins[order] = (first + last) * n_bins // (2 * n)
    return bins


def _equal_width_bins(conf: FloatArray, n_bins: int) -> NDArray[np.intp]:
    """Bins ``[0, 1/B), …, [(B−1)/B, 1]``; a value on an edge belongs to the upper bin.

    Literal edges such as 0.3 bin correctly on their own (``0.3 × 10 == 3.0`` in doubles). The
    tiny epsilon is for confidences produced by arithmetic that land a rounding error below an
    edge: ``0.7 − 0.4`` is ``0.29999999999999993``, and without it that value would fall into
    ``[0.2, 0.3)``.
    """
    raw = np.floor(conf * n_bins + 1e-9).astype(np.intp)
    return np.minimum(raw, n_bins - 1)


def ece(
    confidences: Numbers,
    correct: Flags,
    n_bins: int = DEFAULT_N_BINS,
    strategy: BinStrategy = "equal_mass",
) -> float | None:
    """Expected calibration error: ``Σ_b (n_b / n) · |accuracy_b − mean confidence_b|``.

    The default is SPEC §12.7's 10 equal-mass bins; ``equal_width`` is offered for comparison
    with the literature. Returns ``None`` for empty input.
    """
    if n_bins < 1:
        raise ValueError(f"n_bins must be at least 1, got {n_bins}")
    if strategy not in ("equal_mass", "equal_width"):
        raise ValueError(f"unknown binning strategy {strategy!r}")
    conf, hits = _calibration_inputs(confidences, correct)
    if conf.size == 0:
        return None
    if strategy == "equal_mass":
        bins = _equal_mass_bins(conf, n_bins)
    else:
        bins = _equal_width_bins(conf, n_bins)
    # n_b/n · |acc_b − conf_b| = |Σ_b correct − Σ_b confidence| / n
    conf_sum = np.bincount(bins, weights=conf, minlength=n_bins)
    hit_sum = np.bincount(bins, weights=hits, minlength=n_bins)
    return float(np.abs(hit_sum - conf_sum).sum() / conf.size)


def brier(confidences: Numbers, correct: Flags) -> float | None:
    """Brier score of confidence against correctness; ``None`` for empty input."""
    conf, hits = _calibration_inputs(confidences, correct)
    if conf.size == 0:
        return None
    return float(np.mean((conf - hits) ** 2))


# --------------------------------------------------------------------------------------------
# Selective auto-decision
# --------------------------------------------------------------------------------------------


class ConfidenceCase(Protocol):
    """What the selective analysis needs from a scored case.

    ``correct`` is SPEC §12.1 correctness of the model's own prediction (label and, for
    APPROVE, effective duration). ``gold`` is never INVALID.
    """

    @property
    def confidence(self) -> float: ...

    @property
    def pred(self) -> Label: ...

    @property
    def gold(self) -> Label: ...

    @property
    def correct(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class SelectiveCase:
    """Plain record satisfying :class:`ConfidenceCase`."""

    confidence: float
    pred: Label
    gold: Label
    correct: bool


@dataclass(frozen=True, slots=True)
class SelectivePoint:
    """Outcomes at one threshold: decisions with confidence below it are escalated.

    *Coverage* is the share of all cases decided without a human (effective A or N). A model's
    own ESCALATE above the threshold is not coverage, and an INVALID output always falls back
    to a human.
    """

    threshold: float
    n_cases: int
    n_above: int
    n_auto: int
    n_negatives: int
    n_wrongful_approvals: int
    n_correct_auto: int

    @property
    def coverage(self) -> float | None:
        """#(effective pred ∈ {A, N}) / #cases."""
        return self.n_auto / self.n_cases if self.n_cases else None

    @property
    def war(self) -> float | None:
        """#(effective pred = A ∧ gold ≠ A) / #(gold ≠ A), as in SPEC §12.2."""
        return self.n_wrongful_approvals / self.n_negatives if self.n_negatives else None

    @property
    def correct_automation(self) -> float | None:
        """#(correct ∧ effective pred ∈ {A, N}) / #cases, as in SPEC §12.2."""
        return self.n_correct_auto / self.n_cases if self.n_cases else None


class Certification(NamedTuple):
    """Result of checking a WAR target at a threshold on held-out cases (SPEC §12.7).

    ``k`` wrongful approvals among ``n`` gold-non-APPROVE cases; ``upper`` is the one-sided
    Clopper–Pearson bound (``None`` when ``n == 0``); ``meets`` iff ``upper <= target``.
    """

    k: int
    n: int
    upper: float | None
    meets: bool

    @property
    def status(self) -> Literal["meets", "empirical"]:
        """SPEC §12.7 label: a result that is not certified is reported as *empirical*."""
        return "meets" if self.meets else "empirical"


def _case_arrays(
    cases: Iterable[ConfidenceCase],
) -> tuple[FloatArray, NDArray[np.bool_], NDArray[np.bool_], NDArray[np.bool_], NDArray[np.bool_]]:
    """Confidence plus per-case flags: auto-decidable, wrongful approval, correct auto, gold ≠ A.

    "Auto-decidable" and the two outcome flags describe what happens if the case clears the
    threshold; below it the case is escalated and none of them apply.
    """
    items = list(cases)
    conf = np.array([c.confidence for c in items], dtype=np.float64)
    if not np.all(np.isfinite(conf)):
        raise ValueError("confidences must be finite")
    for c in items:
        if c.gold == Label.INVALID:
            raise ValueError("gold labels must be A, N or E")
    auto = np.array([c.pred in _AUTO_LABELS for c in items], dtype=np.bool_)
    negative = np.array([c.gold != Label.A for c in items], dtype=np.bool_)
    approve = np.array([c.pred == Label.A for c in items], dtype=np.bool_)
    correct = np.array([c.correct for c in items], dtype=np.bool_)
    return conf, auto, approve & negative, auto & correct, negative


def selective_curve(cases: Iterable[ConfidenceCase]) -> list[SelectivePoint]:
    """Operating points for every distinct confidence value, in ascending threshold order.

    At threshold ``t`` a case is auto-decided with its predicted label when
    ``confidence >= t`` and treated as ESCALATE otherwise, so coverage and WAR fall as ``t``
    rises. Built with one sort and suffix sums rather than a pass per threshold.
    """
    conf, auto, wrongful, correct_auto, negative = _case_arrays(cases)
    n = conf.size
    if n == 0:
        return []
    order = np.argsort(conf, kind="stable")
    ordered = conf[order]
    thresholds = np.unique(ordered)
    starts = np.searchsorted(ordered, thresholds, side="left")

    def above(flags: NDArray[np.bool_]) -> NDArray[np.int64]:
        """Count of flagged cases at or above each threshold."""
        suffix = np.cumsum(flags[order][::-1], dtype=np.int64)[::-1]
        return suffix[starts]

    n_negatives = int(np.count_nonzero(negative))
    n_auto, n_wrong, n_correct = above(auto), above(wrongful), above(correct_auto)
    return [
        SelectivePoint(
            threshold=float(t),
            n_cases=n,
            n_above=int(n - s),
            n_auto=int(a),
            n_negatives=n_negatives,
            n_wrongful_approvals=int(w),
            n_correct_auto=int(c),
        )
        for t, s, a, w, c in zip(thresholds, starts, n_auto, n_wrong, n_correct, strict=True)
    ]


def operating_point(cases: Iterable[ConfidenceCase], threshold: float) -> SelectivePoint:
    """Outcomes at an arbitrary threshold, e.g. a ``dev``-chosen threshold applied to ``test``."""
    conf, auto, wrongful, correct_auto, negative = _case_arrays(cases)
    cleared = conf >= threshold
    return SelectivePoint(
        threshold=float(threshold),
        n_cases=conf.size,
        n_above=int(np.count_nonzero(cleared)),
        n_auto=int(np.count_nonzero(cleared & auto)),
        n_negatives=int(np.count_nonzero(negative)),
        n_wrongful_approvals=int(np.count_nonzero(cleared & wrongful)),
        n_correct_auto=int(np.count_nonzero(cleared & correct_auto)),
    )


def choose_threshold(curve: Sequence[SelectivePoint], target_war: float) -> float | None:
    """Threshold with maximum coverage whose observed WAR is at most ``target_war``.

    Points with undefined WAR (no gold-non-APPROVE cases) are skipped, since they cannot show
    the target is met. Among equal-coverage points the highest threshold wins: it automates the
    same cases here and is the more conservative choice on unseen cases. Returns ``None`` when
    no point qualifies.
    """
    best: SelectivePoint | None = None
    for point in curve:
        war = point.war
        if war is None or war > target_war:
            continue
        if (
            best is None
            or point.n_auto > best.n_auto
            or (point.n_auto == best.n_auto and point.threshold > best.threshold)
        ):
            best = point
    return None if best is None else best.threshold


def certify_threshold(
    cases: Iterable[ConfidenceCase],
    threshold: float,
    target: float = DEFAULT_WAR_TARGET,
    alpha: float = DEFAULT_ALPHA,
) -> Certification:
    """Check a WAR target at ``threshold`` with an exact one-sided bound (SPEC §12.7).

    ``cases`` must be independent units (base request cases, §12.8) from a split the threshold
    was *not* chosen on. The target is met only if the ``1 − alpha`` Clopper–Pearson upper bound
    on WAR at the threshold is at or below ``target``; with no gold-non-APPROVE cases it is not.
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if not 0.0 <= target <= 1.0:
        raise ValueError(f"target must be in [0, 1], got {target}")
    point = operating_point(cases, threshold)
    k, n = point.n_wrongful_approvals, point.n_negatives
    if n == 0:
        return Certification(k, n, None, False)
    upper = clopper_pearson_upper(k, n, alpha)
    return Certification(k, n, upper, upper <= target)
