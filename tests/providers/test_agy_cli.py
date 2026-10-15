from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from identitybench.providers import agy_cli
from identitybench.providers._cli import ACTION_SCHEMA_NAME, MAX_ARG_BYTES, NEUTRAL_SYSTEM
from identitybench.providers._subprocess import Command, ProcessResult
from identitybench.providers.agy_cli import (
    AGENT_FILE,
    AgyCliProvider,
    agent_definition,
    served_model,
)
from tests.providers.fakes import DECISION_SCHEMA, FakeRunner, flag_value, make_request, ok

CONVERSATION = "237b62f7-66d2-4d06-98ab-edf18622761d"

# Real `agy --input-format stream-json --output-format stream-json --json-schema ...` events
# (agy 1.3.5, gemini-4-argon at medium effort; the init event's tool list shortened).
INIT = {
    "event": "init",
    "conversation_id": CONVERSATION,
    "init": {
        "model": "gemini-4-argon",
        "cwd": "/tmp/work",
        "agent": "identitybench",
        "tools": ["finish", "manage_task"],
        "permission_mode": "request-review",
    },
}
STEP = {
    "event": "step_update",
    "step_update": {
        "conversation_id": CONVERSATION,
        "step_index": 2,
        "state": "DONE",
        "step_type": "finish",
        "duration_seconds": 0.005186,
    },
}
RESULT = {
    "conversation_id": CONVERSATION,
    "status": "SUCCESS",
    "response": '{"decision":"DENY","rules":["sec-001"],"toolAction":"Finishing access decision'
    ' task","toolSummary":"Access decision completion"}\n',
    "duration_seconds": 11.101832,
    "num_turns": 1,
    "structured_output": {"decision": "DENY", "rules": ["sec-001"]},
    "json_schema": DECISION_SCHEMA,
    "usage": {
        "input_tokens": 821,
        "output_tokens": 50,
        "thinking_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 871,
    },
}
# The lines of the CLI's log that name the model (the log also names the account; it is never
# stored).
LOG = (
    'I1014 21:05:45.647337 1 model_resolver.go:155] model alias "gemini-4-argon" resolved to '
    '"gemini-4-argon-medium"\n'
    "I1014 21:05:45.647400 1 model_resolver.go:107] Resolving model gemini-4-argon-medium\n"
)


def stream(result: dict[str, object] = RESULT, *, log: str | None = LOG) -> object:
    """A responder printing the event stream and writing the CLI's log where it was asked to."""
    events = [INIT, STEP, {"event": "result", "result": result}]
    stdout = "\n".join(json.dumps(event) for event in events) + "\n"

    def respond(command: Command) -> ProcessResult:
        if log is not None:
            Path(flag_value(command.argv, "--log-file")).write_text(log, encoding="utf-8")
        return ok(stdout)

    return respond


def provider(runner: FakeRunner, **kwargs) -> AgyCliProvider:
    return AgyCliProvider(
        "gemini-4-argon", runner=runner, version="1.3.5", retry_base_delay_s=0, **kwargs
    )


def agent_file(command: Command) -> Path:
    return Path(command.env["HOME"]) / AGENT_FILE


class Seen:
    """Records what the CLI would have found in its private home while it ran."""

    def __init__(self, inner: object = None) -> None:
        self.inner = inner or stream()
        self.agent: str | None = None
        self.home_entries: list[str] = []
        self.keychains: Path | None = None

    def __call__(self, command: Command) -> ProcessResult:
        home = Path(command.env["HOME"])
        self.agent = agent_file(command).read_text(encoding="utf-8")
        self.home_entries = sorted(str(p.relative_to(home)) for p in home.rglob("*"))
        link = home / "Library" / "Keychains"
        self.keychains = link.resolve() if link.is_symlink() else None
        assert callable(self.inner)
        return self.inner(command)


# --------------------------------------------------------------------------- the command


