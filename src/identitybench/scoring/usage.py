"""API cost per 1,000 decisions and latency (SPEC §1, §11.4, §15).

A *decision* is one scored unit: a request, one review item, one JML plan. A review call decides
a whole campaign, so counting calls would make reviews look 30 times dearer per decision than
they are. Every repeat is a real inference and counts.

**Cost sources and bases.** ``PredictionUsage.cost_source`` says how the runner learned a
prediction's cost (``runner.cost``): ``metered`` is usage × ``pricing.yaml``, ``unmetered`` is
a real $0 (self-hosted, replay, baselines), ``reported`` is a figure the access path stated,
and ``unknown`` is unknown. ``reported`` covers different things, so the source alone
can't say whether a cost is an API charge: a billing gateway (an ``openai_compatible`` entry
on OpenRouter) reports the USD it actually charged for the call, which is as real and as exact
as a metered price, while a subscription CLI (``claude_cli``, ``grok_cli``) reports its own
API-equivalent estimate (``total_cost_usd``) of a call nobody was billed for. Which one a
configuration's reported costs are is decided per configuration from what its run manifest
records about it (:func:`config_reported_cost`: provider kind, ``base_url_env`` and cost mode),
at scoring time, so a run recorded before the distinction existed scores correctly without
being re-run. Only what the manifest positively identifies as a billing gateway is ``billed``;
a CLI kind is a CLI ``estimate``; anything else that reported a cost (a replay of a recording,
an ``openai_compatible`` endpoint not known to bill per call, a manifest without the model
entry) is ``unverified``: a figure from a source that is neither, so it is not called a charge
or a CLI's estimate. Neither ``estimate`` nor ``unverified`` is API cost: wrongly calling one a
charge would publish a notional figure as a price, while the reverse only leaves a cell n/a.

Each prediction thus has a cost *basis*: ``metered``, ``billed`` (a gateway's charge),
``unmetered``, ``estimate`` (a CLI's figure) and ``unverified`` for a recorded cost, else
``unknown``. The first three are API cost.

**Failed calls that used nothing.** A transport error (a timeout, a refused connection) that
reports no cost and used no tokens is free, as the runner books it (``_Execution._charge``).
The runner records it as metered $0 or unmetered $0 in those modes, but as ``unknown`` in
reported mode, where no figure came back. Scoring treats such a prediction (status ``error``,
cost unknown, every token count zero) as $0 under the configuration's own basis: it adds its
decisions and $0 to the headline (or to the CLI-estimate figure when the configuration's costs
are CLI estimates), is not counted as an unknown cost, and doesn't decide the basis itself. So
one leftover timeout doesn't make a gateway-billed configuration incomplete when the same
timeout costs a metered one nothing. A configuration none of whose other calls has a recorded
cost (a CLI that reports none) stays ``unknown``: a free error is no evidence of a basis.

**The headline figure** (``api_usd_per_1k_decisions``, the README column) is a configuration's
API cost per 1,000 decisions, and it is defined only when *every* prediction of the
configuration has a recorded API cost (a free failed call counting as $0, above). A
configuration with any unknown cost is incomplete, and one with some CLI-estimate (or
unverified) predictions has no API cost for those calls; computing the figure over the rest
would show the configuration as cheaper than it is (or price it off a subset), so both are
n/a, and ``cost_basis`` says why. A configuration whose recorded costs are all CLI estimates
(or all unverified) is labelled by that, with its unknown costs counted (``n_cost_unknown``),
not as incomplete: it would get no API figure even if complete, so the estimate is the reason
for n/a. CLI estimates stay out of the headline
and are listed on their own (``cli_estimate_usd_per_1k_decisions``, ``by_cost_source``). Cost
is whatever the runner recorded for each prediction, cache hits included.

Latency is per call (p50 and p95, linear interpolation), over calls that actually reached the
model: cache hits replay a stored response, and transport errors measure retries and timeouts
rather than the model.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Literal, cast

import numpy as np
from pydantic import BaseModel, ConfigDict

from identitybench.runner.cost import reports_cost
from identitybench.runner.manifest import ConfigManifest, RunManifest
from identitybench.schemas.case import Case
from identitybench.schemas.predictions import CostSource, Prediction, PredictionStatus

__all__ = [
    "API_BASES",
    "API_COST_SOURCES",
    "CLI_PROVIDERS",
    "CostBasis",
    "CostBySource",
    "HeadlineBasis",
    "ReportedCost",
    "UsageScores",
    "config_reported_cost",
    "decisions_in",
    "free_error",
    "prediction_basis",
    "reported_cost_of",
    "usage_scores",
]

API_COST_SOURCES: frozenset[CostSource] = frozenset({"metered", "unmetered"})
"""Sources whose cost is always API cost (SPEC §11.4). ``reported`` is API cost only for a
billing gateway (:data:`ReportedCost`), and ``unknown`` never is."""

ReportedCost = Literal["billed", "estimate", "unverified"]
"""What a configuration's ``reported`` costs are: ``billed``, the USD a billing gateway charged
for each call (an API cost); ``estimate``, a subscription CLI's own API-equivalent figure (not
an API cost); or ``unverified``, a figure from any other source (a replay, an endpoint not
known to bill per call), neither a known charge nor a CLI's estimate (not an API cost)."""

