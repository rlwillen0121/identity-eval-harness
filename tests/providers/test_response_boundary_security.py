"""Offline regressions for untrusted response data and diagnostic boundaries."""

import json
import time
from pathlib import Path

import pytest

from identitybench.providers import _json
from identitybench.providers._cli import as_float, as_int
from identitybench.providers._subprocess import Command, ProcessResult, redact, run_command
from identitybench.providers.claude_cli import ClaudeCliProvider
from identitybench.providers.replay import response_from_dict, response_to_dict
from tests.providers.fakes import make_request
from tests.providers.test_anthropic_api import api_message
from tests.providers.test_anthropic_api import provider as anthropic_provider
from tests.providers.test_openai_compat import completion, responses_provider, responses_reply
from tests.providers.test_openai_compat import provider as openai_provider


@pytest.mark.parametrize("value", [-(10**12), 10**400, float("nan"), float("inf"), "wrong", True])
@pytest.mark.parametrize("kind", ["openai", "responses", "anthropic", "replay"])
async def test_usage_is_bounded_and_cache_serializable(kind, value):
    if kind == "openai":
        reply = completion()
        reply.usage.completion_tokens = value
        reply.usage.prompt_tokens = value
        response = await openai_provider(reply).complete(make_request())
    elif kind == "responses":
        reply = responses_reply()
        reply.usage.output_tokens = value
        reply.usage.input_tokens_details.cached_tokens = value
        response = await responses_provider(reply).complete(make_request())
    elif kind == "anthropic":
        reply = api_message()
        reply.usage.output_tokens = value
        response = await anthropic_provider(reply).complete(make_request())
    else:
        response = response_from_dict({"usage": {"output_tokens": value}})
    assert 0 <= response.usage.output_tokens <= 1_000_000_000
    json.dumps(response_to_dict(response), allow_nan=False)


@pytest.mark.parametrize("kind", ["openai", "responses", "anthropic", "replay"])
async def test_metadata_and_raw_are_safe(kind):
    if kind == "openai":
        reply = completion()
        reply.model = {"bad": "model"}
        reply.id = float("nan")
        reply.choices[0].finish_reason = ["stop"]
        response = await openai_provider(reply).complete(make_request())
    elif kind == "responses":
        reply = responses_reply()
        reply.model = []
        reply.id = float("nan")
        response = await responses_provider(reply).complete(make_request())
    elif kind == "anthropic":
        reply = api_message()
        reply.model = 123
        reply.id = float("inf")
        response = await anthropic_provider(reply).complete(make_request())
    else:
        response = response_from_dict(
            {
                "text": [],
                "served_model": {},
                "stop_reason": 4,
                "raw": {"nested": [float("nan"), {"x": float("inf")}]},
            }
        )
    assert response.served_model is None or isinstance(response.served_model, str)
    assert response.stop_reason is None or isinstance(response.stop_reason, str)
    assert response.text is None or isinstance(response.text, str)
    json.dumps(response_to_dict(response), allow_nan=False)


@pytest.mark.parametrize("bad", [None, [], 42])
@pytest.mark.parametrize("field", ["id", "name"])
@pytest.mark.parametrize("kind", ["openai", "responses", "anthropic", "replay"])
async def test_invalid_tool_identifiers_are_provider_errors(kind, field, bad):
    if kind == "openai":
        reply = completion(
            tool_calls=[
                {
                    "id": "call",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": "{}"},
                }
            ]
        )
        call = reply.choices[0].message.tool_calls[0]
        setattr(call if field == "id" else call.function, field, bad)
        response = await openai_provider(reply).complete(make_request())
    elif kind == "responses":
        reply = responses_reply(
            None,
            function_calls=[
                {
                    "call_id": "call",
                    "name": "lookup",
                    "arguments": "{}",
                }
            ],
        )
        setattr(reply.output[-1], "call_id" if field == "id" else field, bad)
        response = await responses_provider(reply).complete(make_request())
    elif kind == "anthropic":
        reply = api_message([{"type": "tool_use", "id": "call", "name": "lookup", "input": {}}])
        setattr(reply.content[0], field, bad)
        response = await anthropic_provider(reply).complete(make_request())
    else:
        call = {"id": "call", "name": "lookup", "arguments": {}}
        call[field] = bad
        response = response_from_dict({"tool_calls": [call]})
    assert response.error and "malformed response" in response.error
    assert not response.tool_calls


