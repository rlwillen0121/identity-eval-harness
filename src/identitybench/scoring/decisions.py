"""Decision scoring for requests and review items (SPEC §12.1–§12.3, §12.5, §12.8).

Every metric is a ratio of sums over the task's units with a bootstrap interval at the §12.8
unit: base request cases are independent and resampled one by one; review items are resampled
by campaign. A metric whose denominator is 0 is n/a (SPEC §12.2).

Labels follow ``schemas.enums.label_of``: A (APPROVE or CERTIFY), N (DENY or REVOKE), E
(ESCALATE) and INVALID. INVALID is never an approval: it is outside the WAR numerator and the
unsafe-approval denominator, and the loss matrix prices it as routing plus overhead.

**Rare events** (SPEC §12.8). Base request cases are independent, so request WAR (and its
high-risk restriction) carries an exact one-sided Clopper–Pearson bound whenever fewer than
``RARE_EVENT_THRESHOLD`` wrongful approvals were observed, where the bootstrap would collapse
toward [0, 0]. Review WAR gets only the campaign bootstrap; the separate campaign-level estimand
(the share of campaigns with any wrongful certification) carries the exact bound.

**Primary metrics** (SPEC §12.8): NRC, WAR, correct automation and grounded accuracy. Every
other number here, including every breakdown, is exploratory.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict

from identitybench.schemas.enums import Label, Split, Task, Tier
from identitybench.scoring.grounding import GroundingScores, grounding_scores
from identitybench.scoring.loss import LossMatrix, Mix, mix_weights
from identitybench.scoring.metrics import (
    CAMPAIGN_UNITS,
    REQUEST_UNITS,
    REVIEW_UNITS,
    Metric,
    Resampling,
    UnitSpec,
    cp_upper_bound,
    mean_metric,
    ratio_metric,
)
from identitybench.scoring.operating import CalibrationScores, calibration_scores
from identitybench.scoring.settings import ScoreSettings
from identitybench.scoring.stats import RARE_EVENT_THRESHOLD
from identitybench.scoring.units import NO_PREDICTION, DecisionUnit, units_by_case

__all__ = [
    "FAMILY_TAGS",
    "POSITION_THIRDS",
    "Breakdown",
    "CampaignStats",
    "CostScores",
    "DecisionMetrics",
    "DecisionScores",
    "ReviewExtras",
    "SensitivityPoint",
    "SubsetAccuracy",
    "campaign_stats",
    "decision_scores",
    "escalation_costs",
    "unit_costs",
]

FAMILY_TAGS: tuple[str, ...] = ("life", "elig", "sod", "mit", "appr", "temp", "just", "hyg", "jml")
"""SPEC §10.2 rule-family tags, in policy order (the family × tier heatmap rows)."""

POSITION_THIRDS: tuple[str, ...] = ("first", "middle", "last")

_DECISION_LABELS: tuple[Label, ...] = (Label.A, Label.N, Label.E)
_PREDICTED_LABELS: tuple[Label, ...] = (Label.A, Label.N, Label.E, Label.INVALID)


def _flag(value: bool) -> float:
    return 1.0 if value else 0.0


def _flags(units: Sequence[DecisionUnit], test: Callable[[DecisionUnit], bool]) -> list[float]:
    return [_flag(test(u)) for u in units]


class _Rater:
    """Builds metrics over one fixed list of units, so every metric shares clusters and units."""

    def __init__(self, units: Sequence[DecisionUnit], spec: UnitSpec, resampling: Resampling):
        self.units = units
        self.spec = spec
        self.resampling = resampling
        self.clusters = [u.cluster for u in units] if spec.cluster is not None else None

    def rate(
        self,
        hit: Callable[[DecisionUnit], bool],
        among: Callable[[DecisionUnit], bool] | None = None,
        *,
        primary: bool = False,
        rare_event: tuple[str, str] | None = None,
    ) -> Metric:
        """``#(among ∧ hit) / #(among)``; ``rare_event`` = (estimand, failure event) attaches
        the exact bound when fewer than ``RARE_EVENT_THRESHOLD`` events were observed."""
        den = _flags(self.units, among) if among is not None else [1.0] * len(self.units)
        num = [d * _flag(hit(u)) for d, u in zip(den, self.units, strict=True)]
        bound = None
        if rare_event is not None and sum(num) < RARE_EVENT_THRESHOLD:
            bound = cp_upper_bound(
                round(sum(num)),
                round(sum(den)),
                estimand=rare_event[0],
                units=self.spec.units,
                failure_event=rare_event[1],
                alpha=self.resampling.alpha,
            )
        return ratio_metric(
            num, den, self.clusters, self.spec, self.resampling, primary=primary, bound=bound
        )

    def ratio(self, num: Sequence[float], den: Sequence[float], *, primary: bool = False) -> Metric:
        return ratio_metric(num, den, self.clusters, self.spec, self.resampling, primary=primary)

    def mean(self, values: Sequence[float]) -> Metric:
        return mean_metric(values, self.clusters, self.spec, self.resampling)


class DecisionMetrics(BaseModel):
    """SPEC §12.2."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accuracy: Metric
    """P(correct): label and, for APPROVE, effective duration."""
    label_accuracy: Metric
    war: Metric
    """#(pred = A ∧ gold ≠ A) / #(gold ≠ A)."""
    high_risk_war: Metric
    unsafe_approval_share: Metric
    """(wrongful approvals + over-duration approvals) / #(pred = A)."""
    wdr: Metric
    """#(pred = N ∧ gold = A) / #(gold = A)."""
    under_escalation: Metric
    over_escalation: Metric
    correct_automation: Metric
    """#(correct ∧ pred ∈ {A, N}) / N."""
    automatable_coverage: Metric
    over_duration_rate: Metric | None
    """Over gold-APPROVE requests. None (n/a) for reviews: review items have no durations, so
    the rate would read as a misleading 0% rather than as not applying."""
    over_restriction_rate: Metric | None
    """Over gold-APPROVE requests; None (n/a) for reviews, like ``over_duration_rate``."""
    invalid_rate: Metric


