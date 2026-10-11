"""Replay provider: serves recorded responses by cache key, for CI and re-scoring (SPEC §11.1).

The runner computes each request's cache key (SPEC §11.4) and passes it in
``request.params.extra["cache_key"]``. A key with no recording comes back as an error response
(scored INVALID), never as a fabricated answer. ``has`` tells a caller whether a key is
recorded at all, since a recorded provider error comes back as an error response too.

Recordings are a mapping ``cache key -> ModelResponse`` (or its ``dataclasses.asdict`` form), or
a ``responses.jsonl`` file with one record per line::

    {"key": "<cache key>", "response": {"text": "...", "parsed": {...}, "usage": {...}, ...},
     "cost_usd": 0.0123, "cost_source": "metered"}

``cost_usd`` and ``cost_source`` (optional, together) are what the answer cost when it was
recorded, as the recording run priced it (``schemas.predictions.PredictionUsage``): a replay
spends nothing, but the answer it serves still cost that, and ``recorded_cost`` hands it to the
runner so a replayed prediction keeps its cost provenance. Other fields of a record (the
runner's ``config_id`` and ``fresh``) are ignored here.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from identitybench.providers.base import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ToolCall,
    Usage,
    hash_endpoint,
)
from identitybench.schemas.predictions import CostSource

CACHE_KEY_PARAM = "cache_key"

COST_FIELDS = ("cost_usd", "cost_source")
"""Record fields holding a recorded answer's cost (see the module docstring)."""

_RESPONSE_FIELDS = frozenset(field.name for field in dataclasses.fields(ModelResponse))
_USAGE_FIELDS = frozenset(field.name for field in dataclasses.fields(Usage))


def response_to_dict(response: ModelResponse) -> dict[str, Any]:
    """JSON-ready form of a response (the inverse of ``response_from_dict``)."""
    return dataclasses.asdict(response)


def response_from_dict(data: Mapping[str, Any]) -> ModelResponse:
    """Rebuild a ``ModelResponse`` from its dict form.

    Raises ``ValueError`` on unknown fields so a recording written by a different contract
    version fails loudly at load time instead of replaying half a response.
    """
    unknown = set(data) - _RESPONSE_FIELDS
    if unknown:
        raise ValueError(f"unknown ModelResponse fields: {sorted(unknown)}")
    fields = dict(data)
    usage = fields.pop("usage", None) or {}
    unknown_usage = set(usage) - _USAGE_FIELDS
    if unknown_usage:
        raise ValueError(f"unknown Usage fields: {sorted(unknown_usage)}")
    calls = [
        ToolCall(id=call["id"], name=call["name"], arguments=dict(call.get("arguments") or {}))
        for call in fields.pop("tool_calls", None) or []
    ]
    return ModelResponse(**fields, usage=Usage(**usage), tool_calls=calls)


@dataclass(frozen=True)
class RecordedCost:
    """What a recorded answer cost when it was produced: ``PredictionUsage``'s ``cost_usd`` and
    ``cost_source`` for that one call."""

    cost_usd: float | None
    cost_source: CostSource


_COST_SOURCES: frozenset[str] = frozenset(get_args(CostSource))


def recorded_cost_from(record: Mapping[str, Any]) -> RecordedCost | None:
    """The ``cost_usd``/``cost_source`` pair of one recording record, or None when it has
    neither (a recording made before costs were recorded, or by hand). Raises ``ValueError``
    for only one of the two, a cost that isn't a finite non-negative number or null, or an
    unknown source: a recording must fail loudly rather than misstate what an answer cost."""
    present = [name for name in COST_FIELDS if name in record]
    if not present:
        return None
    if len(present) == 1:
        raise ValueError(f"{present[0]!r} needs {set(COST_FIELDS) - set(present)}")
    cost, source = record["cost_usd"], record["cost_source"]
    if cost is not None and (
        isinstance(cost, bool)
        or not isinstance(cost, int | float)
        or not math.isfinite(cost)
        or cost < 0
    ):
        raise ValueError(f"'cost_usd' must be a non-negative number or null, got {cost!r}")
    if source not in _COST_SOURCES:
        raise ValueError(f"'cost_source' must be one of {sorted(_COST_SOURCES)}, got {source!r}")
    return RecordedCost(None if cost is None else float(cost), source)


def _load_records(path: str | Path) -> dict[str, tuple[ModelResponse, RecordedCost | None]]:
    """Every record of a ``responses.jsonl`` recording, by key; the last record of a key wins
    (its cost included, or the lack of one)."""
    records: dict[str, tuple[ModelResponse, RecordedCost | None]] = {}
    with Path(path).open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                key = record["key"]
                response = record["response"]
                if not isinstance(key, str) or not isinstance(response, dict):
                    raise TypeError("'key' must be a string and 'response' an object")
                records[key] = (response_from_dict(response), recorded_cost_from(record))
            except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
                raise ValueError(f"{path}:{number}: not a replay record ({exc})") from exc
    return records


def load_responses_jsonl(path: str | Path) -> dict[str, ModelResponse]:
    """Read a ``responses.jsonl`` recording; the last record wins when a key repeats."""
    return {key: response for key, (response, _) in _load_records(path).items()}


class ReplayProvider:
    """Serves recorded ``ModelResponse``s keyed by the runner's cache key."""

    kind = "replay"

    def __init__(
        self,
        records: Mapping[str, ModelResponse | Mapping[str, Any]] | str | Path,
        *,
        model: str = "replay",
    ) -> None:
        """``records``: a ``responses.jsonl`` path, or a mapping of key to response (which
        carries no recorded costs)."""
        self.model = model
        self._costs: dict[str, RecordedCost] = {}
        if isinstance(records, str | Path):
            loaded = _load_records(records)
            self._records = {key: response for key, (response, _) in loaded.items()}
            self._costs = {key: cost for key, (_, cost) in loaded.items() if cost is not None}
        else:
            self._records = {
                key: value if isinstance(value, ModelResponse) else response_from_dict(value)
                for key, value in records.items()
            }
        keys_digest = hashlib.sha256("\n".join(sorted(self._records)).encode()).hexdigest()
        self._identity = hash_endpoint(self.kind, self.model, keys_digest)

    def __len__(self) -> int:
        return len(self._records)

    def capabilities(self) -> ProviderCapabilities:
        # Replay returns whatever was recorded, so it never constrains the runner.
        return ProviderCapabilities(
            native_structured_output=True, native_tools=True, supports_temperature=True
        )

    def endpoint_identity(self) -> str:
        return self._identity

    def has(self, key: str) -> bool:
        """Whether ``key`` has a recording (an answer, a refusal or a provider error alike):
        the one way to tell a miss from a recorded error, which ``complete`` both answers with
        an error response."""
        return key in self._records

    def recorded_cost(self, key: str) -> RecordedCost | None:
        """What the answer recorded under ``key`` cost when it was recorded; None when the
        recording doesn't say (or holds no such key)."""
        return self._costs.get(key)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        key = request.params.extra.get(CACHE_KEY_PARAM)
        if not isinstance(key, str):
            return ModelResponse(error=f"replay: request has no params.extra[{CACHE_KEY_PARAM!r}]")
        recorded = self._records.get(key)
        if recorded is None:
            return ModelResponse(error=f"replay: no recorded response for key {key}")
        # A copy, so a caller mutating the response can't corrupt later replays of the key.
        return copy.deepcopy(recorded)
