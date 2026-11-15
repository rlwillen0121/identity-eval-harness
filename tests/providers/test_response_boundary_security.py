"""Offline regressions for untrusted provider responses and diagnostic boundaries."""

import json
from pathlib import Path

import pytest

from identitybench.providers import _json
from identitybench.providers._cli import as_float, as_int
from identitybench.providers._json import (
    MAX_JSON_TEXT,
    MAX_RESPONSE_DEPTH,
    extract_json_object,
    json_safe_usage_value,
    raw_usage_payload,
    sanitize_response,
)
from identitybench.providers._subprocess import Command, ProcessResult, redact, run_command
from identitybench.providers.base import ModelResponse, ToolCall, Usage
from identitybench.providers.replay import response_from_dict, response_to_dict
from tests.providers.fakes import make_request
from tests.providers.test_anthropic_api import api_message
from tests.providers.test_anthropic_api import provider as anthropic_provider
from tests.providers.test_openai_compat import completion, responses_provider, responses_reply
from tests.providers.test_openai_compat import provider as openai_provider


def test_json_payloads_have_size_and_scan_work_bounds(monkeypatch):
    oversized = '{"answer":"' + ("x" * (MAX_JSON_TEXT + 1)) + '"}'
    assert extract_json_object(oversized) is None

    calls = 0
    decoder = _json._DECODER

    class CountingDecoder:
        def raw_decode(self, *args):
            nonlocal calls
            calls += 1
            return decoder.raw_decode(*args)

    monkeypatch.setattr(_json, "_DECODER", CountingDecoder())
    deeply_nested = "{" * (512 * 1024)
    assert extract_json_object(deeply_nested) is None
    assert calls < len(deeply_nested) // 8


def test_response_boundary_clamps_usage_and_rejects_malformed_fields():
    response = sanitize_response(
        ModelResponse(
            text=123,  # type: ignore[arg-type]
            served_model={"unexpected": "object"},  # type: ignore[arg-type]
            stop_reason=["stop"],  # type: ignore[arg-type]
            usage=Usage(output_tokens=10**400),
            tool_calls=[ToolCall(id="call", name="lookup", arguments={})],
            raw={"nested": [float("nan"), float("inf")]},
        ),
        "fixture",
    )
    assert response.error == "fixture: malformed response"
    assert response.text is None
    assert response.served_model is None
    assert response.stop_reason is None
    assert response.usage.output_tokens == 1_000_000_000
    assert response.raw is not None
    assert response.raw["nested"] == [None, None]
    assert response.raw["usage_original"]["output_tokens"] == {
        "type": "integer",
        "value": str(10**400),
    }
    assert response.raw["usage_anomaly"] == {"output_tokens": "clamped"}
    json.dumps(response_to_dict(response), allow_nan=False)


def test_replay_rejects_malformed_tool_calls_and_sanitizes_raw_numbers():
    response = response_from_dict(
        {
            "usage": {"output_tokens": float("inf")},
            "raw": {"value": float("nan")},
            "text": "answer",
        }
    )
    assert response.usage.output_tokens == 0
    assert response.raw is not None
    assert response.raw["value"] is None
    assert response.raw["usage_original"]["output_tokens"] == {
        "type": "float",
        "value": "inf",
    }
    assert response.raw["usage_anomaly"] == {"output_tokens": "not_integer"}
    json.dumps(response_to_dict(response), allow_nan=False)

    with pytest.raises(ValueError, match="invalid tool calls"):
        response_from_dict({"tool_calls": [{"id": 5, "name": "lookup", "arguments": {}}]})


