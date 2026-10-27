"""Behaviour shared by every CLI provider (exercised through concrete providers)."""

from __future__ import annotations

import json
import logging
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from identitybench.providers import _cli, claude_cli, codex_cli, grok_cli
from identitybench.providers._cli import (
    COMMON_ENV_KEEP,
    MAX_ARG_BYTES,
    NEUTRAL_SYSTEM,
    OWNER_FILE,
    STDERR_EXCERPT_CHARS,
    SYSTEM_BEGIN,
    SYSTEM_END,
    SYSTEM_POINTER,
    as_float,
    as_int,
    base_child_env,
    call_dir,
    env_items,
    install_shutdown_handlers,
    link_credentials,
    live_call_dirs,
    owner_alive,
    parse_json_payload,
    parse_jsonl,
    prepend_system,
    probe_version,
    register_call_dir,
    remove_live_call_dirs,
    render_transcript,
    scrub_stderr,
    served_models,
    structured_text,
    sweep_stale_call_dirs,
    sync_back,
    system_override,
    unregister_call_dir,
    valid_effort,
)
from identitybench.providers._subprocess import (
    Command,
    ProcessResult,
    live_groups,
    register_group,
)
from identitybench.providers.agy_cli import AgyCliProvider
from identitybench.providers.base import Message, Provider, Role, ToolCall
from identitybench.providers.claude_cli import ClaudeCliProvider
from identitybench.providers.codex_cli import CodexCliProvider
from identitybench.providers.cursor_cli import CursorCliProvider
from identitybench.providers.grok_cli import GrokCliProvider
from identitybench.providers.muse_cli import MuseCliProvider
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


def test_parse_json_payload_prefers_the_result_object() -> None:
    # A progress or event object printed after the result must not be taken for it.
    lines = (
        '{"type": "system", "subtype": "init"}\n'
        '{"type": "result", "subtype": "success", "result": "hello"}\n'
        '{"type": "event", "name": "shutdown"}\n'
    )
    assert parse_json_payload(lines) == {"type": "result", "subtype": "success", "result": "hello"}
    two = '{"type": "result", "n": 1}\n{"type": "result", "n": 2}\n{"type": "other"}'
    assert parse_json_payload(two) == {"type": "result", "n": 2}


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
        AgyCliProvider("m", version="1"),
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
        lambda **kw: AgyCliProvider("m", version="1", **kw),
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
        AgyCliProvider("m", version="1").setup_revision(),
        CursorCliProvider("m", version="1").setup_revision(),
    }
    assert len(revisions) == 5


