from __future__ import annotations

import json
import os
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from identitybench.providers._cli import (
    ACTION_SCHEMA_NAME,
    MAX_ARG_BYTES,
    NEUTRAL_SYSTEM,
    SYSTEM_BEGIN,
    SYSTEM_END,
    SYSTEM_POINTER,
)
from identitybench.providers._subprocess import Command, ProcessResult
from identitybench.providers.grok_cli import GrokCliProvider
from tests.providers.fakes import (
    DECISION_SCHEMA,
    FakeRunner,
    attached_value,
    flag_value,
    make_request,
    ok,
)

# Real `grok --output-format json --json-schema ...` output.
VERIFIED = {
    "text": '{"decision":"DENY"}',
    "stopReason": "end_turn",
    "usage": {
        "input_tokens": 16017,
        "cache_read_input_tokens": 1152,
        "cache_creation_input_tokens": 0,
        "output_tokens": 303,
        "reasoning_tokens": 289,
        "total_tokens": 17472,
    },
    "num_turns": 1,
    "total_cost_usd": 0.034428,
    "modelUsage": {"grok-4.7": {"inputTokens": 16017}},
    "structuredOutput": {"decision": "DENY", "rules": ["LIFE-001"]},
}


@pytest.fixture(autouse=True)
def real_grok_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for the user's ~/.grok, with a login and some personal setup."""
    home = tmp_path / "user-grok"
    (home / "rules").mkdir(parents=True)
    (home / "auth.json").write_text('{"session": "tok"}', encoding="utf-8")
    (home / "auth.json.lock").write_text("", encoding="utf-8")
    (home / "AGENTS.md").write_text("personal rule", encoding="utf-8")
    (home / "rules" / "mine.md").write_text("personal rule", encoding="utf-8")
    (home / "config.toml").write_text("[mcp_servers.x]\ncommand = 'x'\n", encoding="utf-8")
    monkeypatch.setenv("GROK_HOME", str(home))
    return home


def provider(runner: FakeRunner, **kwargs) -> GrokCliProvider:
    return GrokCliProvider(
        "grok-4.7", runner=runner, version="grok 1.0.46", retry_base_delay_s=0, **kwargs
    )


async def test_argv_and_prompt_file() -> None:
    runner = FakeRunner(ok(VERIFIED))
    response = await provider(runner).complete(make_request("Decide REQ-1.", system="SYS"))
    command = runner.command
    prompt_file = flag_value(command.argv, "--prompt-file")
    assert command.argv == (
        "grok",
        "-m",
        "grok-4.7",
        "--prompt-file",
        prompt_file,
        "--system-prompt-override=SYS",
        "--tools",
        "read_file",
        "--disallowed-tools",
        "read_file,search_tool,use_tool",
        "--disable-web-search",
        "--no-subagents",
        "--no-plan",
        "--no-auto-update",
        "--max-turns",
        "2",
        "--verbatim",
        "--output-format",
        "json",
    )
    assert command.stdin is None
    assert runner.snapshots[0].files[prompt_file] == "Decide REQ-1."
    assert runner.snapshots[0].cwd_entries == []
    assert Path(prompt_file).parent != Path(command.cwd)
    assert "substituted_system" not in (response.raw or {})


async def test_schema_and_reasoning_effort_flags_are_attached() -> None:
    runner = FakeRunner(ok(VERIFIED))
    response = await provider(runner, effort="high").complete(make_request(schema=DECISION_SCHEMA))
    argv = runner.command.argv
    assert json.loads(attached_value(argv, "--json-schema")) == DECISION_SCHEMA
    assert attached_value(argv, "--reasoning-effort") == "high"
    assert "--json-schema" not in argv
    assert "--reasoning-effort" not in argv
    assert response.raw is not None
    assert response.raw["effort"] == "high"  # the provider default applied, so it is recorded


async def test_an_action_protocol_turn_is_sent_without_the_schema() -> None:
    """As in ``claude_cli``: the structured-output turn would invite native calls of the listed
    lookups, which here would also use up ``--max-turns 2``."""
    runner = FakeRunner(ok(VERIFIED))
    action = replace(make_request(schema=DECISION_SCHEMA), schema_name=ACTION_SCHEMA_NAME)
    response = await provider(runner).complete(action)
    assert not any(arg.startswith("--json-schema") for arg in runner.command.argv)
    assert response.raw is not None
    assert response.raw["json_schema"] == "withheld (JSON action protocol turn)"


