from __future__ import annotations

import json
from dataclasses import replace

import pytest

from identitybench.providers._cli import (
    ACTION_SCHEMA_NAME,
    MAX_ARG_BYTES,
    NEUTRAL_SYSTEM,
    SYSTEM_BEGIN,
    SYSTEM_END,
    SYSTEM_POINTER,
)
from identitybench.providers.base import Message, Role
from identitybench.providers.claude_cli import ClaudeCliProvider
from identitybench.render import tools as render_tools
from tests.providers.fakes import DECISION_SCHEMA, FakeRunner, flag_value, make_request, ok

# Real `claude -p --output-format json --json-schema ...` output (CLI 2.1.296).
VERIFIED = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 2,
    "duration_ms": 1213,
    "total_cost_usd": 0.0003247,
    "structured_output": {"decision": "DENY", "rules": ["LIFE-001"]},
    "result": '{"decision": "DENY", "rules": ["LIFE-001"]}',
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 1215,
        "cache_read_input_tokens": 0,
        "output_tokens": 163,
        "output_tokens_details": {"thinking_tokens": 0},
    },
    "modelUsage": {"claude-haiku-5-5": {}},
}


def provider(runner: FakeRunner, **kwargs) -> ClaudeCliProvider:
    return ClaudeCliProvider(
        "claude-haiku-5-5", runner=runner, version="2.1.296", retry_base_delay_s=0, **kwargs
    )


async def test_argv_matches_the_stripped_down_invocation() -> None:
    runner = FakeRunner(ok(VERIFIED))
    await provider(runner).complete(make_request("Decide REQ-1.", system="SYS"))
    command = runner.command
    assert command.argv == (
        "claude",
        "-p",
        "--model",
        "claude-haiku-5-5",
        "--system-prompt",
        "SYS",
        "--tools",
        "",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--no-session-persistence",
        "--safe-mode",
        "--disable-slash-commands",
        "--no-chrome",
        "--output-format",
        "json",
    )
    assert command.stdin == "Decide REQ-1."
    assert "--fallback-model" not in command.argv


@pytest.mark.parametrize("system", ["", " \n"])
async def test_an_empty_system_prompt_is_replaced_by_the_neutral_one(system: str) -> None:
    runner = FakeRunner(ok(VERIFIED))
    response = await provider(runner).complete(make_request("Decide.", system=system))
    command = runner.command
    assert flag_value(command.argv, "--system-prompt") == NEUTRAL_SYSTEM
    assert command.stdin == "Decide."
    assert response.raw is not None
    assert response.raw["substituted_system"] == NEUTRAL_SYSTEM


async def test_a_real_system_prompt_is_not_recorded_as_substituted() -> None:
    response = await provider(FakeRunner(ok(VERIFIED))).complete(make_request(system="SYS"))
    assert "substituted_system" not in (response.raw or {})


@pytest.mark.parametrize("system", ["- Follow the policy.", "---\ntitle: policy\n---\nRules."])
async def test_a_system_prompt_starting_with_a_dash_is_passed_verbatim(system: str) -> None:
    """The claude CLI's parser takes the next argument as a required option's value, whatever
    it starts with (unlike grok's; see test_grok_cli)."""
    runner = FakeRunner(ok(VERIFIED))
    await provider(runner).complete(make_request(system=system))
    assert flag_value(runner.command.argv, "--system-prompt") == system


async def test_schema_and_effort_flags() -> None:
    runner = FakeRunner(ok(VERIFIED))
    await provider(runner, binary="/opt/claude").complete(
        make_request(schema=DECISION_SCHEMA, effort="high")
    )
    argv = runner.command.argv
    assert argv[0] == "/opt/claude"
    assert json.loads(flag_value(argv, "--json-schema")) == DECISION_SCHEMA
    assert flag_value(argv, "--effort") == "high"


async def test_an_action_protocol_turn_is_sent_without_the_schema() -> None:
    """``--json-schema`` gives the model a ``StructuredOutput`` tool, and with it the model
    called the listed lookups natively and gave up on them when the CLI refused; the action
    comes back as text instead, which the runner extracts (see the module docstring)."""
    runner = FakeRunner(ok(VERIFIED))
    action = replace(make_request(schema=DECISION_SCHEMA), schema_name=ACTION_SCHEMA_NAME)
    response = await provider(runner).complete(action)
    assert "--json-schema" not in runner.command.argv
    assert response.raw is not None
    assert response.raw["json_schema"] == "withheld (JSON action protocol turn)"
    decision = FakeRunner(ok(VERIFIED))
    await provider(decision).complete(replace(action, schema_name="access_request_decision"))
    assert json.loads(flag_value(decision.command.argv, "--json-schema")) == DECISION_SCHEMA


def test_the_action_schema_name_is_the_renderers() -> None:
    assert ACTION_SCHEMA_NAME == render_tools.ACTION_SCHEMA_NAME


async def test_request_effort_overrides_the_constructor_default() -> None:
    runner = FakeRunner(ok(VERIFIED))
    claude = provider(runner, effort="low")
    await claude.complete(make_request())
    await claude.complete(make_request(effort="max"))
    assert flag_value(runner.commands[0].argv, "--effort") == "low"
    assert flag_value(runner.commands[1].argv, "--effort") == "max"