@pytest.mark.parametrize("content", [[], 123, {"content": "bad"}])
async def test_chat_nontext_content_is_rejected(content):
    reply = completion()
    reply.choices[0].message.content = content
    response = await openai_provider(reply).complete(make_request())
    assert response.error and "malformed response" in response.error


def test_cli_huge_token_count_is_capped():
    assert as_int(10**400) <= 1_000_000_000
    assert as_float(10**400) is None


def test_json_scanning_has_a_decode_budget(monkeypatch):
    calls = 0
    decoder = _json._DECODER

    class CountingDecoder:
        def raw_decode(self, *args):
            nonlocal calls
            calls += 1
            return decoder.raw_decode(*args)

    monkeypatch.setattr(_json, "_DECODER", CountingDecoder())
    text = "{" * (512 * 1024)
    assert _json.extract_json_object(text) is None
    assert calls < len(text) // 8


def test_prose_with_many_stray_braces_still_yields_the_answer():
    text = "use {name} and {id} here\n" * 2000 + '{"answer": 1}'
    assert _json.extract_json_object(text) == {"answer": 1}


def test_oversized_json_is_unparseable():
    assert _json.extract_json_object('{"x":"' + "x" * (2 * 1024 * 1024) + '"}') is None


def test_repeated_reasoning_tags_are_bounded():
    start = time.monotonic()
    assert _json.strip_reasoning("<think>" * 16000) == ""
    assert time.monotonic() - start < 1.0


def test_home_paths_are_redacted(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    text = f"{tmp_path}/bin /Users/private-person/bin /home/private-person/bin"
    assert redact(text, {}) == "~/bin /Users/<redacted>/bin /home/<redacted>/bin"
    provider = ClaudeCliProvider("test", version="fixture")
    response = provider._fail("could not set up a private CLI home", ProcessResult(1, "", text))
    assert "private-person" not in response.error
    assert str(tmp_path) not in response.error


async def test_launch_error_is_redacted():
    result = await run_command(
        Command(
            argv=("/Users/private-person/nonexistent-cli",),
            cwd="/tmp",
            env={},
            timeout_s=1,
        )
    )
    assert result.launch_error and "private-person" not in result.launch_error


@pytest.mark.parametrize(
    "name",
    [
        "HTTPS_PROXY",
        "ALL_PROXY",
        "DB_ADDR",
        "DB_URI",
        "DB_DSN",
        "OPENAI_API_KEY_OLD",
        "TOKEN_BACKUP",
        "MY_SECRET_VALUE",
        "PASSWORD_PREVIOUS",
    ],
)
def test_sensitive_env_name_variants(name):
    assert "sensitive-value" not in redact("failure: sensitive-value", {name: "sensitive-value"})
    assert redact("tiny", {name: "tiny"}) == "tiny"


def test_unknown_url_credentials_are_masked():
    assert redact("connect https://private-user:private-password@example.test/path", {}) == (
        "connect https://<redacted>@example.test/path"
    )


def test_muse_subscription_only_keeps_numeric_boolean_scalars(tmp_path):
    from identitybench.providers.muse_cli import MuseCliProvider
    from tests.providers.test_muse_cli import result

    original = result()
    event = {
        "type": "response.subscription_usage",
        "subscription": {
            "used_percent": 2.5,
            "active": True,
            "email": "private@example.test",
            "nested": {"token": "sensitive"},
            "bad": float("nan"),
        },
    }
    stderr = (
        original.stderr
        + "SSE RESPONSE: event: response.subscription_usage\\ndata: "
        + json.dumps(event)
        + "\n"
    )
    response = MuseCliProvider("muse-wonder-full-1.0", version="fixture")._parse(
        ProcessResult(0, original.stdout, stderr),
        make_request(),
        Path(tmp_path),
    )
    assert response.raw["subscription_usage"] == {"used_percent": 2.5, "active": True}


async def test_raw_only_nonfinite_values_do_not_discard_a_valid_answer():
    reply = completion('{"decision":"DENY"}')
    reply.id = float("nan")
    response = await openai_provider(reply).complete(make_request())
    assert response.ok
    assert response.text == '{"decision":"DENY"}'
    assert response.raw["id"] is None
    json.dumps(response_to_dict(response), allow_nan=False)


def test_reasoning_tag_pairing_and_span_membership_are_linear():
    text = "prefix " + '{"note":"<think>"}<think>draft</think>' * 100
    assert _json.strip_reasoning(text) == "prefix " + '{"note":"<think>"}' * 100
