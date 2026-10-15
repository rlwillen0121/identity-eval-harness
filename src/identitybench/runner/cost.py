"""Cost from recorded usage x dated ``pricing.yaml``, and the budget stop (SPEC §11.4, §16).

The manifest's cost is **metered API cost only**. Three cases must stay distinguishable, so a
model's ``CostMode`` says which one it is:

- ``metered``: a commercial API billed per token. Cost is usage x its ``pricing.yaml`` row; a
  model with no row has an *unknown* cost (None, never a silent $0), and a budget cannot be
  enforced for it.
- ``unmetered``: self-hosted servers and replay. Nothing is metered, so the API cost is a real
  $0 (SPEC §11.4: self-hosted models show $0 API cost).
- ``reported``: the access path states each call's cost, and ``pricing.yaml`` is never applied
  even when the model name has a row; that cost (``ModelResponse.reported_cost_usd``) is
  recorded and counts toward the budget. Subscription CLIs: the figure is the CLI's own (often
  notional) estimate; ``claude_cli`` and ``grok_cli`` report one (``total_cost_usd``),
  ``codex_cli``, ``agy_cli`` and ``cursor_cli`` don't, so a budget can't be enforced for them
  (``reports_cost``). Billing gateways: an ``openai_compatible`` entry on OpenRouter
  (``OPENROUTER_BASE_URL``, the default there) gets the USD OpenRouter charged for the call
  (``usage.cost``), which makes ``budget_usd`` enforceable for models no ``pricing.yaml`` row
  covers. A call that used tokens and comes back without a cost is an unknown cost, never a
  free one: the prediction records it as unknown and the budget stops paid calls
  (``CostTracker``). Only a failed call with no usage at all (a timeout, a refused
  connection) is taken as free.

``budget_usd`` is experiment-wide, so ``BudgetGuard`` sums spend over every config's tracker
and lets concurrent calls reserve their estimated cost before they start.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from identitybench.providers.base import Usage
from identitybench.providers.openai_compat import (
    DEFAULT_BASE_URL_ENV as OPENAI_DEFAULT_BASE_URL_ENV,
)
from identitybench.providers.openai_compat import (
    OPENROUTER_BASE_URL_ENV,
)

PRICING_ENV = "IDENTITYBENCH_PRICING"
"""Environment variable naming a pricing file; overrides the default lookup."""

SOURCE_PRICING_PATH = Path(__file__).resolve().parents[3] / "pricing.yaml"
"""Repo-root ``pricing.yaml`` in a source checkout (src/identitybench/runner/cost.py -> root)."""

CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0

CacheTtl = Literal["5m", "1h"]
CostMode = Literal["metered", "unmetered", "reported"]
_PER_TOKEN = 1_000_000

_DEFAULT_COST_MODE: dict[str, CostMode] = {
    "anthropic": "metered",
    "openai_compatible": "metered",
    "anthropic_compatible": "unmetered",
    "replay": "unmetered",
    "claude_cli": "reported",
    "codex_cli": "reported",
    "grok_cli": "reported",
    "agy_cli": "reported",
    "cursor_cli": "reported",
}


_REPORTS_COST = frozenset({"claude_cli", "grok_cli"})
"""CLI kinds whose provider fills ``ModelResponse.reported_cost_usd``."""

_OPENAI_VENDOR_ENVS: frozenset[str | None] = frozenset({None, OPENAI_DEFAULT_BASE_URL_ENV})
"""``base_url_env`` values that mean OpenAI's own API for an ``openai_compatible`` entry."""


def default_cost_mode(
    provider_kind: str,
    *,
    cache_namespace: str | None = None,
    base_url_env: str | None = None,
) -> CostMode:
    """The usual cost mode for a provider kind.

    ``openai_compatible`` covers OpenAI itself, so it defaults to ``metered``. On OpenRouter
    (``base_url_env: OPENROUTER_BASE_URL``) it is ``reported``: OpenRouter states every call's
    cost, which covers models ``pricing.yaml`` has no row for. It counts as self-hosted (a real
    $0) only when the entry sets a ``cache_namespace`` *and* names its own endpoint, neither
    OpenAI's nor OpenRouter's: SPEC §11.4 makes the namespace optional for any model, so a paid
    entry may set one too (to force fresh inferences), and a namespace alone must never make a
    metered API free. Any other self-hosted server should say ``cost_mode: unmetered``;
    otherwise its cost stays *unknown* (the safe direction: the budget guard flags it rather
    than counting it as free). ``anthropic_compatible`` exists for self-hosted Messages-API
    servers (SPEC §11.1). Unknown kinds (e.g. baselines) cost nothing.
    """
    if provider_kind == "openai_compatible" and base_url_env == OPENROUTER_BASE_URL_ENV:
        return "reported"
    if (
        provider_kind == "openai_compatible"
        and cache_namespace is not None
        and not points_at_metered_vendor(provider_kind, base_url_env)
    ):
        return "unmetered"
    return _DEFAULT_COST_MODE.get(provider_kind, "unmetered")


