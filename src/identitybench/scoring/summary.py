"""Scoring entry points: one configuration, one run, and comparisons between configurations.

- :func:`score_cases` scores one configuration's predictions against cases and returns its
  :class:`ConfigScores`; :func:`score_config` also returns the per-case records and the per-unit
  vectors that paired comparisons need.
- :func:`score_run` reads ``runs/<run_id>/manifest.json`` and ``predictions.jsonl`` with the
  dataset the manifest names, scores every configuration, ranks them, compares them, and writes
  ``scores/summary.json`` (:class:`RunScores`) and ``scores/per_case.jsonl``.
- :func:`compare_configs` runs the paired cluster bootstrap over the units two configurations
  share, for every primary metric, with Holm's correction across the whole family of
  (configuration pair × primary metric) comparisons (SPEC §12.8).

**Comparisons.** A leaderboard compares models, so a run's family is every pair of models plus
each model against ``always_escalate``, the all-human status quo (:func:`comparison_pairs`). No
other baseline is compared with anything: a baseline's behavior is fixed by construction, so
those differences answer no question a leaderboard asks, and every comparison in the family
costs Holm power. A bootstrap p-value can't fall below 1/(B+1), so the resamples per comparison
grow with the family's size (:func:`comparison_resamples`); at a fixed B = 2,000, a family of
more than about 100 comparisons could never reject anything.

The interval on a difference is the bootstrap's, but its p-value alone is anti-conservative
when few units carry the difference: its tail behaves like e^-d in those d units whatever n is
(at n = 40 with all discordance one way, d = 4 gives a bootstrap p near 0.03 where the exact
test gives 0.125). So every comparison's p-value is the larger of the bootstrap's and an exact
paired sign-flip test's on the per-unit differences over the metric's resampling units
(``stats.paired_signflip_test``; a seeded Monte Carlo estimate only for a custom loss matrix
whose costs share no small common step, or past the exact-work budget), and Holm adjusts
that. For 0/1 outcomes on independent units the sign-flip test is the exact sign (McNemar)
test. For NRC and per-campaign review totals it weighs each unit by its difference, so a cost
difference that rests on four catastrophic approvals gets the p-value four units allow,
however many cheap escalations point the other way.

**The oracle** (SPEC §14) returns gold: a pipeline sanity check and the ceiling, not a
contender. Its scores are kept and marked (``ConfigScores.ceiling``), but it is left out of the
rankings, the sensitivity ranks, the Pareto frontier and the comparisons. Ranked, it would sit
first everywhere and alone on the frontier; compared, it would add comparisons whose answer is
known to the Holm family and cost every real comparison power.

**Which cases feed what** (SPEC §12.8). Primary decision metrics use repeat 0 of base cases only:
base request cases, the items of base campaigns, and JML events. Twins and adversarial copies
feed only robustness; repeats feed only the disagreement metrics. A case the configuration has
no repeat-0 prediction for is scored INVALID, so an interrupted run can't look better by
dropping hard cases; the count is reported.

**Runs.** :func:`score_run` doesn't let the predictions file say what a run covered. It rebuilds
the run's case selection from the manifest (split, suites and sample, drawn as ``runner.run``
draws them) and scores each configuration on every case it was scheduled for: every case for a
model, and for a baseline the cases of the tasks it does (SPEC §14: ``noop_plan`` has no answer
for a request). A run that didn't answer every scheduled cell (no ``finished_at``, cells in
``skipped.jsonl``, missing predictions) is refused unless ``allow_incomplete`` is set, and
``RunScores.completeness`` records what was missing either way. The runner schedules case by
case across configurations, so a budget stop leaves the same tail, often a whole suite,
unanswered by every configuration; scoring only the answered cases would rank configurations on
that truncated, unrepresentative set. A crash mid-append leaves a torn last line in
``predictions.jsonl``; it is skipped (never rewritten: scoring doesn't modify a run) and makes
the run incomplete.

**Tool-use evidence** (SPEC §8.6, §12.4, §13.1). A tool-use agent may rightly cite, or name in
a plan, a record its tools returned that the view doesn't hold, so a traced prediction's claims
are checked against the view plus those records, and a traced plan naming one (or a record one
of them references) is simulated against them, its repeats against one view they share
(``scoring.served``); :func:`score_run` serves them from the run's dataset, rebuilding a world
only when a prediction may rest on such a record. Untraced predictions (context mode,
baselines) are checked and simulated against the view, exactly as before.

**Outputs.** ``per_case.jsonl`` is replaced before ``summary.json``, and any old
``summary.json`` is removed before either, so a ``summary.json`` on disk always sits next to
the ``per_case.jsonl`` written with it: a scoring pass that fails partway leaves no summary,
and a reader treats a ``scores/`` directory without one as unscored.

**Determinism.** Cases are ordered by ID and units by campaign position before anything is
computed, every interval uses the settings' seed, and outputs are written as canonical JSON with
sorted keys, so the same cases and predictions give byte-identical files whatever order the
predictions were written in.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import tempfile
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from identitybench import __version__
from identitybench.policy.jml_state import PlanComparison
from identitybench.runner.config import Mode
from identitybench.runner.dataset import DEFAULT_STRATA_NAME, read_cases, stratified_sample
from identitybench.runner.manifest import (
    ConfigManifest,
    DatasetInfo,
    DatasetMismatchError,
    RunManifest,
    dataset_fingerprint,
)
from identitybench.schemas.case import Case
from identitybench.schemas.enums import Label, Split, Suite, Task, Tier
from identitybench.schemas.predictions import Prediction
from identitybench.scoring.decisions import (
    CampaignStats,
    DecisionScores,
    campaign_stats,
    decision_scores,
    escalation_costs,
    unit_costs,
)
from identitybench.scoring.grounding import UnitGrounding
from identitybench.scoring.loss import LossMatrix
from identitybench.scoring.metrics import EVENT_UNITS, REQUEST_UNITS, REVIEW_UNITS, UnitSpec
from identitybench.scoring.plans import PlanScores, PlanUnit, plan_scores, plan_unit
from identitybench.scoring.repeats import RepeatScores, repeat_scores
from identitybench.scoring.robustness import RobustnessScores, robustness_scores
from identitybench.scoring.served import (
    ServedIndex,
    WorldServedIndex,
    claim_index,
    simulation_view,
)
from identitybench.scoring.settings import ScoreSettings
from identitybench.scoring.stats import (
    DEFAULT_ALPHA,
    DEFAULT_N_BOOT,
    PairedResult,
    SignFlipTest,
    holm_bonferroni,
    minimum_detectable_difference,
    paired_bootstrap_diffs,
    paired_signflip_test,
)
from identitybench.scoring.tooluse import ToolScores, tool_scores
from identitybench.scoring.units import (
    DecisionUnit,
    PredictionIndex,
    decision_units,
    is_base,
    prediction_status,
)
from identitybench.scoring.usage import (
    ReportedCost,
    UsageScores,
    config_reported_cost,
    usage_scores,
)
from identitybench.world.entities import RiskTier

__all__ = [
    "CEILING_BASELINE",
    "COMPARISON_METHOD",
    "HOLM_RESOLUTION",
    "METRIC_DIRECTION",
    "PER_CASE_FILE",
    "PRIMARY_METRICS",
    "REFERENCE_BASELINE",
    "SCORES_DIR",
    "SUMMARY_FILE",
    "TOOL_MODE",
    "CaseRecord",
    "Comparison",
    "ConfigInfo",
    "ConfigScores",
    "IncompleteRunError",
    "LoadedRun",
    "MetricVector",
    "ParetoPoint",
    "RunCompleteness",
    "RunScores",
    "ScoredConfig",
    "SettingRanking",
    "SkippedCell",
    "TaskRanking",
    "UnitRecord",
    "compare_configs",
    "comparison_pairs",
    "comparison_resamples",
    "load_run",
    "read_predictions",
    "run_completeness",
    "score_cases",
    "score_config",
    "score_run",
]

SCORES_DIR = "scores"
SUMMARY_FILE = "summary.json"
PER_CASE_FILE = "per_case.jsonl"
MANIFEST_FILE = "manifest.json"
PREDICTIONS_FILE = "predictions.jsonl"
SKIPPED_FILE = "skipped.jsonl"
"""Cells the runner's budget stop left unanswered (present only while any remain)."""