def test_setup_revision_leaves_nothing_behind(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    GrokCliProvider("m", version="1").setup_revision()
    CodexCliProvider("m", version="1").setup_revision()
    AgyCliProvider("m", version="1").setup_revision()
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
    huge = "x" * (CursorCliProvider.MAX_ARG_BYTES + 1)
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


# --------------------------------------------------------------------------- child environment

PARENT_ENV = {
    # The common allow-list.
    "PATH": "/usr/bin:/bin",
    "HOME": "/Users/someone",
    "TMPDIR": "/tmp/x",
    "LANG": "en_US.UTF-8",
    "LC_ALL": "C",
    "TERM": "xterm",
    "SHELL": "/bin/zsh",
    "USER": "someone",
    "XDG_CONFIG_HOME": "/xdg",
    # What .env exports for the API providers, and other credentials of the harness's user.
    "LOCAL_LLM_BASE_URL": "https://llm.example.invalid/v1",
    "LOCAL_LLM_API_KEY": "placeholder-secret-for-tests",
    "OPENAI_API_KEY": "placeholder",
    "ANTHROPIC_API_KEY": "placeholder",
    "ISC_TENANT_URL": "https://tenant.example.invalid",
    "GITHUB_TOKEN": "placeholder",
    "AWS_SECRET_ACCESS_KEY": "placeholder",
    "SOME_RANDOM_SETTING": "x",
    # Code loading, CA and proxy overrides.
    "NODE_OPTIONS": "--require evil.js",
    "NODE_EXTRA_CA_CERTS": "/ca.pem",
    "SSL_CERT_FILE": "/ca.pem",
    "HTTPS_PROXY": "http://proxy.example",
    "https_proxy": "http://proxy.example",
    "ALL_PROXY": "socks5://proxy.example",
    "NO_PROXY": "a",
    "GIT_SSH_COMMAND": "ssh -i key",
    "PYTHONPATH": "/evil",
    "PYTHONSTARTUP": "/evil.py",
    "DYLD_INSERT_LIBRARIES": "/evil.dylib",
    "LD_PRELOAD": "/evil.so",
}
ALLOWED = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM", "SHELL", "USER", "XDG_CONFIG_HOME"}
ALL_PROVIDERS = [
    ClaudeCliProvider,
    CodexCliProvider,
    GrokCliProvider,
    AgyCliProvider,
    CursorCliProvider,
    MuseCliProvider,
]


@pytest.mark.parametrize("provider_cls", ALL_PROVIDERS)
def test_child_env_is_an_allow_list(provider_cls) -> None:
    """Nothing outside the common base reaches a CLI: not .env's keys and URLs, not another
    CLI's login, not code-loading, CA or proxy overrides."""
    kept = {name for name in PARENT_ENV if provider_cls.keeps(name)}
    assert kept <= ALLOWED
    assert {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM", "SHELL", "USER"} <= kept


def test_base_child_env_is_the_common_allow_list_minus_the_common_drops() -> None:
    env = base_child_env({**PARENT_ENV, "LC_CTYPE": "UTF-8", "PYTHONPATH": "/x"})
    assert set(env) == ALLOWED | {"LC_CTYPE"}
    assert all(name in COMMON_ENV_KEEP or name.startswith("LC_") for name in env)


def test_child_env_applies_env_keep_and_env_set_on_top() -> None:
    parent = {**PARENT_ENV, "DISABLE_AUTOUPDATER": "0", "CLAUDE_CODE_OAUTH_TOKEN": "login"}
    env = ClaudeCliProvider("m", version="1").child_env(parent)
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "login"
    assert set(env) == ALLOWED | {"DISABLE_AUTOUPDATER", "CLAUDE_CODE_OAUTH_TOKEN"}


@pytest.mark.parametrize(
    ("provider_cls", "dropped", "kept"),
    [
        (
            ClaudeCliProvider,
            (
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_MODEL",
                "CLAUDECODE",
                "CLAUDE_CODE_SESSION_ID",
                "CLAUDE_CODE_ENTRYPOINT",
                "CLAUDE_EFFORT",
                "CLAUDE_PID",
                "CLAUDE_SOMETHING_NEW",
            ),
            ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR"),
        ),
        (
            CodexCliProvider,
            ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "CODEX_HOME", "CODEX_SANDBOX"),
            (),
        ),
        (
            CursorCliProvider,
            ("CURSOR_API_ENDPOINT", "CURSOR_API_KEY", "CURSOR_DATA_DIR", "CURSOR_SOMETHING"),
            ("CURSOR_CONFIG_DIR",),
        ),
        (GrokCliProvider, ("GROK_HOME", "GROK_XAI_API_BASE_URL", "GROK_CONFIG", "XAI_API_KEY"), ()),
        (
            AgyCliProvider,
            (
                "GEMINI_API_KEY",
                "AGY_LLM_GATEWAY_URL",
                "GOOGLE_API_KEY",
                "GOOGLE_CLOUD_PROJECT",
                "GOOGLE_SOMETHING",
                "ANTIGRAVITY_X",
                "JETSKI_X",
            ),
            (),
        ),
        (
            MuseCliProvider,
            (
                "META_API_KEY",
                "MUSE_BASE_URL",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "XDG_CONFIG_HOME",
            ),
            (),
        ),
    ],
)
def test_each_cli_drops_its_own_variables_by_prefix_and_keeps_only_its_login(
    provider_cls, dropped: tuple[str, ...], kept: tuple[str, ...]
) -> None:
    for name in dropped:
        assert not provider_cls.keeps(name), name
    for name in kept:
        assert provider_cls.keeps(name), name


def test_env_keep_cannot_reopen_a_denied_name(monkeypatch) -> None:
    """A future ENV_KEEP edit may lift a prefix drop (that is how the login survives it) but
    never an exact-name drop, the common drops or the proxy/code-loading rules."""
    monkeypatch.setattr(
        ClaudeCliProvider,
        "ENV_KEEP",
        frozenset(
            {
                "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CODE_SESSION_ID",
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_BASE_URL",
                "CLAUDECODE",
                "GITHUB_TOKEN",
                "HTTPS_PROXY",
                "NODE_OPTIONS",
                "LD_PRELOAD",
            }
        ),
    )
    assert ClaudeCliProvider.keeps("CLAUDE_CODE_OAUTH_TOKEN")
    assert ClaudeCliProvider.keeps("CLAUDE_CODE_SESSION_ID")
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "CLAUDECODE",
        "GITHUB_TOKEN",
        "HTTPS_PROXY",
        "NODE_OPTIONS",
        "LD_PRELOAD",
    ):
        assert not ClaudeCliProvider.keeps(name), name


