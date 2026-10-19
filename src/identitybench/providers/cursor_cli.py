"""Cursor Agent CLI (``cursor-agent -p``) as a provider, using the CLI's own login.

Runs read-only (``--mode ask``) in an empty temporary directory, which it is told to trust so it
doesn't stop to ask. The CLI has no system-prompt override and no structured output, so the
system text is prepended to the prompt and the runner extracts the JSON object from the reply.
The prompt has to be the final argv element (the CLI doesn't read it from stdin), so prompts over
the argv limit fail with an error instead of being truncated.

The child environment is the common allow-list (``_cli.COMMON_ENV_KEEP``) plus
``CURSOR_CONFIG_DIR``, which locates the login. Every other ``CURSOR_*`` variable is dropped by
prefix, and ``CURSOR_API_ENDPOINT`` and ``CURSOR_API_KEY`` also by name, so the CLI uses its own
login and endpoint rather than a gateway configured for something else.

The CLI keeps per-workspace state outside the workspace: a project directory under
``$CURSOR_DATA_DIR/projects`` (trust marker, worker log and socket, and a transcript holding the
full prompt) and a chat database under ``<config dir>/chats/<md5 of the workspace path>``. Left
alone, every call would add both to the user's ``~/.cursor``, with a copy of a test-split case
in each. So ``CURSOR_DATA_DIR`` points into the call's temporary directory, and the call's own
chat entry is deleted afterwards (the config directory also holds the login, so it can't move).

**Not a leaderboard provider.** The free plan only allows ``--model auto``, which routes each
request to a model Cursor chooses and doesn't report, so results can't be attributed to a model
(``served_model`` is always None). Use it for smoke tests and harness checks only.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Unpack

from identitybench.providers._cli import (
    CliOptions,
    CliProvider,
    Invocation,
    Isolation,
    as_dict,
    as_int,
    parse_json_payload,
    prepend_system,
    render_transcript,
)
from identitybench.providers._subprocess import ProcessResult
from identitybench.providers.base import (
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    Usage,
)

logger = logging.getLogger("identitybench.providers")

_RAW_KEYS = ("subtype", "duration_ms", "session_id", "request_id")
DATA_DIR_NAME = "cursor-data"


def resolve_cursor_binary() -> str:
    """Locate the Cursor Agent CLI: ``cursor-agent`` on PATH, then ``agent`` on PATH, then the
    installer's ``~/.local/bin`` links.

    ``agent`` is a generic name (the Grok CLI ships one too), so it only counts when it resolves
    to a Cursor install. Falls back to ``cursor-agent`` so a missing CLI fails with a clear
    "could not start" error at call time.
    """
    found = shutil.which("cursor-agent")
    if found:
        return found
    found = shutil.which("agent")
    if found and _is_cursor_install(found):
        return found
    local_bin = Path.home() / ".local" / "bin"
    for name in ("cursor-agent", "agent"):
        candidate = local_bin / name
        if candidate.exists() and _is_cursor_install(str(candidate)):
            return str(candidate)
    return "cursor-agent"


def _is_cursor_install(path: str) -> bool:
    """The installer links ``agent`` to ``.../cursor-agent/versions/<v>/cursor-agent``."""
    return "cursor-agent" in Path(os.path.realpath(path)).parts


def cursor_config_dir(env: Mapping[str, str]) -> Path:
    """The CLI's configuration directory for a process with environment ``env``, in the CLI's own
    lookup order: ``CURSOR_CONFIG_DIR``, then ``$XDG_CONFIG_HOME/cursor``, then ``~/.cursor``."""
    configured = env.get("CURSOR_CONFIG_DIR", "").strip()
    if configured:
        return Path(configured)
    xdg = env.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "cursor"
    home = env.get("HOME", "").strip()
    return (Path(home) if home else Path.home()) / ".cursor"


def chat_dir(env: Mapping[str, str], work_dir: Path) -> Path:
    """Where the CLI stores the chat history of a session run in ``work_dir``."""
    digest = hashlib.md5(str(work_dir).encode(), usedforsecurity=False).hexdigest()
    return cursor_config_dir(env) / "chats" / digest


class CursorCliProvider(CliProvider):
    KIND = "cursor_cli"
    CAPABILITIES = ProviderCapabilities(
        native_structured_output=False, native_tools=False, supports_temperature=False
    )
    SUPPORTS_EFFORT = False
    ENV_DROP = frozenset({"CURSOR_API_ENDPOINT", "CURSOR_API_KEY"})
    ENV_DROP_PREFIXES = ("CURSOR_",)
    ENV_KEEP = frozenset({"CURSOR_CONFIG_DIR"})

    def __init__(
        self,
        model: str = "auto",
        *,
        timeout_s: float = 600.0,
        binary: str | None = None,
        **options: Unpack[CliOptions],
    ) -> None:
        super().__init__(
            model,
            effort=None,
            timeout_s=timeout_s,
            binary=binary or resolve_cursor_binary(),
            **options,
        )

    def _isolate(self, home: Path) -> Isolation:
        data_dir = home / DATA_DIR_NAME
        data_dir.mkdir()
        return Isolation(env={"CURSOR_DATA_DIR": str(data_dir)})

    def _after_call(self, work_dir: Path, env: Mapping[str, str]) -> None:
        # The directory name is the hash of this call's unique temporary path, so it holds only
        # this call's chat.
        chats = chat_dir(env, work_dir)
        if not chats.is_dir():
            return
        try:
            shutil.rmtree(chats)
        except OSError as exc:
            logger.warning("could not remove the Cursor chat history at %s: %s", chats, exc)

    def _build(
        self, request: ModelRequest, effort: str | None, io_dir: Path, work_dir: Path
    ) -> Invocation:
        prompt = prepend_system(request.system, render_transcript(request.messages))
        if prompt.startswith("-"):
            # A leading dash would be parsed as an option; a newline is harmless to the model.
            prompt = "\n" + prompt
        argv = (
            self.binary,
            "-p",
            "--trust",
            "--mode",
            "ask",
            "--output-format",
            "json",
            "--model",
            self.model,
            prompt,
        )
        return Invocation(argv=argv)

    def _parse(self, result: ProcessResult, request: ModelRequest, io_dir: Path) -> ModelResponse:
        payload = parse_json_payload(result.stdout)
        if payload is None:
            return self._fail("no JSON result on stdout", result)

        usage_in = as_dict(payload.get("usage"))
        text = payload.get("result")
        response = ModelResponse(
            text=text if isinstance(text, str) else None,
            stop_reason=payload.get("subtype") if isinstance(payload.get("subtype"), str) else None,
            usage=Usage(
                input_tokens=as_int(usage_in.get("inputTokens")),
                output_tokens=as_int(usage_in.get("outputTokens")),
                cache_read_tokens=as_int(usage_in.get("cacheReadTokens")),
                cache_write_tokens=as_int(usage_in.get("cacheWriteTokens")),
            ),
            raw={key: payload[key] for key in _RAW_KEYS if key in payload} or None,
        )
        if payload.get("is_error") or result.returncode != 0:
            message = text if isinstance(text, str) and text else "the CLI reported an error"
            response.error = self._error_text(str(message), result)
        return response