BASELINE_PROVIDER = "baseline"
"""``ConfigManifest.provider`` of a SPEC §14 baseline."""

REFERENCE_BASELINE = "always_escalate"
"""SPEC §12.3, §14: the all-human status quo (NRC = 1 by construction). A run compares every
model with it, so "beats the status quo" gets a corrected test like the model-vs-model ones."""

TOOL_MODE: Mode = "tools"
"""``ConfigManifest.mode`` of a tool-use configuration (SPEC §8.6), which also gets
``ConfigScores.tools``."""

CEILING_BASELINE = "oracle"
"""SPEC §14: the baseline that returns gold. A run marks it ``ConfigScores.ceiling`` and leaves
it out of rankings, the Pareto frontier and comparisons (see the module docstring)."""

COMPARISON_METHOD = "paired_bootstrap+sign_flip"
"""``Comparison.method``: the paired cluster bootstrap's interval, and the larger of its p-value
and a paired sign-flip test's on the per-unit differences over the same resampling units."""

HOLM_RESOLUTION = 10
"""A family of m comparisons draws at least ``HOLM_RESOLUTION · m / alpha`` resamples per
comparison (:func:`comparison_resamples`)."""

PRIMARY_METRICS: Mapping[Task, tuple[str, ...]] = {
    Task.access_request: ("nrc", "war", "correct_automation", "grounded_accuracy"),
    Task.access_review: ("nrc", "war", "correct_automation", "grounded_accuracy"),
    Task.jml: ("unsafe_plan_rate",),
}
"""SPEC §12.8: the predeclared primary metrics; everything else is exploratory."""

METRIC_DIRECTION: Mapping[str, Literal["lower", "higher"]] = {
    "nrc": "lower",
    "war": "lower",
    "correct_automation": "higher",
    "grounded_accuracy": "higher",
    "unsafe_plan_rate": "lower",
}
"""Which way each primary metric improves, so a comparison can say which configuration each
discordant unit favors (``Comparison.n_favoring_a``)."""

_DECISION_TASKS: tuple[Task, ...] = (Task.access_request, Task.access_review)

_NRC_RANK_DIGITS = 12
"""NRCs are ranked after rounding to this many significant digits, so two configurations whose
costs are equal except for floating-point rounding (a reweighted mix) don't swap ranks."""


# --------------------------------------------------------------------------------------------
# Per-case records
# --------------------------------------------------------------------------------------------


class UnitRecord(BaseModel):
    """One scored decision in ``per_case.jsonl``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str | None
    position: int | None
    status: str
    gold: Label
    pred: Label
    correct: bool
    gold_duration_days: int | None
    pred_duration_days: int | None
    over_duration: bool
    over_restriction: bool
    risk_tier: RiskTier
    cost: float
    """Under the configured loss matrix, routing included (the failure gallery sorts on it)."""
    confidence: float | None
    grounding: UnitGrounding


class CaseRecord(BaseModel):
    """One case scored for one configuration (repeat 0), as one ``per_case.jsonl`` line."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_id: str
    case_id: str
    task: Task
    suite: Suite
    split: Split
    variant: str
    family_id: str
    twin_of: str | None
    tier: Tier
    tags: tuple[str, ...]
    status: str
    cost: float | None
    """Sum of the units' costs (decision tasks)."""
    units: list[UnitRecord]
    campaign: CampaignStats | None
    plan: PlanComparison | None
    plan_grounding: UnitGrounding | None


def _unit_records(units: Sequence[DecisionUnit], loss: LossMatrix) -> list[UnitRecord]:
    return [
        UnitRecord(
            item_id=u.item_id,
            position=u.position,
            status=u.status,
            gold=u.gold,
            pred=u.pred,
            correct=u.correct,
            gold_duration_days=u.gold_duration,
            pred_duration_days=u.pred_duration,
            over_duration=u.over_duration,
            over_restriction=u.over_restriction,
            risk_tier=u.risk_tier,
            cost=cost,
            confidence=u.confidence,
            grounding=u.grounding,
        )
        for u, cost in zip(units, unit_costs(units, loss), strict=True)
    ]


def _case_record(
    config_id: str,
    case: Case,
    status: str,
    units: Sequence[DecisionUnit] | None,
    plan: PlanUnit | None,
    loss: LossMatrix,
) -> CaseRecord:
    records = _unit_records(units, loss) if units is not None else []
    return CaseRecord(
        config_id=config_id,
        case_id=case.case_id,
        task=case.task,
        suite=case.suite,
        split=case.split,
        variant=case.variant,
        family_id=case.family_id,
        twin_of=case.twin_of,
        tier=case.tier,
        tags=case.tags,
        status=status,
        cost=math.fsum(r.cost for r in records) if units is not None else None,
        units=records,
        campaign=campaign_stats(units) if units and case.task is Task.access_review else None,
        plan=plan.comparison if plan is not None else None,
        plan_grounding=plan.grounding if plan is not None else None,
    )


# --------------------------------------------------------------------------------------------
# One configuration
# --------------------------------------------------------------------------------------------