CostBasis = Literal["metered", "billed", "unmetered", "estimate", "unverified", "unknown"]
"""One prediction's cost basis (see the module docstring); :data:`API_BASES` are API cost."""

API_BASES: frozenset[CostBasis] = frozenset({"metered", "billed", "unmetered"})

HeadlineBasis = Literal[
    "metered",
    "billed",
    "unmetered",
    "api",
    "estimate",
    "unverified",
    "partial",
    "incomplete",
    "unknown",
    "none",
]
"""What a configuration's headline API figure rests on, or why it is n/a.

Defined: ``metered``, ``billed`` or ``unmetered`` when every prediction has that basis, ``api``
when every prediction has an API basis but not one basis (a replay mixing sources). n/a:
``estimate`` (every recorded cost is a CLI estimate; some may be unknown, ``n_cost_unknown``
counts them), ``unverified`` (likewise, for a reported cost from an unverified source),
``partial`` (some recorded costs are API costs and some are CLI estimates or unverified; some
may be unknown), ``incomplete`` (some costs unknown, every recorded one an API cost),
``unknown`` (no cost recorded at all, e.g. an unpriced API model or a CLI that reports none),
``none`` (no predictions). A free failed call (see the module docstring) never decides it."""

_GATEWAY_PROVIDERS = frozenset({"openai_compatible"})
"""Provider kinds whose reported cost, when their endpoint reports one, is a per-call charge."""

CLI_PROVIDERS: frozenset[str] = frozenset({"claude_cli", "codex_cli", "grok_cli", "cursor_cli"})
"""Subscription CLI kinds: what they report is the CLI's own estimate, never a charge (a
report may still price their recorded tokens at list price, ``report.api_equivalent``)."""

_NOT_API: frozenset[CostBasis] = frozenset({"estimate", "unverified"})
"""Recorded costs that are not API cost."""


def reported_cost_of(
    provider: str, *, base_url_env: str | None, cost_mode: str | None
) -> ReportedCost:
    """What a configuration's ``reported`` costs are, from its provider kind, ``base_url_env``
    and resolved cost mode.

    ``billed`` only for an ``openai_compatible`` entry in ``reported`` mode on an endpoint the
    runner knows reports a per-call charge (``runner.cost.reports_cost``: OpenRouter), the
    same test the runner used to decide the costs could be recorded at all. A CLI kind
    (``claude_cli``, ``grok_cli``, ``codex_cli``, ``cursor_cli``) is an ``estimate``; everything
    else (a replay, another endpoint, a baseline) is ``unverified``.
    """
    if (
        provider in _GATEWAY_PROVIDERS
        and cost_mode == "reported"
        and reports_cost(provider, base_url_env)
    ):
        return "billed"
    if provider in CLI_PROVIDERS:
        return "estimate"
    return "unverified"


