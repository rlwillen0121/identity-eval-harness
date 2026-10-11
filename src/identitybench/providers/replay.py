"""Replay provider: serves recorded responses by cache key, for CI and re-scoring (SPEC §11.1).

The runner computes each request's cache key (SPEC §11.4) and passes it in
``request.params.extra["cache_key"]``. A key with no recording comes back as an error response
(scored INVALID), never as a fabricated answer.

Recordings are a mapping ``cache key -> ModelResponse`` (or its ``dataclasses.asdict`` form), or
a ``responses.jsonl`` file with one record per line::

    {"key": "<cache key>", "response": {"text": "...", "parsed": {...}, "usage": {...}, ...}}
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from identitybench.providers.base import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ToolCall,
    Usage,
    hash_endpoint,
)

CACHE_KEY_PARAM = "cache_key"

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


def load_responses_jsonl(path: str | Path) -> dict[str, ModelResponse]:
    """Read a ``responses.jsonl`` recording; the last record wins when a key repeats."""
    records: dict[str, ModelResponse] = {}
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
                records[key] = response_from_dict(response)
            except (ValueError, KeyError, TypeError, AttributeError, RecursionError) as exc:
                raise ValueError(f"{path}:{number}: not a replay record ({exc})") from exc
    return records


class ReplayProvider:
    """Serves recorded ``ModelResponse``s keyed by the runner's cache key."""

    kind = "replay"

    def __init__(
        self,
        records: Mapping[str, ModelResponse | Mapping[str, Any]] | str | Path,
        *,
        model: str = "replay",
    ) -> None:
        self.model = model
        if isinstance(records, str | Path):
            self._records = load_responses_jsonl(records)
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

    async def complete(self, request: ModelRequest) -> ModelResponse:
        key = request.params.extra.get(CACHE_KEY_PARAM)
        if not isinstance(key, str):
            return ModelResponse(error=f"replay: request has no params.extra[{CACHE_KEY_PARAM!r}]")
        recorded = self._records.get(key)
        if recorded is None:
            return ModelResponse(error=f"replay: no recorded response for key {key}")
        # A copy, so a caller mutating the response can't corrupt later replays of the key.
        return copy.deepcopy(recorded)