class ConfigScores(BaseModel):
    """Everything scored for one configuration (model × prompt × mode) on one split."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_id: str
    split: Split | None
    scorer_version: str
    settings: ScoreSettings
    n_cases: int
    n_predictions: int
    n_no_prediction: int
    """Cases without a repeat-0 prediction, scored INVALID."""
    requests: DecisionScores | None
    reviews: DecisionScores | None
    jml: PlanScores | None
    robustness: RobustnessScores | None
    repeats: RepeatScores | None
    usage: UsageScores
    ceiling: bool = False
    """The SPEC §14 ``oracle`` baseline, which returns gold: a pipeline sanity check and the
    ceiling, not a contender. Its scores are complete (they must be perfect), but rankings, the
    Pareto frontier, sensitivity ranks and comparisons leave it out."""
    tools: ToolScores | None = None
    """SPEC §8.6 tool-use metrics (certificate retrieval, lucky guesses, tool calls per case,
    over-budget rate) of a tool-use model; None for a context-mode configuration and for every
    baseline, which answers in-process and never calls a tool. Exploratory, over base cases
    (``scoring.tooluse``)."""


@dataclass(frozen=True, slots=True)
class MetricVector:
    """Per-unit numerators and denominators of one primary metric, for paired comparisons."""

    keys: tuple[tuple[str, str], ...]
    clusters: tuple[str, ...] | None
    numerators: tuple[float, ...]
    denominators: tuple[float, ...]
    spec: UnitSpec
    binary: bool
    """Every unit is a 0/1 outcome over a gold-defined denominator (WAR, correct automation,
    grounded accuracy, unsafe plan rate), so the paired-proportion minimum detectable difference
    applies; NRC's per-unit cost is not binary. Every primary metric's denominator is defined by
    gold alone, binary or not, which the sign-flip test relies on
    (``stats.paired_signflip_test``)."""


@dataclass(frozen=True, slots=True)
class ScoredConfig:
    """:class:`ConfigScores` plus what writing ``per_case.jsonl`` and comparing configurations
    need (not serialized as a whole)."""

    scores: ConfigScores
    records: tuple[CaseRecord, ...]
    vectors: Mapping[tuple[Task, str], MetricVector]


def _flag(value: bool) -> float:
    return 1.0 if value else 0.0


def _decision_vectors(
    task: Task, units: Sequence[DecisionUnit], loss: LossMatrix
) -> dict[tuple[Task, str], MetricVector]:
    spec = REQUEST_UNITS if task is Task.access_request else REVIEW_UNITS
    keys = tuple(u.key for u in units)
    clusters = tuple(u.cluster for u in units) if spec.cluster is not None else None
    columns: dict[str, tuple[list[float], list[float]]] = {
        "nrc": (unit_costs(units, loss), escalation_costs(units, loss)),
        "war": (
            [_flag(u.wrongful_approval) for u in units],
            [_flag(u.gold is not Label.A) for u in units],
        ),
        "correct_automation": ([_flag(u.automated_correct) for u in units], [1.0] * len(units)),
        "grounded_accuracy": (
            [_flag(u.grounding.has_certificate and u.grounding.grounded) for u in units],
            [_flag(u.grounding.has_certificate) for u in units],
        ),
    }
    return {
        (task, name): MetricVector(keys, clusters, tuple(num), tuple(den), spec, name != "nrc")
        for name, (num, den) in columns.items()
    }


def _plan_vectors(units: Sequence[PlanUnit]) -> dict[tuple[Task, str], MetricVector]:
    return {
        (Task.jml, "unsafe_plan_rate"): MetricVector(
            keys=tuple(u.key for u in units),
            clusters=None,
            numerators=tuple(_flag(u.comparison.unsafe) for u in units),
            denominators=tuple(1.0 for _ in units),
            spec=EVENT_UNITS,
            binary=True,
        )
    }


def _by_id(cases: Iterable[Case]) -> dict[str, Case]:
    by_id: dict[str, Case] = {}
    for case in cases:
        if case.case_id in by_id:
            raise ValueError(f"duplicate case {case.case_id}")
        by_id[case.case_id] = case
    return dict(sorted(by_id.items()))


def _split_of(cases: Collection[Case]) -> Split | None:
    splits = {c.split for c in cases}
    if len(splits) > 1:
        raise ValueError(
            f"cases span several splits ({sorted(splits)}); score one split at a time and pass "
            "dev cases through dev_cases to choose a threshold"
        )
    return next(iter(splits)) if splits else None


def _primary_decision_units(
    cases: Mapping[str, Case], index: PredictionIndex, task: Task
) -> list[DecisionUnit]:
    """The primary units of ``task`` on dev, which only choose the operating point (SPEC
    §12.7) from confidence and correctness. Their claims are checked against the view alone,
    whatever the mode: no dev metric reads grounding, so no world is rebuilt for the dev run."""
    return [
        unit
        for case in cases.values()
        if case.task is task and is_base(case)
        for unit in decision_units(case, index.get(case.case_id))
    ]


def score_config(
    cases: Iterable[Case],
    predictions: Iterable[Prediction],
    *,
    settings: ScoreSettings | None = None,
    config_id: str | None = None,
    dev_cases: Iterable[Case] = (),
    dev_predictions: Iterable[Prediction] = (),
    ceiling: bool = False,
    mode: Mode = "context",
    served: ServedIndex | None = None,
    reported_cost: ReportedCost = "estimate",
) -> ScoredConfig:
    """Score one configuration's predictions against ``cases`` (one split).

    ``config_id`` is required only when there are no predictions to name the configuration.
    ``dev_cases``/``dev_predictions`` (the same configuration on ``dev``) choose the selective
    operating point that is then measured, and for requests certified, on ``cases`` (SPEC
    §12.7). ``ceiling`` marks the ``oracle`` baseline (``ConfigScores.ceiling``), which
    :func:`compare_configs` then refuses to compare. ``mode`` is the configuration's agent mode
    (SPEC §8.6): a ``tools`` configuration also gets its tool-use metrics
    (``ConfigScores.tools``). ``served`` gives the records a traced prediction's tools
    returned, which its evidence claims are checked against, and a plan naming one (or a
    record one references) is simulated against, beside the view, in the primary metrics, the
    repeats and the tool metrics alike (SPEC §8.6, §12.4, §12.8, §13.1; ``scoring.served``);
    predictions without a trace never use it. ``reported_cost`` says whether the configuration's
    ``reported`` costs are a billing gateway's charges, CLI estimates or figures from an
    unverified source (``scoring.usage.config_reported_cost``), which decides whether they
    count as API cost.
    Raises ``ValueError`` for predictions that don't
    belong to ``cases`` (unknown case, wrong task, another configuration, conflicting
    duplicates), for cases spanning splits, and (``scoring.served.ServedIndexError``) for a
    traced prediction citing a record its tools returned outside the view, or a traced plan
    naming an ID outside it when its tools returned records outside it, when ``served`` is
    None.
    """
    settings = settings or ScoreSettings()
    by_id = _by_id(cases)
    split = _split_of(by_id.values())
    index = PredictionIndex(predictions, by_id, config_id=config_id)
    dev_by_id = _by_id(dev_cases)
    dev_list = list(dev_predictions)
    dev_index: PredictionIndex | None = None
    if dev_list and not dev_by_id:
        raise ValueError("dev_predictions need the dev_cases they were made for")
    if dev_by_id:
        if _split_of(dev_by_id.values()) is not Split.dev:
            raise ValueError("dev_cases must come from the dev split")
        if split is Split.dev:
            raise ValueError("a threshold chosen on dev must be measured on another split")
        dev_index = PredictionIndex(dev_list, dev_by_id, config_id=index.config_id)

    units: dict[str, list[DecisionUnit]] = {}
    plans: dict[str, PlanUnit] = {}
    for case in by_id.values():
        prediction = index.get(case.case_id)
        claims = claim_index(case, prediction, served)
        if case.task is Task.jml:
            plans[case.case_id] = plan_unit(
                case, prediction, index=claims, view=simulation_view(case, prediction, served)
            )
        else:
            units[case.case_id] = decision_units(case, prediction, index=claims)

    def task_units(task: Task, *, base_only: bool) -> list[DecisionUnit]:
        return [
            u
            for case_id, case_units in units.items()
            if by_id[case_id].task is task and (not base_only or is_base(by_id[case_id]))
            for u in case_units
        ]

    primary_units = {task: task_units(task, base_only=True) for task in _DECISION_TASKS}

    def scores_for(task: Task) -> DecisionScores | None:
        primary = primary_units[task]
        if not primary:
            return None
        dev_units = (
            _primary_decision_units(dev_by_id, dev_index, task) if dev_index is not None else None
        )
        return decision_scores(
            primary, task, settings, dev_units=dev_units or None, evaluated_on=split
        )

    primary_plans = [plans[c.case_id] for c in by_id.values() if c.case_id in plans and is_base(c)]
    scores = ConfigScores(
        config_id=index.config_id,
        split=split,
        scorer_version=__version__,
        settings=settings,
        n_cases=len(by_id),
        n_predictions=len(index),
        n_no_prediction=sum(index.get(case_id) is None for case_id in by_id),
        requests=scores_for(Task.access_request),
        reviews=scores_for(Task.access_review),
        jml=plan_scores(primary_plans, settings) if primary_plans else None,
        robustness=robustness_scores(
            task_units(Task.access_request, base_only=False),
            task_units(Task.access_review, base_only=False),
            settings.resampling,
            small_n=settings.small_n,
        ),
        repeats=repeat_scores(by_id, index, settings.resampling, served=served),
        usage=usage_scores(index.all(), by_id, reported=reported_cost),
        ceiling=ceiling,
        tools=(
            tool_scores(
                by_id.values(),
                index.all(),
                settings=settings,
                config_id=index.config_id,
                served=served,
                plans=plans,
            )
            if mode == TOOL_MODE
            else None
        ),
    )
    records = tuple(
        _case_record(
            index.config_id,
            case,
            prediction_status(index.get(case.case_id)),
            units.get(case.case_id),
            plans.get(case.case_id),
            settings.loss,
        )
        for case in by_id.values()
    )
    vectors: dict[tuple[Task, str], MetricVector] = {}
    for task, primary in primary_units.items():
        if primary:
            vectors.update(_decision_vectors(task, primary, settings.loss))
    if primary_plans:
        vectors.update(_plan_vectors(primary_plans))
    return ScoredConfig(scores=scores, records=records, vectors=vectors)


def score_cases(
    cases: Iterable[Case],
    predictions: Iterable[Prediction],
    *,
    settings: ScoreSettings | None = None,
    config_id: str | None = None,
    dev_cases: Iterable[Case] = (),
    dev_predictions: Iterable[Prediction] = (),
    ceiling: bool = False,
    mode: Mode = "context",
    served: ServedIndex | None = None,
    reported_cost: ReportedCost = "estimate",
) -> ConfigScores:
    """Score one configuration's predictions against ``cases``; see :func:`score_config`."""
    return score_config(
        cases,
        predictions,
        settings=settings,
        config_id=config_id,
        dev_cases=dev_cases,
        dev_predictions=dev_predictions,
        ceiling=ceiling,
        mode=mode,
        served=served,
        reported_cost=reported_cost,
    ).scores


