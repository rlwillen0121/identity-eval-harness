"""API-equivalent cost: a subscription CLI's recorded tokens at its model's dated list price
(SPEC §11.4, §15).

A subscription CLI (``claude_cli``, ``codex_cli``, ``grok_cli``, ``cursor_cli``) bills nobody
per call, so its configurations have no API cost: the scorer's headline figure is n/a for them
(``scoring.usage``), and a CLI's own figure, where it reports one, is kept apart as an
estimate. That would leave "which model is cheapest" unanswerable for exactly the models people
run through a subscription. The *API-equivalent* answers it: every prediction's recorded tokens
priced at the model's row in the dated pricing table (``pricing.yaml``, the table metered API
cost uses). It is not a charge, so every surface labels it and draws it apart from billed
(gateway) and metered costs, and it never enters the scorer's headline figure. It is priced
here, at report time, because it is a price applied to recorded usage, not a score: a run
scored before a model had a price row gets the figure without being re-scored.

**What is priced.** Every prediction of the configuration (every repeat; a prediction replayed
from the response cache carries the usage its call recorded, as for every cost): uncached input,
cache reads and cache writes at the row's cache rates, and output, which includes reasoning
tokens (``providers.base.Usage``). The figure per 1,000 decisions divides by the scorer's
decision count (``UsageScores.n_decisions``), so it is on the same footing as the API column. It
is computed only when the predictions read back are exactly the ones the scorer counted;
otherwise the run changed since it was scored, and the configuration gets no figure.

**Cache-write lifetime.** ``Usage`` has one cache-write counter. ``claude_cli`` writes its prompt
to the 1-hour cache: on every call of the v1 leaderboard run its own cost estimate equals the
recorded tokens at list price with cache writes at the 1-hour rate, so its writes are priced at
that rate (:data:`CACHE_TTL`). Other CLIs are priced at the 5-minute rate (``codex_cli``
records no cache writes at all). Writing every prompt to a 1-hour cache costs twice the input
rate, more than an API caller sending one-off prompts would pay, so the figure keeps the share
spent on cache writes (:attr:`ApiEquivalent.cache_write_usd`) for the report to state.

**Long-context tiers.** A row's long-context tier applies per model request, by that request's
prompt size. A CLI call can make several requests inside one prediction (the CLI's internal
turns), and ``Usage`` holds their sum, not each request's size. A prediction whose summed prompt
is at or under the threshold is exact: every request in it was under it too. One whose summed
prompt passes it can't be told apart from a single long request, so it is priced at the base
rates and counted (:attr:`ApiEquivalent.n_over_threshold`), and :attr:`ApiEquivalent.tier_usd`
is what the configuration would cost if every such prediction were billed at the tier, an upper
bound the report states beside the figure.

**Cross-check, and what it can't catch.** Where the CLI also reported its own estimate for every
prediction, the sum is kept (:attr:`ApiEquivalent.cli_estimate_usd`) so the report can say
whether the two agree. Agreement shows the CLI's tokens and its own price list are consistent;
neither figure can include tokens the CLI never reports.

**The CLI's own prompt.** A CLI's recorded prompt includes what the CLI adds to every call (its
own instructions, and the prompts of any internal turns), which a direct API call with the same
rendered prompt wouldn't send. Where the run also has API configurations (which send only the
rendered prompt), :func:`api_equivalents` estimates that overhead: for each API configuration,
the median over the predictions both made (same case, same repeat) of how many more prompt
tokens the CLI recorded, kept as the lowest and highest of those medians
(:attr:`ApiEquivalent.prompt_gap`). Each model counts tokens with its own tokenizer, so it is an
estimate, and the report states it as a range beside the figure, which runs high by up to about
that share of its prompt cost.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from identitybench.providers.base import Usage
from identitybench.runner.cost import CacheTtl, ModelPrice, PricingTable
from identitybench.schemas.predictions import PredictionUsage
from identitybench.scoring.summary import RunScores
from identitybench.scoring.usage import CLI_PROVIDERS

__all__ = [
    "CACHE_TTL",
    "ApiEquivalent",
    "PredictionKey",
    "api_equivalent",
    "api_equivalents",
    "prompt_gap",
    "prompt_tokens",
    "read_keyed_usages",
    "read_usages",
]

CACHE_TTL: Mapping[str, CacheTtl] = MappingProxyType({"claude_cli": "1h"})
"""The cache-write lifetime a CLI kind's writes are priced at (see the module docstring); any
other CLI kind is priced at the 5-minute rate."""

_PER_TOKEN = 1_000_000

PredictionKey = tuple[str, int]
"""A prediction's case ID and repeat index (``sample_idx``)."""


