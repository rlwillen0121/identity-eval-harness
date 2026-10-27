"""Shared machinery for the subscription-CLI providers (``claude``, ``codex``, ``grok``, ``agy``,
Cursor).

Each CLI call runs in a fresh, empty temporary working directory so the agent has nothing to
read, no project instructions to pick up and nowhere useful to write. Prompt, schema and output
files live in a sibling directory that is never the agent's cwd.

The child environment is built from an allow-list, not by subtraction (``CliProvider.child_env``):
a small common base (``COMMON_ENV_KEEP``: PATH, HOME, the temp and locale variables, the user's
name and shell, the XDG config directories) plus the few names a CLI needs for its own login or
configuration (``ENV_KEEP``). Nothing else in the harness's environment reaches a CLI: not the
API keys and endpoint URLs loaded from ``.env`` for the API providers, not another CLI's login,
not the parent Claude Code session. Deny-lists (``COMMON_ENV_DROP`` and its prefixes, each
provider's ``ENV_DROP`` and ``ENV_DROP_PREFIXES``) stay on top as defense in depth: an exact
name on a deny-list is dropped even if a later ``ENV_KEEP`` edit names it, so a key that would
switch a CLI off its subscription login can't be reopened by accident. ``ENV_SET`` and
``Isolation.env`` are applied last.

Shutdown: every live CLI process group and every live per-call directory is registered in a
module-level registry; ``install_shutdown_handlers`` kills and removes them on SIGTERM/SIGHUP
(then lets the signal terminate the process) and at interpreter exit, and
``sweep_stale_call_dirs`` removes at start-up what a SIGKILLed harness couldn't.

A CLI that loads the user's own setup from its home directory whatever the cwd (global rules,
skills, MCP servers, hooks, memory) also gets a private, empty home for the call
(``CliProvider._isolate``): ``HOME`` and the CLI's config-dir variable point into the call's
temporary directory, which holds only links to the real login files. Without that, every row
would score a prompt that silently differs between machines.

The CLIs can't take a multi-turn transcript or native tools, so transcripts are rendered into a
single user message and requests with tools are rejected (``capabilities().native_tools`` is
False for every CLI provider).

A CLI's endpoint identity (SPEC §11.4) covers more than its version and the model: the harness's
own setup (fixed flags, disabled features, the private home's contents, environment rules) and
the provider's default effort change the prompt or the model's behaviour too, so they are hashed
in as well (``CliProvider.setup_revision``). Changing that setup invalidates cached responses
instead of serving them as if the new setup had produced them.
"""

from __future__ import annotations

import atexit
import contextlib
import functools
import hashlib
import json
import logging
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, TypedDict

from identitybench.providers._json import load_object
from identitybench.providers._subprocess import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRY_BASE_DELAY_S,
    Command,
    ProcessResult,
    ProcessRunner,
    is_transient_error,
    kill_all_live,
    redact,
    retry_transient,
    run_command,
    tail,
)
from identitybench.providers.base import (
    Message,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    Role,
    ToolCall,
    hash_endpoint,
)

logger = logging.getLogger("identitybench.providers")

SYSTEM_BEGIN = "===== SYSTEM INSTRUCTIONS ====="
SYSTEM_END = "===== END SYSTEM INSTRUCTIONS ====="

MAX_ARG_BYTES = 100_000
"""Largest single argv element we pass. Linux caps one argument at 128 KiB (MAX_ARG_STRLEN) and
macOS caps argv+env at 1 MiB in total, so anything bigger moves into stdin or a file."""

SYSTEM_POINTER = (
    "Your instructions are at the start of the user message, between the lines "
    f"{SYSTEM_BEGIN!r} and {SYSTEM_END!r}. Treat them as your system prompt."
)
"""Short system prompt used when the real one is too large for argv and moves into the prompt."""

NEUTRAL_SYSTEM = "You are a helpful assistant."
"""System prompt sent in place of an empty one by CLIs that replace their own system prompt.

An empty value doesn't mean "no system prompt" to these CLIs: grok treats
``--system-prompt-override ""`` as unset and falls back to its built-in coding-agent prompt
(measured on grok 1.0.46: ~2.8K input tokens with an empty override against ~1.8K with a short
one), and claude isn't trusted to do better with ``--system-prompt ""``. The API providers send
no system prompt at all, so the CLI rows get the closest equivalent instead: the default that
chat templates conventionally apply to a conversation without a system message, which carries no
task instructions. Responses record the substitution in ``raw["substituted_system"]``.
"""

ACTION_SCHEMA_NAME = "tool_action"
"""The schema name of a turn of the JSON action protocol (``render.tools.ACTION_SCHEMA_NAME``;
a test pins the two together, since providers don't import the renderer). A CLI may send such
a turn differently from a plain structured answer (``claude_cli`` withholds ``--json-schema``),
so ``setup_revision`` builds one as a template too."""

_EFFORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")


def prepend_system(system: str, body: str) -> str:
    """Put the system text at the top of the prompt between unambiguous delimiter lines."""
    if not system:
        return body
    return f"{SYSTEM_BEGIN}\n{system}\n{SYSTEM_END}\n\n{body}"