async def test_argv_and_stdin() -> None:
    runner = FakeRunner(stream())
    response = await provider(runner).complete(make_request("Decide REQ-1.", system="SYS"))
    command = runner.command
    log_file = flag_value(command.argv, "--log-file")
    assert command.argv == (
        "agy",
        "--model",
        "gemini-4-argon",
        "--agent",
        "identitybench",
        "--disable-slash-commands",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--log-file",
        log_file,
        "-p",
        "",
    )
    assert Path(log_file).parent != Path(command.cwd)
    assert command.stdin is not None
    assert command.stdin.endswith("\n") and command.stdin.count("\n") == 1
    assert json.loads(command.stdin) == {
        "event": "user",
        "message": {"role": "user", "content": "Decide REQ-1."},
    }
    assert runner.snapshots[0].cwd_entries == []
    assert response.ok
    assert "substituted_system" not in (response.raw or {})


async def test_the_system_prompt_is_the_body_of_a_tool_less_agent() -> None:
    seen = Seen()
    await provider(FakeRunner(seen)).complete(make_request(system="You review access."))
    assert seen.agent == agent_definition("You review access.", structured=False)
    front, body = seen.agent.split("\n---\n\n", 1)
    assert body == "You review access."
    assert front.splitlines() == [
        "---",
        "name: identitybench",
        "description: A single model call with no tools.",
        "mainAgent: true",
        "subagent: false",
        "excludeDefaultComponents: true",
        "inheritCustomizations: false",
        "inheritMcp: false",
        "tools: []",
    ]


async def test_a_markdown_system_prompt_is_kept_verbatim() -> None:
    """Front matter, H1 headings and blank lines in the system prompt go into the body as they
    are (the CLI turns each H1 into a section tag; see the module doc)."""
    system = "---\ntitle: x\n---\nRole.\n\n# Identity governance policy\n\n## Rules\n\n- R1\n"
    seen = Seen()
    await provider(FakeRunner(seen)).complete(make_request(system=system))
    assert seen.agent is not None
    assert seen.agent.endswith(f"\n---\n\n{system}")


@pytest.mark.parametrize("system", ["", "  \n\t"])
async def test_an_empty_system_prompt_is_replaced_by_the_neutral_one(system: str) -> None:
    seen = Seen()
    response = await provider(FakeRunner(seen)).complete(make_request(system=system))
    assert seen.agent == agent_definition(NEUTRAL_SYSTEM, structured=False)
    assert response.raw is not None
    assert response.raw["substituted_system"] == NEUTRAL_SYSTEM


async def test_a_schema_goes_in_a_file_and_offers_the_finish_tool() -> None:
    seen = Seen()
    runner = FakeRunner(seen)
    await provider(runner).complete(make_request(schema=DECISION_SCHEMA))
    argv = runner.command.argv
    schema_file = flag_value(argv, "--json-schema")
    assert json.loads(runner.snapshots[0].files[schema_file]) == DECISION_SCHEMA
    assert Path(schema_file).parent != Path(runner.command.cwd)
    assert seen.agent == agent_definition("You are an access reviewer.", structured=True)
    assert "tools: [finish]" in seen.agent
    assert argv[-2:] == ("-p", "")


async def test_an_action_protocol_turn_is_sent_without_the_schema() -> None:
    """As in ``claude_cli``: the ``finish`` tool would put the model in tool-calling mode, and
    it would try to call the listed lookups natively."""
    seen = Seen()
    runner = FakeRunner(seen)
    action = replace(make_request(schema=DECISION_SCHEMA), schema_name=ACTION_SCHEMA_NAME)
    response = await provider(runner).complete(action)
    assert "--json-schema" not in runner.command.argv
    assert seen.agent is not None and "tools: []" in seen.agent
    assert response.raw is not None
    assert response.raw["json_schema"] == "withheld (JSON action protocol turn)"


async def test_effort_flag() -> None:
    runner = FakeRunner(stream())
    response = await provider(runner, effort="medium").complete(make_request())
    argv = runner.command.argv
    assert flag_value(argv, "--effort") == "medium"
    assert argv[-2:] == ("-p", "")
    assert response.raw is not None
    assert response.raw["effort"] == "medium"  # the provider default applied, so it is recorded


async def test_no_effort_flag_by_default() -> None:
    runner = FakeRunner(stream())
    await provider(runner).complete(make_request())
    assert "--effort" not in runner.command.argv


