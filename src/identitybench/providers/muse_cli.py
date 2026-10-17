"""Muse Code subscription calls, isolated from personal context and tools.

Measured against 1.4.10-R6427.1. The empty preset has no tools; the benchmark
system prompt replaces the coding prompt through run.system_prompt. Built-in
skills are disabled explicitly. Muse still adds fixed workspace/permission and
muse-core developer reminders, which are part of this CLI's scaffolding.

Transport tracing is captured only in memory to verify the actual response model,
usage and subscription accounting. Neither request bodies nor raw traces enter
response artifacts or error messages. An API key or non-subscription login is
refused, and model substitutions fail closed.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Unpack

from identitybench.providers._cli import (
    NEUTRAL_SYSTEM,
    CliOptions,
    CliProvider,
    Invocation,
    Isolation,
    as_dict,
    as_int,
    link_credentials,
    parse_jsonl,
    render_transcript,
)
from identitybench.providers._json import load_object
from identitybench.providers._subprocess import ProcessResult
from identitybench.providers.base import ModelRequest, ModelResponse, Usage

DEFAULT_EFFORT = "medium"
SETTINGS = ".config/muse/settings.json"
BUNDLED_SKILLS = (
    "blender",
    "browser-app-delivery",
    "create-plugin",
    "create-skill",
    "doctor",
    "durable-test-collateral",
    "git",
    "greenfield-project-scaffolding",
    "grill",
    "import",
    "manage-settings",
    "migrate",
    "plan",
    "python-env",
    "read-session",
    "requirements-clarification",
    "resume-claude",
    "resume-codex",
    "table-fit",
    "taste",
    "workflow-authoring",
)


def settings(system: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "contributor_mode_enabled": False,
        "telemetry": {"enabled": False},
        "run": {"system_prompt": system.strip() or NEUTRAL_SYSTEM, "developer_prompt": ""},
        "skills": {
            "activation": {
                "bundled": {
                    f"bundled://muse-core/skills/{name}/SKILL.md": "off" for name in BUNDLED_SKILLS
                },
                "plugin": {"plugin://threejs/skills/threejs/SKILL.md": "off"},
            }
        },
    }


def trace_events(stderr: str) -> list[dict[str, Any]]:
    """Decode SSE trace data without ever retaining the trace in a response."""
    events = []
    for line in stderr.splitlines():
        if not line.startswith("SSE RESPONSE: "):
            continue
        _, separator, data = line.partition("data: ")
        if separator and (event := load_object(data)) is not None:
            events.append(event)
    return events


class MuseCliProvider(CliProvider):
    KIND = "muse_cli"
    ENV_DROP_PREFIXES = ("META_", "MUSE_", "OPENAI_", "ANTHROPIC_", "XDG_")
    ENV_SET = MappingProxyType({"MUSE_TRANSPORT_TRACE": "1"})

    def __init__(
        self,
        model: str,
        *,
        effort: str | None = DEFAULT_EFFORT,
        timeout_s: float = 900.0,
        binary: str = "muse",
        **options: Unpack[CliOptions],
    ) -> None:
        # Quota failures must not cause an unbounded subscription retry loop.
        options.setdefault("max_retries", 0)
        super().__init__(model, effort=effort, timeout_s=timeout_s, binary=binary, **options)

    def _isolate(self, home: Path) -> Isolation:
        real = Path.home() / ".config/muse"
        auth = real / "auth.json"
        if auth.exists():
            data = load_object(auth.read_text()) or {}
            meta = as_dict(as_dict(data.get("providers")).get("meta"))
            if meta.get("mechanism") != "oauth" or meta.get("subs_active") is not True:
                raise OSError("Muse requires an active OAuth subscription login")
            if meta.get("api_base_url") != "https://api.meta.ai/v1":
                raise OSError("Muse subscription login has an unexpected API endpoint")
        links = link_credentials(
            real, home / ".config/muse", ("auth.json",), shared=(".auth.json.lock",)
        )
        keychains = Path.home() / "Library/Keychains"
        if keychains.is_dir():
            (home / "Library").mkdir(exist_ok=True)
            (home / "Library/Keychains").symlink_to(keychains, target_is_directory=True)
        return Isolation(env={"HOME": str(home)}, links=links)

    def _build(
        self, request: ModelRequest, effort: str | None, io_dir: Path, work_dir: Path
    ) -> Invocation:
        prompt = io_dir / "prompt.txt"
        prompt.write_text(render_transcript(request.messages), encoding="utf-8")
        argv = [
            self.binary,
            "exec",
            "--provider",
            "meta",
            "--model",
            self.model,
            "--preset",
            "empty",
            "--json",
            "--contributor-mode",
            "off",
            "--no-session-log",
            "--disable-write",
            "--disable-shell",
            "--disable-web-tools",
            "--disable-reminders",
            "--disable-request-user-input",
            "--no-foreign-personal-context",
            "--approval-judge",
            "off",
            "--max-model-steps",
            "1",
            "--prompt-file",
            str(prompt),
        ]
        argv += ["--reasoning-effort", effort or DEFAULT_EFFORT]
        if request.output_schema is not None:
            schema = io_dir / "schema.json"
            schema.write_text(json.dumps(request.output_schema), encoding="utf-8")
            argv += ["--output-schema", str(schema)]
        raw: dict[str, Any] = {
            "billing_route": "muse_code_subscription",
            "contributor_mode": False,
            "system_outer_whitespace_normalized": True,
            "cli_scaffolding": "workspace, permissions, muse-core reminders",
        }
        if not request.system.strip():
            raw["substituted_system"] = NEUTRAL_SYSTEM
        return Invocation(
            tuple(argv), raw=raw, home_files={SETTINGS: json.dumps(settings(request.system))}
        )

    def _parse(self, result: ProcessResult, request: ModelRequest, io_dir: Path) -> ModelResponse:
        terminals = [
            as_dict(e.get("payload"))
            for e in parse_jsonl(result.stdout)
            for _ in (0,)
            if str(e.get("payload_type", "")).startswith("run.terminal.")
        ]
        events = trace_events(result.stderr)
        responses = [
            as_dict(e.get("response"))
            for e in events
            if e.get("type") in ("response.completed", "response.incomplete")
        ]
        terminal = terminals[-1] if terminals else {}
        payload = responses[-1] if responses else {}
        usage = as_dict(payload.get("usage"))
        cached = as_int(as_dict(usage.get("input_tokens_details")).get("cached_tokens"))
        subscription = [
            as_dict(e.get("subscription"))
            for e in events
            if e.get("type") == "response.subscription_usage"
        ]
        text = terminal.get("text")
        response = ModelResponse(
            text=text if isinstance(text, str) else None,
            parsed=load_object(text) if isinstance(text, str) else None,
            served_model=payload.get("model"),
            stop_reason=payload.get("status"),
            usage=Usage(
                input_tokens=max(as_int(usage.get("input_tokens")) - cached, 0),
                output_tokens=as_int(usage.get("output_tokens")),
                cache_read_tokens=cached,
                reasoning_tokens=as_int(
                    as_dict(usage.get("output_tokens_details")).get("reasoning_tokens")
                ),
            ),
            raw={"subscription_usage": subscription[-1] if subscription else None},
        )
        problem = None
        if result.returncode != 0 or terminal.get("terminal") != "completed":
            # Do not pass result to _error_text: stderr contains benchmark prompts.
            reason = terminal.get("reason") or "no completed terminal event"
            problem = f"exit {result.returncode}: {reason}"
        elif len(responses) != 1:
            problem = "expected exactly one provider response"
        elif payload.get("model") != self.model:
            problem = "served model did not match the requested model"
        elif not subscription:
            problem = "subscription accounting was not reported"
        elif payload.get("status") != "completed":
            problem = "incomplete model response"
        requests = [
            load_object(line.removeprefix("REQUEST: "))
            for line in result.stderr.splitlines()
            if line.startswith("REQUEST: ")
        ]
        if len(requests) != 1 or requests[0] is None:
            problem = problem or "expected exactly one traced provider request"
        elif requests[0].get("tools"):
            problem = "unexpected tools in provider request"
        elif requests[0].get("instructions") != (request.system.strip() or NEUTRAL_SYSTEM):
            problem = "benchmark system instructions were changed"
        if problem:
            response.error = self._error_text(problem)
        return response