async def test_secrets_in_the_harness_environment_never_reach_a_cli(monkeypatch) -> None:
    for name, value in PARENT_ENV.items():
        monkeypatch.setenv(name, value)
    runner = FakeRunner(ok(SUCCESS))
    await claude(runner).complete(make_request())
    env = runner.command.env
    allowed = COMMON_ENV_KEEP | ClaudeCliProvider.ENV_KEEP | set(ClaudeCliProvider.ENV_SET)
    assert {name for name in env if not name.startswith("LC_")} <= allowed
    for value in ("placeholder-secret-for-tests", "https://llm.example.invalid/v1", "/evil"):
        assert value not in env.values()


def test_probe_version_runs_in_a_private_directory_with_the_child_environment(
    tmp_path: Path, monkeypatch
) -> None:
    script = tmp_path / "fake-cli"
    script.write_text('#!/bin/sh\necho "cwd=$PWD key=$LOCAL_LLM_API_KEY marker=$MARKER"\n')
    script.chmod(0o755)
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "placeholder-secret-for-tests")
    monkeypatch.setenv("MARKER", "from-the-harness")
    probe_version.cache_clear()

    default = probe_version(str(script))
    assert "placeholder-secret-for-tests" not in default
    assert "from-the-harness" not in default  # not allow-listed either
    cwd = default.split()[0].removeprefix("cwd=")
    assert "identitybench-probe-" in cwd
    assert not Path(cwd).exists()  # removed after the probe
    assert cwd != os.getcwd()

    explicit = probe_version(
        str(script), env_items({"PATH": os.environ["PATH"], "MARKER": "from-the-provider"})
    )
    assert explicit.endswith("key= marker=from-the-provider")


# --------------------------------------------------------------------------- stderr excerpts


def test_scrub_stderr_drops_prompt_lines_and_caps_the_excerpt() -> None:
    system = "You are an access reviewer.\nApply the policy strictly.\n"
    user = "Decide REQ-1 for Dana Reyes, who holds ent_gl_je_approve.\nSecond line of the case."
    request = make_request(user, system=system)
    stderr = "\n".join(
        [
            "usage: fake-cli [options] <prompt>",
            'error: unexpected argument "You are an access reviewer."',  # quotes the first line
            "Apply the policy strictly.",  # a prompt line verbatim
            "Decide REQ-1 for Dana Reyes, who holds ent_gl_je_approve.",
            "Second line of the case.",
            "exit status 2",
        ]
    )
    assert scrub_stderr(stderr, request) == "usage: fake-cli [options] <prompt>\nexit status 2"
    long = "x" * 1000
    assert scrub_stderr(long, request) == "…" + "x" * STDERR_EXCERPT_CHARS
    assert scrub_stderr(long, None) == "…" + "x" * STDERR_EXCERPT_CHARS
    assert scrub_stderr("   \n", request) == ""
    # Short prompt lines are not used as marks: they would match ordinary error text.
    assert scrub_stderr("error: no", make_request("no", system="")) == "error: no"


SHORT_SYSTEM = "Be strict.\nBe brief.\nBe fair."
"""Three short lines: none is long enough to be a mark, so nothing in the opening of the
prompt helps the scrub."""
SHORT_HEADED_CASE = (
    "Case: REQ-1\nActor: Dana\nTarget: GL\n"
    "Dana Reyes requests ent_gl_je_approve for quarter-end journal entry approval.\n"
    "Her manager has not countersigned the request yet."
)