def system_override(system: str, body: str, *, prefix: str = "") -> tuple[str, str, dict[str, Any]]:
    """``(system argument, prompt body, raw notes)`` for a CLI that replaces its system prompt.

    - An empty or whitespace-only ``system`` becomes ``NEUTRAL_SYSTEM`` (noted in the raw notes),
      so the CLI never falls back to its own agent prompt.
    - A ``system`` too large for one argv element (``prefix`` is whatever shares the element with
      it, such as an attached ``--flag=``) moves to the top of the body between delimiter lines,
      and the argument becomes ``SYSTEM_POINTER``.
    """
    if not system.strip():
        return NEUTRAL_SYSTEM, body, {"substituted_system": NEUTRAL_SYSTEM}
    if exceeds_arg_limit(prefix + system):
        return SYSTEM_POINTER, prepend_system(system, body), {}
    return system, body, {}


def render_transcript(messages: Sequence[Message]) -> str:
    """Render a provider-neutral transcript as one user message.

    A single user message (context mode, the common case) is passed through verbatim so CLI and
    API providers see byte-identical prompts.
    """
    if len(messages) == 1 and messages[0].role is Role.user:
        return messages[0].content
    parts: list[str] = []
    for message in messages:
        if message.role is Role.user:
            parts.append(f"[User]\n{message.content}")
        elif message.role is Role.assistant:
            lines = [message.content] if message.content else []
            lines.extend(
                f"(tool call {call.name} id={call.id}: "
                f"{json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)})"
                for call in message.tool_calls
            )
            parts.append("[Assistant]\n" + "\n".join(lines))
        else:
            parts.append(f"[Tool result {message.tool_call_id}]\n{message.content}")
    return "\n\n".join(parts)


def exceeds_arg_limit(text: str) -> bool:
    return len(text.encode()) > MAX_ARG_BYTES


def valid_effort(effort: str) -> bool:
    """Effort values are interpolated into argv (and, for codex, a TOML string), so they must be
    plain identifiers like ``low`` or ``xhigh``."""
    return _EFFORT_RE.match(effort) is not None


def parse_json_payload(stdout: str) -> dict[str, Any] | None:
    """The CLI's JSON result object: the whole stdout, or else one of its lines that is one.

    Some CLIs print warnings or progress lines (JSON ones included) before the result. When
    several lines are objects, the last whose ``type`` is ``"result"`` wins, so a progress or
    event object printed after the result can't be mistaken for it; without one, the last object
    is the result.
    """
    text = stdout.strip()
    if not text:
        return None
    whole = load_object(text)
    if whole is not None:
        return whole
    candidates: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("{") and (value := load_object(line)) is not None:
            candidates.append(value)
    if not candidates:
        return None
    results = [value for value in candidates if value.get("type") == "result"]
    return (results or candidates)[-1]


def parse_jsonl(stdout: str) -> list[dict[str, Any]]:
    """Every JSON-object line of a JSONL event stream; other lines are ignored."""
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and (value := load_object(line)) is not None:
            events.append(value)
    return events


def as_int(value: object) -> int:
    """Token counts from CLI JSON: missing, null or malformed counts are 0.

    ``json.loads`` accepts ``NaN`` and ``Infinity``, so non-finite floats count as malformed.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float) and math.isfinite(value):
        return max(int(value), 0)
    return 0


def as_float(value: object) -> float | None:
    """A reported cost from CLI JSON; missing, malformed or non-finite values are None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def served_models(model_usage: object) -> str | None:
    """Models the CLI reports it used (``modelUsage`` keys), comma-joined when several did."""
    if not isinstance(model_usage, dict) or not model_usage:
        return None
    return ",".join(sorted(str(key) for key in model_usage))


def structured_text(
    text: object, structured: dict[str, Any] | None
) -> tuple[str | None, str | None]:
    """``(response text, CLI text to keep in raw)`` for a CLI result.

    ``ModelResponse.text`` must agree with ``parsed``: anything that reads the text later (cache
    inspection, the judge, re-extraction) has to see the answer that was scored. So the CLI's own
    text is used only when it *is* the structured output; otherwise the text is the structured
    output serialized and the CLI's text is returned separately for ``raw["cli_text"]``.
    """
    cli_text = text if isinstance(text, str) and text.strip() else None
    if structured is None:
        return (text if isinstance(text, str) else None), None
    if cli_text is not None and load_object(cli_text) == structured:
        return cli_text, None
    return json.dumps(structured, ensure_ascii=False), cli_text


EnvItems = tuple[tuple[str, str], ...]
"""An environment as a hashable, sorted tuple of items (``env_items``), so it can key a cache."""


def env_items(env: Mapping[str, str]) -> EnvItems:
    return tuple(sorted(env.items()))


@functools.cache
def probe_version(binary: str, env: EnvItems | None = None) -> str:
    """``<binary> --version`` (first line), cached per binary and environment for the life of
    the process.

    Part of a CLI provider's endpoint identity (SPEC §11.4): upgrading the CLI changes the system
    prompt and agent scaffolding around the model, so cached responses from an older CLI must not
    be reused.

    The probe runs the way a call does: in an empty temporary directory, with the provider's
    child environment (``env``; the common allow-list, ``base_child_env``, when none is given)
    rather than the harness's own, so even ``--version`` sees no secret and no project files.
    """
    environment = dict(env) if env is not None else base_child_env()
    try:
        with call_dir("identitybench-probe-") as cwd:
            proc = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                stdin=subprocess.DEVNULL,
                cwd=str(cwd),
                env=environment,
            )
    except FileNotFoundError:
        return "not-installed"
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    for stream in (proc.stdout, proc.stderr):
        for line in stream.splitlines():
            if line.strip():
                return line.strip()
    return "unknown"