# --------------------------------------------------------------------------------------------
# Comparisons
# --------------------------------------------------------------------------------------------


class Comparison(BaseModel):
    """Paired difference ``a − b`` of one primary metric over the units both configs scored.

    The interval is the paired cluster bootstrap's (SPEC §12.8). The p-value is the larger of
    the bootstrap's and a paired sign-flip test's on the per-unit differences over the same
    resampling units (``stats.paired_signflip_test``): the bootstrap's tail is anti-conservative
    when few units carry the difference, so no comparison claims more than the randomization
    distribution of its own per-unit differences allows. Holm adjusts that combined p-value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    config_a: str
    config_b: str
    task: Task
    metric: str
    point_a: float | None
    point_b: float | None
    diff: float | None
    low: float | None
    high: float | None
    p_value: float | None
    """``max(p_bootstrap, p_signflip)``. None when the bootstrap has no p-value (either point is
    n/a, or fewer than two clusters have a nonzero denominator), and such a comparison is left
    out of the Holm family."""
    p_bootstrap: float | None
    """The paired bootstrap's two-sided p-value, floored at 1/(B+1)."""
    p_signflip: float
    """The two-sided paired sign-flip p-value of the discordant units' differences
    (``stats.signflip_p``): 1 when none differ, never 0, and for a binary metric on independent
    units the exact sign test's min(1, 2·P(Binomial(d, 1/2) ≤ smaller side))."""
    signflip_exact: bool
    """``p_signflip`` is exact. False when the per-unit differences share no small common step
    (a custom loss matrix with incommensurate costs, at any size) or their null distribution is
    past ``stats.MAX_EXACT_WORK``; it is then a Monte Carlo estimate (k + 1)/(n_boot + 1) over
    ``n_boot`` seeded sign draws."""
    adjusted_p: float | None
    """Holm-adjusted across every comparison in the family that has a p-value."""
    reject: bool
    family_size: int
    """m: the comparisons in the Holm family (those with a p-value)."""
    n_units: int
    n_clusters: int
    units: str
    cluster: str | None
    method: str = COMPARISON_METHOD
    n_boot: int
    """Resamples drawn, sized to the family (:func:`comparison_resamples`)."""
    dropped_resamples: int
    alpha: float
    binary: bool
    """Each unit is a 0/1 outcome (WAR, correct automation, grounded accuracy, unsafe plan
    rate). NRC's per-unit cost is not, so it has no minimum detectable difference."""
    n_discordant: int
    """Resampling units (base request cases, review campaigns, JML events) whose contribution
    to the metric differs between the two configurations: the sign-flip test's d, and what
    comparison power rests on (SPEC §10.3). For a binary metric on independent units, the
    units in the denominator where exactly one configuration has the outcome."""
    n_favoring_a: int
    """Discordant units where ``a`` does better: a lower contribution for NRC, WAR and unsafe
    plan rate, a higher one for correct automation and grounded accuracy
    (:data:`METRIC_DIRECTION`)."""
    n_favoring_b: int
    """Discordant units where ``b`` does better."""
    discordance_rate: float | None
    """``n_discordant`` over the resampling units in the metric's denominator; None when no
    unit is in it."""
    minimum_detectable_difference: float | None
    """Smallest difference detectable at 80% power given this discordance (Connor 1987). Only
    for binary metrics on independent units; the approximation is optimistic for clustered
    review items."""


def _detectable(vector: MetricVector, signflip: SignFlipTest, alpha: float) -> float | None:
    """The minimum detectable difference of a binary metric over independent units."""
    rate = signflip.rate
    if not vector.binary or vector.spec.cluster is not None or rate is None:
        return None
    return minimum_detectable_difference(rate, signflip.n_units, alpha=alpha)


def _aligned(
    a: MetricVector, b: MetricVector
) -> tuple[list[float], list[float], list[float], list[float], list[str] | None]:
    """Both vectors restricted to their shared units, in sorted key order."""
    position_b = {key: i for i, key in enumerate(b.keys)}
    shared = sorted((key, i, position_b[key]) for i, key in enumerate(a.keys) if key in position_b)
    clusters = None
    if a.clusters is not None:
        clusters = [a.clusters[i] for _, i, _ in shared]
    return (
        [a.numerators[i] for _, i, _ in shared],
        [a.denominators[i] for _, i, _ in shared],
        [b.numerators[j] for _, _, j in shared],
        [b.denominators[j] for _, _, j in shared],
        clusters,
    )


def comparison_pairs(
    configs: Collection[str],
    *,
    baselines: Collection[str] = (),
    reference: str | None = None,
    ceilings: Collection[str] = (),
) -> list[tuple[str, str]]:
    """The configuration pairs a leaderboard compares (SPEC §12.8), in a fixed order.

    Every pair of models (configurations that are neither in ``baselines`` or ``ceilings`` nor
    the ``reference``), then each model against ``reference``, oriented so a difference reads
    "model − reference". Baselines are never compared with each other or with models (except
    the reference): their behavior is fixed by construction, so those comparisons answer no
    question a leaderboard asks, and each one would cost the family's Holm correction power.
    ``ceilings`` (the ``oracle``, SPEC §14) are never compared with anything, whether or not
    they are also listed as baselines, and can't be the reference. With no baselines, ceilings
    or reference, every pair is compared.
    """
    named = set(baselines) | set(ceilings)
    if reference is not None:
        named.add(reference)
    unknown = sorted(named - set(configs))
    if unknown:
        raise ValueError(f"unknown configurations {unknown}")
    if reference is not None and reference in ceilings:
        raise ValueError(f"the reference {reference!r} is a ceiling, which is never compared")
    models = sorted(c for c in configs if c not in named)
    pairs = list(itertools.combinations(models, 2))
    if reference is not None:
        pairs.extend((model, reference) for model in models)
    return pairs


def comparison_resamples(family_size: int, *, alpha: float, n_boot: int) -> int:
    """Bootstrap resamples per comparison for a Holm family of ``family_size`` comparisons.

    A bootstrap p-value can't fall below 1/(B+1) (``stats.paired_bootstrap_diff``), while
    Holm's first step rejects only a p-value at most alpha/m. With B fixed at 2,000, a family of
    more than about 100 comparisons could never reject anything, however large its differences.
    So B is at least ``HOLM_RESOLUTION · m / alpha``: the floor then sits an order of magnitude
    below alpha/m, and a comparison whose true p-value is half of alpha/m gets a bootstrap
    p-value at or below alpha/m about 96% of the time, rather than being lost in the
    bootstrap's Monte Carlo error. ``n_boot`` is a floor, so a small family keeps the configured
    B.
    """
    return max(n_boot, math.ceil(HOLM_RESOLUTION * family_size / alpha))


@dataclass(frozen=True, slots=True)
class _Tested:
    """One comparison's bootstrap and sign-flip test, before Holm adjusts its p-value."""

    config_a: str
    config_b: str
    task: Task
    metric: str
    result: PairedResult
    signflip: SignFlipTest
    vector: MetricVector

    @property
    def p_value(self) -> float | None:
        """The larger of the two p-values; None when the bootstrap has none."""
        if self.result.p_value is None:
            return None
        return max(self.result.p_value, self.signflip.p_value)

    @property
    def favoring(self) -> tuple[int, int]:
        """(units favoring a, units favoring b) among the discordant units."""
        if METRIC_DIRECTION[self.metric] == "lower":
            return self.signflip.b_higher, self.signflip.a_higher
        return self.signflip.a_higher, self.signflip.b_higher