def test_scrub_stderr_drops_a_rewrapped_echo_of_the_prompt() -> None:
    """A CLI that re-wraps and re-indents the prompt in its error prints no line of it
    verbatim; what every fragment shares with the prompt is a run of text."""
    request = make_request(SHORT_HEADED_CASE, system=SHORT_SYSTEM)
    stderr = "\n".join(
        [
            "error: could not parse the prompt:",
            "    Dana Reyes requests ent_gl_je_approve for",
            "    quarter-end journal entry approval. Her",
            "    manager has not countersigned the request yet.",
            "exit status 2",
        ]
    )
    assert scrub_stderr(stderr, request) == "error: could not parse the prompt:\nexit status 2"


def test_scrub_stderr_drops_an_escaped_argv_echo_whose_first_lines_are_short() -> None:
    """An argv echo quotes the whole prompt on one line with ``\\n`` for its line breaks; the
    prompt's first lines are too short to be marks, so the case text sits past them."""
    request = make_request(SHORT_HEADED_CASE, system=SHORT_SYSTEM)
    echo = "\\n".join([*SHORT_SYSTEM.splitlines(), "", *SHORT_HEADED_CASE.splitlines()])
    stderr = f"error: unexpected argument '{echo}'\nusage: fake-cli [options] <prompt>"
    assert "Dana Reyes" in stderr and "\\n" in stderr  # the echo is one line
    assert scrub_stderr(stderr, request) == "usage: fake-cli [options] <prompt>"
    windows_only = "error: argument too long: ...uarter-end journal entry approv..."
    assert scrub_stderr(windows_only, request) == ""


def test_scrub_stderr_keeps_benign_lines_that_share_less_than_a_window() -> None:
    request = make_request(SHORT_HEADED_CASE, system=SHORT_SYSTEM)
    stderr = "\n".join(
        [
            "usage: fake-cli [options] <prompt>",
            "error: journal entry approval needs --json",  # 22 shared characters, not an echo
            'warning: use "\\n" to separate lines',
            "Dana Reyes requests ent_gl_je_approve for quarter-end journal entry approval.",
            "exit status 2",
        ]
    )
    assert scrub_stderr(stderr, request) == "\n".join(
        [
            "usage: fake-cli [options] <prompt>",
            "error: journal entry approval needs --json",
            'warning: use "\\n" to separate lines',
            "exit status 2",
        ]
    )


def test_scrub_stderr_takes_marks_from_every_prompt_line_up_to_a_cap() -> None:
    lines = [f"Policy clause number {n:04d} applies to the request." for n in range(500)]
    request = make_request("\n".join(lines), system="")
    assert scrub_stderr("error: 'Policy clause number 0450 applies to the request.'", request) == ""
    assert scrub_stderr("error: 'Policy clause number 0003 applies to the request.'", request) == ""
    assert scrub_stderr("error: nothing like the prompt here", request) == (
        "error: nothing like the prompt here"
    )


async def test_error_excerpts_do_not_echo_the_prompt() -> None:
    request = make_request("Decide REQ-1 for Dana Reyes, who holds ent_gl_je_approve.")
    stderr = (
        "usage error: bad prompt 'Decide REQ-1 for Dana Reyes, who holds ent_gl_je_approve.'\n"
        "You are an access reviewer.\n" + "x" * 500 + "\nexit 2"
    )
    runner = FakeRunner(ok("", stderr=stderr, returncode=2))
    response = await claude(runner).complete(request)
    assert response.error is not None
    assert "Dana Reyes" not in response.error
    assert "access reviewer" not in response.error
    assert response.error.endswith("exit 2)")
    assert "x" * 500 not in response.error  # capped at STDERR_EXCERPT_CHARS


# --------------------------------------------------------------------------- call directories


def test_call_dir_is_registered_while_it_exists_and_removed_after() -> None:
    with call_dir("identitybench-test_cli-") as root:
        assert root.is_dir()
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert str(root) in {os.path.realpath(path) for path in live_call_dirs()}
        assert (root / OWNER_FILE).read_text(encoding="utf-8") == str(os.getpid())
        assert owner_alive(str(root)) is True
    assert not root.exists()
    assert str(root) not in {os.path.realpath(path) for path in live_call_dirs()}