def _decision_metrics(rater: _Rater, task: Task) -> DecisionMetrics:
    """SPEC §12.2 over the rater's units. Requests (independent cases) get the rare-event bound
    on WAR and the duration rates; review items get neither (§12.8, §8.2)."""
    requests = task is Task.access_request
    war_bound = (
        (
            "WAR: share of gold non-APPROVE cases predicted APPROVE",
            "predicted APPROVE on a case whose gold isn't APPROVE",
        )
        if requests
        else None
    )
    high_risk_bound = (
        (
            "high-risk WAR: share of gold non-APPROVE high- or critical-tier cases predicted "
            "APPROVE",
            "predicted APPROVE on a high- or critical-tier case whose gold isn't APPROVE",
        )
        if requests
        else None
    )
    return DecisionMetrics(
        accuracy=rater.rate(lambda u: u.correct),
        label_accuracy=rater.rate(lambda u: u.pred is u.gold),
        war=rater.rate(
            lambda u: u.pred is Label.A,
            lambda u: u.gold is not Label.A,
            primary=True,
            rare_event=war_bound,
        ),
        high_risk_war=rater.rate(
            lambda u: u.pred is Label.A,
            lambda u: u.gold is not Label.A and u.high_risk,
            rare_event=high_risk_bound,
        ),
        unsafe_approval_share=rater.rate(
            lambda u: u.gold is not Label.A or u.over_duration, lambda u: u.pred is Label.A
        ),
        wdr=rater.rate(lambda u: u.pred is Label.N, lambda u: u.gold is Label.A),
        under_escalation=rater.rate(
            lambda u: u.pred in (Label.A, Label.N), lambda u: u.gold is Label.E
        ),
        over_escalation=rater.rate(lambda u: u.pred is Label.E, lambda u: u.gold is not Label.E),
        correct_automation=rater.rate(lambda u: u.automated_correct, primary=True),
        automatable_coverage=rater.rate(
            lambda u: u.automated_correct, lambda u: u.gold in (Label.A, Label.N)
        ),
        over_duration_rate=(
            rater.rate(lambda u: u.over_duration, lambda u: u.gold is Label.A) if requests else None
        ),
        over_restriction_rate=(
            rater.rate(lambda u: u.over_restriction, lambda u: u.gold is Label.A)
            if requests
            else None
        ),
        invalid_rate=rater.rate(lambda u: u.pred is Label.INVALID),
    )