@pytest.mark.parametrize("kind", ["openai", "responses", "anthropic"])
async def test_api_usage_is_bounded_before_returning(kind):
    if kind == "openai":
        reply = completion()
        reply.usage.completion_tokens = 10**400
        reply.usage.prompt_tokens = 10**400
        response = await openai_provider(reply).complete(make_request())
    elif kind == "responses":
        reply = responses_reply()
        reply.usage.output_tokens = 10**400
        reply.usage.input_tokens_details.cached_tokens = 10**400
        response = await responses_provider(reply).complete(make_request())
    else:
        reply = api_message()
        reply.usage.output_tokens = 10**400
        response = await anthropic_provider(reply).complete(make_request())

    assert response.usage.output_tokens == 1_000_000_000
    assert response.raw is not None
    assert response.raw["usage_anomaly"]
    json.dumps(response_to_dict(response), allow_nan=False)


@pytest.mark.parametrize("bad_raw", [{"nested": object()}, {"nested": {1: "bad key"}}])
def test_response_boundary_fails_closed_on_non_json_nested_raw_data(bad_raw):
    response = sanitize_response(ModelResponse(text="answer", raw=bad_raw), "fixture")

    assert response.error == "fixture: malformed response"
    assert response.text is None
    assert response.raw is None


def test_response_boundary_deep_copies_nested_tool_arguments():
    arguments = {"nested": [{"values": ["original"]}]}
    response = sanitize_response(
        ModelResponse(tool_calls=[ToolCall("call", "lookup", arguments)]), "fixture"
    )

    arguments["nested"][0]["values"][0] = "source changed"
    assert response.tool_calls[0].arguments["nested"][0]["values"] == ["original"]
    response.tool_calls[0].arguments["nested"][0]["values"][0] = "response changed"
    assert arguments["nested"][0]["values"] == ["source changed"]


def test_normal_provider_raw_usage_mapping_and_codex_list_are_preserved():
    mapping = sanitize_response(
        ModelResponse(
            usage=Usage(input_tokens=3, output_tokens=2),
            raw={"raw_usage": {"input_tokens": 3, "output_tokens": 2}},
        ),
        "fixture",
    )
    codex = sanitize_response(
        ModelResponse(
            usage=Usage(input_tokens=3, output_tokens=2),
            raw={"raw_usage": [{"input_tokens": 3, "output_tokens": 2}]},
        ),
        "codex",
    )

    assert mapping.raw == {"raw_usage": {"input_tokens": 3, "output_tokens": 2}}
    assert codex.raw == {"raw_usage": [{"input_tokens": 3, "output_tokens": 2}]}


@pytest.mark.parametrize(
    "response",
    [
        ModelResponse(text="x" * (MAX_JSON_TEXT + 1)),
        ModelResponse(raw={"value": "x" * (MAX_JSON_TEXT + 1)}),
        ModelResponse(parsed={"value": "x" * (MAX_JSON_TEXT + 1)}),
        ModelResponse(tool_calls=[ToolCall("c", "tool", {"value": "x" * (MAX_JSON_TEXT + 1)})]),
        ModelResponse(raw={"value": 10**400}),
        ModelResponse(raw={"items": [None] * 10_001}),
    ],
)
def test_response_boundary_fails_closed_on_oversized_or_excessive_values(response):
    sanitized = sanitize_response(response, "fixture")

    assert sanitized.error == "fixture: malformed response"
    assert sanitized.text is None
    assert sanitized.parsed is None
    assert sanitized.tool_calls == []
    json.dumps(response_to_dict(sanitized), allow_nan=False)


def test_response_boundary_fails_closed_on_excessive_depth_and_cycles():
    deep: dict[str, object] = {"leaf": "value"}
    for _ in range(MAX_RESPONSE_DEPTH + 2):
        deep = {"next": deep}
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    for response in (
        ModelResponse(raw={"deep": deep}),
        ModelResponse(raw=cyclic),
        ModelResponse(tool_calls=[ToolCall("c", "tool", cyclic)]),
    ):
        sanitized = sanitize_response(response, "fixture")
        assert sanitized.error == "fixture: malformed response"
        assert sanitized.raw is None
        json.dumps(response_to_dict(sanitized), allow_nan=False)