def config_reported_cost(manifest: RunManifest, config: ConfigManifest) -> ReportedCost:
    """:func:`reported_cost_of` for one configuration of a run, from its manifest.

    ``base_url_env`` and the resolved cost mode live in the experiment's model entry
    (``ExperimentConfig.resolved_dict``), matched on the configuration's label and provider;
    the cost mode falls back to the manifest's ``CostSummary``. A configuration whose entry
    can't be found (a baseline, a hand-built manifest) is never ``billed``: without the endpoint
    there is no evidence its reported costs were charged. It is an ``estimate`` for a CLI kind,
    else ``unverified``.
    """
    entry = _model_entry(manifest, config)
    if entry is None:
        return reported_cost_of(config.provider, base_url_env=None, cost_mode=None)
    base_url_env = entry.get("base_url_env")
    cost_mode = entry.get("cost_mode")
    if cost_mode is None and config.config_id in manifest.cost:
        cost_mode = manifest.cost[config.config_id].cost_mode
    return reported_cost_of(
        config.provider,
        base_url_env=base_url_env if isinstance(base_url_env, str) else None,
        cost_mode=cost_mode if isinstance(cost_mode, str) else None,
    )


def _model_entry(manifest: RunManifest, config: ConfigManifest) -> dict[str, object] | None:
    """The experiment's model entry for ``config`` (labels are unique within an experiment),
    or None when the manifest records none."""
    models: object = manifest.experiment.get("models")
    if not isinstance(models, list):
        return None
    for raw in cast(list[object], models):
        if not isinstance(raw, dict):
            continue
        entry = {str(k): v for k, v in cast(dict[object, object], raw).items()}
        if entry.get("label") == config.label and entry.get("provider") == config.provider:
            return entry
    return None


def free_error(prediction: Prediction) -> bool:
    """A failed call that recorded no cost and used no tokens (a timeout, a refused
    connection): free, as the runner books it (see the module docstring)."""
    usage = prediction.usage
    return (
        prediction.status is PredictionStatus.error
        and (usage.cost_usd is None or usage.cost_source == "unknown")
        and usage.input_tokens == 0
        and usage.output_tokens == 0
        and usage.cache_read_tokens == 0
        and usage.cache_write_tokens == 0
        and usage.reasoning_tokens == 0
    )


def prediction_basis(prediction: Prediction, reported: ReportedCost) -> CostBasis:
    """One prediction's recorded cost basis, given what its configuration's reported costs
    are. A free failed call (:func:`free_error`) is ``unknown`` here; :func:`usage_scores`
    costs it at $0 under the configuration's basis."""
    usage = prediction.usage
    if usage.cost_usd is None or usage.cost_source == "unknown":
        return "unknown"
    if usage.cost_source == "reported":
        return reported
    return usage.cost_source


class CostBySource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n_predictions: int
    n_decisions: int
    n_cost_unknown: int
    """Predictions of this source with no recorded cost."""
    total_usd: float | None
    """Sum of recorded costs; None when none is recorded."""
    usd_per_1k_decisions: float | None
    """Over the predictions with a recorded cost."""