@dataclass(frozen=True, slots=True)
class ApiEquivalent:
    """One configuration's recorded tokens at its model's list price (see the module
    docstring)."""

    model: str
    provider: str
    as_of: date
    """The price row's date."""
    source: str
    """Where the price row was taken from."""
    table_sha256: str
    """The pricing table's sha256 (of the file's bytes)."""
    table_name: str
    """The pricing file's name (``pricing.yaml``), or ``in-memory table``."""
    cache_ttl: CacheTtl
    input_rate: float
    """USD per 1M uncached input tokens (base rates)."""
    cache_write_rate: float
    """USD per 1M cache-write tokens at :attr:`cache_ttl` (base rates)."""
    n_predictions: int
    n_decisions: int
    total_usd: float
    cache_write_usd: float
    """The part of :attr:`total_usd` spent on cache writes."""
    n_over_threshold: int
    """Predictions whose summed prompt passes the row's long-context threshold, priced at the
    base rates."""
    tier_usd: float | None
    """The total if every one of those were billed at the long-context tier, an upper bound;
    None when there are none."""
    cli_estimate_usd: float | None
    """The CLI's own estimate summed over the same predictions, when it reported one for every
    prediction; None otherwise."""
    prompt_tokens: int = 0
    """Prompt tokens over every prediction: uncached input, cache reads and cache writes."""
    prompt_gap: tuple[float, float] | None = None
    """How many more prompt tokens per prediction the CLI recorded than the run's API
    configurations did on the same predictions: the lowest and highest, over those
    configurations, of the median paired difference (see the module docstring); None without
    an API configuration to compare with."""
    n_peers: int = 0
    """The API configurations :attr:`prompt_gap` compares with."""

    @property
    def usd_per_1k_decisions(self) -> float | None:
        return _per_1k(self.total_usd, self.n_decisions)

    @property
    def mean_prompt_tokens(self) -> float | None:
        return self.prompt_tokens / self.n_predictions if self.n_predictions else None

    @property
    def prompt_gap_share(self) -> tuple[float, float] | None:
        """:attr:`prompt_gap` as shares of the mean prompt per prediction."""
        mean = self.mean_prompt_tokens
        if self.prompt_gap is None or not mean:
            return None
        return self.prompt_gap[0] / mean, self.prompt_gap[1] / mean

    @property
    def tier_usd_per_1k_decisions(self) -> float | None:
        return None if self.tier_usd is None else _per_1k(self.tier_usd, self.n_decisions)

    @property
    def cache_write_share(self) -> float | None:
        """The share of :attr:`total_usd` spent on cache writes (None for a $0 total)."""
        return self.cache_write_usd / self.total_usd if self.total_usd > 0 else None

    @property
    def estimate_gap(self) -> float | None:
        """How far the figure is from the CLI's own estimate, as a share of the estimate
        (None without an estimate, or for a $0 one)."""
        if self.cli_estimate_usd is None or self.cli_estimate_usd <= 0:
            return None
        return abs(self.total_usd - self.cli_estimate_usd) / self.cli_estimate_usd


def _per_1k(total: float, decisions: int) -> float | None:
    return 1000.0 * total / decisions if decisions else None


def prompt_tokens(u: PredictionUsage) -> int:
    """A prediction's prompt tokens: uncached input, cache reads and cache writes."""
    return u.input_tokens + u.cache_read_tokens + u.cache_write_tokens


def prompt_gap(
    cli: Mapping[PredictionKey, int], peers: Iterable[Mapping[PredictionKey, int]]
) -> tuple[tuple[float, float], int] | None:
    """How many more prompt tokens per prediction ``cli`` recorded than each of ``peers`` (each
    a configuration's prompt tokens by prediction): for each peer, the median of the paired
    differences over the predictions both recorded a prompt for; returns the lowest and
    highest of those medians and how many peers had a shared prediction, or None for none."""
    medians: list[float] = []
    for peer in peers:
        diffs = [
            tokens - peer[key] for key, tokens in cli.items() if tokens > 0 and peer.get(key, 0) > 0
        ]
        if diffs:
            medians.append(float(statistics.median(diffs)))
    if not medians:
        return None
    return (min(medians), max(medians)), len(medians)


def _usage(u: PredictionUsage) -> Usage:
    return Usage(
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_read_tokens=u.cache_read_tokens,
        cache_write_tokens=u.cache_write_tokens,
        reasoning_tokens=u.reasoning_tokens,
    )


