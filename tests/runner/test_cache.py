from __future__ import annotations

import json
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from identitybench.providers.base import (
    GenerationParams,
    Message,
    ModelRequest,
    ModelResponse,
    Role,
    ToolCall,
    ToolSpec,
    Usage,
)
from identitybench.providers.replay import CACHE_KEY_PARAM
from identitybench.runner.cache import (
    ResponseCache,
    cache_key,
    canonical_json,
    key_for_request,
)


def _args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "kind": "anthropic",
        "model": "claude-opus-5-5",
        "endpoint_identity": "abc123",
        "params": {"temperature": 0, "max_output_tokens": 2048},
        "system": "You are a reviewer.",
        "messages": [{"role": "user", "content": "Decide."}],
        "output_schema": {"type": "object", "properties": {"decision": {"type": "string"}}},
        "tools": None,
        "sample_idx": 0,
    }
    args.update(overrides)
    return args


def test_key_is_stable_hex_and_order_insensitive() -> None:
    key = cache_key(**_args())
    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)
    assert cache_key(**_args()) == key
    reordered = _args(
        params={"max_output_tokens": 2048, "temperature": 0},
        output_schema={"properties": {"decision": {"type": "string"}}, "type": "object"},
    )
    assert cache_key(**reordered) == key


def test_key_pinned_value() -> None:
    """Pins the key format: a change here silently invalidates every cached response."""
    assert cache_key(**_args()) == (
        "d056f249b4a22188e8339edcd17f16a8ec6aed007a81795432cf8449c1997b47"
    )
    assert canonical_json({"b": 1, "a": [1, "é"]}) == '{"a":[1,"é"],"b":1}'


@pytest.mark.parametrize(
    "change",
    [
        {"sample_idx": 1},
        {"kind": "openai_compatible"},
        {"model": "claude-haiku-5-5"},
        {"endpoint_identity": "def456"},
        {"params": {"temperature": 0.5, "max_output_tokens": 2048}},
        {"system": "You are a strict reviewer."},
        {"messages": [{"role": "user", "content": "Decide!"}]},
        {"output_schema": None},
        {"tools": [{"name": "lookup", "description": "d", "input_schema": {}}]},
    ],
)
def test_key_sensitive_to_every_input(change: dict[str, Any]) -> None:
    assert cache_key(**_args(**change)) != cache_key(**_args())


def test_none_and_empty_tools_are_equivalent() -> None:
    assert cache_key(**_args(tools=None)) == cache_key(**_args(tools=[]))


def _request(**params: Any) -> ModelRequest:
    return ModelRequest(
        system="sys",
        messages=(Message(role=Role.user, content="hi"),),
        output_schema={"type": "object"},
        tools=(ToolSpec(name="t", description="d", input_schema={"type": "object"}),),
        params=GenerationParams(**params),
    )


def test_key_for_request() -> None:
    base = key_for_request("anthropic", "m", "e", _request(), 0)
    assert base == key_for_request("anthropic", "m", "e", _request(), 0)
    assert base != key_for_request("anthropic", "m", "e", _request(), 1)
    assert base != key_for_request("anthropic", "m", "e", _request(temperature=0.0), 0)
    assert base != key_for_request("anthropic", "m", "e", _request(effort="low"), 0)
    assert base != key_for_request("anthropic", "m", "e", _request(extra={"top_k": 5}), 0)
    # The replay key the runner injects after computing the key is not part of it.
    injected = _request(extra={CACHE_KEY_PARAM: base})
    assert key_for_request("anthropic", "m", "e", injected, 0) == base


def test_get_put_roundtrip_and_sharding(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    key = cache_key(**_args())
    assert cache.get(key) is None
    assert key not in cache
    path = cache.put(key, {"text": "{}", "n": 1})
    assert path == tmp_path / key[:2] / f"{key}.json"
    assert cache.get(key) == {"text": "{}", "n": 1}
    assert key in cache
    assert not list(path.parent.glob("*.tmp"))


def test_refresh_bypasses_reads_but_writes(tmp_path: Path) -> None:
    key = cache_key(**_args())
    ResponseCache(tmp_path).put(key, {"v": 1})
    fresh = ResponseCache(tmp_path, refresh=True)
    assert fresh.get(key) is None
    fresh.put(key, {"v": 2})
    assert ResponseCache(tmp_path).get(key) == {"v": 2}


def test_corrupt_or_mismatched_entries_miss(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    key = cache_key(**_args())
    path = cache.put(key, {"v": 1})
    path.write_text("{truncated", encoding="utf-8")
    assert cache.get(key) is None
    path.write_text(json.dumps({"key": "0" * 64, "value": {"v": 1}}), encoding="utf-8")
    assert cache.get(key) is None


@pytest.mark.parametrize("bad", ["../etc/passwd", "ABC", "a" * 63, "g" * 64])
def test_invalid_keys_rejected(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError, match="invalid cache key"):
        ResponseCache(tmp_path).path_for(bad)


def test_response_roundtrip(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    response = ModelResponse(
        text='{"decision":"DENY"}',
        parsed={"decision": "DENY"},
        tool_calls=[ToolCall(id="c1", name="lookup", arguments={"id": "u_1"})],
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=5, cache_read_tokens=3, reasoning_tokens=2),
        latency_s=1.25,
        served_model="claude-opus-5-5",
        reported_cost_usd=0.01,
    )
    key = cache_key(**_args())
    cache.put_response(key, response)
    assert cache.get_response(key) == response
    assert cache.get_response(cache_key(**_args(sample_idx=9))) is None


@pytest.mark.parametrize(
    "record",
    [
        {"text": "x", "not_a_field": 1},
        {"usage": {"tokens": 3}},
        {"usage": "lots"},
        {"tool_calls": [{"id": "c1"}]},
        {"tool_calls": 5},
    ],
)
def test_incompatible_cached_record_is_a_miss(tmp_path: Path, record: dict[str, Any]) -> None:
    cache = ResponseCache(tmp_path)
    key = cache_key(**_args())
    cache.put(key, record)
    assert cache.get(key) == record
    assert cache.get_response(key) is None


def test_concurrent_thread_puts_are_atomic(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    key = cache_key(**_args())
    big = "x" * 200_000
    barrier = threading.Barrier(16)
    seen: list[dict[str, Any] | None] = []

    def writer(i: int) -> None:
        barrier.wait()
        cache.put(key, {"writer": i, "blob": big})
        seen.append(cache.get(key))

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(writer, range(16)))
    # Every read saw a complete record from some writer, never a torn file.
    assert all(r is not None and r["blob"] == big for r in seen)
    final = cache.get(key)
    assert final is not None and final["writer"] in range(16)
    assert not list(cache.path_for(key).parent.glob("*.tmp"))


def _process_put(root: str, key: str, i: int) -> None:
    ResponseCache(root).put(key, {"writer": i, "blob": "y" * 100_000})


def test_concurrent_process_puts_are_atomic(tmp_path: Path) -> None:
    key = cache_key(**_args())
    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(_process_put, [str(tmp_path)] * 8, [key] * 8, range(8)))
    final = ResponseCache(tmp_path).get(key)
    assert final is not None and final["blob"] == "y" * 100_000