# --------------------------------------------------------------------------------------------
# Risk cost (SPEC §12.3)
# --------------------------------------------------------------------------------------------


def unit_costs(units: Sequence[DecisionUnit], loss: LossMatrix) -> list[float]:
    """Each unit's total cost under ``loss``, routing included."""
    return [
        loss.cost(
            u.gold,
            u.pred,
            u.weight,
            over_duration=u.over_duration,
            over_restriction=u.over_restriction,
        )
        for u in units
    ]


def escalation_costs(units: Sequence[DecisionUnit], loss: LossMatrix) -> list[float]:
    """What ``always_escalate`` costs on each unit under ``loss``: NRC's normalizer."""
    return [loss.cost(u.gold, Label.E, u.weight) for u in units]


class SensitivityPoint(BaseModel):
    """Mean cost and NRC (re-normalized under this setting) for one sensitivity setting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    setting: str
    wrongful_approval_multiplier: float
    """× w: the DENY→APPROVE cell under this setting (10 in the default matrix)."""
    escalation_approval: float
    """× w: the ESCALATE→APPROVE cell under this setting. It is the other wrongful approval, so
    it scales with the multiplier and keeps its configured ratio to the DENY→APPROVE cell (5:10
    by default, so 1.5, 5 and 15 for multipliers 3, 10 and 30); see ``LossMatrix.with_setting``."""
    routing: float
    mix: Mix
    mean_cost: float | None
    always_escalate_mean_cost: float | None
    nrc: float | None
    """None when the mix can't be formed (a class it weights has no cases) or no units."""