# --------------------------------------------------------------------------- stderr excerpts

_CURRENT_REQUEST: ContextVar[ModelRequest | None] = ContextVar(
    "identitybench_current_request", default=None
)
"""The request a CLI call is serving, for the duration of ``CliProvider._complete`` (per task,
so concurrent calls on one provider don't see each other's)."""

STDERR_EXCERPT_CHARS = 300
"""Most characters of stderr quoted in an error message."""
_MARK_CHARS = 60
_MIN_MARK_CHARS = 16
_MAX_MARKS = 400
"""Most prompt lines whose opening is used as a mark; the window check covers the rest."""
_WINDOW_CHARS = 24
"""Length of the prompt fragments a stderr line may not share with the prompt."""
_ESCAPED_WHITESPACE = (("\\r\\n", "\n"), ("\\n", "\n"), ("\\r", "\n"), ("\\t", " "))
"""How a quoted argv echo spells the prompt's line breaks and tabs (``'...\\n...'``)."""


def _normalize(text: str) -> str:
    """``text`` with every run of whitespace (line breaks included) collapsed to one space, so a
    re-wrapped or re-indented echo compares equal to the prompt."""
    return " ".join(text.split())


def _unescape(line: str) -> str:
    """A stderr line with ``\\n``-style escapes turned back into the whitespace they stand for."""
    for escaped, plain in _ESCAPED_WHITESPACE:
        line = line.replace(escaped, plain)
    return line


def _windows(text: str) -> set[str]:
    """Every ``_WINDOW_CHARS``-long substring of ``text``."""
    return {text[i : i + _WINDOW_CHARS] for i in range(len(text) - _WINDOW_CHARS + 1)}


def scrub_stderr(
    stderr: str, request: ModelRequest | None, limit: int = STDERR_EXCERPT_CHARS
) -> str:
    """The end of ``stderr`` for an error message, without the prompt and capped at ``limit``.

    A CLI's usage error can echo its arguments, prompt included, and a benchmark prompt holds a
    test-split case that must not land in a run's error log. So a stderr line is dropped when
    it echoes the prompt (system text or any message) in any of these ways, and only the last
    ``limit`` characters of what remains are kept:

    - it is a line of the prompt verbatim, or holds one once its ``\\n`` escapes are undone (an
      argv echo quotes the whole prompt on one line);
    - it contains the opening (``_MARK_CHARS``) of a prompt line, from any line of the prompt
      (the first ``_MAX_MARKS`` of them);
    - it shares any ``_WINDOW_CHARS``-long run of text with the prompt once both have their
      whitespace normalized, which catches an echo that re-wraps, re-indents or quotes a
      fragment of the prompt.

    Prompt lines shorter than ``_MIN_MARK_CHARS`` are not used as lines or marks: they would
    match ordinary error text.
    """
    if not stderr.strip():
        return ""
    if request is None:
        return tail(stderr, limit)
    texts = (request.system, *(message.content for message in request.messages))
    verbatim: set[str] = set()
    marks: set[str] = set()
    for text in texts:
        for line in (line.strip() for line in text.splitlines()):
            if len(line) < _MIN_MARK_CHARS:
                continue
            verbatim.add(line)
            if len(marks) < _MAX_MARKS:
                marks.add(line[:_MARK_CHARS])
    windows = _windows(_normalize("\n".join(texts)))

    def echoes_prompt(raw: str) -> bool:
        line = _unescape(raw)
        if any(part.strip() in verbatim for part in line.splitlines()):
            return True
        if any(mark in line for mark in marks):
            return True
        normalized = _normalize(line)
        return any(
            normalized[i : i + _WINDOW_CHARS] in windows
            for i in range(len(normalized) - _WINDOW_CHARS + 1)
        )

    kept = [line for line in stderr.splitlines() if not echoes_prompt(line)]
    return tail("\n".join(kept), limit)


# --------------------------------------------------------------------------- call directories

_LIVE_CALL_DIRS: set[str] = set()
_LIVE_CALL_DIRS_LOCK = threading.RLock()


def register_call_dir(path: str) -> None:
    with _LIVE_CALL_DIRS_LOCK:
        _LIVE_CALL_DIRS.add(path)


def unregister_call_dir(path: str) -> None:
    with _LIVE_CALL_DIRS_LOCK:
        _LIVE_CALL_DIRS.discard(path)


def live_call_dirs() -> frozenset[str]:
    """The per-call temporary directories of the calls running right now."""
    with _LIVE_CALL_DIRS_LOCK:
        return frozenset(_LIVE_CALL_DIRS)


def remove_live_call_dirs() -> int:
    """Remove every registered call directory and forget it; returns how many were removed."""
    with _LIVE_CALL_DIRS_LOCK:
        paths = sorted(_LIVE_CALL_DIRS)
        _LIVE_CALL_DIRS.clear()
    removed = 0
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.lexists(path):
            removed += 1
    return removed