class UsageScores(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    n_predictions: int
    n_decisions: int
    n_cache_hits: int
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    by_cost_source: dict[str, CostBySource]
    """Keyed by the recorded ``cost_source``; what ``reported`` means here is
    :attr:`reported_cost`."""
    api_usd_per_1k_decisions: float | None
    """API cost (metered, gateway-billed, or a real $0) per 1,000 decisions; None unless every
    prediction has a recorded API cost, a free failed call counting as $0 (:attr:`cost_basis`
    says why). The README column."""
    n_latency: int
    latency_p50_s: float | None
    latency_p95_s: float | None
    reported_cost: ReportedCost = "estimate"
    """What this configuration's ``reported`` costs are (:func:`config_reported_cost`). A
    summary written before the distinction was recorded treated every reported cost as an
    estimate, hence the default (the report prints such a summary's headline as "n/a
    (re-score)", since its :attr:`cost_basis` is None)."""
    cost_basis: HeadlineBasis | None = None
    """What :attr:`api_usd_per_1k_decisions` rests on, or why it is n/a. None in a summary
    written before it was recorded."""
    n_cost_unknown: int = 0
    """Predictions with no recorded cost, whatever their source, free failed calls
    (:attr:`n_free_errors`) aside."""
    cli_estimate_usd_per_1k_decisions: float | None = None
    """CLI-estimate cost per 1,000 decisions, over the estimate predictions with a recorded
    cost (and, when every recorded cost is an estimate, the free failed calls at $0); None when
    there are none. Never part of the headline."""
    n_free_errors: int = 0
    """Failed calls that recorded no cost and used no tokens, costed at $0 (module docstring)."""


def decisions_in(case: Case) -> int:
    """Scored decisions one prediction for ``case`` makes: a campaign's item count, else 1."""
    campaign = case.view.campaign
    return len(campaign.items) if campaign is not None else 1


def _per_1k(total: float, decisions: int) -> float | None:
    return 1000.0 * total / decisions if decisions else None


def _headline_basis(bases: Sequence[CostBasis], *, n_free: int = 0) -> HeadlineBasis:
    """The headline basis over the predictions' recorded bases (free failed calls left out:
    ``n_free`` only tells an all-free configuration from an empty one). A non-API recorded cost
    (a CLI estimate, an unverified figure) is the reason for n/a before any unknown cost is."""
    kinds = set(bases)
    if not kinds:
        return "unknown" if n_free else "none"
    recorded = kinds - {"unknown"}
    if not recorded:
        return "unknown"
    if recorded & _NOT_API:
        if len(recorded) == 1:
            (only,) = recorded
            return only
        return "partial"
    if "unknown" in kinds:
        return "incomplete"
    if len(kinds) == 1:
        (only,) = kinds
        return only
    return "api"


def usage_scores(
    predictions: Sequence[Prediction],
    cases: Mapping[str, Case],
    *,
    reported: ReportedCost = "estimate",
) -> UsageScores:
    """Usage over every prediction of one configuration (all repeats), in canonical order.

    ``reported`` is what the configuration's ``reported`` costs are
    (:func:`config_reported_cost`); the default treats them as CLI estimates, never an API
    cost, the safe reading when the configuration's endpoint is unknown. A free failed call
    (:func:`free_error`) costs $0 under the configuration's basis (module docstring).
    """
    decisions = {p.case_id: decisions_in(cases[p.case_id]) for p in predictions}
    by_source: dict[str, CostBySource] = {}
    for source in sorted({p.usage.cost_source for p in predictions}):
        group = [p for p in predictions if p.usage.cost_source == source]
        known = [p for p in group if p.usage.cost_usd is not None]
        total = math.fsum(p.usage.cost_usd or 0.0 for p in known)
        by_source[source] = CostBySource(
            n_predictions=len(group),
            n_decisions=sum(decisions[p.case_id] for p in group),
            n_cost_unknown=len(group) - len(known),
            total_usd=total if known else None,
            usd_per_1k_decisions=_per_1k(total, sum(decisions[p.case_id] for p in known)),
        )
    free = [free_error(p) for p in predictions]
    bases: list[CostBasis] = [prediction_basis(p, reported) for p in predictions]
    recorded_bases: list[CostBasis] = [b for b, f in zip(bases, free, strict=True) if not f]
    headline = _headline_basis(recorded_bases, n_free=sum(free))
    api_cost: float | None = None
    if headline in ("metered", "billed", "unmetered", "api"):
        # Free failed calls are recorded as unknown with no cost: they add $0 here.
        api_cost = _per_1k(
            math.fsum(p.usage.cost_usd or 0.0 for p in predictions),
            sum(decisions[p.case_id] for p in predictions),
        )
    estimates = [
        p
        for p, b, f in zip(predictions, bases, free, strict=True)
        if b == "estimate" or (f and headline == "estimate")
    ]
    estimate_cost = _per_1k(
        math.fsum(p.usage.cost_usd or 0.0 for p in estimates),
        sum(decisions[p.case_id] for p in estimates),
    )
    latencies = [
        p.latency_s
        for p in predictions
        if not p.cache_hit and p.status is not PredictionStatus.error
    ]
    p50 = p95 = None
    if latencies:
        p50, p95 = (float(q) for q in np.percentile(np.asarray(latencies), [50.0, 95.0]))
    return UsageScores(
        n_predictions=len(predictions),
        n_decisions=sum(decisions[p.case_id] for p in predictions),
        n_cache_hits=sum(p.cache_hit for p in predictions),
        input_tokens=sum(p.usage.input_tokens for p in predictions),
        output_tokens=sum(p.usage.output_tokens for p in predictions),
        reasoning_tokens=sum(p.usage.reasoning_tokens for p in predictions),
        by_cost_source=by_source,
        api_usd_per_1k_decisions=api_cost,
        n_latency=len(latencies),
        latency_p50_s=p50,
        latency_p95_s=p95,
        reported_cost=reported,
        cost_basis=headline,
        n_cost_unknown=recorded_bases.count("unknown"),
        cli_estimate_usd_per_1k_decisions=estimate_cost if estimates else None,
        n_free_errors=sum(free),
    )