def api_equivalent(
    usages: Sequence[PredictionUsage],
    price: ModelPrice,
    *,
    model: str,
    provider: str,
    n_decisions: int,
    table_sha256: str,
    table_name: str = "in-memory table",
) -> ApiEquivalent:
    """Price one configuration's predictions (``usages``, one per prediction) at ``price``."""
    ttl: CacheTtl = CACHE_TTL.get(provider, "5m")
    tier = price.long_context
    base: list[float] = []
    tiered: list[float] = []
    writes: list[float] = []
    over = 0
    for recorded in usages:
        u = _usage(recorded)
        cost = price.cost(u, ttl)
        base.append(cost)
        writes.append(u.cache_write_tokens * price.cache_write_rate(ttl) / _PER_TOKEN)
        prompt = u.input_tokens + u.cache_read_tokens + u.cache_write_tokens
        if tier is not None and prompt > tier.threshold_tokens:
            over += 1
            tiered.append(tier.cost(u, ttl))
        else:
            tiered.append(cost)
    estimates = [u.cost_usd for u in usages if u.cost_source == "reported"]
    complete = bool(usages) and len(estimates) == len(usages) and None not in estimates
    prompts = sum(prompt_tokens(u) for u in usages)
    return ApiEquivalent(
        model=model,
        provider=provider,
        as_of=price.as_of,
        source=price.source,
        table_sha256=table_sha256,
        table_name=table_name,
        cache_ttl=ttl,
        input_rate=price.input,
        cache_write_rate=price.cache_write_rate(ttl),
        n_predictions=len(usages),
        n_decisions=n_decisions,
        total_usd=math.fsum(base),
        cache_write_usd=math.fsum(writes),
        n_over_threshold=over,
        tier_usd=math.fsum(tiered) if over else None,
        cli_estimate_usd=math.fsum(e or 0.0 for e in estimates) if complete else None,
        prompt_tokens=prompts,
    )


def api_equivalents(
    scores: RunScores,
    usages: Mapping[str, Sequence[PredictionUsage]],
    table: PricingTable,
    *,
    prompts: Mapping[str, Mapping[PredictionKey, int]] | None = None,
) -> dict[str, ApiEquivalent]:
    """The API-equivalent of every scored subscription-CLI configuration whose model has a
    price row in ``table``, by config ID. ``usages`` holds each configuration's predictions'
    usage (every repeat); a configuration whose count differs from the scorer's has changed
    since it was scored and is left out. ``prompts`` holds prompt tokens by prediction for the
    CLI configurations and the API configurations to compare them with (every configuration
    in it whose provider isn't a CLI is a peer), for :attr:`ApiEquivalent.prompt_gap`."""
    peers = [
        tokens
        for config_id, tokens in (prompts or {}).items()
        if (info := scores.config_info.get(config_id)) is not None
        and info.provider not in CLI_PROVIDERS
    ]
    out: dict[str, ApiEquivalent] = {}
    for config_id, config in scores.configs.items():
        info = scores.config_info.get(config_id)
        if info is None or info.provider not in CLI_PROVIDERS:
            continue
        price = table.get(info.model)
        recorded = usages.get(config_id, ())
        if price is None or len(recorded) != config.usage.n_predictions or not recorded:
            continue
        figure = api_equivalent(
            recorded,
            price,
            model=info.model,
            provider=info.provider,
            n_decisions=config.usage.n_decisions,
            table_sha256=table.sha256,
            table_name=table.source_path.name if table.source_path else "in-memory table",
        )
        own = (prompts or {}).get(config_id)
        gap = prompt_gap(own, peers) if own is not None else None
        if gap is not None:
            figure = replace(figure, prompt_gap=gap[0], n_peers=gap[1])
        out[config_id] = figure
    return out


class _UsageLine(BaseModel):
    """The fields of a ``predictions.jsonl`` line the API-equivalent needs."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    config_id: str
    case_id: str = ""
    sample_idx: int = 0
    usage: PredictionUsage = PredictionUsage()


def read_usages(path: Path | str, config_ids: Iterable[str]) -> dict[str, list[PredictionUsage]]:
    """Every prediction's usage in a ``predictions.jsonl`` file for ``config_ids``, without
    parsing outputs (a README needs the usage, not the answers). A torn last line (no trailing
    newline, doesn't parse) is skipped, as ``scoring.summary.read_predictions`` skips it; any
    other line that doesn't parse raises ``ValueError``."""
    return {
        c: [usage for _, usage in lines] for c, lines in read_keyed_usages(path, config_ids).items()
    }


def read_keyed_usages(
    path: Path | str, config_ids: Iterable[str]
) -> dict[str, list[tuple[PredictionKey, PredictionUsage]]]:
    """:func:`read_usages`, with each usage's prediction key (case ID, repeat index)."""
    wanted = set(config_ids)
    out: dict[str, list[tuple[PredictionKey, PredictionUsage]]] = {c: [] for c in wanted}
    with Path(path).open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                parsed = _UsageLine.model_validate_json(line)
            except ValueError as exc:
                if not line.endswith("\n"):
                    break
                raise ValueError(f"{path}:{lineno}: invalid prediction: {exc}") from exc
            if parsed.config_id in wanted:
                out[parsed.config_id].append(((parsed.case_id, parsed.sample_idx), parsed.usage))
    return out