def points_at_metered_vendor(provider_kind: str, base_url_env: str | None = None) -> bool:
    """Whether an entry talks to a commercial per-token API: the ``anthropic`` kind, or
    ``openai_compatible`` on OpenAI's default endpoint (no ``base_url_env``, or OpenAI's own)
    or on OpenRouter.

    An entry like that resolving to ``unmetered`` would show $0 for paid calls and escape the
    budget stop, so the config warns about it.
    """
    if provider_kind == "anthropic":
        return True
    return provider_kind == "openai_compatible" and (
        base_url_env in _OPENAI_VENDOR_ENVS or base_url_env == OPENROUTER_BASE_URL_ENV
    )


def reports_cost(provider_kind: str, base_url_env: str | None = None) -> bool:
    """Whether a ``reported``-mode entry's provider reports a per-call cost at all.

    ``claude_cli`` and ``grok_cli`` do. ``openai_compatible`` does on OpenRouter
    (``OPENROUTER_BASE_URL``), which puts each call's USD in ``usage.cost``. Any other
    endpoint is assumed not to: a budget there is refused up front, as for a CLI that reports
    nothing, rather than spending a call of unknown cost to find out.
    """
    if provider_kind == "openai_compatible":
        return base_url_env == OPENROUTER_BASE_URL_ENV
    return provider_kind in _REPORTS_COST


def cache_write_multiplier(ttl: CacheTtl) -> float:
    """Default cache-write rate as a multiple of the input rate for a cache lifetime."""
    return CACHE_WRITE_1H_MULTIPLIER if ttl == "1h" else CACHE_WRITE_5M_MULTIPLIER


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Rates(_Strict):
    """USD per 1M tokens. Optional cache rates default to multiples of ``input``."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float | None = Field(default=None, ge=0)
    cache_write_5m: float | None = Field(default=None, ge=0)
    cache_write_1h: float | None = Field(default=None, ge=0)

    @property
    def cache_read_rate(self) -> float:
        if self.cache_read is not None:
            return self.cache_read
        return self.input * CACHE_READ_MULTIPLIER

    def cache_write_rate(self, ttl: CacheTtl) -> float:
        explicit = self.cache_write_1h if ttl == "1h" else self.cache_write_5m
        return explicit if explicit is not None else self.input * cache_write_multiplier(ttl)

    def cost(self, usage: Usage, ttl: CacheTtl = "5m") -> float:
        """USD for one call. Reasoning tokens are already inside ``output_tokens``."""
        total = (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_read_tokens * self.cache_read_rate
            + usage.cache_write_tokens * self.cache_write_rate(ttl)
        )
        return total / _PER_TOKEN


class LongContextTier(Rates):
    threshold_tokens: int = Field(ge=1)
    """Calls whose prompt tokens exceed this are billed entirely at the tier's rates."""


class ModelPrice(Rates):
    long_context: LongContextTier | None = None
    as_of: date
    source: str = Field(min_length=1)

    def rates_for(self, usage: Usage) -> Rates:
        """Base rates, or the long-context tier when this call's prompt is over its threshold.

        The prompt size counts cached tokens too: they are part of the prompt the model reads.
        """
        tier = self.long_context
        prompt = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        if tier is not None and prompt > tier.threshold_tokens:
            return tier
        return self

    def call_cost(self, usage: Usage, ttl: CacheTtl = "5m") -> float:
        return self.rates_for(usage).cost(usage, ttl)


