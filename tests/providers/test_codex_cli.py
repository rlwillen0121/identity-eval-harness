from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from identitybench.providers._cli import SYSTEM_BEGIN, SYSTEM_END
from identitybench.providers._subprocess import Command, ProcessResult
from identitybench.providers.codex_cli import (
    CONFIG_OVERRIDES,
    DISABLED_FEATURES,
    CodexCliProvider,
)
from tests.providers.fakes import DECISION_SCHEMA, FakeRunner, flag_value, make_request, ok

# Usage block from a real `codex exec --json` run (codex-cli 0.162.1, gpt-6-astra, the provider's
# full argv) whose output matched the schema.
VERIFIED_USAGE = {
    "input_tokens": 8870,
    "cached_input_tokens": 0,
    "output_tokens": 16,
    "reasoning_output_tokens": 0,
}


def events(*items: dict) -> str:
    return "\n".join(json.dumps(item) for item in items) + "\n"


def codex_run(final_message: str | None, *event_items: dict, returncode: int = 0, stderr: str = ""):
    """Responder that writes the final message to the -o file like codex does."""

    def respond(command: Command) -> ProcessResult:
        if final_message is not None:
            Path(flag_value(command.argv, "-o")).write_text(final_message, encoding="utf-8")
        return ProcessResult(returncode=returncode, stdout=events(*event_items), stderr=stderr)

    return respond


STANDARD_EVENTS = (
    {"type": "thread.started", "thread_id": "th_123"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "x"}},
    {"type": "turn.completed", "usage": VERIFIED_USAGE},
)


