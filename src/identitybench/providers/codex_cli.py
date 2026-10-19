"""``codex exec`` (OpenAI Codex CLI, headless) as a provider, using the CLI's ChatGPT login.

Codex runs read-only in an empty temporary directory, ephemerally (no session files), with the
prompt on stdin. Structured output uses ``--output-schema``; the final message is written to a
file (``-o``), and usage comes from the ``turn.completed`` event of the ``--json`` event stream.

``codex exec`` loads ``$CODEX_HOME/AGENTS.md`` (global instructions), ``config.toml`` (MCP
servers, ``model_provider``, profiles, hooks) and ``~/.agents/skills`` whatever the cwd. A
config-level ``model_provider`` could even route requests somewhere the endpoint identity doesn't
know about. So each call runs with ``--ignore-user-config`` and ``--ignore-rules``, and with
``HOME`` and ``CODEX_HOME`` pointing at a private directory that holds only a link to the real
``auth.json`` (the ChatGPT login still resolves).

That only removes setup kept in the home directory. Out of the box ``codex exec`` is a full agent:
a shell (which the read-only sandbox still lets read any file on disk, a local dataset's gold
labels included), browser and computer use, image generation, subagents, memories, hooks,
skills, web search, and apps, plugins and plugin recommendations tied to the ChatGPT account
rather than to the home directory, so the prompt would differ between accounts. Every call turns
those off (``DISABLED_FEATURES``, ``CONFIG_OVERRIDES``). Codex rejects an unknown feature name,
so a CLI upgrade that drops one fails every call loudly instead of silently changing the setup.
Measured with codex-cli 0.162.1 and gpt-6-astra, a tiny structured request drops from ~23K input
tokens to ~8.8K, and ``codex debug prompt-input`` shows the account-linked plugin list and the
bundled skills gone. What remains is the CLI's fixed scaffolding for the model (base
instructions, sandbox and multi-agent notes, environment context), which depends only on the CLI
version and model, both part of the endpoint identity.

Codex has no way to replace its own system prompt, so the request's system text is prepended to
the prompt between delimiter lines. That makes Codex rows a slightly different prompt setup from
API rows; reports should say so.

The child environment is the common allow-list (``_cli.COMMON_ENV_KEEP``) and nothing of the
CLI's own: every ``CODEX_*`` and ``OPENAI_*`` variable is dropped by prefix, and
``OPENAI_BASE_URL``, ``OPENAI_API_KEY`` and ``CODEX_HOME`` also by name, so the CLI uses the
private home's login and its own endpoint instead of whatever OpenAI-compatible server the
harness points at. ``HOME`` and ``CODEX_HOME`` are then set to the private home.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any, Unpack

from identitybench.providers._cli import (
    CliOptions,
    CliProvider,
    Invocation,
    Isolation,
    as_dict,
    as_int,
    link_credentials,
    parse_jsonl,
    prepend_system,
    render_transcript,
)
from identitybench.providers._json import load_object
from identitybench.providers._subprocess import ProcessResult
from identitybench.providers.base import ModelRequest, ModelResponse, Usage

OUTPUT_FILE = "last_message.txt"
SCHEMA_FILE = "output_schema.json"
DISABLED_FEATURES: tuple[str, ...] = (
    # Tied to the ChatGPT account, not to CODEX_HOME.
    "apps",
    "plugins",
    "remote_plugin",
    "plugin_hooks",
    "recommended_plugins",
    "tool_suggest",
    # Reading the filesystem or running code.
    "shell_tool",
    "unified_exec",
    "js_repl",
    "view_image",
    # Agent features a single structured answer doesn't need.
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
"""Codex features switched off for every call (``--disable``; see the module docstring)."""

CONFIG_OVERRIDES: tuple[str, ...] = (
    "skills.bundled.enabled=false",
    "skills.include_instructions=false",
    'web_search="disabled"',
)
"""``-c`` overrides: no bundled skills and no skills section in the prompt, no web search."""

DEFAULT_EFFORT = "medium"
"""Effort used when neither the request nor the constructor sets one. Codex's own default
depends on the model (and on the user config this provider ignores), so it is pinned and the
factory records it in the spec's generation parameters."""


def real_codex_home() -> Path:
    """The user's Codex config directory (``CODEX_HOME``, default ``~/.codex``)."""
    configured = os.environ.get("CODEX_HOME")
    return Path(configured) if configured else Path.home() / ".codex"