class PricingTable(_Strict):
    """Price rows by model name, plus where they came from.

    ``load_pricing`` records the file's path and the sha256 of its bytes, because which file
    is read depends on the environment (``default_pricing_path``) and the manifest must say
    which prices produced its costs. A table built in memory has no path, and its sha256 is
    that of its canonical JSON.
    """

    models: dict[str, ModelPrice]
    _source_path: Path | None = PrivateAttr(default=None)
    _file_sha256: str | None = PrivateAttr(default=None)

    def get(self, model: str) -> ModelPrice | None:
        return self.models.get(model)

    @property
    def source_path(self) -> Path | None:
        """The file this table was loaded from (None for an in-memory table)."""
        return self._source_path

    @property
    def sha256(self) -> str:
        """sha256 of the file's bytes, or of the canonical JSON of an in-memory table."""
        if self._file_sha256 is not None:
            return self._file_sha256
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_pricing_path() -> Path:
    """Where the default pricing file is: ``$IDENTITYBENCH_PRICING``, else ``./pricing.yaml``,
    else the source checkout's repo-root file.

    The working-directory step makes an installed (non-editable) package work when run from a
    project directory, where the source-checkout path doesn't exist.
    """
    override = os.environ.get(PRICING_ENV)
    if override:
        return Path(override)
    candidates = [Path.cwd() / "pricing.yaml", SOURCE_PRICING_PATH]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    tried = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(f"no pricing.yaml found (tried {tried}); set {PRICING_ENV}")


def load_pricing(path: Path | str | None = None) -> PricingTable:
    """Parse a pricing file (default: ``default_pricing_path()``), keeping its path and sha256."""
    p = default_pricing_path() if path is None else Path(path)
    raw = p.read_bytes()
    table = PricingTable.model_validate(yaml.safe_load(raw.decode("utf-8")))
    table._source_path = p  # pyright: ignore[reportPrivateUsage]
    table._file_sha256 = hashlib.sha256(raw).hexdigest()  # pyright: ignore[reportPrivateUsage]
    return table


@functools.cache
def default_pricing() -> PricingTable:
    """The default table, parsed once per process."""
    return load_pricing()


def cost_usd(
    model: str,
    usage: Usage,
    *,
    table: PricingTable | None = None,
    cache_ttl: CacheTtl = "5m",
) -> float | None:
    """Metered USD for one call's ``usage``, or None when ``model`` has no price row.

    Must be called per call, not on summed usage: the long-context tier depends on the size
    of each individual prompt. ``Usage`` has one cache-write counter, so the caller says which
    TTL the writes used (the harness's prompt caching uses the 5-minute default).
    """
    price = (table or default_pricing()).get(model)
    return None if price is None else price.call_cost(usage, cache_ttl)


class CostTracker:
    """Running usage and cost for one configuration.

    ``spend_usd`` is what the budget is checked against. Per call it adds the metered cost
    (``metered`` mode, priced model), else any cost the access path reported, else zero.

    ``reports_cost=False`` declares up front that a ``reported``-mode access path never
    reports a cost (``cost.reports_cost(kind)``), so its spend is unknown from the start.
    """

    def __init__(
        self,
        model: str,
        *,
        mode: CostMode = "metered",
        table: PricingTable | None = None,
        cache_ttl: CacheTtl = "5m",
        reports_cost: bool = True,
    ) -> None:
        self.model = model
        self.mode: CostMode = mode
        self.reports_cost = reports_cost
        self._cache_ttl: CacheTtl = cache_ttl
        self.pricing: PricingTable | None = (
            (table or default_pricing()) if mode == "metered" else None
        )
        """The table metered calls are priced from (None unless ``mode`` is ``metered``, so a
        run with no metered model never needs a pricing file)."""
        self._price = self.pricing.get(model) if self.pricing is not None else None
        self.usage = Usage()
        self.calls = 0
        self.unreported_calls = 0
        """Calls that contributed to ``spend_usd`` with no known cost (unpriced, unreported)."""
        self.metered_usd = 0.0
        self.reported_usd: float | None = None
        self.spend_usd = 0.0

    @property
    def price(self) -> ModelPrice | None:
        """The ``pricing.yaml`` row this config's calls are billed from, if any."""
        return self._price

    @property
    def cache_ttl(self) -> CacheTtl:
        """The cache lifetime cache-write tokens are billed at, so a pre-call estimate prices
        them like ``add`` will."""
        return self._cache_ttl

    @property
    def priced(self) -> bool:
        """True when calls are billed from a ``pricing.yaml`` row."""
        return self._price is not None

    @property
    def budget_enforceable(self) -> bool:
        """False when this config's spend can't be known, so a budget can't stop it.

        That is a metered model with no price row, or a ``reported`` access path that doesn't
        report costs: declared up front (``reports_cost=False``) or observed (a call came back
        without one). The runner should refuse or warn before starting, and re-check after
        calls, since the observed case only shows up then.
        """
        if self.mode == "metered":
            return self.priced
        if self.mode == "reported":
            return self.reports_cost and self.unreported_calls == 0
        return True

    def add(self, usage: Usage, reported_cost_usd: float | None = None) -> float:
        """Record one call; return the amount it added to ``spend_usd``."""
        self.calls += 1
        self.usage.add(usage)
        if reported_cost_usd is not None:
            self.reported_usd = (self.reported_usd or 0.0) + reported_cost_usd
        if self._price is not None:
            spent = self._price.call_cost(usage, self._cache_ttl)
            self.metered_usd += spent
        elif self.mode == "unmetered":
            spent = 0.0
        else:
            spent = reported_cost_usd or 0.0
            if reported_cost_usd is None:
                self.unreported_calls += 1
        self.spend_usd += spent
        return spent

    def would_exceed(self, budget_usd: float | None, pending_usd: float = 0.0) -> bool:
        """True when this config's spend plus ``pending_usd`` is over ``budget_usd``.

        A per-config check; the experiment-wide ``budget_usd`` stop is ``BudgetGuard``.
        """
        if budget_usd is None:
            return False
        return self.spend_usd + pending_usd > budget_usd

    @property
    def metered_api_usd(self) -> float | None:
        """Metered API cost for the manifest: the priced total, a real 0.0 when nothing is
        metered (self-hosted, replay), and None when it is unknown (unpriced API) or not
        applicable (subscription CLI)."""
        if self.priced:
            return self.metered_usd
        return 0.0 if self.mode == "unmetered" else None