async def test_env_drops_api_credentials_and_parent_session_vars(monkeypatch) -> None:
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_EFFORT",
        "CLAUDE_PID",
    ):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "subscription-login")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/tmp/cfg")
    monkeypatch.setenv("OPENAI_API_KEY", "another-providers-key")
    monkeypatch.setenv("DISABLE_AUTOUPDATER", "0")
    runner = FakeRunner(ok(VERIFIED))
    await provider(runner).complete(make_request())
    env = runner.command.env
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_EFFORT",
        "CLAUDE_PID",
    ):
        assert name not in env, name
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "subscription-login"
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/cfg"
    assert "OPENAI_API_KEY" not in env  # another provider's key is not allow-listed
    assert "PATH" in env
    # The self-updater is off for every call, so the probed version is the one that runs.
    assert env["DISABLE_AUTOUPDATER"] == "1"


async def test_the_updater_is_disabled_even_when_the_parent_never_mentions_it(monkeypatch) -> None:
    monkeypatch.delenv("DISABLE_AUTOUPDATER", raising=False)
    runner = FakeRunner(ok(VERIFIED))
    await provider(runner).complete(make_request())
    assert runner.command.env["DISABLE_AUTOUPDATER"] == "1"


async def test_parses_the_verified_payload() -> None:
    response = await provider(FakeRunner(ok(VERIFIED))).complete(
        make_request(schema=DECISION_SCHEMA)
    )
    assert response.ok
    assert response.parsed == {"decision": "DENY", "rules": ["LIFE-001"]}
    assert json.loads(response.text or "") == response.parsed
    assert response.usage.input_tokens == 2
    assert response.usage.cache_write_tokens == 1215
    assert response.usage.cache_read_tokens == 0
    assert response.usage.output_tokens == 163
    assert response.usage.reasoning_tokens == 0
    assert response.reported_cost_usd == pytest.approx(0.0003247)
    assert response.served_model == "claude-haiku-5-5"
    assert response.raw is not None
    assert response.raw["num_turns"] == 2
    assert response.raw["subtype"] == "success"


async def test_structured_output_without_result_text_is_serialized() -> None:
    payload = {**VERIFIED, "result": ""}
    response = await provider(FakeRunner(ok(payload))).complete(make_request())
    assert response.text == '{"decision": "DENY", "rules": ["LIFE-001"]}'


async def test_plain_text_result_without_schema() -> None:
    payload = {k: v for k, v in VERIFIED.items() if k != "structured_output"}
    payload["result"] = 'Here is my answer: {"decision": "APPROVE"}'
    response = await provider(FakeRunner(ok(payload))).complete(make_request())
    assert response.ok
    assert response.parsed is None
    assert response.text == payload["result"]


async def test_thinking_tokens_are_reported_as_reasoning() -> None:
    payload = json.loads(json.dumps(VERIFIED))
    payload["usage"]["output_tokens_details"]["thinking_tokens"] = 120
    response = await provider(FakeRunner(ok(payload))).complete(make_request())
    assert response.usage.reasoning_tokens == 120


async def test_multiple_models_are_all_reported() -> None:
    payload = {**VERIFIED, "modelUsage": {"claude-opus-5-5": {}, "claude-haiku-5-5": {}}}
    response = await provider(FakeRunner(ok(payload))).complete(make_request())
    assert response.served_model == "claude-haiku-5-5,claude-opus-5-5"


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 2},
        {"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in"},
        {"type": "result", "subtype": "error_during_execution", "is_error": False},
    ],
)
async def test_error_payloads_become_errors_with_usage_kept(payload: dict) -> None:
    payload = {**payload, "usage": {"input_tokens": 5, "output_tokens": 1}, "total_cost_usd": 0.1}
    response = await provider(FakeRunner(ok(payload, returncode=1))).complete(make_request())
    assert response.error is not None
    assert response.error.startswith("claude_cli: ")
    assert response.usage.input_tokens == 5
    assert response.reported_cost_usd == 0.1
    assert not response.ok


async def test_refusal_stop_reason() -> None:
    payload = {**VERIFIED, "stop_reason": "refusal", "result": "I can't help with that."}
    del payload["structured_output"]
    response = await provider(FakeRunner(ok(payload))).complete(make_request())
    assert response.refused
    assert response.error is None
    assert not response.ok
    assert response.stop_reason == "refusal"


async def test_unparseable_stdout_is_an_error_with_stderr() -> None:
    runner = FakeRunner(ok("Segmentation fault", stderr="boom", returncode=139))
    response = await provider(runner).complete(make_request())
    assert response.error is not None
    assert "no JSON result" in response.error
    assert "exit 139" in response.error
    assert "boom" in response.error


async def test_large_system_prompt_moves_to_stdin() -> None:
    runner = FakeRunner(ok(VERIFIED))
    system = "R" * (MAX_ARG_BYTES + 10)
    await provider(runner).complete(make_request("Decide.", system=system))
    command = runner.command
    assert flag_value(command.argv, "--system-prompt") == SYSTEM_POINTER
    assert command.stdin == f"{SYSTEM_BEGIN}\n{system}\n{SYSTEM_END}\n\nDecide."
    assert all(len(arg.encode()) <= MAX_ARG_BYTES for arg in command.argv)


async def test_multi_turn_transcript_goes_in_as_one_user_message() -> None:
    runner = FakeRunner(ok(VERIFIED))
    messages = (
        Message(Role.user, "first"),
        Message(Role.assistant, "reply"),
        Message(Role.user, "second"),
    )
    await provider(runner).complete(make_request(messages=messages))
    assert runner.command.stdin == "[User]\nfirst\n\n[Assistant]\nreply\n\n[User]\nsecond"


async def test_success_payload_with_a_nonzero_exit_is_an_error() -> None:
    response = await provider(FakeRunner(ok(VERIFIED, returncode=1))).complete(
        make_request(schema=DECISION_SCHEMA)
    )
    assert response.error is not None
    assert "nonzero exit" in response.error
    assert response.parsed == {"decision": "DENY", "rules": ["LIFE-001"]}  # kept for debugging
