"""Fakes for provider tests: a scripted subprocess runner, minimal SDK clients, request helpers.

No test in this package spawns a model CLI or opens a network connection (the live tests in
``test_live.py`` are skipped unless ``IDENTITYBENCH_LIVE=1``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from identitybench.providers._subprocess import Command, ProcessResult
from identitybench.providers.base import (
    GenerationParams,
    Message,
    ModelRequest,
    Role,
    ToolSpec,
)

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["APPROVE", "DENY", "ESCALATE"]},
        "rules": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "rules"],
    "additionalProperties": False,
}

LOOKUP_TOOL = ToolSpec(
    name="get_identity",
    description="Look up an identity by id.",
    input_schema={
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
        "additionalProperties": False,
    },
)


def make_request(
    user: str = "Decide REQ-1.",
    *,
    system: str = "You are an access reviewer.",
    schema: dict[str, Any] | None = None,
    messages: tuple[Message, ...] | None = None,
    tools: tuple[ToolSpec, ...] = (),
    **params: Any,
) -> ModelRequest:
    return ModelRequest(
        system=system,
        messages=messages if messages is not None else (Message(Role.user, user),),
        output_schema=schema,
        tools=tools,
        params=GenerationParams(**params),
    )


# --------------------------------------------------------------------------- subprocess fake

Responder = ProcessResult | Callable[[Command], ProcessResult]


def ok(
    stdout: str | dict[str, Any] = "", *, stderr: str = "", returncode: int = 0
) -> ProcessResult:
    text = json.dumps(stdout) if isinstance(stdout, dict) else stdout
    return ProcessResult(returncode=returncode, stdout=text, stderr=stderr)


@dataclass
class Snapshot:
    """What the fake saw on disk while the command was 'running'."""

    cwd_entries: list[str]
    files: dict[str, str]


class FakeRunner:
    """Scripted ``ProcessRunner``: returns responders in order (the last one repeats)."""

    def __init__(self, *responders: Responder) -> None:
        self.responders = list(responders)
        self.commands: list[Command] = []
        self.snapshots: list[Snapshot] = []

    async def __call__(self, command: Command) -> ProcessResult:
        self.commands.append(command)
        files: dict[str, str] = {}
        for arg in command.argv:
            path = Path(arg)
            if arg.startswith("/") and path.is_file():
                files[arg] = path.read_text(encoding="utf-8")
        self.snapshots.append(Snapshot(sorted(p.name for p in Path(command.cwd).iterdir()), files))
        index = min(len(self.commands) - 1, len(self.responders) - 1)
        responder = self.responders[index]
        return responder(command) if callable(responder) else responder

    @property
    def command(self) -> Command:
        assert len(self.commands) == 1, f"expected one command, got {len(self.commands)}"
        return self.commands[0]


def flag_value(argv: tuple[str, ...], flag: str) -> str:
    index = argv.index(flag)
    return argv[index + 1]


def attached_value(argv: tuple[str, ...], flag: str) -> str:
    """The value of the one ``--flag=value`` element of ``argv``."""
    prefix = f"{flag}="
    values = [arg.removeprefix(prefix) for arg in argv if arg.startswith(prefix)]
    assert len(values) == 1, f"expected one {prefix}… argument, got {len(values)}"
    return values[0]


# --------------------------------------------------------------------------- SDK fakes


class FakeStream:
    def __init__(self, result: Any) -> None:
        self._result = result

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get_final_message(self) -> Any:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class FakeAnthropicMessages:
    def __init__(self, results: list[Any]) -> None:
        self._results = results
        self.create_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    def _next(self) -> Any:
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]

    async def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        result = self._next()
        if isinstance(result, BaseException):
            raise result
        return result

    def stream(self, **kwargs: Any) -> FakeStream:
        self.stream_calls.append(kwargs)
        return FakeStream(self._next())


class FakeAnthropicClient:
    def __init__(self, *results: Any) -> None:
        self.messages = FakeAnthropicMessages(list(results))


class FakeOpenAICompletions:
    def __init__(self, results: list[Any]) -> None:
        self._results = results
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self._results.pop(0) if len(self._results) > 1 else self._results[0]
        if isinstance(result, BaseException):
            raise result
        return result


class FakeOpenAIClient:
    """``chat.completions`` and ``responses`` share one queue of scripted results."""

    def __init__(self, *results: Any) -> None:
        queue = list(results)
        self.completions = FakeOpenAICompletions(queue)
        self.responses = FakeOpenAICompletions(queue)
        self.chat = self
