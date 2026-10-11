"""Behaviour shared by every CLI provider (exercised through concrete providers)."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from identitybench.providers import _cli, claude_cli, codex_cli, grok_cli
from identitybench.providers._cli import (
    MAX_ARG_BYTES,
    NEUTRAL_SYSTEM,
    SYSTEM_BEGIN,
    SYSTEM_END,
    SYSTEM_POINTER,
    as_float,
    as_int,
    link_credentials,
    parse_json_payload,
    parse_jsonl,
    prepend_system,
    probe_version,
    render_transcript,
    served_models,
    structured_text,
    sync_back,
    system_override,
    valid_effort,
)
from identitybench.providers._subprocess import Command, ProcessResult
from identitybench.providers.base import Message, Provider, Role, ToolCall
from identitybench.providers.claude_cli import ClaudeCliProvider
from identitybench.providers.codex_cli import CodexCliProvider
from identitybench.providers.cursor_cli import CursorCliProvider
from identitybench.providers.grok_cli import GrokCliProvider
from tests.providers.fakes import LOOKUP_TOOL, FakeRunner, make_request, ok

SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "hello",
    "usage": {"input_tokens": 3, "output_tokens": 2},
}


def claude(runner: FakeRunner, **kwargs) -> ClaudeCliProvider:
    kwargs.setdefault("retry_base_delay_s", 0)
    return ClaudeCliProvider("claude-haiku-5-5", runner=runner, version="2.1.296", **kwargs)


# --------------------------------------------------------------------------- pure helpers


def test_single_user_message_is_passed_through_verbatim() -> None:
    assert render_transcript((Message(Role.user, "exact  text\n"),)) == "exact  text\n"


def test_multi_turn_transcript_is_rendered_with_roles_and_tool_traffic() -> None:
    messages = (
        Message(Role.user, "Decide REQ-1."),
        Message(
            Role.assistant,
            "Looking it up.",
            tool_calls=(ToolCall("call_1", "get_identity", {"id": "U1"}),),
        ),
        Message(Role.tool, '{"id": "U1"}', tool_call_id="call_1"),
        Message(Role.user, "Now decide."),
    )
    rendered = render_transcript(messages)
    assert rendered == (
        "[User]\nDecide REQ-1.\n\n"
        "[Assistant]\nLooking it up.\n"
        '(tool call get_identity id=call_1: {"id": "U1"})\n\n'
        '[Tool result call_1]\n{"id": "U1"}\n\n'
        "[User]\nNow decide."
    )


def test_prepend_system_uses_delimiters_and_skips_empty_system() -> None:
    assert prepend_system("", "body") == "body"
    assert prepend_system("rules", "body") == f"{SYSTEM_BEGIN}\nrules\n{SYSTEM_END}\n\nbody"


def test_system_override() -> None:
    assert system_override("SYS", "body") == ("SYS", "body", {})
    neutral = {"substituted_system": NEUTRAL_SYSTEM}
    assert system_override("", "body") == (NEUTRAL_SYSTEM, "body", neutral)
    assert system_override(" \n\t", "body") == (NEUTRAL_SYSTEM, "body", neutral)
    big = "x" * (MAX_ARG_BYTES + 1)
    assert system_override(big, "body") == (
        SYSTEM_POINTER,
        f"{SYSTEM_BEGIN}\n{big}\n{SYSTEM_END}\n\nbody",
        {},
    )
    # The prefix shares the argv element, so it counts toward the limit.
    fits = "x" * (MAX_ARG_BYTES - 3)
    assert system_override(fits, "body")[0] == fits
    assert system_override(fits, "body", prefix="--a=")[0] == SYSTEM_POINTER


def test_parse_json_payload() -> None:
    assert parse_json_payload('{"a": 1}') == {"a": 1}
    assert parse_json_payload('warning: something\n{"a": 2}\n') == {"a": 2}
    assert parse_json_payload('{"a": 1}\n{"b": 2}') == {"b": 2}
    assert parse_json_payload("") is None
    assert parse_json_payload("not json") is None
    assert parse_json_payload("[1, 2]") is None


def test_parse_jsonl_skips_noise() -> None:
    stdout = '{"type": "a"}\nnoise\n{"broken": \n[1]\n{"type": "b"}\n'
    assert parse_jsonl(stdout) == [{"type": "a"}, {"type": "b"}]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, 5),
        (5.9, 5),
        (None, 0),
        ("7", 0),
        (True, 0),
        (-3, 0),
        (float("nan"), 0),
        (float("inf"), 0),
        (float("-inf"), 0),
    ],
)
def test_as_int(value: object, expected: int) -> None:
    assert as_int(value) == expected


def test_as_int_survives_non_finite_numbers_from_json() -> None:
    payload = json.loads('{"a": NaN, "b": Infinity}')
    assert as_int(payload["a"]) == 0
    assert as_int(payload["b"]) == 0


def test_as_float_and_served_models() -> None:
    assert as_float(0.5) == 0.5
    assert as_float(1) == 1.0
    assert as_float("0.5") is None
    assert as_float(None) is None
    assert as_float(True) is None
    assert as_float(float("nan")) is None
    assert as_float(float("inf")) is None
    assert served_models({"b": {}, "a": {}}) == "a,b"
    assert served_models({}) is None
    assert served_models(None) is None


def test_structured_text_agrees_with_the_structured_output() -> None:
    # The CLI's own text is kept only when it is the structured output (formatting preserved).
    assert structured_text('{"a":1}', {"a": 1}) == ('{"a":1}', None)
    # Otherwise the text is the structured output, and the CLI text is returned for raw.
    assert structured_text('{"a":2}', {"a": 1}) == ('{"a": 1}', '{"a":2}')
    assert structured_text("Here you go", {"a": 1}) == ('{"a": 1}', "Here you go")
    assert structured_text("", {"a": 1}) == ('{"a": 1}', None)
    assert structured_text(None, {"a": 1}) == ('{"a": 1}', None)
    # No structured output: the CLI text as is.
    assert structured_text("plain", None) == ("plain", None)
    assert structured_text(None, None) == (None, None)


def test_link_credentials_links_only_existing_files(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "auth.json").write_text('{"k": 1}', encoding="utf-8")
    (real / "auth.json.lock").write_text("", encoding="utf-8")
    links = link_credentials(
        real, tmp_path / "private", ("auth.json", "missing.json"), shared=("auth.json.lock",)
    )
    assert [(item.link.name, item.sync_back) for item in links] == [
        ("auth.json", True),
        ("auth.json.lock", False),
    ]
    for item in links:
        assert item.link.is_symlink()
        assert item.link.resolve() == item.real.resolve()
    assert not (tmp_path / "private" / "missing.json").exists()


def test_sync_back_copies_a_replaced_login_file_atomically(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "auth.json").write_text('{"token": "old"}', encoding="utf-8")
    (link,) = link_credentials(real, tmp_path / "private", ("auth.json",))
    # The CLI refreshed its token by writing a new file and renaming it over the link.
    staged = link.link.with_name("auth.json.tmp")
    staged.write_text('{"token": "new"}', encoding="utf-8")
    os.replace(staged, link.link)
    sync_back([link])
    assert json.loads((real / "auth.json").read_text(encoding="utf-8")) == {"token": "new"}
    assert stat.S_IMODE((real / "auth.json").stat().st_mode) == 0o600
    assert sorted(p.name for p in real.iterdir()) == ["auth.json"]


def test_sync_back_leaves_the_real_file_alone_otherwise(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "auth.json").write_text('{"token": "old"}', encoding="utf-8")
    (real / "auth.json.lock").write_text("", encoding="utf-8")
    links = link_credentials(real, tmp_path / "private", ("auth.json",), shared=("auth.json.lock",))
    sync_back(links)  # links intact (writes went through them): nothing to do
    assert (real / "auth.json").read_text(encoding="utf-8") == '{"token": "old"}'
    auth, lock = links
    for junk in ("", "not json", "[1]"):
        auth.link.unlink(missing_ok=True)
        auth.link.write_text(junk, encoding="utf-8")
        sync_back([auth])
        assert (real / "auth.json").read_text(encoding="utf-8") == '{"token": "old"}'
    lock.link.unlink()
    lock.link.write_text("{}", encoding="utf-8")
    sync_back([lock])  # lock files are shared, never synced
    assert (real / "auth.json.lock").read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max", "minimal"])
def test_valid_effort(effort: str) -> None:
    assert valid_effort(effort)


@pytest.mark.parametrize("effort", ['high"; x=1', "", "high low", "-x", "a" * 40])
def test_invalid_effort(effort: str) -> None:
    assert not valid_effort(effort)


def test_probe_version_reads_first_line_and_handles_missing_binaries() -> None:
    probe_version.cache_clear()
    assert probe_version(sys.executable).startswith("Python 3.")
    assert probe_version("/nonexistent/binary") == "not-installed"


# --------------------------------------------------------------------------- provider behaviour


@pytest.mark.parametrize(
    "provider",
    [
        ClaudeCliProvider("m", version="1"),
        CodexCliProvider("m", version="1"),
        GrokCliProvider("m", version="1"),
        CursorCliProvider(version="1"),
    ],
)
def test_cli_providers_satisfy_the_protocol_and_reject_tools_and_temperature(
    provider: Provider,
) -> None:
    assert isinstance(provider, Provider)
    caps = provider.capabilities()
    assert caps.native_tools is False
    assert caps.supports_temperature is False


def test_endpoint_identity_hashes_kind_version_and_model() -> None:
    a = ClaudeCliProvider("claude-haiku-5-5", version="2.1.296")
    assert (
        a.endpoint_identity()
        == ClaudeCliProvider("claude-haiku-5-5", version="2.1.296").endpoint_identity()
    )
    assert (
        a.endpoint_identity()
        != ClaudeCliProvider("claude-haiku-5-5", version="2.1.297").endpoint_identity()
    )
    assert (
        a.endpoint_identity()
        != ClaudeCliProvider("claude-opus-5-5", version="2.1.296").endpoint_identity()
    )
    assert (
        a.endpoint_identity()
        != GrokCliProvider("claude-haiku-5-5", version="2.1.296").endpoint_identity()
    )
    assert len(a.endpoint_identity()) == 16


def test_endpoint_identity_includes_the_constructor_default_effort() -> None:
    identities = {
        ClaudeCliProvider("m", version="1", effort=effort).endpoint_identity()
        for effort in (None, "low", "high")
    }
    assert len(identities) == 3
    assert (
        CodexCliProvider("m", version="1").endpoint_identity()
        != CodexCliProvider("m", version="1", effort="high").endpoint_identity()
    )


def test_endpoint_identity_changes_with_the_codex_feature_and_config_setup(monkeypatch) -> None:
    before = CodexCliProvider("m", version="1").endpoint_identity()
    monkeypatch.setattr(codex_cli, "DISABLED_FEATURES", codex_cli.DISABLED_FEATURES[1:])
    fewer_features = CodexCliProvider("m", version="1").endpoint_identity()
    monkeypatch.setattr(codex_cli, "CONFIG_OVERRIDES", ())
    no_overrides = CodexCliProvider("m", version="1").endpoint_identity()
    assert len({before, fewer_features, no_overrides}) == 3


def test_endpoint_identity_changes_with_the_grok_isolation_setup(monkeypatch) -> None:
    before = GrokCliProvider("m", version="1").endpoint_identity()
    monkeypatch.setattr(grok_cli, "ISOLATION_ENV", {**grok_cli.ISOLATION_ENV, "GROK_MEMORY": "1"})
    changed_env = GrokCliProvider("m", version="1").endpoint_identity()
    monkeypatch.setattr(grok_cli, "DISALLOWED_TOOLS", grok_cli.DISALLOWED_TOOLS[:2])
    changed_tools = GrokCliProvider("m", version="1").endpoint_identity()
    assert len({before, changed_env, changed_tools}) == 3


def test_endpoint_identity_changes_with_the_environment_rules_and_neutral_system(
    monkeypatch,
) -> None:
    before = ClaudeCliProvider("m", version="1").endpoint_identity()
    monkeypatch.setattr(ClaudeCliProvider, "ENV_SET", {})
    no_env_set = ClaudeCliProvider("m", version="1").endpoint_identity()
    monkeypatch.setattr(_cli, "NEUTRAL_SYSTEM", "Be terse.")
    other_neutral = ClaudeCliProvider("m", version="1").endpoint_identity()
    assert len({before, no_env_set, other_neutral}) == 3


@pytest.mark.parametrize(
    "make",
    [
        lambda **kw: ClaudeCliProvider("m", version="1", **kw),
        lambda **kw: CodexCliProvider("m", version="1", **kw),
        lambda **kw: GrokCliProvider("m", version="1", **kw),
        lambda **kw: CursorCliProvider(version="1", **kw),
    ],
)
def test_setup_revision_is_independent_of_machine_details(
    make, tmp_path: Path, monkeypatch
) -> None:
    """Same setup, same revision: whatever the temporary directory, the binary's location, the
    model name or whether the user is logged in."""
    first = make().setup_revision()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    assert make(binary="/opt/elsewhere/cli").setup_revision() == first
    for name in ("GROK_HOME", "CODEX_HOME"):
        empty = tmp_path / f"no-login-{name}"
        empty.mkdir()
        monkeypatch.setenv(name, str(empty))
    assert make().setup_revision() == first
    assert len(first) == 16


def test_setup_revision_ignores_the_model_but_the_identity_does_not() -> None:
    a, b = GrokCliProvider("grok-a", version="1"), GrokCliProvider("grok-b", version="1")
    assert a.setup_revision() == b.setup_revision()
    assert a.endpoint_identity() != b.endpoint_identity()


def test_setup_revision_covers_how_an_action_protocol_turn_is_sent(monkeypatch) -> None:
    """``claude_cli`` withholds ``--json-schema`` from a JSON action protocol turn; a change to
    that must reach the cache key, so the revision builds such a turn as a template."""
    before = ClaudeCliProvider("m", version="1").setup_revision()
    monkeypatch.setattr(claude_cli, "ACTION_SCHEMA_NAME", "not_the_action_schema")
    assert ClaudeCliProvider("m", version="1").setup_revision() != before


def test_setup_revisions_differ_between_cli_kinds() -> None:
    revisions = {
        ClaudeCliProvider("m", version="1").setup_revision(),
        CodexCliProvider("m", version="1").setup_revision(),
        GrokCliProvider("m", version="1").setup_revision(),
        CursorCliProvider("m", version="1").setup_revision(),
    }
    assert len(revisions) == 4


def test_setup_revision_leaves_nothing_behind(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    GrokCliProvider("m", version="1").setup_revision()
    CodexCliProvider("m", version="1").setup_revision()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("make", "prompt_of"),
    [
        (
            lambda runner: CodexCliProvider("m", runner=runner, version="1"),
            lambda command: command.stdin,
        ),
        (
            lambda runner: CursorCliProvider(runner=runner, version="1", binary="cursor-agent"),
            lambda command: command.argv[-1],
        ),
    ],
)
async def test_clis_without_a_system_override_send_an_empty_system_as_nothing(
    make, prompt_of
) -> None:
    """Codex and Cursor keep their own system prompt whatever the request says, and an empty
    request system adds no delimiter block, so there is nothing to substitute."""
    runner = FakeRunner(ok(SUCCESS))
    response = await make(runner).complete(make_request("Decide.", system=""))
    assert prompt_of(runner.command) == "Decide."
    assert "substituted_system" not in (response.raw or {})


def test_endpoint_identity_probes_the_binary_version_once() -> None:
    probe_version.cache_clear()
    provider = ClaudeCliProvider("m", binary=sys.executable)
    first = provider.endpoint_identity()
    assert provider.endpoint_identity() == first
    assert probe_version.cache_info().misses == 1


async def test_runs_in_a_fresh_empty_directory_that_is_removed_afterwards() -> None:
    runner = FakeRunner(ok(SUCCESS))
    provider = claude(runner)
    await provider.complete(make_request())
    await provider.complete(make_request())
    first, second = (Path(command.cwd) for command in runner.commands)
    assert first != second
    assert runner.snapshots[0].cwd_entries == []
    assert not first.exists()
    assert not second.exists()


async def test_tools_are_rejected_without_spawning_anything() -> None:
    runner = FakeRunner(ok(SUCCESS))
    response = await claude(runner).complete(make_request(tools=(LOOKUP_TOOL,)))
    assert response.error is not None
    assert "tools" in response.error
    assert runner.commands == []


async def test_empty_transcript_is_rejected() -> None:
    runner = FakeRunner(ok(SUCCESS))
    response = await claude(runner).complete(make_request(messages=()))
    assert response.error == "claude_cli: request has no messages"
    assert runner.commands == []


async def test_invalid_effort_is_rejected() -> None:
    runner = FakeRunner(ok(SUCCESS))
    response = await claude(runner).complete(make_request(effort='high" --evil'))
    assert response.error is not None
    assert "invalid effort" in response.error
    assert runner.commands == []


async def test_oversized_argument_is_rejected_not_truncated() -> None:
    runner = FakeRunner(ok(SUCCESS))
    huge = "x" * (MAX_ARG_BYTES + 1)
    response = await CursorCliProvider(runner=runner, version="1").complete(make_request(huge))
    assert response.error is not None
    assert "argv limit" in response.error
    assert runner.commands == []


async def test_timeout_is_an_error_and_is_not_retried() -> None:
    runner = FakeRunner(ProcessResult(returncode=-9, stdout="", stderr="", timed_out=True))
    response = await claude(runner, timeout_s=5).complete(make_request())
    assert response.error == "claude_cli: no result within the 5s timeout"
    assert len(runner.commands) == 1


async def test_launch_error_is_an_error() -> None:
    runner = FakeRunner(
        ProcessResult(returncode=-1, stdout="", stderr="", launch_error="could not start 'x'")
    )
    response = await claude(runner).complete(make_request())
    assert response.error == "claude_cli: could not start 'x'"
    assert not response.ok


async def test_transient_failures_are_retried_until_success() -> None:
    overloaded = {
        **SUCCESS,
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "API Error: 529 Overloaded",
    }
    runner = FakeRunner(ok(overloaded, returncode=1), ok(overloaded, returncode=1), ok(SUCCESS))
    response = await claude(runner).complete(make_request())
    assert response.ok
    assert response.text == "hello"
    assert len(runner.commands) == 3


async def test_retried_attempts_are_billed_and_latency_is_the_final_attempt() -> None:
    failed = {
        **SUCCESS,
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "API Error: 529 Overloaded",
        "usage": {"input_tokens": 100, "output_tokens": 7},
        "total_cost_usd": 0.25,
    }
    succeeded = {**SUCCESS, "total_cost_usd": 0.5}

    def slow_success(command: Command) -> ProcessResult:
        import time

        time.sleep(0.02)
        return ok(succeeded)

    runner = FakeRunner(ok(failed, returncode=1), slow_success)
    response = await claude(runner, retry_base_delay_s=0.05).complete(make_request())
    assert response.ok
    assert (response.usage.input_tokens, response.usage.output_tokens) == (103, 9)
    assert response.reported_cost_usd == pytest.approx(0.75)
    assert response.raw is not None
    assert response.raw["attempts"] == 2
    # latency_s is the successful attempt alone; the backoff sleep shows up in wall_s only.
    assert 0.02 <= response.latency_s < 0.05
    assert response.raw["wall_s"] >= 0.07


async def test_single_attempt_has_no_attempt_bookkeeping() -> None:
    response = await claude(FakeRunner(ok(SUCCESS))).complete(make_request())
    assert response.raw is not None
    assert "attempts" not in response.raw
    assert "wall_s" not in response.raw


async def test_transient_retries_stop_after_max_retries() -> None:
    runner = FakeRunner(ok("", stderr="Error: 429 Too Many Requests", returncode=1))
    response = await claude(runner, max_retries=2).complete(make_request())
    assert response.error is not None
    assert "429" in response.error
    assert len(runner.commands) == 3


async def test_permanent_failures_are_not_retried() -> None:
    runner = FakeRunner(ok("", stderr="Invalid model name", returncode=1))
    response = await claude(runner).complete(make_request())
    assert response.error is not None
    assert "Invalid model name" in response.error
    assert len(runner.commands) == 1


async def test_ignored_temperature_is_recorded_in_raw() -> None:
    runner = FakeRunner(ok(SUCCESS))
    response = await claude(runner).complete(make_request(temperature=0.0))
    assert response.ok
    assert response.raw is not None
    assert response.raw["ignored_params"] == ["temperature", "max_output_tokens"]


@pytest.mark.parametrize("max_output_tokens", [None, 4096, 2048])
async def test_max_output_tokens_is_always_recorded_as_ignored(
    max_output_tokens: int | None,
) -> None:
    """No CLI can cap output, so the record mustn't depend on whether the request spelled out
    the default (4096) or left it implicit."""
    request = (
        make_request()
        if max_output_tokens is None
        else make_request(max_output_tokens=max_output_tokens)
    )
    response = await claude(FakeRunner(ok(SUCCESS))).complete(request)
    assert response.raw is not None
    assert response.raw["ignored_params"] == ["max_output_tokens"]


async def test_provider_default_effort_is_recorded_in_raw() -> None:
    response = await claude(FakeRunner(ok(SUCCESS)), effort="low").complete(make_request())
    assert response.raw is not None
    assert response.raw["effort"] == "low"
    explicit = await claude(FakeRunner(ok(SUCCESS)), effort="low").complete(
        make_request(effort="high")
    )
    assert "effort" not in (explicit.raw or {})


async def test_home_relative_variables_survive_without_isolation(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/somewhere")
    runner = FakeRunner(ok(SUCCESS))
    await claude(runner).complete(make_request())
    assert runner.command.env["XDG_CONFIG_HOME"] == "/somewhere"
    assert runner.command.env.get("HOME") == os.environ.get("HOME")


async def test_latency_is_measured() -> None:
    def slow(command):
        import time

        time.sleep(0.02)
        return ok(SUCCESS)

    response = await claude(FakeRunner(slow)).complete(make_request())
    assert response.latency_s >= 0.02


async def test_secrets_in_stderr_are_redacted(monkeypatch) -> None:
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "supersecretvalue123")
    runner = FakeRunner(ok("", stderr="auth failed with supersecretvalue123", returncode=1))
    response = await claude(runner).complete(make_request())
    assert response.error is not None
    assert "supersecretvalue123" not in response.error
    assert "<redacted>" in response.error
