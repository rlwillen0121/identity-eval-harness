"""``agy -p`` (Google Antigravity CLI, headless) as a provider, using the CLI's own Google login.

Out of the box ``agy`` is a full coding agent: its built-in agent sends a ~12K-token system
prompt (identity, artifacts, subagents, slash commands, skills, the workspace paths and the
user's rules) and some forty tools (shell, file edits, browser, web search, subagents). It has no
flag that replaces the system prompt or turns the tools off. What it does have is custom agents:
an ``agent.md`` file whose YAML front matter picks the tools and whose Markdown body is the
system prompt. So each call defines one (``identitybench``) and runs it with ``--agent``:

- ``excludeDefaultComponents: true`` drops the built-in prompt sections and tools, and
  ``inheritCustomizations: false`` / ``inheritMcp: false`` the user's skills, rules, plugins and
  MCP servers.
- ``tools`` lists only ``finish``, the tool through which the CLI returns ``--json-schema``
  output, and only when the request has a schema. ``manage_task`` (list or cancel background
  tasks; there are none) stays whatever the front matter says: agy 1.3.5 keeps it for every
  custom agent.
- The body is the request's system prompt (``NEUTRAL_SYSTEM`` for an empty one). The CLI sends it
  as the system instruction wrapped in ``<System Prompt>...</System Prompt>``. A Markdown H1
  heading starts a new section, which the CLI wraps in a tag named after the heading instead:
  ``# Identity governance policy`` becomes ``<Identity governance policy>...</Identity governance
  policy>``. Every other line, blank lines included, reaches the model unchanged.

Measured with agy 1.3.5 and Gemini 4 Argon, a tiny request drops from ~11.9K input tokens to
~0.6K; a structured one with the ``finish`` tool costs ~0.8K. What remains is fixed scaffolding:
the prompt wrapped in ``<USER_REQUEST>`` tags, the local time, a note that the model selection
changed, and the two tool declarations.

The agent definition lives in a private home (``HOME`` in the call's temporary directory), which
also keeps the user's own ``~/.gemini`` setup (settings, hooks, permission grants, rules, skills,
MCP servers) out, and keeps the CLI's per-call state (conversation database, transcripts, logs)
out of the user's home: every call would otherwise leave a copy of a test-split case there. The
login is an OAuth token in the OS keyring, which agy looks up through the user's keychain: on
macOS the keychain search list is found through ``HOME``, so the private home links
``Library/Keychains`` to the real one (no file is copied; a refreshed token is written straight
to the keychain).

The prompt goes in on stdin as one ``--input-format stream-json`` message, because the CLI reads
``-p`` text only from argv and an access review case can exceed the argv limit; the output is
then the ``stream-json`` event stream, whose final ``result`` event carries the answer
(``structured_output`` under ``--json-schema``; the CLI validates the ``finish`` call against the
schema and makes the model retry an invalid one), the usage summed over every model turn, and
the status. ``--disable-slash-commands`` keeps a prompt starting with ``/`` from running a
command. Effort goes in as ``--effort``: the CLI resolves a model alias such as
``gemini-4-argon`` plus an effort to one of its listed models (``gemini-4-argon-medium``). The
resolved model is read from the CLI's log (``--log-file`` in the call's directory) as the served
model; the log is never stored, since it also names the account.

The child environment is the common allow-list (``_cli.COMMON_ENV_KEEP``) and nothing of the
CLI's own: every ``GEMINI_*``, ``AGY_*``, ``ANTIGRAVITY_*``, ``JETSKI_*`` and ``GOOGLE_*``
variable is dropped by prefix, and the Google API and Cloud credentials and endpoints also by
name: with ``GEMINI_API_KEY`` (and ``GOOGLE_GEMINI_BASE_URL``) the CLI can run against the
Gemini API or a proxy instead of the subscription login, and ``AGY_LLM_GATEWAY_*`` routes it
through a gateway.
``AGY_CLI_DISABLE_AUTO_UPDATE=true`` is set so the CLI can't update itself during a run, after
``endpoint_identity`` recorded the version.

agy reports no cost. Its usage block counts ``input_tokens`` including cache reads (``total_tokens``
is input + output) and ``output_tokens`` including ``thinking_tokens``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Unpack

from identitybench.providers._cli import (
    ACTION_SCHEMA_NAME,
    NEUTRAL_SYSTEM,
    CliOptions,
    CliProvider,
    Invocation,
    Isolation,
    as_dict,
    as_int,
    parse_jsonl,
    render_transcript,
    structured_text,
)
from identitybench.providers._subprocess import ProcessResult
from identitybench.providers.base import ModelRequest, ModelResponse, Usage

AGENT_NAME = "identitybench"
AGENT_FILE = f".gemini/config/agents/{AGENT_NAME}/agent.md"
"""Where the call's agent definition goes, relative to the private home."""
SCHEMA_FILE = "output_schema.json"
LOG_FILE = "agy.log"
STRUCTURED_OUTPUT_TOOL = "finish"
KEYCHAINS = Path("Library") / "Keychains"
_RAW_KEYS = ("status", "num_turns", "duration_seconds", "conversation_id")
_RESOLVED_RE = re.compile(r'model alias "[^"]+" resolved to "([^"]+)"|Resolving model (\S+)')


