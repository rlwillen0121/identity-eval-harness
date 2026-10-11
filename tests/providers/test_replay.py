from __future__ import annotations

import json

import pytest

from identitybench.providers.base import ModelResponse, Provider, ToolCall, Usage
from identitybench.providers.replay import (
    CACHE_KEY_PARAM,
    RecordedCost,
    ReplayProvider,
    load_responses_jsonl,
    recorded_cost_from,
    response_from_dict,
    response_to_dict,
)
from tests.providers.fakes import make_request

RECORDED = ModelResponse(
    text='{"decision": "DENY"}',
    parsed={"decision": "DENY"},
    tool_calls=[ToolCall("t1", "get_identity", {"id": "U1"})],
    stop_reason="end_turn",
    usage=Usage(input_tokens=10, output_tokens=5, cache_read_tokens=100, reasoning_tokens=2),
    latency_s=1.5,
    served_model="claude-opus-5-5",
    reported_cost_usd=0.01,
    raw={"id": "msg_1"},
)


def keyed(key: str | None):
    extra = {CACHE_KEY_PARAM: key} if key is not None else {}
    return make_request(extra=extra)


def test_dict_round_trip() -> None:
    assert response_from_dict(response_to_dict(RECORDED)) == RECORDED
    assert response_from_dict(json.loads(json.dumps(response_to_dict(RECORDED)))) == RECORDED


def test_from_dict_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="unknown ModelResponse fields"):
        response_from_dict({"text": "x", "confidence": 1})
    with pytest.raises(ValueError, match="unknown Usage fields"):
        response_from_dict({"text": "x", "usage": {"tokens": 1}})


async def test_serves_recorded_responses_by_cache_key() -> None:
    replay = ReplayProvider({"k1": RECORDED, "k2": response_to_dict(ModelResponse(text="two"))})
    assert isinstance(replay, Provider)
    assert len(replay) == 2
    first = await replay.complete(keyed("k1"))
    assert first == RECORDED
    second = await replay.complete(keyed("k2"))
    assert second.text == "two"


async def test_returned_responses_are_copies() -> None:
    replay = ReplayProvider({"k1": RECORDED})
    first = await replay.complete(keyed("k1"))
    first.usage.input_tokens = 999
    assert first.parsed is not None
    first.parsed["decision"] = "APPROVE"
    again = await replay.complete(keyed("k1"))
    assert again == RECORDED


async def test_unknown_or_missing_key_is_an_error() -> None:
    replay = ReplayProvider({"k1": RECORDED})
    unknown = await replay.complete(keyed("nope"))
    assert unknown.error == "replay: no recorded response for key nope"
    missing = await replay.complete(keyed(None))
    assert missing.error is not None
    assert "cache_key" in missing.error


async def test_loads_a_responses_jsonl_file(tmp_path) -> None:
    path = tmp_path / "responses.jsonl"
    lines = [
        json.dumps({"key": "k1", "response": response_to_dict(ModelResponse(text="old"))}),
        "",
        json.dumps({"key": "k1", "response": response_to_dict(RECORDED)}),
        json.dumps({"key": "k2", "response": {"error": "transport failed"}}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    replay = ReplayProvider(path, model="claude-opus-5-5")
    assert replay.model == "claude-opus-5-5"
    assert await replay.complete(keyed("k1")) == RECORDED  # last record wins
    assert (await replay.complete(keyed("k2"))).error == "transport failed"
    assert ReplayProvider(str(path)).endpoint_identity() == ReplayProvider(path).endpoint_identity()


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        '{"response": {}}',
        '{"key": "k"}',
        '{"key": 1, "response": {}}',
        "[1]",
        '{"key": "k", "response": {"confidence": 1}}',
        '{"key": "k", "response": {"tool_calls": [{"name": "x"}]}}',
        '{"key": "k", "response": {"usage": [1]}}',
    ],
)
def test_malformed_jsonl_fails_loudly(tmp_path, line: str) -> None:
    path = tmp_path / "responses.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"responses.jsonl:1"):
        load_responses_jsonl(path)


def test_identity_depends_on_model_and_recorded_keys() -> None:
    a = ReplayProvider({"k1": RECORDED}).endpoint_identity()
    assert a == ReplayProvider({"k1": ModelResponse(text="other")}).endpoint_identity()
    assert a != ReplayProvider({"k2": RECORDED}).endpoint_identity()
    assert a != ReplayProvider({"k1": RECORDED}, model="m").endpoint_identity()


def test_capabilities_never_constrain_the_runner() -> None:
    caps = ReplayProvider({}).capabilities()
    assert caps.native_structured_output and caps.native_tools and caps.supports_temperature


async def test_has_tells_a_miss_from_a_recorded_error() -> None:
    """Both come back from ``complete`` as error responses; only ``has`` tells them apart."""
    failed = ModelResponse(error="replay: no recorded response for key k1")  # a miss's words
    replay = ReplayProvider({"k1": failed, "k2": RECORDED})
    assert replay.has("k1") and replay.has("k2") and not replay.has("k3")
    assert (await replay.complete(keyed("k1"))).error == failed.error
    assert (await replay.complete(keyed("k3"))).error == "replay: no recorded response for key k3"


def record(key: str, **fields: object) -> str:
    return json.dumps({"key": key, "response": response_to_dict(RECORDED), **fields})


def test_recorded_costs_come_from_the_recording(tmp_path) -> None:
    """The cost an answer had when it was recorded; the last record of a key wins, including
    one that records no cost."""
    path = tmp_path / "responses.jsonl"
    lines = [
        record("k1", cost_usd=0.5, cost_source="metered", config_id="c", fresh=True),
        record("k2", cost_usd=0.01, cost_source="reported"),
        record("k2", cost_usd=None, cost_source="unknown"),
        record("k3", cost_usd=0.2, cost_source="metered"),
        record("k3"),
        record("k4", cost_usd=0, cost_source="unmetered"),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    replay = ReplayProvider(path)
    assert replay.recorded_cost("k1") == RecordedCost(0.5, "metered")
    assert replay.recorded_cost("k2") == RecordedCost(None, "unknown")
    assert replay.recorded_cost("k3") is None
    assert replay.recorded_cost("k4") == RecordedCost(0.0, "unmetered")
    assert replay.recorded_cost("nope") is None
    assert ReplayProvider({"k1": RECORDED}).recorded_cost("k1") is None
    assert load_responses_jsonl(path)["k1"] == RECORDED


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"cost_usd": 0.1}, "needs"),
        ({"cost_source": "metered"}, "needs"),
        ({"cost_usd": -1, "cost_source": "metered"}, "non-negative"),
        ({"cost_usd": True, "cost_source": "metered"}, "non-negative"),
        ({"cost_usd": "0.1", "cost_source": "metered"}, "non-negative"),
        ({"cost_usd": float("nan"), "cost_source": "metered"}, "non-negative"),
        ({"cost_usd": 0.1, "cost_source": "free"}, "cost_source"),
    ],
)
def test_malformed_recorded_costs_fail_loudly(
    tmp_path, fields: dict[str, object], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        recorded_cost_from(fields)
    path = tmp_path / "responses.jsonl"
    path.write_text(record("k", **fields) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"responses.jsonl:1: not a replay record"):
        ReplayProvider(path)