TAKES_A_VALUE = frozenset(
    {"-m", "--prompt-file", "--tools", "--disallowed-tools", "--max-turns", "--output-format"}
)
SWITCHES = frozenset(
    {"--disable-web-search", "--no-subagents", "--no-plan", "--no-auto-update", "--verbatim"}
)
ATTACHED = ("--system-prompt-override=", "--json-schema=", "--reasoning-effort=")


def separate_values(argv: tuple[str, ...]) -> list[str]:
    """The values passed as their own argv element, read the way grok's parser (clap) reads
    them; fails on any element the parser would see as an unknown flag."""
    values: list[str] = []
    args = iter(argv[1:])
    for arg in args:
        if arg in TAKES_A_VALUE:
            values.append(next(args))
        else:
            assert arg in SWITCHES or arg.startswith(ATTACHED), f"unknown flag {arg!r}"
    return values


@pytest.mark.parametrize(
    "system",
    [
        "- You are an access reviewer.\n- Follow the policy.",
        "---\ntitle: Access policy\nversion: 3\n---\nYou are an access reviewer.",
        "--foo is not a flag here",
        "-",
    ],
)
async def test_a_system_prompt_starting_with_a_dash_is_never_a_separate_argument(
    system: str,
) -> None:
    runner = FakeRunner(ok(VERIFIED))
    response = await provider(runner, effort="low").complete(
        make_request(system=system, schema=DECISION_SCHEMA)
    )
    argv = runner.command.argv
    assert attached_value(argv, "--system-prompt-override") == system
    assert system not in argv
    assert not [value for value in separate_values(argv) if value.startswith("-")]
    assert response.ok


@pytest.mark.parametrize("system", ["", "  \n\t"])
async def test_an_empty_system_prompt_is_replaced_by_the_neutral_one(system: str) -> None:
    runner = FakeRunner(ok(VERIFIED))
    response = await provider(runner).complete(make_request("Decide.", system=system))
    argv = runner.command.argv
    assert attached_value(argv, "--system-prompt-override") == NEUTRAL_SYSTEM
    assert runner.snapshots[0].files[flag_value(argv, "--prompt-file")] == "Decide."
    assert response.raw is not None
    assert response.raw["substituted_system"] == NEUTRAL_SYSTEM


async def test_the_substitution_is_recorded_on_failed_calls_too() -> None:
    runner = FakeRunner(ok({"type": "error", "message": "model grok-9 not found"}, returncode=1))
    response = await provider(runner).complete(make_request(system=""))
    assert response.error is not None
    assert response.raw == {
        "substituted_system": NEUTRAL_SYSTEM,
        "ignored_params": ["max_output_tokens"],
    }


async def test_parses_the_verified_payload() -> None:
    response = await provider(FakeRunner(ok(VERIFIED))).complete(
        make_request(schema=DECISION_SCHEMA)
    )
    assert response.ok
    assert response.parsed == {"decision": "DENY", "rules": ["LIFE-001"]}
    # The CLI's text disagrees with its structured output here; text must match what is scored.
    assert response.text == '{"decision": "DENY", "rules": ["LIFE-001"]}'
    assert response.stop_reason == "end_turn"
    usage = response.usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (
        16017,
        1152,
        0,
    )
    assert (usage.output_tokens, usage.reasoning_tokens) == (303, 289)
    assert response.reported_cost_usd == pytest.approx(0.034428)
    assert response.served_model == "grok-4.7"
    assert response.raw == {
        "num_turns": 1,
        "cli_text": '{"decision":"DENY"}',
        "ignored_params": ["max_output_tokens"],
    }


async def test_consistent_cli_text_is_kept_verbatim() -> None:
    payload = {**VERIFIED, "text": '{"decision":"DENY","rules":["LIFE-001"]}'}
    response = await provider(FakeRunner(ok(payload))).complete(make_request())
    assert response.text == '{"decision":"DENY","rules":["LIFE-001"]}'
    assert response.raw == {"num_turns": 1, "ignored_params": ["max_output_tokens"]}


# --------------------------------------------------------------------------- isolation