OWNER_FILE = ".owner"
"""File at the top of every call directory holding the pid of the harness that created it, so
``sweep_stale_call_dirs`` in another harness can tell a call in progress from a leak."""


def _write_owner(path: str) -> None:
    try:
        Path(path, OWNER_FILE).write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:  # the directory still works; a sweep then goes by age alone
        logger.warning("could not record the owner of call directory %s: %s", path, exc)


def owner_alive(path: str) -> bool | None:
    """Whether the harness that owns the call directory at ``path`` is still running.

    True or False by the pid in its ``OWNER_FILE`` (a pid that can't be signalled for lack of
    permission counts as alive: it exists), None when the file is missing or unreadable (a
    directory from before owner files, or a marker a crash left half-written).
    """
    try:
        pid = int(Path(path, OWNER_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: another user's process, alive
        return True
    return True


@contextlib.contextmanager
def call_dir(prefix: str) -> Iterator[Path]:
    """A fresh private temporary directory (mode 0700), marked with this process's pid
    (``OWNER_FILE``), registered for emergency removal while it exists and removed afterwards.

    Yields the resolved path, so paths handed to a CLI match what it sees (macOS ``/var`` ->
    ``/private/var``). Removal errors are ignored: a CLI can leave read-only files or a
    straggling writer in the directory, and that must not turn a finished call into an
    exception; what remains is caught by the next ``sweep_stale_call_dirs``.
    """
    tmp = tempfile.mkdtemp(prefix=prefix)
    _write_owner(tmp)
    register_call_dir(tmp)
    try:
        yield Path(tmp).resolve()
    finally:
        unregister_call_dir(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


STALE_CALL_DIR_AGE_S = 2 * 60 * 60
"""Age past which a call directory whose owner is gone counts as a leak. Age alone never
decides: a call's timeout is unbounded and a call keeps its directory across every retry, so a
directory whose owner process is still alive is left alone however old it is."""
_CALL_DIR_RE = re.compile(r"^identitybench-(?:[a-z0-9]+_cli-(?:setup-)?|probe-)[A-Za-z0-9_]+$")
"""Names ``call_dir`` creates: ``identitybench-<kind>-<random>``, the setup-revision and
version-probe variants. Nothing else under the temp directory is touched."""


def sweep_stale_call_dirs(max_age_s: float = STALE_CALL_DIR_AGE_S) -> int:
    """Remove call directories older than ``max_age_s`` from the temp directory; returns the
    count (also logged at INFO).

    For what the shutdown handlers can't reach: a harness that was SIGKILLed (or lost power)
    leaves its calls' directories behind, and the next harness start removes them. Only
    directories that look exactly like ours are removed: a ``call_dir`` name, owned by the
    current user, mode 0700 (what ``mkdtemp`` creates), not a symlink, not a directory of a
    call running in this process, and not one whose ``OWNER_FILE`` names a live process (a
    call in progress in another harness, whatever its age). A directory without an owner file
    (from before owner files) goes by age alone. Safe to call from any command.
    """
    root = tempfile.gettempdir()
    cutoff = time.time() - max_age_s
    uid = os.getuid()
    live = live_call_dirs()
    removed = 0
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        logger.warning("could not scan %s for stale CLI call directories: %s", root, exc)
        return 0
    for entry in entries:
        if not _CALL_DIR_RE.match(entry.name):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_mtime > cutoff
            or entry.path in live
            or os.path.realpath(entry.path) in live
            or owner_alive(entry.path) is True
        ):
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        if not os.path.lexists(entry.path):
            removed += 1
    logger.info("removed %d stale CLI call directories under %s", removed, root)
    return removed


# --------------------------------------------------------------------------- shutdown

SHUTDOWN_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGHUP)
_atexit_registered = False
_signals_installed = False


def emergency_cleanup() -> tuple[int, int]:
    """Kill every live CLI process group and remove every live call directory; returns the
    counts. Safe to call more than once and when there is nothing to do."""
    return kill_all_live(), remove_live_call_dirs()


def _on_shutdown_signal(signum: int, frame: object) -> None:
    killed, removed = emergency_cleanup()
    if killed or removed:
        with contextlib.suppress(Exception):
            logger.warning(
                "signal %d: killed %d live CLI process group(s), removed %d call directory(ies)",
                signum,
                killed,
                removed,
            )
    # Then the signal's own effect: the default action terminates the process with it, so the
    # exit status (and anything waiting on it) sees the signal, not a normal exit.
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def install_shutdown_handlers() -> bool:
    """Make a dying harness clean up after its CLI calls. Returns True if the signal handlers
    were installed by this call.

    Registers ``emergency_cleanup`` to run at interpreter exit (once), and on the main thread
    installs it as the SIGTERM and SIGHUP handler, after which the signal terminates the process
    as it would have. A handler someone else installed is left alone. Idempotent, and a no-op
    for the signals when called off the main thread (Python allows handlers only there); a
    later call from the main thread still installs them.
    """
    global _atexit_registered, _signals_installed
    if not _atexit_registered:
        atexit.register(emergency_cleanup)
        _atexit_registered = True
    if _signals_installed or threading.current_thread() is not threading.main_thread():
        return False
    for signum in SHUTDOWN_SIGNALS:
        try:
            if signal.getsignal(signum) in (signal.SIG_DFL, None):
                signal.signal(signum, _on_shutdown_signal)
        except (ValueError, OSError):  # not the main thread after all, or an unsupported signal
            return False
    _signals_installed = True
    return True


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    stdin: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict[str, Any])
    """Facts about how the request was sent (e.g. a substituted system prompt), merged into the
    response's ``raw``."""
    home_files: Mapping[str, str] = field(default_factory=dict[str, str])
    """Files for the call's private home (path relative to it -> text), written after
    ``_isolate`` set the home up: per-request setup a CLI only reads from there (``agy_cli``'s
    agent definition). Only for providers whose ``_isolate`` sets ``HOME``."""


