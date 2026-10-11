"""``grok`` (Grok Build CLI, headless single-turn) as a provider, using the CLI's own login.

Grok loads a lot of the user's own setup whatever the cwd: global rules in ``~/.grok``, skills
(also from ``~/.claude/skills``), MCP servers (also from ``~/.claude.json``), hooks, memory and
plugins. Flags don't turn those off, so each call gets a private home (``HOME`` and
``GROK_HOME`` in the call's temporary directory) holding only a link to the real ``auth.json``
and a ``config.toml`` that also ignores Grok's bundled skills. Measured with grok 1.0.46, that
takes a ~60-token request from ~16K input tokens (personal rules, 35 skills, 4 MCP servers, 25
agent tools) to ~1.9K, the CLI's fixed scaffolding.

Tools: an empty ``--tools ""`` allowlist means "no restriction" to the CLI, and it injects
``search_tool``/``use_tool`` even under an allowlist. So the call allowlists one tool and then
removes it and the injected ones with ``--disallowed-tools``, which leaves zero tools. Web
search, subagents, plan mode and memory are switched off as well.

The prompt goes in through ``--prompt-file`` and is sent verbatim; the system prompt is replaced
with ``--system-prompt-override`` (an empty one by ``NEUTRAL_SYSTEM``). Structured output uses
``--json-schema``; the CLI returns the object as ``structuredOutput``. ``--max-turns 2`` leaves
room for the structured-output turn (there are no tools for a turn to use).

Values go in attached form (``--system-prompt-override=<text>``): grok's argument parser (clap)
reads a separate value that starts with ``-`` as an unknown flag and exits with status 2, so a
system prompt opening with a Markdown bullet (``- ``), a rule or front matter (``---``) would
fail every call. ``--no-auto-update`` keeps the CLI from updating itself during a run, after
``endpoint_identity`` recorded the version: grok 1.0.46's headless agent code includes its update
checker, and nothing else in the call turns it off. The flag is session-scoped and writes no
config.

Every ``GROK_*`` variable of the parent environment is removed from the child's. Grok 1.0.46
reads well over a hundred of them, among them ones that route requests elsewhere
(``GROK_XAI_API_BASE_URL``, ``GROK_WS_URL``, ``GROK_MODELS_BASE_URL``), point it back at the
user's real configuration (``GROK_CONFIG``, ``GROK_CONFIG_PATH``, ``GROK_MANAGED_CONFIG_URL``),
swap its auth (``GROK_AUTH_PROVIDER_*``), turn on Claude and Cursor setup loading
(``GROK_CLAUDE_*_ENABLED``) or re-enable auto-update (``GROK_AUTO_UPDATE``); a hand-picked list
would always lag the binary. The variables the call needs (``GROK_HOME`` and the feature
switches) are set afresh. ``XAI_API_KEY`` is removed too, so the call stays on the subscription
login like the other CLIs.

Grok's usage block is Anthropic-shaped: ``input_tokens`` excludes cache reads and
``output_tokens`` includes ``reasoning_tokens`` (``total_tokens`` = input + cache read + output).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Unpack

from identitybench.providers._cli import (
    CliOptions,
    CliProvider,
    Invocation,
    Isolation,
    as_dict,
    as_float,
    as_int,
    link_credentials,
    parse_json_payload,
    render_transcript,
    served_models,
    structured_text,
    system_override,
)
from identitybench.providers._subprocess import ProcessResult
from identitybench.providers.base import ModelRequest, ModelResponse, Usage

PROMPT_FILE = "prompt.md"
SYSTEM_FLAG = "--system-prompt-override="
_RAW_KEYS = ("num_turns", "session_id", "duration_ms")

ALLOWLISTED_TOOL = "read_file"
"""The one tool the allowlist names; ``--disallowed-tools`` then removes it (see module doc)."""
DISALLOWED_TOOLS = (ALLOWLISTED_TOOL, "search_tool", "use_tool")
"""Removed after the allowlist: the allowlisted tool plus the two the CLI always injects."""

ISOLATION_ENV = {"GROK_MEMORY": "0", "GROK_SUBAGENTS": "0", "GROK_WEB_FETCH": "0"}
"""Feature switches pinned off for the call, in case remote settings would enable them."""


def real_grok_home() -> Path:
    """The user's Grok config directory (``GROK_HOME``, default ``~/.grok``)."""
    configured = os.environ.get("GROK_HOME")
    return Path(configured) if configured else Path.home() / ".grok"