def _event_error(event: dict[str, Any]) -> str | None:
    """Error text from an ``error`` or ``turn.failed`` event."""
    kind = event.get("type")
    if kind == "error":
        message = event.get("message")
        return str(message) if message else "error event"
    if kind == "turn.failed":
        message = as_dict(event.get("error")).get("message")
        return str(message) if message else "turn failed"
    return None


def _agent_message(event: dict[str, Any]) -> str | None:
    if event.get("type") != "item.completed":
        return None
    item = as_dict(event.get("item"))
    if item.get("type") in ("agent_message", "assistant_message") or item.get("item_type") in (
        "agent_message",
        "assistant_message",
    ):
        text = item.get("text")
        return text if isinstance(text, str) else None
    return None


class CodexCliProvider(CliProvider):
    KIND = "codex_cli"
    ENV_DROP = frozenset({"OPENAI_BASE_URL", "OPENAI_API_KEY", "CODEX_HOME"})
    ENV_DROP_PREFIXES = ("CODEX_", "OPENAI_")

    def __init__(
        self,
        model: str,
        *,
        effort: str | None = DEFAULT_EFFORT,
        timeout_s: float = 900.0,
        binary: str = "codex",
        **options: Unpack[CliOptions],
    ) -> None:
        super().__init__(model, effort=effort, timeout_s=timeout_s, binary=binary, **options)

    def _stale_outputs(self, io_dir: Path) -> tuple[Path, ...]:
        return (io_dir / OUTPUT_FILE,)

    def _isolate(self, home: Path) -> Isolation:
        private = home / ".codex"
        links = link_credentials(real_codex_home(), private, ("auth.json",))
        return Isolation(env={"HOME": str(home), "CODEX_HOME": str(private)}, links=links)

    def _build(
        self, request: ModelRequest, effort: str | None, io_dir: Path, work_dir: Path
    ) -> Invocation:
        prompt = prepend_system(request.system, render_transcript(request.messages))
        argv = [self.binary, "exec", "-m", self.model]
        if effort is not None:
            argv += ["-c", f'model_reasoning_effort="{effort}"']
        argv += [
            "-s",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
        ]
        for feature in DISABLED_FEATURES:
            argv += ["--disable", feature]
        for override in CONFIG_OVERRIDES:
            argv += ["-c", override]
        argv += ["--json", "-o", str(io_dir / OUTPUT_FILE)]
        if request.output_schema is not None:
            schema_path = io_dir / SCHEMA_FILE
            schema_path.write_text(json.dumps(request.output_schema), encoding="utf-8")
            argv += ["--output-schema", str(schema_path)]
        argv += ["-C", str(work_dir), "-"]
        return Invocation(argv=tuple(argv), stdin=prompt)

    def _parse(self, result: ProcessResult, request: ModelRequest, io_dir: Path) -> ModelResponse:
        events = parse_jsonl(result.stdout)
        usage = Usage()
        raw: dict[str, Any] = {}
        errors: list[str] = []
        streamed_text: str | None = None
        turn_completed = False
        for event in events:
            kind = event.get("type")
            if kind == "thread.started" and event.get("thread_id"):
                raw["thread_id"] = event["thread_id"]
            elif kind == "turn.completed":
                turn_completed = True
                counts = as_dict(event.get("usage"))
                total_in = as_int(counts.get("input_tokens"))
                cached = min(as_int(counts.get("cached_input_tokens")), total_in)
                # Codex reports OpenAI-style counts: input includes cached tokens and output
                # includes reasoning tokens. ``Usage.input_tokens`` is uncached input only.
                usage.add(
                    Usage(
                        input_tokens=total_in - cached,
                        cache_read_tokens=cached,
                        output_tokens=as_int(counts.get("output_tokens")),
                        reasoning_tokens=as_int(counts.get("reasoning_output_tokens")),
                    )
                )
            elif (message := _agent_message(event)) is not None:
                streamed_text = message
            elif (error := _event_error(event)) is not None:
                errors.append(error)

        text: str | None = None
        with contextlib.suppress(OSError):
            text = (io_dir / OUTPUT_FILE).read_text(encoding="utf-8").strip() or None
        if text is None and streamed_text:
            text = streamed_text.strip() or None

        response = ModelResponse(
            text=text,
            usage=usage,
            stop_reason="completed" if turn_completed else None,
            raw=raw or None,
        )
        if text is None:
            reason = errors[-1] if errors else "no final message"
            response.error = self._error_text(reason, result)
            return response
        if result.returncode != 0 and not turn_completed:
            reason = errors[-1] if errors else "exited without completing the turn"
            response.error = self._error_text(reason, result)
            return response
        if request.output_schema is not None:
            response.parsed = load_object(text)
        return response
