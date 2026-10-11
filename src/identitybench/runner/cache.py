"""Content-addressed response cache (SPEC §11.4).

A response is stored under the sha256 of everything that can change it: provider kind, model,
non-secret endpoint identity, generation params, the rendered system prompt and messages, the
output schema, the tool schemas and ``sample_idx`` (so each repeat is a fresh inference).

Entries live at ``<root>/<key[:2]>/<key>.json``. Writes go to a unique temp file in the same
directory and are then ``os.replace``d into place, which is atomic on POSIX and Windows, so
concurrent writers (asyncio tasks, threads or processes) never leave a torn entry: the last
complete write wins, and since the key covers every input, competing writes hold equivalent
responses anyway.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from identitybench.providers.base import ModelRequest, ModelResponse
from identitybench.providers.replay import CACHE_KEY_PARAM, response_from_dict, response_to_dict

CACHE_KEY_VERSION = 1
"""Bump to invalidate every existing entry when the key payload changes shape."""

DEFAULT_CACHE_ROOT = Path(".cache/responses")

_KEY = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(obj: Any) -> str:
    """Sorted keys, compact separators, UTF-8 kept, NaN rejected: one byte string per value."""
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def cache_key(
    kind: str,
    model: str,
    endpoint_identity: str,
    params: dict[str, Any],
    system: str,
    messages: list[dict[str, Any]],
    output_schema: dict[str, Any] | None,
    tools: list[dict[str, Any]] | None,
    sample_idx: int,
) -> str:
    """Hex sha256 over the canonical JSON of every response-determining input."""
    payload = {
        "v": CACHE_KEY_VERSION,
        "kind": kind,
        "model": model,
        "endpoint": endpoint_identity,
        "params": params,
        "system": system,
        "messages": messages,
        "output_schema": output_schema,
        "tools": tools or [],
        "sample_idx": sample_idx,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def key_for_request(
    kind: str, model: str, endpoint_identity: str, request: ModelRequest, sample_idx: int
) -> str:
    """``cache_key`` for a provider-neutral ``ModelRequest``.

    ``schema_name`` goes into params because some providers send it on the wire (OpenAI
    ``json_schema.name``, a forced tool name), so it can change the response.

    ``params.extra["cache_key"]`` is left out: the runner computes this key and then injects it
    there for the replay provider (``providers.replay.CACHE_KEY_PARAM``), so the key must not
    depend on itself. Hashing the request before or after injection gives the same key.
    """
    extra = {k: v for k, v in request.params.extra.items() if k != CACHE_KEY_PARAM}
    params = dataclasses.asdict(request.params) | {
        "extra": extra,
        "schema_name": request.schema_name,
    }
    return cache_key(
        kind=kind,
        model=model,
        endpoint_identity=endpoint_identity,
        params=params,
        system=request.system,
        messages=[dataclasses.asdict(m) for m in request.messages],
        output_schema=request.output_schema,
        tools=[dataclasses.asdict(t) for t in request.tools],
        sample_idx=sample_idx,
    )


class ResponseCache:
    """Sharded on-disk JSON cache keyed by ``cache_key``.

    ``refresh=True`` implements ``--refresh``: every ``get`` misses, so every call is a fresh
    inference. Fresh results are still written, replacing the stale entries: the point of a
    refresh is that those entries are wrong (e.g. a redeployed model behind an unchanged
    ``cache_namespace``), so leaving them in place would serve them again on the next run.
    """

    def __init__(self, root: Path | str = DEFAULT_CACHE_ROOT, refresh: bool = False) -> None:
        self.root = Path(root)
        self.refresh = refresh

    def path_for(self, key: str) -> Path:
        if not _KEY.fullmatch(key):
            raise ValueError(f"invalid cache key {key!r}")
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        """The stored record, or None on a miss, under ``refresh``, or for a corrupt entry."""
        path = self.path_for(key)
        if self.refresh:
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(entry, dict) or entry.get("key") != key:
            return None
        value = entry.get("value")
        return value if isinstance(value, dict) else None

    def put(self, key: str, record: dict[str, Any]) -> Path:
        """Atomically store ``record`` under ``key``; return the entry path."""
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = canonical_json({"key": key, "value": record}).encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{key}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def get_response(self, key: str) -> ModelResponse | None:
        """The cached response, or None on a miss.

        A record that no longer fits ``ModelResponse`` (written by another contract version,
        or hand-edited) is a miss, not a crash: the call is simply re-run and re-cached.
        """
        record = self.get(key)
        if record is None:
            return None
        try:
            return response_from_dict(record)
        except (TypeError, ValueError, KeyError, AttributeError):
            return None

    def put_response(self, key: str, response: ModelResponse) -> Path:
        return self.put(key, response_to_dict(response))
