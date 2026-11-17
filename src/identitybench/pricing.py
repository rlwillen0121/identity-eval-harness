"""Dated token pricing and file provenance for v4.1 reporting."""

from __future__ import annotations

import functools
import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from identitybench.providers._json import bounded_token_count
from identitybench.providers.base import Usage

PRICING_ENV = "IDENTITYBENCH_PRICING"


SOURCE_PRICING_PATH = Path(__file__).resolve().parents[2] / "pricing.yaml"


CACHE_READ_MULTIPLIER = 0.1


CACHE_WRITE_5M_MULTIPLIER = 1.25


CACHE_WRITE_1H_MULTIPLIER = 2.0


CacheTtl = Literal["5m", "1h"]


_PER_TOKEN = 1_000_000


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
            bounded_token_count(usage.input_tokens) * self.input
            + bounded_token_count(usage.output_tokens) * self.output
            + bounded_token_count(usage.cache_read_tokens) * self.cache_read_rate
            + bounded_token_count(usage.cache_write_tokens) * self.cache_write_rate(ttl)
        )
        return total / _PER_TOKEN


class LongContextTier(Rates):
    threshold_tokens: int = Field(ge=1)
    """Calls whose prompt tokens exceed this are billed entirely at the tier's rates."""


class ModelPrice(Rates):
    long_context: LongContextTier | None = None
    as_of: date
    source: str = Field(min_length=1)
    valid_until: date | None = None
    """The last day a time-limited price (an introductory price) applies, when the vendor
    dates its end; the report states it beside an API-equivalent priced at this row. The
    runner doesn't use it: a row is updated when its price changes."""

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