@dataclass(frozen=True)
class CredentialLink:
    """A symlink in a private CLI home pointing at one of the user's real login files."""

    real: Path
    link: Path
    sync_back: bool
    """Copy the file back to ``real`` if the CLI replaced the link (see ``sync_back``)."""


@dataclass(frozen=True)
class Isolation:
    """How one CLI call is cut off from the user's own CLI setup."""

    env: Mapping[str, str] = field(default_factory=dict[str, str])
    """Environment overrides (``HOME``, the CLI's config-dir variable, feature switches)."""
    links: tuple[CredentialLink, ...] = ()


HOME_RELATIVE_VARS = frozenset({"XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"})
"""Dropped whenever a call gets a private ``HOME`` (``Isolation.env`` sets it): they would point
config lookups back at the user's real directories. A CLI that keeps the real ``HOME`` keeps
them too, since they may be how it finds its own login."""


def link_credentials(
    real_dir: Path,
    private_dir: Path,
    files: Sequence[str],
    *,
    shared: Sequence[str] = (),
) -> tuple[CredentialLink, ...]:
    """Symlink the login ``files`` (and lock ``shared`` files) of ``real_dir`` into ``private_dir``.

    Only files that exist are linked; a missing login makes the CLI fail with its own "not logged
    in" error, which is the right outcome. Lock files are shared so a token refresh in the call
    still coordinates with the user's other sessions of the same CLI.
    """
    private_dir.mkdir(parents=True, exist_ok=True)
    links: list[CredentialLink] = []
    for name, sync in [*((name, True) for name in files), *((name, False) for name in shared)]:
        real = real_dir / name
        if not real.is_file():
            continue
        link = private_dir / name
        link.symlink_to(real)
        links.append(CredentialLink(real=real, link=link, sync_back=sync))
    return tuple(links)


def sync_back(links: Sequence[CredentialLink]) -> None:
    """Propagate a refreshed login from a private CLI home to the user's real one.

    A CLI that refreshes its OAuth token either writes through the symlink (the real file is
    already current) or writes a new file and renames it over the link. In the second case the
    refreshed token, and possibly a rotated refresh token, exists only in the temporary home, and
    losing it could log the user out of the CLI. So a replaced link whose content is a JSON object
    different from the real file is copied back atomically with owner-only permissions. Failures
    are logged, never raised: the model call itself already finished.
    """
    for item in links:
        if not item.sync_back or item.link.is_symlink() or not item.link.is_file():
            continue
        try:
            content = item.link.read_bytes()
            if load_object(content) is None:
                continue
            if item.real.is_file() and item.real.read_bytes() == content:
                continue
            staging = item.real.with_name(f".{item.real.name}.identitybench-{os.getpid()}")
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.replace(staging, item.real)
        except OSError as exc:
            logger.warning("could not sync a refreshed CLI login back to %s: %s", item.real, exc)


class CliOptions(TypedDict, total=False):
    """Keyword options every CLI provider accepts on top of its own."""

    runner: ProcessRunner | None
    """Subprocess runner; tests inject a fake. Defaults to ``run_command``."""
    version: str | None
    """CLI version to use in the endpoint identity instead of probing ``--version``."""
    max_retries: int
    retry_base_delay_s: float


COMMON_ENV_KEEP: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "TERM",
        "COLORTERM",
        "USER",
        "LOGNAME",
        "SHELL",
        "TZ",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
    }
)
"""The only parent variables any CLI inherits (plus ``COMMON_ENV_KEEP_PREFIXES`` and the
provider's ``ENV_KEEP``): what a process needs to find its binaries, home, temp directory,
locale and terminal. The XDG config variables are kept so a CLI without a private home still
finds its own login; ``HOME_RELATIVE_VARS`` removes them again when a call gets one."""

COMMON_ENV_KEEP_PREFIXES: tuple[str, ...] = ("LC_",)

COMMON_ENV_DROP: frozenset[str] = frozenset(
    {
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "XAI_API_KEY",
        "OPENROUTER_API_KEY",
        "LOCAL_LLM_API_KEY",
        "OPENCODE_GO_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONSTARTUP",
    }
)
"""Never passed to any CLI, whatever its ``ENV_KEEP`` says (defense in depth under the
allow-list): other providers' credentials, and variables that would load code into the child."""

COMMON_ENV_DROP_PREFIXES: tuple[str, ...] = ("NODE_", "SSL_CERT_", "GIT_", "PYTHON", "DYLD_", "LD_")
"""Likewise by prefix: Node options and CA overrides, git hooks and helpers, Python start-up
code, and dynamic-loader injection."""