def reaped_pid() -> int:
    """The pid of a process that has already exited and been waited for."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    assert proc.wait() == 0
    return proc.pid


def test_owner_alive(tmp_path: Path) -> None:
    def marked(name: str, content: str | None) -> str:
        path = tmp_path / name
        path.mkdir()
        if content is not None:
            (path / OWNER_FILE).write_text(content, encoding="utf-8")
        return str(path)

    assert owner_alive(marked("mine", f"{os.getpid()}\n")) is True
    assert owner_alive(marked("dead", str(reaped_pid()))) is False
    assert owner_alive(marked("none", None)) is None
    assert owner_alive(marked("garbled", "not a pid")) is None
    assert owner_alive(marked("empty", "")) is None
    assert owner_alive(marked("zero", "0")) is None  # would signal the whole group
    assert owner_alive(marked("negative", "-1")) is None
    assert owner_alive(str(tmp_path / "missing")) is None


def test_sweep_spares_a_directory_whose_owner_is_alive_whatever_its_age(
    tmp_path: Path, monkeypatch
) -> None:
    """A call keeps its directory for an unbounded timeout across every retry, and any
    ``identitybench`` command in another shell sweeps; the owner pid tells a call in progress
    from a leak. Only a directory whose owner is dead or unknown is swept, and only when old."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    old = time.time() - 3 * 3600

    def make(name: str, owner: str | None, *, age: float = old) -> Path:
        path = tmp_path / name
        path.mkdir(mode=0o700)
        path.chmod(0o700)
        (path / "work").mkdir()
        if owner is not None:
            (path / OWNER_FILE).write_text(owner, encoding="utf-8")
        os.utime(path, (age, age))
        return path

    mine = make("identitybench-claude_cli-mine1234", str(os.getpid()))
    mine_probe = make("identitybench-probe-mine1234", str(os.getpid()))
    dead = make("identitybench-claude_cli-dead1234", str(reaped_pid()))
    dead_setup = make("identitybench-codex_cli-setup-dead1234", str(reaped_pid()))
    legacy = make("identitybench-grok_cli-none1234", None)  # from before owner files
    garbled = make("identitybench-agy_cli-junk1234", "not a pid")
    fresh_dead = make("identitybench-claude_cli-fresh123", str(reaped_pid()), age=time.time())
    fresh_legacy = make("identitybench-grok_cli-fresh123", None, age=time.time())

    assert sweep_stale_call_dirs() == 4
    for path in (dead, dead_setup, legacy, garbled):
        assert not path.exists(), path
    for path in (mine, mine_probe, fresh_dead, fresh_legacy):
        assert path.exists(), path
    # Age never overrides a live owner.
    assert sweep_stale_call_dirs(max_age_s=0) == 2  # the two fresh ones, now past the age
    assert mine.exists() and mine_probe.exists()
    assert not fresh_dead.exists() and not fresh_legacy.exists()


def test_remove_live_call_dirs(tmp_path: Path) -> None:
    leaked = tmp_path / "leaked"
    leaked.mkdir()
    (leaked / "io").mkdir()
    (leaked / "io" / "prompt.md").write_text("case text", encoding="utf-8")
    register_call_dir(str(leaked))
    assert remove_live_call_dirs() == 1
    assert not leaked.exists()
    assert not live_call_dirs()
    assert remove_live_call_dirs() == 0