async def test_runs_with_a_private_home_holding_only_the_login(real_grok_home: Path) -> None:
    seen: dict[str, object] = {}

    def respond(command: Command) -> ProcessResult:
        home = Path(command.env["HOME"])
        private = Path(command.env["GROK_HOME"])
        seen["home"] = home
        seen["entries"] = sorted(p.name for p in private.iterdir())
        seen["auth_target"] = (private / "auth.json").resolve()
        seen["lock_target"] = (private / "auth.json.lock").resolve()
        seen["config"] = tomllib.loads((private / "config.toml").read_text(encoding="utf-8"))
        seen["home_entries"] = sorted(p.name for p in home.iterdir())
        return ok(VERIFIED)

    runner = FakeRunner(respond)
    await provider(runner).complete(make_request())
    env = runner.command.env
    home = seen["home"]
    assert isinstance(home, Path)
    assert env["GROK_HOME"] == str(home / ".grok")
    assert home.parent == Path(runner.command.cwd).parent  # inside the call's temp directory
    assert seen["home_entries"] == [".grok"]
    # No personal rules, AGENTS.md or config: only the login, its lock and our config.
    assert seen["entries"] == ["auth.json", "auth.json.lock", "config.toml"]
    assert seen["auth_target"] == (real_grok_home / "auth.json").resolve()
    assert seen["lock_target"] == (real_grok_home / "auth.json.lock").resolve()
    assert seen["config"] == {"skills": {"ignore": [str(home / ".grok" / "bundled" / "skills")]}}
    assert (env["GROK_MEMORY"], env["GROK_SUBAGENTS"], env["GROK_WEB_FETCH"]) == ("0", "0", "0")
    assert not home.exists()  # removed with the call's temp directory
    assert (real_grok_home / "auth.json").read_text(encoding="utf-8") == '{"session": "tok"}'


