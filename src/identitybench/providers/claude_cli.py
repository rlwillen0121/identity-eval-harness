"""``claude -p`` (Claude Code, headless) as a provider, using the CLI's subscription login.

The call is stripped down to a plain model call: the system prompt is replaced, every built-in
tool is disabled (``--tools ""``), MCP servers and user/project/local settings are not loaded,
nothing is persisted, and the process runs in an empty temporary directory. An empty system
prompt is replaced by ``NEUTRAL_SYSTEM`` rather than passed as ``--system-prompt ""``, which the
CLI could read as unset and answer with its own agent prompt. ``--safe-mode`` also
turns off every customization the flags above don't name (CLAUDE.md files, installed plugins,
hooks, custom agents and commands, output styles), ``--disable-slash-commands`` removes skills
and ``--no-chrome`` the browser integration. (``--bare`` would be stricter still but only
authenticates with an API key, never the subscription login.) Structured output uses
``--json-schema``; the CLI returns the validated object as ``structured_output``.

``ANTHROPIC_API_KEY``, ``ANTHROPIC_BASE_URL`` and ``ANTHROPIC_AUTH_TOKEN`` are removed from the
child environment so the CLI can't silently switch from the subscription login to an API key or
a different endpoint. So are the parent Claude Code session's variables (``CLAUDECODE``,
``CLAUDE_CODE_*``, ``CLAUDE_EFFORT``): when the harness itself runs inside Claude Code they would
otherwise tie the child to that session or override ``--effort``. ``CLAUDE_CODE_OAUTH_TOKEN`` is
kept because it *is* a subscription login. ``DISABLE_AUTOUPDATER=1`` is set so the CLI can't
update itself during a run, after ``endpoint_identity`` recorded the version.

Sampling temperature can't be set through the CLI (and current Claude models reject it anyway);
``max_output_tokens`` is not configurable either. The CLI's ``--fallback-model`` is never passed:
an eval must not silently answer with a different model.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Unpack

from identitybench.providers._cli import (
    CliOptions,
    CliProvider,
    Invocation,
    as_dict,
    as_float,
    as_int,
    parse_json_payload,
    render_transcript,
    served_models,
    structured_text,
    system_override,
)
from identitybench.providers._subprocess import ProcessResult
from identitybench.providers.base import ModelRequest, ModelResponse, Usage

_RAW_KEYS = ("subtype", "num_turns", "duration_ms", "stop_reason", "session_id")


class ClaudeCliProvider(CliProvider):
    KIND = "claude_cli"
    ENV_DROP = frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDECODE",
            "CLAUDE_PID",
            "CLAUDE_EFFORT",
        }
    )
    ENV_DROP_PREFIXES = ("CLAUDE_CODE_",)
    ENV_KEEP = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})
    ENV_SET = MappingProxyType({"DISABLE_AUTOUPDATER": "1"})

    def __init__(
        self,
        model: str,
        *,
        effort: str | None = None,
        timeout_s: float = 600.0,
        binary: str = "claude",
        **options: Unpack[CliOptions],
    ) -> None:
        super().__init__(model, effort=effort, timeout_s=timeout_s, binary=binary, **options)

    def _build(
        self, request: ModelRequest, effort: str | None, io_dir: Path, work_dir: Path
    ) -> Invocation:
        system, body, notes = system_override(request.system, render_transcript(request.messages))
        argv = [
            self.binary,
            "-p",
            "--model",
            self.model,
            "--system-prompt",
            system,
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
        ]
        if request.output_schema is not None:
            argv += ["--json-schema", json.dumps(request.output_schema)]
        if effort is not None:
            argv += ["--effort", effort]
        return Invocation(argv=tuple(argv), stdin=body, raw=notes)

    def _parse(self, result: ProcessResult, request: ModelRequest, io_dir: Path) -> ModelResponse:
        payload = parse_json_payload(result.stdout)
        if payload is None:
            return self._fail("no JSON result on stdout", result)

        usage_in = as_dict(payload.get("usage"))
        usage = Usage(
            input_tokens=as_int(usage_in.get("input_tokens")),
            output_tokens=as_int(usage_in.get("output_tokens")),
            cache_read_tokens=as_int(usage_in.get("cache_read_input_tokens")),
            cache_write_tokens=as_int(usage_in.get("cache_creation_input_tokens")),
            reasoning_tokens=as_int(
                as_dict(usage_in.get("output_tokens_details")).get("thinking_tokens")
            ),
        )
        subtype = payload.get("subtype")
        stop_reason = payload.get("stop_reason")
        response = ModelResponse(
            usage=usage,
            stop_reason=stop_reason if isinstance(stop_reason, str) else None,
            served_model=served_models(payload.get("modelUsage")),
            reported_cost_usd=as_float(payload.get("total_cost_usd")),
            raw={key: payload[key] for key in _RAW_KEYS if key in payload},
        )
        if payload.get("is_error") or (subtype is not None and subtype != "success"):
            message = payload.get("result") or payload.get("error") or "the CLI reported an error"
            response.error = self._error_text(f"{subtype or 'error'}: {message}", result)
            return response
        if response.stop_reason == "refusal":
            response.refused = True

        structured = payload.get("structured_output")
        response.parsed = structured if isinstance(structured, dict) else None
        response.text, cli_text = structured_text(payload.get("result"), response.parsed)
        if cli_text is not None and response.raw is not None:
            response.raw["cli_text"] = cli_text
        if result.returncode != 0:
            # A success payload from a process that then failed is not trusted (as for grok and
            # Cursor); the parsed output stays on the response for debugging.
            response.error = self._error_text("nonzero exit after a success result", result)
        return response