def compare_configs(
    configs: Mapping[str, ScoredConfig],
    *,
    pairs: Sequence[tuple[str, str]] | None = None,
    baselines: Collection[str] = (),
    reference: str | None = None,
    n_boot: int = DEFAULT_N_BOOT,
    alpha: float = DEFAULT_ALPHA,
    seed: int = 0,
) -> list[Comparison]:
    """Paired comparisons for every primary metric of every compared configuration pair.

    ``pairs`` defaults to :func:`comparison_pairs` of ``configs``, ``baselines`` and
    ``reference``: every pair of models plus each model against the reference, never two
    baselines and never a ceiling (``ConfigScores.ceiling``, the ``oracle``); explicit
    ``pairs`` naming a ceiling are refused. Each comparison uses only the units both
    configurations scored, resampled at the metric's §12.8 units, so the case-mix noise they
    share cancels.

    Each comparison carries the paired cluster bootstrap's interval on the difference, and a
    p-value that is the larger of the bootstrap's and a paired sign-flip test's on the per-unit
    differences over the same units (ties dropped; ``stats.paired_signflip_test``). The
    bootstrap alone is anti-conservative when few units carry the difference, whatever n is; the
    sign-flip test is exact there and weighs each unit by its difference (for a binary metric on
    independent units it is the exact sign test), and when many units carry the difference the
    bootstrap's own p-value, floored at 1/(B+1), is the binding one. Holm–Bonferroni
    runs on those p-values across the whole family of (pair × primary metric) comparisons that
    have one (SPEC §12.8). ``n_boot`` is a floor: every comparison draws
    :func:`comparison_resamples` for the family's size, so a large family can still reject.
    Every comparison reports how many units differ and which way, and binary metrics on
    independent units the smallest difference that discordance can detect (SPEC §10.3). NRC is
    only comparable under one loss matrix, so configurations scored with different matrices are
    refused.
    """
    if pairs is not None and (baselines or reference is not None):
        raise ValueError("pass pairs, or baselines and reference to build them, not both")
    losses = [s.scores.settings.loss for s in configs.values()]
    if any(loss != losses[0] for loss in losses[1:]):
        raise ValueError("configurations were scored with different loss matrices")
    ceilings = {name for name, s in configs.items() if s.scores.ceiling}
    chosen = (
        list(pairs)
        if pairs is not None
        else comparison_pairs(configs, baselines=baselines, reference=reference, ceilings=ceilings)
    )
    planned: list[tuple[tuple[str, str], tuple[Task, str]]] = []
    for name_a, name_b in chosen:
        if name_a not in configs or name_b not in configs:
            raise ValueError(f"unknown configuration in pair ({name_a!r}, {name_b!r})")
        if name_a in ceilings or name_b in ceilings:
            raise ValueError(
                f"pair ({name_a!r}, {name_b!r}) names a ceiling (the oracle baseline, which "
                "returns gold); a ceiling is never compared"
            )
        for task, metrics in PRIMARY_METRICS.items():
            for metric in metrics:
                key = (task, metric)
                if key in configs[name_a].vectors and key in configs[name_b].vectors:
                    planned.append(((name_a, name_b), key))
    # Sized on every planned comparison, an upper bound on the family that ends up with a
    # p-value, so the resolution never falls short of the family actually tested.
    resamples = comparison_resamples(len(planned), alpha=alpha, n_boot=n_boot)
    aligned = [
        _aligned(configs[pair[0]].vectors[key], configs[pair[1]].vectors[key])
        for pair, key in planned
    ]
    boot_results = paired_bootstrap_diffs(aligned, n_boot=resamples, alpha=alpha, seed=seed)
    tested = [
        _Tested(
            config_a=pair[0],
            config_b=pair[1],
            task=key[0],
            metric=key[1],
            result=boot,
            signflip=paired_signflip_test(
                num_a, den_a, num_b, den_b, clusters, n_draws=resamples, seed=seed
            ),
            vector=configs[pair[0]].vectors[key],
        )
        for (pair, key), (num_a, den_a, num_b, den_b, clusters), boot in zip(
            planned, aligned, boot_results, strict=True
        )
    ]
    p_values = {i: p for i, t in enumerate(tested) if (p := t.p_value) is not None}
    holm = holm_bonferroni(p_values, alpha) if p_values else {}
    comparisons: list[Comparison] = []
    for i, t in enumerate(tested):
        adjusted = holm.get(i)
        favoring_a, favoring_b = t.favoring
        comparisons.append(
            Comparison(
                config_a=t.config_a,
                config_b=t.config_b,
                task=t.task,
                metric=t.metric,
                point_a=t.result.point_a,
                point_b=t.result.point_b,
                diff=t.result.diff,
                low=t.result.low,
                high=t.result.high,
                p_value=t.p_value,
                p_bootstrap=t.result.p_value,
                p_signflip=t.signflip.p_value,
                signflip_exact=t.signflip.exact,
                adjusted_p=adjusted.adjusted_p if adjusted is not None else None,
                reject=adjusted.reject if adjusted is not None else False,
                family_size=len(p_values),
                n_units=t.result.n_units,
                n_clusters=t.result.n_clusters,
                units=t.vector.spec.units,
                cluster=t.vector.spec.cluster,
                n_boot=resamples,
                dropped_resamples=t.result.dropped_resamples,
                alpha=alpha,
                binary=t.vector.binary,
                n_discordant=t.signflip.n_discordant,
                n_favoring_a=favoring_a,
                n_favoring_b=favoring_b,
                discordance_rate=t.signflip.rate,
                minimum_detectable_difference=_detectable(t.vector, t.signflip, alpha),
            )
        )
    return comparisons


# --------------------------------------------------------------------------------------------
# Rankings and the Pareto frontier
# --------------------------------------------------------------------------------------------