def agent_definition(system: str, *, structured: bool) -> str:
    """The ``agent.md`` for one call: front matter that strips the agent down, then ``system``."""
    tools = f"[{STRUCTURED_OUTPUT_TOOL}]" if structured else "[]"
    front = "\n".join(
        (
            "---",
            f"name: {AGENT_NAME}",
            "description: A single model call with no tools.",
            "mainAgent: true",
            "subagent: false",
            "excludeDefaultComponents: true",
            "inheritCustomizations: false",
            "inheritMcp: false",
            f"tools: {tools}",
            "---",
        )
    )
    return f"{front}\n\n{system}"


def served_model(log_text: str) -> str | None:
    """The model the CLI resolved the request's model (and effort) to, from its log."""
    found: str | None = None
    for match in _RESOLVED_RE.finditer(log_text):
        found = match.group(1) or match.group(2)
    return found


class AgyCliProvider(CliProvider):
    KIND = "agy_cli"
    ENV_DROP = frozenset(
        {
            "GOOGLE_API_KEY",
            "GOOGLE_GEMINI_BASE_URL",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_QUOTA_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_CLOUD_REGION",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_GENAI_USE_ENTERPRISE",
            "CLOUD_CODE_URL",
        }
    )
    ENV_DROP_PREFIXES = ("GEMINI_", "AGY_", "ANTIGRAVITY_", "JETSKI_", "GOOGLE_")
    ENV_SET = MappingProxyType({"AGY_CLI_DISABLE_AUTO_UPDATE": "true"})

    def __init__(
        self,
        model: str,
        *,
        effort: str | None = None,
        timeout_s: float = 900.0,
        binary: str = "agy",
        **options: Unpack[CliOptions],
    ) -> None:
        super().__init__(model, effort=effort, timeout_s=timeout_s, binary=binary, **options)

    def _isolate(self, home: Path) -> Isolation:
        real_keychains = Path.home() / KEYCHAINS
        if real_keychains.is_dir():
            (home / KEYCHAINS).parent.mkdir(parents=True, exist_ok=True)
            (home / KEYCHAINS).symlink_to(real_keychains, target_is_directory=True)
        return Isolation(env={"HOME": str(home)})

    def _build(
        self, request: ModelRequest, effort: str | None, io_dir: Path, work_dir: Path
    ) -> Invocation:
        notes: dict[str, Any] = {}
        system = request.system
        if not system.strip():
            system = NEUTRAL_SYSTEM
            notes["substituted_system"] = NEUTRAL_SYSTEM
        structured = request.output_schema is not None and request.schema_name != ACTION_SCHEMA_NAME
        if request.output_schema is not None and not structured:
            notes["json_schema"] = "withheld (JSON action protocol turn)"
        message = {
            "event": "user",
            "message": {"role": "user", "content": render_transcript(request.messages)},
        }
        argv = [
            self.binary,
            "--model",
            self.model,
            "--agent",
            AGENT_NAME,
            "--disable-slash-commands",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--log-file",
            str(io_dir / LOG_FILE),
        ]
        if structured:
            schema_path = io_dir / SCHEMA_FILE
            schema_path.write_text(json.dumps(request.output_schema), encoding="utf-8")
            argv += ["--json-schema", str(schema_path)]
        if effort is not None:
            argv += ["--effort", effort]
        # The prompt comes from stdin; -p needs a value, and an empty one reads nothing.
        argv += ["-p", ""]
        return Invocation(
            argv=tuple(argv),
            stdin=json.dumps(message, ensure_ascii=False) + "\n",
            raw=notes,
            home_files={AGENT_FILE: agent_definition(system, structured=structured)},
        )

    def _stale_outputs(self, io_dir: Path) -> tuple[Path, ...]:
        return (io_dir / LOG_FILE,)

    def _parse(self, result: ProcessResult, request: ModelRequest, io_dir: Path) -> ModelResponse:
        results = [
            as_dict(event.get("result"))
            for event in parse_jsonl(result.stdout)
            if event.get("event") == "result"
        ]
        if not results:
            return self._fail("no result event on stdout", result)
        payload = results[-1]

        usage_in = as_dict(payload.get("usage"))
        cache_read = as_int(usage_in.get("cache_read_tokens"))
        structured = payload.get("structured_output")
        parsed = structured if isinstance(structured, dict) else None
        text, cli_text = structured_text(payload.get("response"), parsed)
        raw = {key: payload[key] for key in _RAW_KEYS if key in payload}
        if cli_text is not None:
            raw["cli_text"] = cli_text
        log_path = io_dir / LOG_FILE
        log_text = (
            log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        )
        response = ModelResponse(
            text=text,
            parsed=parsed,
            usage=Usage(
                input_tokens=max(as_int(usage_in.get("input_tokens")) - cache_read, 0),
                output_tokens=as_int(usage_in.get("output_tokens")),
                cache_read_tokens=cache_read,
                reasoning_tokens=as_int(usage_in.get("thinking_tokens")),
            ),
            served_model=served_model(log_text),
            raw=raw or None,
        )
        status = payload.get("status")
        if status != "SUCCESS":
            message = payload.get("error") or f"status {status!r}"
            response.error = self._error_text(str(message), result)
        elif result.returncode != 0:
            response.error = self._error_text("nonzero exit after a success result", result)
        return response