class GrokCliProvider(CliProvider):
    KIND = "grok_cli"
    ENV_DROP = frozenset({"XAI_API_KEY"})
    ENV_DROP_PREFIXES = ("GROK_",)

    def __init__(
        self,
        model: str,
        *,
        effort: str | None = None,
        timeout_s: float = 600.0,
        binary: str = "grok",
        **options: Unpack[CliOptions],
    ) -> None:
        super().__init__(model, effort=effort, timeout_s=timeout_s, binary=binary, **options)

    def _isolate(self, home: Path) -> Isolation:
        private = home / ".grok"
        links = link_credentials(
            real_grok_home(), private, ("auth.json",), shared=("auth.json.lock",)
        )
        # Grok copies its bundled skills into GROK_HOME on start-up and lists them in the prompt;
        # they are the same on every machine but are agent scaffolding, not part of the task.
        bundled_skills = json.dumps(str(private / "bundled" / "skills"))
        (private / "config.toml").write_text(
            f"[skills]\nignore = [{bundled_skills}]\n", encoding="utf-8"
        )
        env = {"HOME": str(home), "GROK_HOME": str(private), **ISOLATION_ENV}
        return Isolation(env=env, links=links)

    def _build(
        self, request: ModelRequest, effort: str | None, io_dir: Path, work_dir: Path
    ) -> Invocation:
        system, body, notes = system_override(
            request.system, render_transcript(request.messages), prefix=SYSTEM_FLAG
        )
        prompt_path = io_dir / PROMPT_FILE
        prompt_path.write_text(body, encoding="utf-8")
        argv = [
            self.binary,
            "-m",
            self.model,
            "--prompt-file",
            str(prompt_path),
            f"{SYSTEM_FLAG}{system}",
            "--tools",
            ALLOWLISTED_TOOL,
            "--disallowed-tools",
            ",".join(DISALLOWED_TOOLS),
            "--disable-web-search",
            "--no-subagents",
            "--no-plan",
            "--no-auto-update",
            "--max-turns",
            "2",
            "--verbatim",
            "--output-format",
            "json",
        ]
        if request.output_schema is not None:
            argv.append(f"--json-schema={json.dumps(request.output_schema)}")
        if effort is not None:
            argv.append(f"--reasoning-effort={effort}")
        return Invocation(argv=tuple(argv), raw=notes)

    def _parse(self, result: ProcessResult, request: ModelRequest, io_dir: Path) -> ModelResponse:
        payload = parse_json_payload(result.stdout)
        if payload is None:
            return self._fail("no JSON result on stdout", result)
        if payload.get("type") == "error" or payload.get("is_error"):
            message = payload.get("message") or payload.get("error") or "the CLI reported an error"
            return self._fail(str(message), result)

        usage_in = as_dict(payload.get("usage"))
        stop_reason = payload.get("stopReason")
        structured = payload.get("structuredOutput")
        parsed = structured if isinstance(structured, dict) else None
        text, cli_text = structured_text(payload.get("text"), parsed)
        raw = {key: payload[key] for key in _RAW_KEYS if key in payload}
        if cli_text is not None:
            raw["cli_text"] = cli_text
        response = ModelResponse(
            text=text,
            parsed=parsed,
            stop_reason=stop_reason if isinstance(stop_reason, str) else None,
            refused=stop_reason == "refusal",
            usage=Usage(
                input_tokens=as_int(usage_in.get("input_tokens")),
                output_tokens=as_int(usage_in.get("output_tokens")),
                cache_read_tokens=as_int(usage_in.get("cache_read_input_tokens")),
                cache_write_tokens=as_int(usage_in.get("cache_creation_input_tokens")),
                reasoning_tokens=as_int(usage_in.get("reasoning_tokens")),
            ),
            served_model=served_models(payload.get("modelUsage")),
            reported_cost_usd=as_float(payload.get("total_cost_usd")),
            raw=raw or None,
        )
        if result.returncode != 0:
            response.error = self._error_text("nonzero exit", result)
        return response