@pytest.fixture(autouse=True)
def real_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for the user's ~/.codex, with a login and personal setup."""
    home = tmp_path / "user-codex"
    home.mkdir()
    (home / "auth.json").write_text('{"tokens": {"access": "a"}}', encoding="utf-8")
    (home / "AGENTS.md").write_text("personal global instructions", encoding="utf-8")
    (home / "config.toml").write_text('model_provider = "elsewhere"\n', encoding="utf-8")
    (home / "hooks.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def provider(runner: FakeRunner, **kwargs) -> CodexCliProvider:
    return CodexCliProvider(
        "gpt-test", runner=runner, version="codex-cli 0.162.1", retry_base_delay_s=0, **kwargs
    )


DISABLED_FEATURES_ARGV = tuple(arg for name in DISABLED_FEATURES for arg in ("--disable", name))
CONFIG_OVERRIDES_ARGV = tuple(arg for item in CONFIG_OVERRIDES for arg in ("-c", item))


def test_pinned_feature_and_config_lists() -> None:
    """The setup every codex row runs with (checked against codex-cli 0.162.1; see the module
    docstring). Changing it changes the prompt, so it is pinned here."""
    assert DISABLED_FEATURES == (
        "apps",
        "plugins",
        "remote_plugin",
        "plugin_hooks",
        "recommended_plugins",
        "tool_suggest",
        "shell_tool",
        "unified_exec",
        "js_repl",
        "view_image",
        "memories",
        "hooks",
        "skill_search",
        "skill_mcp_dependency_install",
        "multi_agent",
        "multi_agent_v2",
        "browser_use",
        "browser_use_external",
        "computer_use",
        "image_generation",
        "web_search_request",
        "web_search_cached",
        "standalone_web_search",
        "search_tool",
        "tool_search",
        "sleep_tool",
        "request_permissions_tool",
        "goals",
        "workspace_dependencies",
    )
    assert CONFIG_OVERRIDES == (
        "skills.bundled.enabled=false",
        "skills.include_instructions=false",
        'web_search="disabled"',
    )


async def test_argv_prompt_and_schema_file() -> None:
    runner = FakeRunner(codex_run('{"decision": "DENY", "rules": []}', *STANDARD_EVENTS))
    await provider(runner).complete(
        make_request("Decide REQ-1.", system="SYS", schema=DECISION_SCHEMA)
    )
    command = runner.command
    argv = command.argv
    out_file = flag_value(argv, "-o")
    schema_file = flag_value(argv, "--output-schema")
    work_dir = flag_value(argv, "-C")
    assert argv == (
        "codex",
        "exec",
        "-m",
        "gpt-test",
        "-c",
        'model_reasoning_effort="medium"',
        "-s",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        *DISABLED_FEATURES_ARGV,
        *CONFIG_OVERRIDES_ARGV,
        "--json",
        "-o",
        out_file,
        "--output-schema",
        schema_file,
        "-C",
        work_dir,
        "-",
    )
    assert work_dir == command.cwd
    assert runner.snapshots[0].cwd_entries == []
    assert Path(out_file).parent != Path(work_dir)
    assert json.loads(runner.snapshots[0].files[schema_file]) == DECISION_SCHEMA
    assert command.stdin == f"{SYSTEM_BEGIN}\nSYS\n{SYSTEM_END}\n\nDecide REQ-1."


async def test_no_schema_flag_without_schema_and_effort_override() -> None:
    runner = FakeRunner(codex_run("hi", *STANDARD_EVENTS))
    await provider(runner, effort="low").complete(make_request(effort="xhigh"))
    argv = runner.command.argv
    assert "--output-schema" not in argv
    assert flag_value(argv, "-c") == 'model_reasoning_effort="xhigh"'


async def test_effort_none_omits_the_config_override() -> None:
    runner = FakeRunner(codex_run("hi", *STANDARD_EVENTS))
    await provider(runner, effort=None).complete(make_request())
    assert not [arg for arg in runner.command.argv if "model_reasoning_effort" in arg]


async def test_env_drops_openai_endpoint_and_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://proxy.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "kept")
    runner = FakeRunner(codex_run("hi", *STANDARD_EVENTS))
    await provider(runner).complete(make_request())
    env = runner.command.env
    assert "OPENAI_BASE_URL" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["ANTHROPIC_API_KEY"] == "kept"


async def test_parses_output_file_and_verified_usage() -> None:
    runner = FakeRunner(codex_run('{"decision": "DENY", "rules": ["LIFE-001"]}', *STANDARD_EVENTS))
    response = await provider(runner).complete(make_request(schema=DECISION_SCHEMA))
    assert response.ok
    assert response.text == '{"decision": "DENY", "rules": ["LIFE-001"]}'
    assert response.parsed == {"decision": "DENY", "rules": ["LIFE-001"]}
    assert response.usage.input_tokens == 8870
    assert response.usage.cache_read_tokens == 0
    assert response.usage.output_tokens == 16
    assert response.usage.reasoning_tokens == 0
    # No effort in the request, so the provider's pinned default applied and is recorded.
    assert response.raw == {
        "thread_id": "th_123",
        "effort": "medium",
        "ignored_params": ["max_output_tokens"],
    }
    assert response.stop_reason == "completed"
    assert response.served_model is None
    assert response.reported_cost_usd is None


async def test_cached_input_is_split_out_of_input_tokens() -> None:
    usage = {
        "input_tokens": 1000,
        "cached_input_tokens": 800,
        "output_tokens": 50,
        "reasoning_output_tokens": 30,
    }
    runner = FakeRunner(codex_run("ok", {"type": "turn.completed", "usage": usage}))
    response = await provider(runner).complete(make_request())
    assert response.usage.input_tokens == 200
    assert response.usage.cache_read_tokens == 800
    assert response.usage.output_tokens == 50
    assert response.usage.reasoning_tokens == 30


async def test_parsed_only_when_a_schema_was_requested() -> None:
    runner = FakeRunner(codex_run('{"decision": "DENY"}', *STANDARD_EVENTS))
    response = await provider(runner).complete(make_request())
    assert response.text == '{"decision": "DENY"}'
    assert response.parsed is None


async def test_non_json_output_with_schema_leaves_parsed_empty() -> None:
    runner = FakeRunner(codex_run("not json", *STANDARD_EVENTS))
    response = await provider(runner).complete(make_request(schema=DECISION_SCHEMA))
    assert response.ok
    assert response.parsed is None


async def test_falls_back_to_the_streamed_agent_message() -> None:
    runner = FakeRunner(codex_run(None, *STANDARD_EVENTS))
    response = await provider(runner).complete(make_request())
    assert response.text == "x"


async def test_turn_failed_is_an_error() -> None:
    runner = FakeRunner(
        codex_run(
            None,
            {"type": "thread.started", "thread_id": "th_1"},
            {"type": "error", "message": "stream disconnected - retrying"},
            {"type": "turn.failed", "error": {"message": "model not supported"}},
            returncode=1,
        )
    )
    response = await provider(runner).complete(make_request())
    assert response.error is not None
    assert "model not supported" in response.error
    assert len(runner.commands) == 1


async def test_non_fatal_error_events_do_not_fail_a_completed_turn() -> None:
    runner = FakeRunner(
        codex_run("answer", {"type": "error", "message": "reconnecting"}, *STANDARD_EVENTS)
    )
    response = await provider(runner).complete(make_request())
    assert response.ok
    assert response.text == "answer"


async def test_rate_limit_is_retried_and_stale_output_is_cleared() -> None:
    seen_stale: list[bool] = []

    def first(command: Command) -> ProcessResult:
        Path(flag_value(command.argv, "-o")).write_text("partial", encoding="utf-8")
        return ProcessResult(
            returncode=1,
            stdout=events({"type": "turn.failed", "error": {"message": "429 rate limit"}}),
            stderr="",
        )

    def second(command: Command) -> ProcessResult:
        seen_stale.append(Path(flag_value(command.argv, "-o")).exists())
        return ProcessResult(
            returncode=1,
            stdout=events({"type": "turn.failed", "error": {"message": "429 rate limit"}}),
            stderr="",
        )

    runner = FakeRunner(first, second, codex_run("final", *STANDARD_EVENTS))
    response = await provider(runner).complete(make_request())
    assert seen_stale == [False]
    assert response.ok
    assert response.text == "final"
    assert len(runner.commands) == 3


async def test_partial_output_after_a_crash_is_not_trusted() -> None:
    runner = FakeRunner(codex_run("half an answ", returncode=1, stderr="panic"))
    response = await provider(runner).complete(make_request())
    assert response.error is not None
    assert "panic" in response.error


async def test_missing_output_and_no_events_is_an_error() -> None:
    runner = FakeRunner(ok("", stderr="Not logged in", returncode=1))
    response = await provider(runner).complete(make_request())
    assert response.error is not None
    assert "no final message" in response.error
    assert "Not logged in" in response.error


# --------------------------------------------------------------------------- isolation


async def test_runs_with_a_private_codex_home_holding_only_the_login(
    real_codex_home: Path,
) -> None:
    seen: dict[str, object] = {}

    def respond(command: Command) -> ProcessResult:
        private = Path(command.env["CODEX_HOME"])
        seen["home"] = Path(command.env["HOME"])
        seen["entries"] = sorted(p.name for p in private.iterdir())
        seen["auth_target"] = (private / "auth.json").resolve()
        return codex_run("ok", *STANDARD_EVENTS)(command)

    runner = FakeRunner(respond)
    response = await provider(runner).complete(make_request())
    assert response.ok
    home = seen["home"]
    assert isinstance(home, Path)
    assert runner.command.env["CODEX_HOME"] == str(home / ".codex")
    assert home.parent == Path(runner.command.cwd).parent
    # No AGENTS.md, config.toml or hooks: global instructions and MCP servers can't load.
    assert seen["entries"] == ["auth.json"]
    assert seen["auth_target"] == (real_codex_home / "auth.json").resolve()
    assert not home.exists()


async def test_xdg_variables_are_dropped(monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/elsewhere")
    runner = FakeRunner(codex_run("ok", *STANDARD_EVENTS))
    await provider(runner).complete(make_request())
    assert "XDG_CONFIG_HOME" not in runner.command.env


async def test_a_refreshed_login_written_by_rename_is_synced_back(real_codex_home: Path) -> None:
    def respond(command: Command) -> ProcessResult:
        link = Path(command.env["CODEX_HOME"]) / "auth.json"
        staged = link.with_name("auth.json.new")
        staged.write_text('{"tokens": {"access": "b"}}', encoding="utf-8")
        os.replace(staged, link)
        return codex_run("ok", *STANDARD_EVENTS)(command)

    await provider(FakeRunner(respond)).complete(make_request())
    auth = json.loads((real_codex_home / "auth.json").read_text(encoding="utf-8"))
    assert auth == {"tokens": {"access": "b"}}