async def test_grok_variables_api_keys_and_home_relative_variables_are_dropped(
    monkeypatch,
) -> None:
    for name in (
        # Routing (found in the grok 1.0.46 binary), config, auth and agent-profile overrides.
        "GROK_XAI_API_BASE_URL",
        "GROK_WS_URL",
        "GROK_MODELS_BASE_URL",
        "GROK_MODELS_LIST_URL",
        "GROK_CLI_CHAT_PROXY_BASE_URL",
        "GROK_CONFIG",
        "GROK_CONFIG_PATH",
        "GROK_MANAGED_CONFIG_URL",
        "GROK_AUTH_PROVIDER_COMMAND",
        "GROK_CODE_XAI_API_KEY",
        "GROK_CLAUDE_RULES_ENABLED",
        "GROK_AGENT",
        "GROK_SANDBOX",
        "GROK_SOME_FUTURE_SWITCH",
        "XAI_API_KEY",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.setenv(name, "/elsewhere")
    # Auto-update is pinned off by --no-auto-update; a user switch could only re-enable it.
    monkeypatch.setenv("GROK_AUTO_UPDATE", "true")
    monkeypatch.setenv("GROK_DISABLE_AUTOUPDATER", "1")
    monkeypatch.setenv("UNRELATED", "not-allow-listed")
    runner = FakeRunner(ok(VERIFIED))
    await provider(runner).complete(make_request())
    env = runner.command.env
    assert not {key for key, value in env.items() if value == "/elsewhere"}
    # Only the variables the call sets itself.
    assert {key for key in env if key.startswith("GROK_")} == {
        "GROK_HOME",
        "GROK_MEMORY",
        "GROK_SUBAGENTS",
        "GROK_WEB_FETCH",
    }
    assert "--no-auto-update" in runner.command.argv
    assert "UNRELATED" not in env  # not on the allow-list, so not inherited either


async def test_default_grok_home_is_under_the_user_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GROK_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    (tmp_path / "user" / ".grok").mkdir(parents=True)
    (tmp_path / "user" / ".grok" / "auth.json").write_text("{}", encoding="utf-8")
    targets: list[Path] = []

    def respond(command: Command) -> ProcessResult:
        targets.append((Path(command.env["GROK_HOME"]) / "auth.json").resolve())
        return ok(VERIFIED)

    await provider(FakeRunner(respond)).complete(make_request())
    assert targets == [(tmp_path / "user" / ".grok" / "auth.json").resolve()]


async def test_missing_login_is_not_linked_and_the_cli_reports_it(real_grok_home: Path) -> None:
    (real_grok_home / "auth.json").unlink()
    entries: list[list[str]] = []

    def respond(command: Command) -> ProcessResult:
        entries.append(sorted(p.name for p in Path(command.env["GROK_HOME"]).iterdir()))
        return ok({"type": "error", "message": "Not logged in"}, returncode=1)

    response = await provider(FakeRunner(respond)).complete(make_request())
    assert entries == [["auth.json.lock", "config.toml"]]
    assert response.error is not None
    assert "Not logged in" in response.error


async def test_failing_to_build_the_private_home_is_an_error_not_a_crash(monkeypatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(Path, "symlink_to", broken)
    runner = FakeRunner(ok(VERIFIED))
    response = await provider(runner).complete(make_request())
    assert response.error is not None
    assert "private CLI home" in response.error
    assert runner.commands == []


async def test_a_refreshed_login_written_by_rename_is_synced_back(real_grok_home: Path) -> None:
    def respond(command: Command) -> ProcessResult:
        link = Path(command.env["GROK_HOME"]) / "auth.json"
        staged = link.with_name("auth.json.new")
        staged.write_text('{"session": "refreshed"}', encoding="utf-8")
        os.replace(staged, link)
        return ok(VERIFIED)

    response = await provider(FakeRunner(respond)).complete(make_request())
    assert response.ok
    auth = json.loads((real_grok_home / "auth.json").read_text(encoding="utf-8"))
    assert auth == {"session": "refreshed"}


async def test_error_payload_with_nonzero_exit() -> None:
    runner = FakeRunner(ok({"type": "error", "message": "model grok-9 not found"}, returncode=1))
    response = await provider(runner).complete(make_request())
    assert response.error is not None
    assert "model grok-9 not found" in response.error
    assert len(runner.commands) == 1


async def test_rate_limit_error_is_retried() -> None:
    runner = FakeRunner(
        ok({"type": "error", "message": "429: rate limit exceeded"}, returncode=1), ok(VERIFIED)
    )
    response = await provider(runner).complete(make_request())
    assert response.ok
    assert len(runner.commands) == 2


async def test_nonzero_exit_with_a_result_is_still_an_error() -> None:
    response = await provider(FakeRunner(ok(VERIFIED, returncode=2))).complete(make_request())
    assert response.error is not None
    assert response.parsed is not None  # kept for debugging; the error makes it INVALID


async def test_refusal_stop_reason() -> None:
    payload = {**VERIFIED, "stopReason": "refusal", "text": "I can't help."}
    del payload["structuredOutput"]
    response = await provider(FakeRunner(ok(payload))).complete(make_request())
    assert response.refused
    assert response.error is None


@pytest.mark.parametrize(
    "size",
    [
        MAX_ARG_BYTES + 1,
        # Fits on its own, but not together with the attached flag name.
        MAX_ARG_BYTES - len("--system-prompt-override=") + 1,
    ],
)
async def test_large_system_prompt_is_prepended_into_the_prompt_file(size: int) -> None:
    runner = FakeRunner(ok(VERIFIED))
    system = "R" * size
    response = await provider(runner).complete(make_request("Decide.", system=system))
    assert response.ok
    argv = runner.command.argv
    assert attached_value(argv, "--system-prompt-override") == SYSTEM_POINTER
    assert all(len(arg.encode()) <= MAX_ARG_BYTES for arg in argv)
    prompt = runner.snapshots[0].files[flag_value(argv, "--prompt-file")]
    assert prompt == f"{SYSTEM_BEGIN}\n{system}\n{SYSTEM_END}\n\nDecide."


async def test_a_system_prompt_that_just_fits_stays_in_argv() -> None:
    runner = FakeRunner(ok(VERIFIED))
    system = "R" * (MAX_ARG_BYTES - len("--system-prompt-override="))
    await provider(runner).complete(make_request("Decide.", system=system))
    argv = runner.command.argv
    assert attached_value(argv, "--system-prompt-override") == system
    assert runner.snapshots[0].files[flag_value(argv, "--prompt-file")] == "Decide."


async def test_garbage_output_is_an_error() -> None:
    response = await provider(FakeRunner(ok("Error: something broke", returncode=1))).complete(
        make_request()
    )
    assert response.error is not None
    assert "no JSON result" in response.error