class SettingRanking(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    setting: str
    nrc: dict[str, float | None]
    ranks: dict[str, int | None]
    changed: list[str]
    """Configurations whose rank differs from their headline rank. A setting under which NRC is
    n/a (its case mix can't be formed from these cases) ranks nothing and changes nothing."""


class TaskRanking(BaseModel):
    """Configurations ranked by NRC (lower is better; ties share a rank) under the configured
    settings, and under every sensitivity setting, with rank changes flagged (SPEC §12.3).

    A ceiling (``ConfigScores.ceiling``, the ``oracle``) isn't a key here: it would rank first
    under every setting, pushing every contender down one place."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nrc: dict[str, float | None]
    ranks: dict[str, int | None]
    by_setting: list[SettingRanking]
    rank_changes: bool


class ParetoPoint(BaseModel):
    """WAR against correct automation; the frontier is the configs no other beats on both.

    A ceiling has no point: with zero WAR and every decidable unit automated correctly it would
    beat every configuration on both and be the whole frontier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    war: float | None
    correct_automation: float | None
    on_frontier: bool | None
    """None when either coordinate is n/a."""


def _ranks(values: Mapping[str, float | None]) -> dict[str, int | None]:
    rounded = {k: float(f"{v:.{_NRC_RANK_DIGITS}g}") for k, v in values.items() if v is not None}
    return {
        k: (1 + sum(other < rounded[k] for other in rounded.values()) if k in rounded else None)
        for k in values
    }


def _task_ranking(scores: Mapping[str, DecisionScores]) -> TaskRanking:
    nrc = {name: s.cost.nrc.point for name, s in scores.items()}
    ranks = _ranks(nrc)
    by_setting: list[SettingRanking] = []
    settings_keys = [p.setting for p in next(iter(scores.values())).cost.sensitivity]
    for position, setting in enumerate(settings_keys):
        values = {name: s.cost.sensitivity[position].nrc for name, s in scores.items()}
        setting_ranks = _ranks(values)
        by_setting.append(
            SettingRanking(
                setting=setting,
                nrc=values,
                ranks=setting_ranks,
                changed=[
                    name
                    for name in scores
                    if setting_ranks[name] is not None
                    and ranks[name] is not None
                    and setting_ranks[name] != ranks[name]
                ],
            )
        )
    return TaskRanking(
        nrc=nrc,
        ranks=ranks,
        by_setting=by_setting,
        rank_changes=any(s.changed for s in by_setting),
    )


def _pareto(scores: Mapping[str, DecisionScores]) -> dict[str, ParetoPoint]:
    points = {
        name: (s.metrics.war.point, s.metrics.correct_automation.point)
        for name, s in scores.items()
    }
    defined = {k: (w, c) for k, (w, c) in points.items() if w is not None and c is not None}

    def dominated(war: float, automation: float) -> bool:
        return any(
            w <= war and c >= automation and (w < war or c > automation)
            for w, c in defined.values()
        )

    return {
        name: ParetoPoint(
            war=war,
            correct_automation=automation,
            on_frontier=None if name not in defined else not dominated(*defined[name]),
        )
        for name, (war, automation) in points.items()
    }


# --------------------------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------------------------


class ConfigInfo(BaseModel):
    """What the manifest says a configuration is (for leaderboard columns)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    provider: str
    model: str
    prompt_variant: str
    mode: str
    temperature: float | None
    """None means no temperature was sent; SPEC §12.8 flags models that don't accept one."""

    @classmethod
    def of(cls, config: ConfigManifest) -> ConfigInfo:
        return cls(
            label=config.label,
            provider=config.provider,
            model=config.model,
            prompt_variant=config.prompt_variant,
            mode=config.mode,
            temperature=config.params.temperature,
        )


class IncompleteRunError(ValueError):
    """:func:`score_run` was given a run that didn't answer every cell it scheduled, without
    ``allow_incomplete``."""


class SkippedCell(BaseModel):
    """One ``skipped.jsonl`` line: a cell the runner's budget stop left unanswered."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    config_id: str
    case_id: str
    sample_idx: int


class RunCompleteness(BaseModel):
    """Whether a run answered every cell it scheduled (SPEC §11.4: runs are resumable and stop
    hard at ``budget_usd``, so a run directory can hold a partial run)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    complete: bool
    """Finished, nothing skipped, no torn line, and no scheduled case is missing a repeat-0
    prediction."""
    finished: bool
    """The manifest's ``finished_at`` is set, which the runner does only once every scheduled
    cell has a prediction."""
    n_expected_cases: int
    """Cases the manifest's split, suites and sample select."""
    n_skipped_cells: int
    """Cells (any repeat) ``skipped.jsonl`` lists as left unanswered by the budget stop."""
    missing_predictions: dict[str, int]
    """Per configuration in the manifest: cases it was scheduled for that have no repeat-0
    prediction. A scored configuration scores them INVALID."""
    torn_tail: bool = False
    """``predictions.jsonl`` ends in an unterminated line that doesn't parse: the run was
    interrupted mid-append, or is still running. The line was skipped."""


class RunScores(BaseModel):
    """``scores/summary.json``: every configuration of one run, ranked and compared."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    split: Split
    dataset: DatasetInfo
    scorer_version: str
    settings: ScoreSettings
    completeness: RunCompleteness
    """An incomplete run is only scored on request (``allow_incomplete``); reports should say
    so wherever its numbers appear."""
    dev_run_id: str | None
    """The ``dev`` run each configuration's selective operating point was chosen on."""
    dev_completeness: RunCompleteness | None
    configs: dict[str, ConfigScores]
    """Every scored configuration, the ceiling (``ConfigScores.ceiling``) included."""
    config_info: dict[str, ConfigInfo]
    unscored_configs: list[str]
    """Configurations in the manifest with no predictions at all: a baseline that does none of
    the run's tasks, or, in an incomplete run, a configuration the run never reached."""
    rankings: dict[Task, TaskRanking]
    """Per decision task, every scored configuration except the ceiling."""
    pareto: dict[Task, dict[str, ParetoPoint]]
    """Per decision task, every scored configuration except the ceiling."""
    comparisons: list[Comparison]


@dataclass(frozen=True, slots=True)
class LoadedRun:
    """A run directory read back: its manifest, the cases it selected, its predictions by
    configuration and the cells its budget stop skipped."""

    run_dir: Path
    manifest: RunManifest
    dataset_dir: Path
    cases: tuple[Case, ...]
    """Every case the manifest's split, suites and sample select, by ID, answered or not."""
    predictions: Mapping[str, tuple[Prediction, ...]]
    skipped: tuple[SkippedCell, ...]
    torn_tail: bool = False
    """``predictions.jsonl`` ended in a torn line, which was skipped (:func:`read_predictions`)."""


def _read_predictions(path: Path | str) -> tuple[list[Prediction], bool]:
    """The predictions in a ``predictions.jsonl`` file, and whether a torn last line was skipped
    (see :func:`read_predictions`)."""
    predictions: list[Prediction] = []
    with Path(path).open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                predictions.append(Prediction.model_validate_json(line))
            except ValueError as exc:
                if not line.endswith("\n"):
                    return predictions, True
                raise ValueError(f"{path}:{lineno}: invalid prediction: {exc}") from exc
    return predictions, False


def read_predictions(path: Path | str) -> list[Prediction]:
    """Every prediction in a ``predictions.jsonl`` file; blank lines are ignored.

    The runner appends one line per finished call, so a crash or kill mid-append leaves an
    unterminated last line, and so does reading a run that is still appending. A last line
    that has no trailing newline and doesn't parse is that torn tail and is skipped, the rule
    ``runner.run.load_predictions`` applies; unlike the runner, nothing rewrites the file. Any
    other line that doesn't parse is corruption and raises ``ValueError``.
    """
    return _read_predictions(path)[0]


def _read_skipped(path: Path) -> tuple[SkippedCell, ...]:
    """The cells in ``skipped.jsonl``; none when the file is absent (nothing was skipped)."""
    if not path.is_file():
        return ()
    cells: list[SkippedCell] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                cells.append(SkippedCell.model_validate_json(line))
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: invalid skipped cell: {exc}") from exc
    return tuple(cells)


def _resolve_dataset(run_dir: Path, manifest: RunManifest, dataset_dir: Path | str | None) -> Path:
    """The dataset directory the run used, recognized by its fingerprint.

    An explicit ``dataset_dir`` must match the manifest's fingerprint. Otherwise the manifest's
    path is relative to the repo the runner ran in, which holds ``runs/``, so it is tried there
    first and then from the working directory, and the first candidate whose fingerprint
    matches wins: a directory of the same name elsewhere (another checkout, a regenerated
    dataset) is passed over rather than picked first and then failing, or worse, scored
    against. Raises ``FileNotFoundError`` when no candidate exists and
    ``DatasetMismatchError`` when none matches.
    """
    if dataset_dir is not None:
        candidates = [Path(dataset_dir)]
    else:
        recorded = Path(manifest.dataset.path)
        candidates = list(dict.fromkeys((run_dir.parent.parent / recorded, recorded)))
    mismatches: list[str] = []
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        try:
            fingerprint = dataset_fingerprint(candidate)
        except DatasetMismatchError as exc:
            mismatches.append(str(exc))
            continue
        if fingerprint == manifest.dataset.sha256:
            return candidate
        mismatches.append(f"{candidate} has fingerprint {fingerprint}")
    if not mismatches:
        tried = ", ".join(str(c) for c in candidates)
        raise FileNotFoundError(
            f"dataset {manifest.dataset.path!r} named by {run_dir / MANIFEST_FILE} not found "
            f"(tried {tried}); pass dataset_dir"
        )
    raise DatasetMismatchError(
        f"run {manifest.run_id} used dataset {manifest.dataset.sha256}, but "
        + "; ".join(mismatches)
    )


def _recorded_suites(manifest: RunManifest) -> list[str] | None:
    """The suites the experiment ran (``tasks:``), or None when the manifest doesn't record
    them, which reads every suite of the split: the safe direction, since more expected cases
    can only make a run look less complete."""
    recorded: object = manifest.experiment.get("tasks")
    if recorded is None:
        return None
    if not isinstance(recorded, list):
        raise ValueError(f"run {manifest.run_id}: experiment.tasks must be a list of suites")
    suites: list[str] = []
    for suite in cast(list[object], recorded):
        if not isinstance(suite, str):
            raise ValueError(f"run {manifest.run_id}: experiment.tasks must be a list of suites")
        suites.append(suite)
    return suites


def _selected_cases(data_dir: Path, manifest: RunManifest) -> list[Case]:
    """The cases the run selected, as ``runner.run`` selects them: the manifest's split and
    suites, then its stratified sample (by the recorded unit and seed). A run answers cases in
    schedule order, so this, not the predictions file, says what a partial run left out."""
    cases = read_cases(data_dir, split=manifest.split, suites=_recorded_suites(manifest))
    if manifest.sample is not None:
        if manifest.sample_strata != DEFAULT_STRATA_NAME:
            raise ValueError(
                f"run {manifest.run_id} was sampled with strata {manifest.sample_strata!r}; "
                f"only {DEFAULT_STRATA_NAME!r} samples can be rebuilt"
            )
        if "sample" not in manifest.seeds:
            raise ValueError(f"run {manifest.run_id} records a sample but no sample seed")
        cases = stratified_sample(
            cases, manifest.sample, seed=manifest.seeds["sample"], unit=manifest.sample_unit
        )
    if manifest.counts.cases and manifest.counts.cases != len(cases):
        raise ValueError(
            f"run {manifest.run_id} counts {manifest.counts.cases} cases, but its split, suites "
            f"and sample select {len(cases)} from {data_dir}"
        )
    return sorted(cases, key=lambda c: c.case_id)


def load_run(run_dir: Path | str, dataset_dir: Path | str | None = None) -> LoadedRun:
    """Read a run directory and the dataset it was run on.

    The dataset must still be the one the run used (its fingerprint must match the manifest's),
    or every score would be against different gold (see :func:`_resolve_dataset`). The cases
    are the run's whole selection, rebuilt from the manifest, so cases a partial run never
    answered are still expected. Predictions and skipped cells must carry the run's ID, name
    configurations the manifest lists and name selected cases; anything else means the files
    don't belong together, and raises ``ValueError``.
    """
    root = Path(run_dir)
    manifest = RunManifest.load(root / MANIFEST_FILE)
    predictions, torn_tail = _read_predictions(root / PREDICTIONS_FILE)
    foreign = sorted({p.run_id for p in predictions} - {manifest.run_id})
    if foreign:
        raise ValueError(f"{root / PREDICTIONS_FILE} holds predictions of other runs: {foreign}")
    data_dir = _resolve_dataset(root, manifest, dataset_dir)
    cases = _selected_cases(data_dir, manifest)
    skipped = _read_skipped(root / SKIPPED_FILE)
    configs = {c.config_id for c in manifest.configs}
    selected = {c.case_id for c in cases}
    for path, cells in (
        (root / PREDICTIONS_FILE, [(p.config_id, p.case_id) for p in predictions]),
        (root / SKIPPED_FILE, [(s.config_id, s.case_id) for s in skipped]),
    ):
        unknown = sorted({config_id for config_id, _ in cells} - configs)
        if unknown:
            raise ValueError(f"{path} names configurations the manifest doesn't list: {unknown}")
        stray = sorted({case_id for _, case_id in cells} - selected)
        if stray:
            raise ValueError(
                f"{path} names cases outside the run's selection (its {manifest.split} split, "
                f"suites and sample): {stray[:5]}"
            )
    grouped: dict[str, list[Prediction]] = {}
    for p in predictions:
        grouped.setdefault(p.config_id, []).append(p)
    return LoadedRun(
        run_dir=root,
        manifest=manifest,
        dataset_dir=data_dir,
        cases=tuple(cases),
        predictions={k: tuple(v) for k, v in sorted(grouped.items())},
        skipped=skipped,
        torn_tail=torn_tail,
    )


def _config(run: LoadedRun, config_id: str) -> ConfigManifest:
    return next(c for c in run.manifest.configs if c.config_id == config_id)


def _scheduled_cases(run: LoadedRun, config: ConfigManifest) -> list[Case]:
    """The cases the runner scheduled ``config`` for.

    A model answers every case. A baseline answers only the tasks it does (SPEC §14), which the
    manifest doesn't record, so they are read off the cells it answered or the budget skipped.
    That is exact for a finished run, where every scheduled cell has a prediction; and since
    baselines answer in-process and the budget never skips them, only a crash can hide one of a
    baseline's tasks, in a run that then reads as incomplete.
    """
    if config.provider != BASELINE_PROVIDER:
        return list(run.cases)
    tasks = {p.task for p in run.predictions.get(config.config_id, ())}
    skipped = {s.case_id for s in run.skipped if s.config_id == config.config_id}
    tasks |= {c.task for c in run.cases if c.case_id in skipped}
    return [c for c in run.cases if c.task in tasks]


def run_completeness(run: LoadedRun) -> RunCompleteness:
    """What ``run`` left unanswered: see :class:`RunCompleteness`."""
    missing: dict[str, int] = {}
    for config in sorted(run.manifest.configs, key=lambda c: c.config_id):
        answered = {
            p.case_id for p in run.predictions.get(config.config_id, ()) if p.sample_idx == 0
        }
        missing[config.config_id] = sum(
            c.case_id not in answered for c in _scheduled_cases(run, config)
        )
    finished = run.manifest.finished_at is not None
    return RunCompleteness(
        complete=finished and not run.skipped and not run.torn_tail and not any(missing.values()),
        finished=finished,
        n_expected_cases=len(run.cases),
        n_skipped_cells=len(run.skipped),
        missing_predictions=missing,
        torn_tail=run.torn_tail,
    )


def _require_complete(run: LoadedRun, completeness: RunCompleteness, *, option: str) -> None:
    """Raise ``IncompleteRunError`` naming everything ``run`` left unanswered and ``option``,
    the :func:`score_run` setting that would accept it (``allow_incomplete=True`` for the run,
    ``allow_incomplete_dev=True`` for its dev run)."""
    if completeness.complete:
        return
    problems: list[str] = []
    if not completeness.finished:
        problems.append("its manifest has no finished_at")
    if completeness.n_skipped_cells:
        problems.append(f"{completeness.n_skipped_cells} cells are in {SKIPPED_FILE}")
    if completeness.torn_tail:
        problems.append(f"{PREDICTIONS_FILE} ends in a torn line (interrupted or still running)")
    missing = [f"{name} {n}" for name, n in completeness.missing_predictions.items() if n]
    if missing:
        problems.append(f"scheduled cases lack a repeat-0 prediction ({', '.join(missing)})")
    raise IncompleteRunError(
        f"run {run.manifest.run_id} is incomplete: {'; '.join(problems)}. Resume it, or pass "
        f"{option} to score it with every missing case INVALID"
    )


def _canonical_json(data: Any, *, indent: int | None) -> str:
    separators = None if indent is not None else (",", ":")
    return json.dumps(
        data,
        sort_keys=True,
        indent=indent,
        separators=separators,
        ensure_ascii=False,
        allow_nan=False,
    )


def _write_scores(out: Path, *, summary: str, per_case: str) -> None:
    """Write ``summary.json`` and ``per_case.jsonl`` so a summary on disk always belongs with
    the per-case records beside it.

    Both texts go to temporary files in full first, so a failed write replaces nothing. Then
    the old summary is removed, ``per_case.jsonl`` is replaced, and ``summary.json`` is replaced
    last: a failure in between leaves no summary (the run reads as unscored), never a summary
    next to another pass's per-case records.
    """
    out.mkdir(parents=True, exist_ok=True)
    temps: dict[str, Path] = {}
    try:
        for name, text in ((PER_CASE_FILE, per_case), (SUMMARY_FILE, summary)):
            fd, tmp = tempfile.mkstemp(dir=out, prefix=f".{name}.", suffix=".tmp")
            temps[name] = Path(tmp)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        (out / SUMMARY_FILE).unlink(missing_ok=True)
        os.replace(temps[PER_CASE_FILE], out / PER_CASE_FILE)
        os.replace(temps[SUMMARY_FILE], out / SUMMARY_FILE)
    finally:
        for tmp in temps.values():
            tmp.unlink(missing_ok=True)


def score_run(
    run_dir: Path | str,
    dataset_dir: Path | str | None = None,
    *,
    dev_run_dir: Path | str | None = None,
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = 0,
    allow_incomplete: bool = False,
    allow_incomplete_dev: bool | None = None,
) -> RunScores:
    """Score every configuration of a run and write ``scores/summary.json`` and
    ``scores/per_case.jsonl`` under ``run_dir``.

    The loss matrix is the one the run's experiment config declared (``loss_matrix:``).
    ``dev_run_dir``, a run of the same configurations on ``dev``, supplies the data that chooses
    each configuration's selective operating point (SPEC §12.7); an explicit ``dataset_dir``
    applies to it too, since a dataset holds both splits.

    A run that didn't answer every cell it scheduled raises ``IncompleteRunError`` unless
    ``allow_incomplete``: then each configuration is still scored on every case it was
    scheduled for, a missing one as INVALID, and ``RunScores`` records the run as incomplete.
    ``allow_incomplete_dev`` decides the same for the dev run, separately, since an operating
    point chosen on a partial dev run is a different compromise from scoring a partial run;
    None (the default) follows ``allow_incomplete``.

    Configurations are compared as :func:`comparison_pairs` describes, with the manifest's
    baselines as baselines and its ``always_escalate`` baseline, when scored, as the reference.
    The ``oracle`` baseline is scored in full and marked ``ConfigScores.ceiling``, and left out
    of the rankings, the Pareto frontier and the comparisons (SPEC §14: a pipeline check). A
    model the manifest records in ``tools`` mode also carries its tool-use metrics
    (``ConfigScores.tools``, SPEC §8.6). Baselines never do: the manifest gives them the
    experiment's mode, but they answer in-process and never call a tool.

    Whether a configuration's ``reported`` costs count as API cost is read off the manifest
    (``scoring.usage.config_reported_cost``: a billing gateway's per-call charges do, a CLI's
    estimates and other unverified figures don't), so a run recorded before the distinction
    scores the same way.

    A traced prediction's evidence claims are checked, and a traced plan naming such a record
    is simulated, against the view plus the records its tools returned (``scoring.served``),
    served from the case's world rebuilt from the run's dataset (and checked against its
    ``worlds.json``). A world is rebuilt only when some prediction cites a record its tools
    returned outside the view, or names an ID outside the view after its tools returned such
    records, once per world.
    """
    run = load_run(run_dir, dataset_dir)
    completeness = run_completeness(run)
    if not allow_incomplete:
        _require_complete(run, completeness, option="allow_incomplete=True")
    loss = LossMatrix.from_config(run.manifest.experiment.get("loss_matrix", "default"))
    settings = ScoreSettings(loss=loss, n_boot=n_boot, seed=seed)
    dev = load_run(dev_run_dir, dataset_dir) if dev_run_dir is not None else None
    dev_completeness: RunCompleteness | None = None
    if dev is not None:
        if dev.manifest.split is not Split.dev:
            raise ValueError(f"dev_run_dir is a {dev.manifest.split} run, not a dev run")
        dev_completeness = run_completeness(dev)
        if not (allow_incomplete if allow_incomplete_dev is None else allow_incomplete_dev):
            # The dev run's own setting, whatever allow_incomplete was: a complete run can be
            # scored with a partial dev run without accepting a partial run.
            _require_complete(dev, dev_completeness, option="allow_incomplete_dev=True")
    served = WorldServedIndex(run.dataset_dir)
    scored: dict[str, ScoredConfig] = {}
    unscored: list[str] = []
    for config in sorted(run.manifest.configs, key=lambda c: c.config_id):
        predictions = run.predictions.get(config.config_id, ())
        if not predictions:
            unscored.append(config.config_id)
            continue
        dev_predictions = dev.predictions.get(config.config_id, ()) if dev is not None else ()
        dev_cases = (
            _scheduled_cases(dev, _config(dev, config.config_id))
            if dev is not None and dev_predictions
            else ()
        )
        scored[config.config_id] = score_config(
            _scheduled_cases(run, config),
            predictions,
            settings=settings,
            config_id=config.config_id,
            dev_cases=dev_cases,
            dev_predictions=dev_predictions,
            ceiling=_is_ceiling(config),
            mode=_scoring_mode(config),
            served=served,
            reported_cost=config_reported_cost(run.manifest, config),
        )
    rankings: dict[Task, TaskRanking] = {}
    pareto: dict[Task, dict[str, ParetoPoint]] = {}
    for task in _DECISION_TASKS:
        task_scores = {
            name: s
            for name, entry in scored.items()
            if not entry.scores.ceiling
            and (s := _decision_scores_of(entry.scores, task)) is not None
        }
        if task_scores:
            rankings[task] = _task_ranking(task_scores)
            pareto[task] = _pareto(task_scores)
    result = RunScores(
        run_id=run.manifest.run_id,
        split=run.manifest.split,
        dataset=run.manifest.dataset,
        scorer_version=__version__,
        settings=settings,
        completeness=completeness,
        dev_run_id=dev.manifest.run_id if dev is not None else None,
        dev_completeness=dev_completeness,
        configs={name: s.scores for name, s in scored.items()},
        config_info={name: ConfigInfo.of(_config(run, name)) for name in scored},
        unscored_configs=unscored,
        rankings=rankings,
        pareto=pareto,
        comparisons=compare_configs(
            scored,
            baselines=_baselines(run, scored),
            reference=_reference(run, scored),
            n_boot=n_boot,
            alpha=settings.alpha,
            seed=seed,
        ),
    )
    lines = [
        _canonical_json(record.model_dump(mode="json"), indent=None) + "\n"
        for name in sorted(scored)
        for record in scored[name].records
    ]
    _write_scores(
        run.run_dir / SCORES_DIR,
        summary=_canonical_json(result.model_dump(mode="json"), indent=2) + "\n",
        per_case="".join(lines),
    )
    return result


def _scoring_mode(config: ConfigManifest) -> Mode:
    """The agent mode ``config`` is scored in: its manifest's, except that a baseline is always
    scored in context mode.

    The runner records the experiment's one mode on every configuration, baselines included,
    but a baseline answers in-process from the case (``providers.baselines``) and never calls a
    tool whatever that mode is. Its predictions carry no trace, so tool metrics for it would be
    a block of n/a with every case untraced, and would list it beside the tool-use models.
    """
    return "context" if config.provider == BASELINE_PROVIDER else config.mode


def _is_ceiling(config: ConfigManifest) -> bool:
    """Whether ``config`` is the ``oracle`` baseline. A model named "oracle" is not: only the
    baseline provider is known to return gold."""
    return config.provider == BASELINE_PROVIDER and config.model == CEILING_BASELINE


def _baselines(run: LoadedRun, scored: Collection[str]) -> set[str]:
    """The scored configurations that are SPEC §14 baselines."""
    return {
        c.config_id
        for c in run.manifest.configs
        if c.provider == BASELINE_PROVIDER and c.config_id in scored
    }


def _reference(run: LoadedRun, scored: Collection[str]) -> str | None:
    """The scored ``always_escalate`` baseline, if the run has one."""
    return next(
        (
            c.config_id
            for c in sorted(run.manifest.configs, key=lambda c: c.config_id)
            if c.provider == BASELINE_PROVIDER
            and c.model == REFERENCE_BASELINE
            and c.config_id in scored
        ),
        None,
    )


def _decision_scores_of(scores: ConfigScores, task: Task) -> DecisionScores | None:
    return scores.requests if task is Task.access_request else scores.reviews