class CostScores(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mean_cost: Metric
    always_escalate_mean_cost: float | None
    nrc: Metric
    """Mean cost over ``always_escalate``'s mean cost under the same settings (primary)."""
    sensitivity: list[SensitivityPoint]


def _weighted_mean(values: Sequence[float], weights: Sequence[float]) -> float | None:
    total = math.fsum(weights)
    if total == 0.0:
        return None
    return math.fsum(v * w for v, w in zip(values, weights, strict=True)) / total


def _cost_scores(
    rater: _Rater, task: Task, settings: ScoreSettings
) -> tuple[CostScores, list[float]]:
    """Cost scores plus each unit's cost under the configured matrix (for breakdowns)."""
    units = rater.units
    costs = unit_costs(units, settings.loss)
    references = escalation_costs(units, settings.loss)
    golds = [u.gold for u in units]
    points: list[SensitivityPoint] = []
    for setting in settings.grid(task):
        matrix = settings.loss.with_setting(setting)
        target = settings.production_mix.get(task) if setting.mix == "production" else None
        weights = mix_weights(golds, target)
        mean_cost = reference = nrc = None
        if weights is not None:
            # fsum is correctly rounded, so equal cost vectors give bit-identical sums and an
            # ``always_escalate`` NRC of exactly 1 under every setting.
            mean_cost = _weighted_mean(unit_costs(units, matrix), weights)
            reference = _weighted_mean(escalation_costs(units, matrix), weights)
            if mean_cost is not None and reference:
                nrc = mean_cost / reference
        points.append(
            SensitivityPoint(
                setting=setting.key,
                wrongful_approval_multiplier=matrix.wrongful_approval,
                escalation_approval=matrix.escalation_approval,
                routing=matrix.routing,
                mix=setting.mix,
                mean_cost=mean_cost,
                always_escalate_mean_cost=reference,
                nrc=nrc,
            )
        )
    scores = CostScores(
        mean_cost=rater.mean(costs),
        always_escalate_mean_cost=math.fsum(references) / len(references) if references else None,
        nrc=rater.ratio(costs, references, primary=True),
        sensitivity=points,
    )
    return scores, costs


# --------------------------------------------------------------------------------------------
# Breakdowns (exploratory)
# --------------------------------------------------------------------------------------------


class Breakdown(BaseModel):
    """Exploratory results on a subset (a tier or a tag)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_units: int
    n_clusters: int
    small_n: bool
    """Fewer than ``ScoreSettings.small_n`` (30) clusters: the n of SPEC §10.2's flag is what the
    interval resamples. For requests that is the cases; for reviews it is the campaigns, so a
    tag spanning 60 items of two campaigns is flagged, since its interval rests on two."""
    accuracy: Metric
    war: Metric
    correct_automation: Metric
    mean_cost: Metric


def _breakdown(
    units: Sequence[DecisionUnit],
    costs: Sequence[float],
    spec: UnitSpec,
    settings: ScoreSettings,
) -> Breakdown:
    rater = _Rater(units, spec, settings.resampling)
    n_clusters = len({u.cluster for u in units})
    return Breakdown(
        n_units=len(units),
        n_clusters=n_clusters,
        small_n=n_clusters < settings.small_n,
        accuracy=rater.rate(lambda u: u.correct),
        war=rater.rate(lambda u: u.pred is Label.A, lambda u: u.gold is not Label.A),
        correct_automation=rater.rate(lambda u: u.automated_correct),
        mean_cost=rater.mean(costs),
    )


class SubsetAccuracy(BaseModel):
    """Exploratory accuracy on a small subset: one cell of the family × tier heatmap, or one
    third of the review lists. Flagged like :class:`Breakdown`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_units: int
    """Units in the subset (the accuracy's denominator)."""
    n_clusters: int
    """Clusters with a unit in the subset: cases for requests, campaigns for review items."""
    small_n: bool
    """Fewer than ``ScoreSettings.small_n`` clusters (SPEC §10.2), counted as in
    :class:`Breakdown`: what the interval resamples."""
    accuracy: Metric


def _subset_accuracy(
    subset: Sequence[DecisionUnit], accuracy: Metric, settings: ScoreSettings
) -> SubsetAccuracy:
    """Wrap ``accuracy``, computed over ``subset``, with the subset's n and small-n flag."""
    n_clusters = len({u.cluster for u in subset})
    return SubsetAccuracy(
        n_units=len(subset),
        n_clusters=n_clusters,
        small_n=n_clusters < settings.small_n,
        accuracy=accuracy,
    )


def _subset(
    units: Sequence[DecisionUnit], costs: Sequence[float], keep: Callable[[DecisionUnit], bool]
) -> tuple[list[DecisionUnit], list[float]]:
    pairs = [(u, c) for u, c in zip(units, costs, strict=True) if keep(u)]
    return [u for u, _ in pairs], [c for _, c in pairs]


def _tiers(units: Sequence[DecisionUnit]) -> list[Tier]:
    present = {u.tier for u in units}
    return [t for t in Tier if t in present]


# --------------------------------------------------------------------------------------------
# Review campaign extras (SPEC §12.5)
# --------------------------------------------------------------------------------------------


class CampaignStats(BaseModel):
    """One campaign's review extras (written per case)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_items: int
    accuracy: float
    wrongful_certifications: int
    gold_non_certify: int
    wrongful_certification_rate: float | None
    must_revoke_hits: int
    gold_revoke: int
    must_revoke_recall: float | None
    certification_rate: float
    gold_certification_rate: float


def campaign_stats(units: Sequence[DecisionUnit]) -> CampaignStats:
    """Per-campaign wrongful certification rate and must-revoke recall (SPEC §12.5)."""
    if not units:
        raise ValueError("a campaign has at least one item")
    n = len(units)
    wrongful = sum(u.wrongful_approval for u in units)
    non_certify = sum(u.gold is not Label.A for u in units)
    hits = sum(u.pred is Label.N and u.gold is Label.N for u in units)
    revoke = sum(u.gold is Label.N for u in units)
    return CampaignStats(
        n_items=n,
        accuracy=sum(u.correct for u in units) / n,
        wrongful_certifications=wrongful,
        gold_non_certify=non_certify,
        wrongful_certification_rate=wrongful / non_certify if non_certify else None,
        must_revoke_hits=hits,
        gold_revoke=revoke,
        must_revoke_recall=hits / revoke if revoke else None,
        certification_rate=sum(u.pred is Label.A for u in units) / n,
        gold_certification_rate=sum(u.gold is Label.A for u in units) / n,
    )


class ReviewExtras(BaseModel):
    """SPEC §12.5. Item-level review WAR (the wrongful certification rate) is
    ``DecisionMetrics.war``; these add the per-campaign view and the rubber-stamp indicators."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_campaigns: int
    campaign_wrongful_certification_rate: Metric
    """Mean over campaigns (with a gold non-CERTIFY item) of each campaign's own WAR."""
    campaigns_with_wrongful_certification: Metric
    """Share of campaigns with any wrongful certification; carries the exact bound (§12.8)."""
    must_revoke_recall: Metric
    """#(pred = REVOKE ∧ gold = REVOKE) / #(gold = REVOKE), items resampled by campaign."""
    campaign_must_revoke_recall: Metric
    """Mean over campaigns (with a gold REVOKE item) of each campaign's recall."""
    accuracy_by_position: dict[str, SubsetAccuracy]
    """Accuracy on the first, middle and last third of each campaign's list. Each third's n is
    its items and the campaigns they come from; the accuracy resamples every campaign, so a
    campaign too short to have items in a third adds nothing to that third's numbers."""
    certification_rate: Metric
    gold_certification_rate: Metric
    certification_excess: Metric
    """Mean per item of 1[pred = CERTIFY] − 1[gold = CERTIFY]: positive means rubber-stamping."""


def _third(unit: DecisionUnit) -> str:
    assert unit.position is not None and unit.campaign_size is not None  # review items only
    return POSITION_THIRDS[min(2, 3 * unit.position // unit.campaign_size)]


def _per_campaign(values: Sequence[float | None], resampling: Resampling) -> Metric:
    """Mean of a per-campaign rate over the campaigns where it is defined."""
    return ratio_metric(
        [v or 0.0 for v in values],
        [_flag(v is not None) for v in values],
        None,
        CAMPAIGN_UNITS,
        resampling,
    )


def _review_extras(rater: _Rater, settings: ScoreSettings) -> ReviewExtras:
    campaigns = [campaign_stats(items) for items in units_by_case(rater.units).values()]
    any_wrongful = [_flag(c.wrongful_certifications > 0) for c in campaigns]
    n = len(campaigns)
    bound = cp_upper_bound(
        round(sum(any_wrongful)),
        n,
        estimand="share of campaigns with any wrongful certification",
        units=CAMPAIGN_UNITS.units,
        failure_event="a campaign with at least one gold non-CERTIFY item predicted CERTIFY",
        alpha=settings.alpha,
    )
    return ReviewExtras(
        n_campaigns=n,
        campaign_wrongful_certification_rate=_per_campaign(
            [c.wrongful_certification_rate for c in campaigns], settings.resampling
        ),
        campaigns_with_wrongful_certification=ratio_metric(
            any_wrongful, [1.0] * n, None, CAMPAIGN_UNITS, settings.resampling, bound=bound
        ),
        must_revoke_recall=rater.rate(lambda u: u.pred is Label.N, lambda u: u.gold is Label.N),
        campaign_must_revoke_recall=_per_campaign(
            [c.must_revoke_recall for c in campaigns], settings.resampling
        ),
        accuracy_by_position={
            third: _subset_accuracy(
                [u for u in rater.units if _third(u) == third],
                rater.rate(lambda u: u.correct, lambda u, t=third: _third(u) == t),
                settings,
            )
            for third in POSITION_THIRDS
        },
        certification_rate=rater.rate(lambda u: u.pred is Label.A),
        gold_certification_rate=rater.rate(lambda u: u.gold is Label.A),
        certification_excess=rater.mean(
            [_flag(u.pred is Label.A) - _flag(u.gold is Label.A) for u in rater.units]
        ),
    )


# --------------------------------------------------------------------------------------------
# Task scores
# --------------------------------------------------------------------------------------------


class DecisionScores(BaseModel):
    """Everything SPEC §12 and §13.1 report for one decision task (requests or reviews)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task: Task
    units: str
    cluster: str | None
    n_units: int
    n_clusters: int
    n_cases: int
    n_no_prediction: int
    """Cases the configuration has no prediction for (scored INVALID)."""
    metrics: DecisionMetrics
    cost: CostScores
    grounding: GroundingScores
    confusion: dict[Label, dict[Label, int]]
    """gold label (A, N, E) -> predicted label (A, N, E, INVALID) -> units."""
    by_tier: dict[Tier, Breakdown]
    by_tag: dict[str, Breakdown]
    family_by_tier: dict[str, dict[Tier, SubsetAccuracy]]
    """Accuracy by rule-family tag × tier (the report's heatmap), each cell with its n and
    small-n flag: a cell crosses a tag with a tier, so its n is smaller still than ``by_tag``'s."""
    review: ReviewExtras | None = None
    calibration: CalibrationScores | None = None


_TASK_UNITS: Mapping[Task, UnitSpec] = {
    Task.access_request: REQUEST_UNITS,
    Task.access_review: REVIEW_UNITS,
}


def decision_scores(
    units: Sequence[DecisionUnit],
    task: Task,
    settings: ScoreSettings,
    *,
    dev_units: Sequence[DecisionUnit] | None = None,
    evaluated_on: Split | None = None,
) -> DecisionScores:
    """Score one task's primary units (base request cases, or items of base campaigns).

    ``units`` must be in canonical order (cases by ID, items by position). ``dev_units`` (the
    same task on ``dev``) choose the selective operating point measured on ``units``.
    """
    if any(u.task is not task for u in units):
        raise ValueError(f"every unit must be a {task} unit")
    spec = _TASK_UNITS[task]
    rater = _Rater(units, spec, settings.resampling)
    cost, costs = _cost_scores(rater, task, settings)
    confusion = {g: {p: 0 for p in _PREDICTED_LABELS} for g in _DECISION_LABELS}
    for u in units:
        confusion[u.gold][u.pred] += 1
    tags = sorted({t for u in units for t in u.tags})
    family_by_tier: dict[str, dict[Tier, SubsetAccuracy]] = {}
    for family in (f for f in FAMILY_TAGS if f in tags):
        family_units = [u for u in units if family in u.tags]
        cells: dict[Tier, SubsetAccuracy] = {}
        for tier in _tiers(family_units):
            cell = [u for u in family_units if u.tier is tier]
            accuracy = _Rater(cell, spec, settings.resampling).rate(lambda u: u.correct)
            cells[tier] = _subset_accuracy(cell, accuracy, settings)
        family_by_tier[family] = cells
    cases = units_by_case(units)
    return DecisionScores(
        task=task,
        units=spec.units,
        cluster=spec.cluster,
        n_units=len(units),
        n_clusters=len(cases),
        n_cases=len(cases),
        n_no_prediction=sum(items[0].status == NO_PREDICTION for items in cases.values()),
        metrics=_decision_metrics(rater, task),
        cost=cost,
        grounding=grounding_scores(units, rater.clusters, spec, settings.resampling, primary=True),
        confusion=confusion,
        by_tier={
            tier: _breakdown(*_subset(units, costs, lambda u, t=tier: u.tier is t), spec, settings)
            for tier in _tiers(units)
        },
        by_tag={
            tag: _breakdown(*_subset(units, costs, lambda u, t=tag: t in u.tags), spec, settings)
            for tag in tags
        },
        family_by_tier=family_by_tier,
        review=_review_extras(rater, settings) if task is Task.access_review else None,
        calibration=calibration_scores(
            units,
            dev_units=dev_units,
            target=settings.war_target,
            alpha=settings.alpha,
            certify=task is Task.access_request,
            evaluated_on=evaluated_on,
        ),
    )