def test_sweep_removes_only_stale_private_call_dirs(tmp_path: Path, monkeypatch, caplog) -> None:
    """What a SIGKILLed harness left behind is removed at the next start; a running call's
    directory, anything fresh, shared, differently named, or not a real directory is not."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    old = time.time() - 3 * 3600

    def make(name: str, *, mode: int = 0o700, age: float = old) -> Path:
        path = tmp_path / name
        path.mkdir()
        (path / "work").mkdir()
        path.chmod(mode)
        os.utime(path, (age, age))
        return path

    stale = make("identitybench-claude_cli-abc12345")
    stale_setup = make("identitybench-codex_cli-setup-abc12345")
    stale_probe = make("identitybench-probe-abc12345")
    fresh = make("identitybench-grok_cli-abc12345", age=time.time())
    shared = make("identitybench-agy_cli-abc12345", mode=0o755)
    other = make("identitybench-sandbox-abc12345")
    file = tmp_path / "identitybench-muse_cli-abc12345"
    file.write_text("", encoding="utf-8")
    os.utime(file, (old, old))
    link = tmp_path / "identitybench-cursor_cli-abc12345"
    link.symlink_to(shared)
    live = make("identitybench-claude_cli-live1234")
    register_call_dir(str(live))
    try:
        with caplog.at_level(logging.INFO, logger="identitybench.providers"):
            assert sweep_stale_call_dirs() == 3
    finally:
        unregister_call_dir(str(live))
    for path in (stale, stale_setup, stale_probe):
        assert not path.exists(), path
    for path in (fresh, shared, other, file, link, live):
        assert path.exists(), path
    assert "removed 3 stale CLI call directories" in caplog.text
    # Once its call is over (unregistered above) a stale directory is fair game.
    assert sweep_stale_call_dirs() == 1
    assert not live.exists()


def test_sweep_age_is_configurable_and_a_fresh_dir_survives_the_default(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    recent = tmp_path / "identitybench-claude_cli-recent12"
    recent.mkdir(mode=0o700)
    recent.chmod(0o700)
    minute_ago = time.time() - 60
    os.utime(recent, (minute_ago, minute_ago))
    assert sweep_stale_call_dirs() == 0
    assert recent.is_dir()
    assert sweep_stale_call_dirs(max_age_s=30) == 1
    assert not recent.exists()


# --------------------------------------------------------------------------- shutdown


def test_install_shutdown_handlers_is_idempotent_and_main_thread_only(monkeypatch) -> None:
    monkeypatch.setattr(_cli, "_atexit_registered", False)
    monkeypatch.setattr(_cli, "_signals_installed", False)
    registered: list[object] = []
    installed: list[tuple[int, object]] = []
    monkeypatch.setattr(_cli.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(_cli.signal, "getsignal", lambda signum: signal.SIG_DFL)
    monkeypatch.setattr(
        _cli.signal, "signal", lambda signum, handler: installed.append((signum, handler))
    )
    assert install_shutdown_handlers() is True
    assert registered == [_cli.emergency_cleanup]
    assert installed == [
        (signal.SIGTERM, _cli._on_shutdown_signal),
        (signal.SIGHUP, _cli._on_shutdown_signal),
    ]
    assert install_shutdown_handlers() is False
    assert len(registered) == 1 and len(installed) == 2

    # Off the main thread only the atexit fallback can be registered; the handlers wait for a
    # call from the main thread.
    monkeypatch.setattr(_cli, "_atexit_registered", False)
    monkeypatch.setattr(_cli, "_signals_installed", False)
    registered.clear()
    installed.clear()
    outcome: list[bool] = []
    thread = threading.Thread(target=lambda: outcome.append(install_shutdown_handlers()))
    thread.start()
    thread.join()
    assert outcome == [False]
    assert registered == [_cli.emergency_cleanup] and installed == []
    assert install_shutdown_handlers() is True
    assert len(registered) == 1 and len(installed) == 2


def test_install_shutdown_handlers_leaves_another_handler_alone(monkeypatch) -> None:
    monkeypatch.setattr(_cli, "_atexit_registered", True)
    monkeypatch.setattr(_cli, "_signals_installed", False)
    installed: list[tuple[int, object]] = []
    theirs = lambda signum, frame: None  # noqa: E731
    monkeypatch.setattr(
        _cli.signal, "getsignal", lambda signum: theirs if signum == signal.SIGHUP else None
    )
    monkeypatch.setattr(
        _cli.signal, "signal", lambda signum, handler: installed.append((signum, handler))
    )
    assert install_shutdown_handlers() is True
    assert installed == [(signal.SIGTERM, _cli._on_shutdown_signal)]


def test_a_shutdown_signal_kills_live_groups_removes_dirs_then_terminates(
    tmp_path: Path, monkeypatch
) -> None:
    killed: list[tuple[int, int]] = []
    sent: list[tuple[int, int]] = []
    reset: list[tuple[int, object]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(signal, "signal", lambda signum, handler: reset.append((signum, handler)))
    leaked = tmp_path / "identitybench-claude_cli-live5678"
    leaked.mkdir()
    (leaked / "io").mkdir()
    register_call_dir(str(leaked))
    register_group(70010)

    _cli._on_shutdown_signal(signal.SIGTERM, None)

    assert killed == [(70010, signal.SIGKILL)]
    assert not leaked.exists()
    assert reset == [(signal.SIGTERM, signal.SIG_DFL)]
    assert sent == [(os.getpid(), signal.SIGTERM)]
    assert not live_groups() and not live_call_dirs()


def test_emergency_cleanup_is_a_safe_no_op_when_nothing_is_live() -> None:
    assert _cli.emergency_cleanup() == (0, 0)