class BudgetGuard:
    """The experiment-wide ``budget_usd`` stop, over every config's ``CostTracker``.

    With concurrency, checking spend alone lets every in-flight call pass the same check and
    together overshoot the budget. So a call ``reserve``s its estimated cost before it starts
    (refused when spend plus outstanding reservations plus the estimate would exceed the
    budget) and ``release``s it once its actual cost has been ``add``ed to its tracker. The
    reserve check has no ``await`` inside, so it is atomic under asyncio.
    """

    def __init__(
        self,
        budget_usd: float | None,
        *,
        table: PricingTable | None = None,
        cache_ttl: CacheTtl = "5m",
    ) -> None:
        self.budget_usd = budget_usd
        self._table = table
        self._cache_ttl: CacheTtl = cache_ttl
        self.trackers: dict[str, CostTracker] = {}
        self.reserved_usd = 0.0

    def track(
        self, config_id: str, model: str, mode: CostMode, *, reports_cost: bool = True
    ) -> CostTracker:
        """Create and register the tracker for one config."""
        if config_id in self.trackers:
            raise ValueError(f"config {config_id!r} is already tracked")
        tracker = CostTracker(
            model,
            mode=mode,
            table=self._table,
            cache_ttl=self._cache_ttl,
            reports_cost=reports_cost,
        )
        self.trackers[config_id] = tracker
        return tracker

    def __iter__(self) -> Iterator[tuple[str, CostTracker]]:
        return iter(self.trackers.items())

    @property
    def spend_usd(self) -> float:
        return sum(t.spend_usd for t in self.trackers.values())

    def unenforceable(self) -> list[str]:
        """Config IDs whose spend can't be known; empty when there is no budget to enforce."""
        if self.budget_usd is None:
            return []
        return [cid for cid, t in self.trackers.items() if not t.budget_enforceable]

    def would_exceed(self, pending_usd: float = 0.0) -> bool:
        """True when summed spend plus outstanding reservations plus ``pending_usd`` (an
        estimate for the next call) is over the budget. No budget never stops the run."""
        if self.budget_usd is None:
            return False
        return self.spend_usd + self.reserved_usd + pending_usd > self.budget_usd

    def reserve(self, estimate_usd: float) -> bool:
        """Hold ``estimate_usd`` for a call about to start; False (nothing held) when that
        would exceed the budget. Pair every successful reserve with one ``release``."""
        if estimate_usd < 0:
            raise ValueError("estimate_usd must be >= 0")
        if self.would_exceed(estimate_usd):
            return False
        self.reserved_usd += estimate_usd
        return True

    def release(self, estimate_usd: float) -> None:
        """Drop a reservation once the call has finished (and its cost was ``add``ed)."""
        if estimate_usd < 0:
            raise ValueError("estimate_usd must be >= 0")
        self.reserved_usd = max(0.0, self.reserved_usd - estimate_usd)