async def test_a_prompt_over_the_argv_limit_goes_through_stdin() -> None:
    runner = FakeRunner(stream())
    prompt = "R" * (MAX_ARG_BYTES * 2)
    response = await provider(runner).complete(make_request(prompt))
    assert response.ok
    assert json.loads(runner.command.stdin or "")["message"]["content"] == prompt
    assert all(len(arg.encode()) <= MAX_ARG_BYTES for arg in runner.command.argv)


async def test_a_prompt_starting_with_a_slash_or_dash_is_sent_as_is() -> None:
    runner = FakeRunner(stream())
    await provider(runner).complete(make_request("/clear --model x"))
    assert json.loads(runner.command.stdin or "")["message"]["content"] == "/clear --model x"
    assert "--disable-slash-commands" in runner.command.argv


# --------------------------------------------------------------------------- isolation


async def test_runs_with_a_private_home_holding_only_the_agent_and_the_keychain_link(
    tmp_path: Path, monkeypatch
) -> None:
    user = tmp_path / "user"
    (user / "Library" / "Keychains").mkdir(parents=True)
    (user / ".gemini" / "config").mkdir(parents=True)
    (user / ".gemini" / "config" / "hooks.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(user))
    seen = Seen()
    runner = FakeRunner(seen)
    await provider(runner).complete(make_request())
    home = Path(runner.command.env["HOME"])
    assert home != user
    assert home.parent == Path(runner.command.cwd).parent  # inside the call's temp directory
    assert seen.home_entries == [
        ".gemini",
        ".gemini/config",
        ".gemini/config/agents",
        ".gemini/config/agents/identitybench",
        ".gemini/config/agents/identitybench/agent.md",
        "Library",
        "Library/Keychains",
    ]
    assert seen.keychains == (user / "Library" / "Keychains").resolve()
    assert not home.exists()  # removed with the call's temp directory


async def test_no_keychain_link_where_the_user_has_no_keychain_directory(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    seen = Seen()
    await provider(FakeRunner(seen)).complete(make_request())
    assert seen.keychains is None
    assert "Library" not in seen.home_entries


async def test_routing_and_credential_variables_are_dropped(monkeypatch) -> None:
    for name in (
        "GEMINI_API_KEY",
        "GEMINI_BASE_URL",
        "GEMINI_DEFAULT_AUTH_TYPE",
        "GOOGLE_GEMINI_BASE_URL",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "CLOUD_CODE_URL",
        "AGY_LLM_GATEWAY_URL",
        "AGY_LLM_GATEWAY_API_KEY",
        "AGY_ACCOUNT",
        "ANTIGRAVITY_APP_DATA_DIR",
        "JETSKI_APP_DATA_DIR",
        "JETSKI_OAUTH_TOKEN",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    ):
        monkeypatch.setenv(name, "/elsewhere")
    monkeypatch.setenv("AGY_CLI_DISABLE_AUTO_UPDATE", "false")
    monkeypatch.setenv("UNRELATED", "kept")
    runner = FakeRunner(stream())
    await provider(runner).complete(make_request())
    env = runner.command.env
    assert not {key for key, value in env.items() if value == "/elsewhere"}
    assert {key for key in env if key.startswith(("AGY_", "GEMINI_"))} == {
        "AGY_CLI_DISABLE_AUTO_UPDATE"
    }
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
    assert env["UNRELATED"] == "kept"


async def test_failing_to_build_the_private_home_is_an_error_not_a_crash(monkeypatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(Path, "write_text", broken)
    runner = FakeRunner(stream())
    response = await provider(runner).complete(make_request(schema=None, system="S"))
    assert response.error is not None
    assert runner.commands == []


def test_setup_revision_covers_the_agent_definition(monkeypatch) -> None:
    before = AgyCliProvider("m", version="1").setup_revision()
    monkeypatch.setattr(agy_cli, "AGENT_NAME", "other")
    assert AgyCliProvider("m", version="1").setup_revision() != before


# --------------------------------------------------------------------------- the result


async def test_parses_the_verified_stream() -> None:
    response = await provider(FakeRunner(stream())).complete(make_request(schema=DECISION_SCHEMA))
    assert response.ok
    assert response.parsed == {"decision": "DENY", "rules": ["sec-001"]}
    # The CLI's text includes the finish tool's own fields; the text must match what is scored.
    assert response.text == '{"decision": "DENY", "rules": ["sec-001"]}'
    usage = response.usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (821, 0, 0)
    assert (usage.output_tokens, usage.reasoning_tokens) == (50, 0)
    assert response.reported_cost_usd is None
    assert response.served_model == "gemini-4-argon-medium"
    assert response.raw is not None
    assert response.raw["status"] == "SUCCESS"
    assert response.raw["num_turns"] == 1
    assert response.raw["cli_text"] == RESULT["response"]
    assert "json_schema" not in response.raw


async def test_cache_reads_are_taken_out_of_the_input_count() -> None:
    usage = {"input_tokens": 9000, "output_tokens": 700, "thinking_tokens": 600}
    result = {**RESULT, "usage": {**usage, "cache_read_tokens": 4000, "total_tokens": 9700}}
    response = await provider(FakeRunner(stream(result))).complete(make_request())
    assert (response.usage.input_tokens, response.usage.cache_read_tokens) == (5000, 4000)
    assert (response.usage.output_tokens, response.usage.reasoning_tokens) == (700, 600)


async def test_plain_text_answer_without_a_schema() -> None:
    result = {**RESULT, "response": '{"decision": "APPROVE"}\n'}
    del result["structured_output"]
    response = await provider(FakeRunner(stream(result))).complete(make_request())
    assert response.ok
    assert response.parsed is None
    assert response.text == '{"decision": "APPROVE"}\n'


async def test_no_log_means_no_served_model() -> None:
    response = await provider(FakeRunner(stream(log=None))).complete(make_request())
    assert response.ok
    assert response.served_model is None


def test_served_model_takes_the_last_resolution() -> None:
    assert served_model(LOG) == "gemini-4-argon-medium"
    assert served_model("Resolving model gemini-3.8-flash-low\n") == "gemini-3.8-flash-low"
    assert served_model("nothing here") is None


async def test_error_status_is_an_error() -> None:
    result = {**RESULT, "status": "ERROR", "error": "model gemini-9 not found", "response": ""}
    del result["structured_output"]
    runner = FakeRunner(stream(result))
    response = await provider(runner).complete(make_request())
    assert response.error is not None
    assert "model gemini-9 not found" in response.error
    assert len(runner.commands) == 1


async def test_quota_error_is_retried() -> None:
    error = {**RESULT, "status": "ERROR", "error": "429 RESOURCE_EXHAUSTED: rate limit"}
    runner = FakeRunner(stream(error), stream())
    response = await provider(runner).complete(make_request())
    assert response.ok
    assert len(runner.commands) == 2
    assert response.raw is not None and response.raw["attempts"] == 2


async def test_a_retry_never_reads_the_previous_attempts_log() -> None:
    error = {**RESULT, "status": "ERROR", "error": "503 service unavailable"}
    runner = FakeRunner(stream(error), stream(log=None))
    response = await provider(runner).complete(make_request())
    assert response.ok
    assert response.served_model is None


async def test_nonzero_exit_with_a_success_result_is_still_an_error() -> None:
    events = "\n".join(json.dumps(e) for e in (INIT, {"event": "result", "result": RESULT}))
    response = await provider(FakeRunner(ok(events, returncode=3))).complete(make_request())
    assert response.error is not None
    assert response.parsed is not None  # kept for debugging; the error makes it INVALID


async def test_no_result_event_is_an_error() -> None:
    runner = FakeRunner(ok(json.dumps(INIT) + "\n", stderr="AGY_ERROR: {}", returncode=3))
    response = await provider(runner).complete(make_request())
    assert response.error is not None
    assert "no result event" in response.error
    assert "AGY_ERROR" in response.error


async def test_garbage_output_is_an_error() -> None:
    response = await provider(FakeRunner(ok("Error: something broke", returncode=1))).complete(
        make_request()
    )
    assert response.error is not None