def test_cyclic_raw_usage_keeps_bounded_json_safe_anomaly_evidence():
    cycle: list[object] = []
    cycle.append(cycle)
    response = sanitize_response(
        ModelResponse(
            usage=Usage(input_tokens=3, output_tokens=2),
            raw={"raw_usage": cycle},
        ),
        "fixture",
    )

    assert response.raw is not None
    assert response.raw["usage_anomaly"]
    assert "cycle" in json.dumps(response.raw, allow_nan=False)
    captured = raw_usage_payload({"input_tokens": 1, "bad": cycle})
    assert captured is not None
    json.dumps(captured, allow_nan=False)
    json.dumps(json_safe_usage_value(cycle), allow_nan=False)


async def test_api_provider_rejects_nontext_message_content():
    reply = completion()
    reply.choices[0].message.content = ["unexpected"]
    response = await openai_provider(reply).complete(make_request())
    assert response.error is not None
    assert "malformed response" in response.error


def test_cli_numeric_metadata_is_bounded():
    assert as_int(10**400) == 1_000_000_000
    assert as_float(10**400) is None


def test_diagnostics_redact_url_userinfo_and_query_values():
    text = (
        "request https://private-user:private-passphrase@example.invalid/v1"
        "?api_key=synthetic-value&state=synthetic-state#synthetic-fragment"
    )
    safe = redact(text)
    for private in (
        "private-user",
        "private-passphrase",
        "synthetic-value",
        "synthetic-state",
        "synthetic-fragment",
    ):
        assert private not in safe
    assert "https://<redacted>" in safe
    assert "example.invalid/v1" not in safe


def test_diagnostics_redact_inline_url_repeated_mixed_case_host():
    text = "Failed at https://MixedCase-GW.Internal.Lan:8443/v1 on MIXEDCASE-GW.INTERNAL.LAN"
    safe = redact(text, {})
    assert "mixedcase-gw.internal.lan" not in safe.lower()
    assert safe == "Failed at https://<redacted> on <redacted>"


@pytest.mark.parametrize(
    "name",
    ["TOKEN_BACKUP", "OPENAI_API_KEY_OLD", "DB_URI", "DB_DSN", "HTTP_PROXY"],
)
def test_sensitive_environment_name_segments_are_redacted(name):
    safe = redact("failure: synthetic-sensitive-value", {name: "synthetic-sensitive-value"})
    assert "synthetic-sensitive-value" not in safe


def test_home_paths_are_redacted(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    text = f"{tmp_path}/bin /Users/private-person/bin /home/private-person/bin"
    assert redact(text, {}) == "~/bin /Users/<redacted>/bin /home/<redacted>/bin"


async def test_launch_error_redacts_user_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = await run_command(
        Command(
            argv=(str(tmp_path / "missing-cli"),),
            cwd=str(tmp_path),
            env={},
            timeout_s=1,
        )
    )
    assert result.launch_error is not None
    assert str(tmp_path) not in result.launch_error


def test_muse_raw_subscription_metadata_is_scalar_only(tmp_path):
    from identitybench.providers.muse_cli import MuseCliProvider
    from tests.providers.fakes import make_request
    from tests.providers.test_muse_cli import result as muse_result

    original = muse_result()
    event = {
        "type": "response.subscription_usage",
        "subscription": {
            "used_percent": 2.5,
            "active": True,
            "email": "private@example.invalid",
            "nested": {"detail": "private"},
            "bad": float("nan"),
        },
    }
    stderr = (
        original.stderr
        + "SSE RESPONSE: event: response.subscription_usage\\ndata: "
        + json.dumps(event)
        + "\n"
    )
    response = MuseCliProvider("muse-test-model", version="fixture")._parse(
        ProcessResult(0, original.stdout, stderr), make_request(), Path(tmp_path)
    )
    assert response.raw["subscription_usage"] == {"used_percent": 2.5, "active": True}