COMMON_ENV_DROP_SUFFIXES: tuple[str, ...] = ("_PROXY",)
"""Likewise by suffix, case-insensitively (``HTTPS_PROXY``, ``https_proxy``, ``ALL_PROXY``,
``NO_PROXY``): a proxy variable would route a CLI's traffic through an endpoint the endpoint
identity doesn't know about."""


def always_dropped(name: str) -> bool:
    """True for a variable no provider may pass on, whatever its own rules say."""
    return (
        name in COMMON_ENV_DROP
        or name.startswith(COMMON_ENV_DROP_PREFIXES)
        or name.upper().endswith(COMMON_ENV_DROP_SUFFIXES)
    )


def base_child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The common allow-list applied to ``base`` (default ``os.environ``), no provider rules."""
    source = os.environ if base is None else base
    return {
        name: value
        for name, value in source.items()
        if (name in COMMON_ENV_KEEP or name.startswith(COMMON_ENV_KEEP_PREFIXES))
        and not always_dropped(name)
    }


class CliProvider(ABC):
    """Base for subscription-CLI providers. Subclasses build the argv and parse the output."""

    KIND: ClassVar[str]
    CAPABILITIES: ClassVar[ProviderCapabilities] = ProviderCapabilities(
        native_structured_output=True, native_tools=False, supports_temperature=False
    )
    SUPPORTS_EFFORT: ClassVar[bool] = True
    ENV_DROP: ClassVar[frozenset[str]] = frozenset()
    """Names this CLI must never see even if ``ENV_KEEP`` later names them (an exact-name drop
    beats a keep): the API keys and endpoint variables that would switch it off its login."""
    ENV_DROP_PREFIXES: ClassVar[tuple[str, ...]] = ()
    """Prefixes of this CLI's own variables (``CLAUDE_``, ``GROK_``...): dropped unless a name
    is in ``ENV_KEEP``, which is how a login or config-dir variable survives its prefix."""
    ENV_KEEP: ClassVar[frozenset[str]] = frozenset()
    """Names this CLI needs beyond ``COMMON_ENV_KEEP``: its login token or config directory."""
    ENV_SET: ClassVar[Mapping[str, str]] = MappingProxyType({})
    """Variables set for every call whatever the parent environment holds (e.g. switching the
    CLI's self-updater off, so the version in the endpoint identity stays the one that runs)."""
    MAX_ARG_BYTES: ClassVar[int] = MAX_ARG_BYTES
    """Per-argument argv byte ceiling enforced in ``_complete`` (defaults to ``MAX_ARG_BYTES``)."""

    def __init__(
        self,
        model: str,
        *,
        effort: str | None,
        timeout_s: float,
        binary: str,
        runner: ProcessRunner | None = None,
        version: str | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_delay_s: float = DEFAULT_RETRY_BASE_DELAY_S,
    ) -> None:
        self.kind: str = self.KIND
        self.model = model
        self.effort = effort
        self.timeout_s = timeout_s
        self.binary = binary
        self._runner: ProcessRunner = runner or run_command
        self._version = version
        self._max_retries = max_retries
        self._retry_base_delay_s = retry_base_delay_s
        self._setup_revision: str | None = None

    def capabilities(self) -> ProviderCapabilities:
        return self.CAPABILITIES

    def endpoint_identity(self) -> str:
        """Hash of the kind, CLI version, model, ``setup_revision()`` and the default effort.

        The default effort is the constructor's, which applies whenever a request leaves effort
        unset; a request's own effort is part of the cache key already.
        """
        if self._version is None:
            self._version = probe_version(self.binary, env_items(self.child_env()))
        return hash_endpoint(
            self.kind, self._version, self.model, self.setup_revision(), self.effort
        )

    def setup_revision(self) -> str:
        """Short hash of the call setup the harness controls, computed once per provider.

        It is derived from the setup itself rather than a hand-bumped constant, so no change can
        be forgotten: the argv, stdin, input files and private-home files ``_build`` produces for
        three template requests (an empty system prompt, no schema and no effort; a system
        prompt, a multi-turn transcript, a schema and an effort; and the same as a JSON action
        protocol turn), the environment and files ``_isolate`` sets up in a private home (login
        links excluded: whether the user is logged in is not setup), the environment rules, and
        the large-system-prompt fallback. Temporary paths, the binary's location and the model name
        are replaced by placeholders, so the revision is the same on every machine and for every
        model of the provider.

        An ``OSError`` while setting up the scratch home propagates: a machine that can't do that
        can't run a call either.
        """
        if self._setup_revision is None:
            self._setup_revision = self._compute_setup_revision()
        return self._setup_revision

    def _compute_setup_revision(self) -> str:
        transcript = (
            Message(Role.user, "<prompt>"),
            Message(
                Role.assistant,
                "<reply>",
                tool_calls=(ToolCall("<call-id>", "<tool>", {"<arg>": "<value>"}),),
            ),
            Message(Role.tool, "<result>", tool_call_id="<call-id>"),
            Message(Role.user, "<follow-up>"),
        )
        templates = (
            (ModelRequest(system="", messages=(Message(Role.user, "<prompt>"),)), None),
            (
                ModelRequest(
                    system="<system>",
                    messages=transcript,
                    output_schema={"type": "object", "properties": {}},
                ),
                "<effort>" if self.SUPPORTS_EFFORT else None,
            ),
            (
                ModelRequest(
                    system="<system>",
                    messages=transcript,
                    output_schema={"type": "object", "properties": {}},
                    schema_name=ACTION_SCHEMA_NAME,
                ),
                None,
            ),
        )
        with call_dir(f"identitybench-{self.kind}-setup-") as root:
            invocations: list[dict[str, Any]] = []
            for number, (request, effort) in enumerate(templates):
                io_dir = root / f"t{number}" / "io"
                work_dir = root / f"t{number}" / "work"
                io_dir.mkdir(parents=True)
                work_dir.mkdir()
                built = self._build(request, effort, io_dir, work_dir)
                argv = [
                    "<binary>",
                    *("<model>" if arg == self.model else arg for arg in built.argv[1:]),
                ]
                invocation: dict[str, Any] = {
                    "argv": argv,
                    "stdin": built.stdin,
                    "raw": dict(built.raw),
                    "files": _files(io_dir),
                }
                if built.home_files:
                    # Only when present, so the revisions of providers without any stay as
                    # they were (and their cached responses valid).
                    invocation["home_files"] = dict(built.home_files)
                invocations.append(invocation)
            home = root / "home"
            home.mkdir()
            isolation = self._isolate(home)
            setup = {
                "invocations": invocations,
                "isolation": {"env": dict(isolation.env), "files": _files(home)},
                "env": {
                    "allow": sorted(COMMON_ENV_KEEP),
                    "allow_prefixes": list(COMMON_ENV_KEEP_PREFIXES),
                    "common_drop": sorted(COMMON_ENV_DROP),
                    "common_drop_prefixes": list(COMMON_ENV_DROP_PREFIXES),
                    "common_drop_suffixes": list(COMMON_ENV_DROP_SUFFIXES),
                    "drop": sorted(self.ENV_DROP),
                    "drop_prefixes": list(self.ENV_DROP_PREFIXES),
                    "keep": sorted(self.ENV_KEEP),
                    "set": dict(self.ENV_SET),
                    "home_relative": sorted(HOME_RELATIVE_VARS),
                },
                "large_system": [MAX_ARG_BYTES, SYSTEM_POINTER, SYSTEM_BEGIN, SYSTEM_END],
            }
            text = json.dumps(setup, sort_keys=True, ensure_ascii=False).replace(str(root), "<tmp>")
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def child_env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """The environment for the CLI process: the variables of ``base`` (default
        ``os.environ``) that ``keeps`` allows, plus ``ENV_SET``."""
        source = os.environ if base is None else base
        env = {name: value for name, value in source.items() if self.keeps(name)}
        env.update(self.ENV_SET)
        return env

    @classmethod
    def keeps(cls, name: str) -> bool:
        """Whether a parent variable named ``name`` reaches this CLI, in order of precedence:

        1. never when ``always_dropped`` (``COMMON_ENV_DROP`` and its prefixes and suffixes) or
           in ``ENV_DROP``, whatever ``ENV_KEEP`` says;
        2. otherwise yes when in ``ENV_KEEP`` (this beats the CLI's ``ENV_DROP_PREFIXES``: the
           login variable usually shares the CLI's prefix);
        3. otherwise no when it matches ``ENV_DROP_PREFIXES``;
        4. otherwise only when in the common allow-list (``COMMON_ENV_KEEP`` and
           ``COMMON_ENV_KEEP_PREFIXES``).
        """
        if always_dropped(name) or name in cls.ENV_DROP:
            return False
        if name in cls.ENV_KEEP:
            return True
        if name.startswith(cls.ENV_DROP_PREFIXES):
            return False
        return name in COMMON_ENV_KEEP or name.startswith(COMMON_ENV_KEEP_PREFIXES)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        response = await self._complete(request)
        ignored = self._ignored_params(request)
        if ignored:
            response.raw = {**(response.raw or {}), "ignored_params": ignored}
        return response

    def _ignored_params(self, request: ModelRequest) -> list[str]:
        """Requested settings the CLI can't apply, recorded so the manifest isn't misread.

        ``max_output_tokens`` is always listed: every request carries a value (the dataclass
        default included) and no CLI can apply one, so leaving it out for the default would
        make the record depend on whether the value happened to be spelled out.
        """
        ignored: list[str] = []
        if request.params.temperature is not None:
            ignored.append("temperature")
        ignored.append("max_output_tokens")
        if request.params.effort is not None and not self.SUPPORTS_EFFORT:
            ignored.append("effort")
        return ignored

    def _isolate(self, home: Path) -> Isolation:
        """Private-home setup for one call under ``home`` (an empty directory). Default: none,
        for CLIs whose flags already keep the user's own setup out."""
        return Isolation()

    def _after_call(self, work_dir: Path, env: Mapping[str, str]) -> None:
        """Remove per-call state the CLI left outside the call's temporary directory (``env`` is
        the child's environment). Default: nothing to remove. Must log, never raise."""
        return None

    async def _complete(self, request: ModelRequest) -> ModelResponse:
        if request.tools:
            return self._fail("native tools are not supported by CLI providers; use context mode")
        if not request.messages:
            return self._fail("request has no messages")
        effort = (request.params.effort or self.effort) if self.SUPPORTS_EFFORT else None
        if effort is not None and not valid_effort(effort):
            return self._fail(f"invalid effort value {effort!r}")

        with call_dir(f"identitybench-{self.kind}-") as root:
            io_dir = root / "io"
            work_dir = root / "work"
            home_dir = root / "home"
            for directory in (io_dir, work_dir, home_dir):
                directory.mkdir()
            built = self._build(request, effort, io_dir, work_dir)
            too_long = next(
                (arg for arg in built.argv if len(arg.encode()) > self.MAX_ARG_BYTES), None
            )
            if too_long is not None:
                return self._fail(
                    f"an argument is {len(too_long.encode())} bytes, over the "
                    f"{self.MAX_ARG_BYTES}-byte argv limit"
                )
            try:
                isolation = self._isolate(home_dir)
                for relative, text in built.home_files.items():
                    path = home_dir / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(text, encoding="utf-8")
            except OSError as exc:
                return self._fail(f"could not set up a private CLI home: {exc}")
            env = self.child_env()
            if "HOME" in isolation.env:
                for name in HOME_RELATIVE_VARS:
                    env.pop(name, None)
            env.update(isolation.env)
            command = Command(
                argv=built.argv,
                cwd=str(work_dir),
                env=env,
                timeout_s=self.timeout_s,
                stdin=built.stdin,
            )
            attempts: list[ModelResponse] = []

            async def attempt() -> ModelResponse:
                for stale in self._stale_outputs(io_dir):
                    stale.unlink(missing_ok=True)
                started = time.perf_counter()
                result = await self._runner(command)
                if result.launch_error is not None:
                    response = self._fail(result.launch_error)
                elif result.timed_out:
                    response = self._fail(f"no result within the {self.timeout_s:g}s timeout")
                else:
                    response = self._parse(result, request, io_dir)
                response.latency_s = time.perf_counter() - started
                attempts.append(response)
                return response

            started = time.perf_counter()
            # The request the call is for, so ``_error_text`` can keep its prompt out of a
            # stderr excerpt; a context variable, since ``_parse`` calls ``_fail`` without it.
            marker = _CURRENT_REQUEST.set(request)
            try:
                response = await retry_transient(
                    attempt,
                    lambda response: (
                        response.error is not None and is_transient_error(response.error)
                    ),
                    label=f"{self.kind}:{self.model}",
                    max_retries=self._max_retries,
                    base_delay_s=self._retry_base_delay_s,
                    describe=lambda response: response.error or "",
                )
            finally:
                _CURRENT_REQUEST.reset(marker)
                sync_back(isolation.links)
                self._after_call(work_dir, env)
            if len(attempts) > 1:
                _merge_attempts(response, attempts, wall_s=time.perf_counter() - started)
            if built.raw:
                response.raw = {**(response.raw or {}), **built.raw}
            if request.params.effort is None and effort is not None:
                # The provider's own default applied; record it, since the request (and so the
                # cache key and manifest parameters) says nothing about effort.
                response.raw = {**(response.raw or {}), "effort": effort}
            return response

    def _stale_outputs(self, io_dir: Path) -> tuple[Path, ...]:
        """Files an earlier attempt may have written; deleted before each attempt so a retry
        never reads a previous attempt's output."""
        return ()

    @abstractmethod
    def _build(
        self, request: ModelRequest, effort: str | None, io_dir: Path, work_dir: Path
    ) -> Invocation:
        """The command line (and stdin) for ``request``; may write input files to ``io_dir``."""

    @abstractmethod
    def _parse(self, result: ProcessResult, request: ModelRequest, io_dir: Path) -> ModelResponse:
        """Turn a finished process into a ``ModelResponse`` (never raises)."""

    def _error_text(self, message: str, result: ProcessResult | None = None) -> str:
        text = f"{self.kind}: {message}"
        if result is not None:
            details = [f"exit {result.returncode}"]
            excerpt = scrub_stderr(result.stderr, _CURRENT_REQUEST.get())
            if excerpt:
                details.append(f"stderr: {excerpt}")
            text += f" ({'; '.join(details)})"
        return redact(text)

    def _fail(self, message: str, result: ProcessResult | None = None) -> ModelResponse:
        return ModelResponse(error=self._error_text(message, result))


def _files(directory: Path) -> dict[str, str]:
    """Regular files under ``directory`` by relative path, with their text; symlinks (login
    links) are skipped."""
    return {
        str(path.relative_to(directory)): path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _merge_attempts(final: ModelResponse, attempts: Sequence[ModelResponse], wall_s: float) -> None:
    """Fold the failed attempts of a retried call into the final response.

    Usage and reported cost cover every attempt, because every attempt was billed and the
    runner's hard ``budget_usd`` stop must see that spend. ``latency_s`` stays the final
    attempt's own duration; the wall time including backoff sleeps goes in ``raw``.
    """
    for earlier in attempts[:-1]:
        final.usage.add(earlier.usage)
    costs = [item.reported_cost_usd for item in attempts if item.reported_cost_usd is not None]
    final.reported_cost_usd = sum(costs) if costs else None
    final.raw = {
        **(final.raw or {}),
        "attempts": len(attempts),
        "wall_s": round(wall_s, 3),
    }
