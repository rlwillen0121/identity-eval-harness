"""Subprocess plumbing for the subscription-CLI providers.

Everything a CLI provider does with the OS goes through a ``ProcessRunner`` (a coroutine taking a
``Command`` and returning a ``ProcessResult``). Production uses ``run_command``; tests inject a
fake runner, so no test ever spawns a real model CLI.

``run_command`` never raises for process-level failures: a missing binary, a crash or a timeout
all come back as a ``ProcessResult`` the provider turns into ``ModelResponse.error``. Captured
output is capped at ``MAX_CAPTURE_BYTES`` per stream (a CLI stuck in a logging loop must not
exhaust the harness's memory); a truncated stream ends in ``TRUNCATION_MARKER``.

Every child runs in its own session (process group), and the group is registered in a
module-level registry for as long as it lives (``live_groups``). ``kill_all_live`` empties that
registry with SIGKILL: the shutdown handlers in ``_cli`` call it when the harness receives
SIGTERM/SIGHUP or exits, so a dying harness doesn't leave CLIs still talking to a model.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger("identitybench.providers")

DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY_S = 2.0

MAX_CAPTURE_BYTES = 16 * 1024 * 1024
"""Most bytes kept per captured stream; the rest of the stream is read and discarded."""
TRUNCATION_MARKER = "\n[identitybench: output truncated at 16 MiB]"
_READ_CHUNK = 64 * 1024


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    timeout_s: float
    stdin: str | None = None


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    duration_s: float = 0.0
    timed_out: bool = False
    launch_error: str | None = None
    """Set when the process couldn't be started at all (e.g. binary not found)."""


ProcessRunner = Callable[[Command], Awaitable[ProcessResult]]


# --------------------------------------------------------------------------- live process groups

_LIVE_GROUPS: set[int] = set()
_LIVE_GROUPS_LOCK = threading.RLock()
"""Reentrant: a signal handler runs on the main thread, which may hold the lock already."""


def register_group(pgid: int) -> None:
    with _LIVE_GROUPS_LOCK:
        _LIVE_GROUPS.add(pgid)


def unregister_group(pgid: int) -> None:
    with _LIVE_GROUPS_LOCK:
        _LIVE_GROUPS.discard(pgid)


def live_groups() -> frozenset[int]:
    """Process groups of the CLI children that are running right now."""
    with _LIVE_GROUPS_LOCK:
        return frozenset(_LIVE_GROUPS)


def kill_all_live() -> int:
    """SIGKILL every registered process group and forget it; returns how many were signalled.

    For emergency shutdown (a signal or interpreter exit): nothing waits for the children, since
    the event loop that would reap them may already be gone.
    """
    with _LIVE_GROUPS_LOCK:
        groups = sorted(_LIVE_GROUPS)
        _LIVE_GROUPS.clear()
    for pgid in groups:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
    return len(groups)


# --------------------------------------------------------------------------- running a command


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the child and everything it spawned.

    The CLIs are Node/Rust launchers that fork workers; killing only the direct child would leave
    an orphan still talking to the model (and still billing) after the timeout.
    """
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


def _kill_stragglers(pgid: int) -> None:
    """Kill whatever is left in a finished child's process group.

    A CLI can exit normally while helpers it started (a language server, a worker) keep running
    with the output pipes closed; nothing would ever stop them, and at benchmark scale they pile
    up. Daemons that detach into their own session aren't in the group and are left alone.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


@dataclass(frozen=True)
class _Capture:
    data: bytes
    truncated: bool

    def text(self) -> str:
        decoded = self.data.decode(errors="replace")
        return decoded + TRUNCATION_MARKER if self.truncated else decoded


async def _drain(stream: asyncio.StreamReader | None, limit: int) -> _Capture:
    """Read ``stream`` to EOF, keeping the first ``limit`` bytes."""
    if stream is None:
        return _Capture(b"", False)
    chunks: list[bytes] = []
    size = 0
    truncated = False
    while chunk := await stream.read(_READ_CHUNK):
        if size >= limit:
            truncated = True
            continue
        keep = chunk[: limit - size]
        if len(keep) < len(chunk):
            truncated = True
        chunks.append(keep)
        size += len(keep)
    return _Capture(b"".join(chunks), truncated)


async def _feed(proc: asyncio.subprocess.Process, data: bytes) -> None:
    stdin = proc.stdin
    if stdin is None:
        raise RuntimeError("Subprocess pipes not initialized")
    try:
        stdin.write(data)
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # the child exited (or closed stdin) before reading it all: its output says why
    finally:
        stdin.close()


async def _communicate(
    proc: asyncio.subprocess.Process, stdin_bytes: bytes | None
) -> tuple[_Capture, _Capture]:
    """``communicate()`` with bounded buffers: both streams are drained concurrently with the
    stdin write, so a child that fills a pipe before reading stdin can't deadlock the call."""
    feeders = [_feed(proc, stdin_bytes)] if stdin_bytes is not None else []
    stdout, stderr, *_ = await asyncio.gather(
        _drain(proc.stdout, MAX_CAPTURE_BYTES), _drain(proc.stderr, MAX_CAPTURE_BYTES), *feeders
    )
    _kill_stragglers(proc.pid)
    await proc.wait()
    if stdout is None or stderr is None:
        raise RuntimeError("Subprocess pipes not initialized")
    return stdout, stderr


async def _reap(proc: asyncio.subprocess.Process) -> None:
    """Wait for a child that was just killed, so it doesn't linger as a zombie."""
    with contextlib.suppress(Exception):
        await proc.wait()


async def run_command(command: Command) -> ProcessResult:
    """Run ``command`` with captured (capped) output and a hard timeout."""
    start = time.perf_counter()
    stdin = asyncio.subprocess.PIPE if command.stdin is not None else asyncio.subprocess.DEVNULL
    try:
        proc = await asyncio.create_subprocess_exec(
            *command.argv,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=command.cwd,
            env=dict(command.env),
            start_new_session=True,
        )
    except OSError as exc:
        return ProcessResult(
            returncode=-1,
            stdout="",
            stderr="",
            duration_s=time.perf_counter() - start,
            launch_error=f"could not start {command.argv[0]!r}: {exc.strerror or exc}",
        )

    register_group(proc.pid)  # start_new_session: the child's pid is its process group id
    stdin_bytes = command.stdin.encode() if command.stdin is not None else None
    try:
        try:
            stdout, stderr = await asyncio.wait_for(
                _communicate(proc, stdin_bytes), timeout=command.timeout_s
            )
        except TimeoutError:
            _kill_group(proc)
            await _reap(proc)
            return ProcessResult(
                returncode=proc.returncode if proc.returncode is not None else -9,
                stdout="",
                stderr="",
                duration_s=time.perf_counter() - start,
                timed_out=True,
            )
        except asyncio.CancelledError:
            _kill_group(proc)
            with contextlib.suppress(asyncio.CancelledError):
                await _reap(proc)
            raise
        _kill_stragglers(proc.pid)
        return ProcessResult(
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout.text(),
            stderr=stderr.text(),
            duration_s=time.perf_counter() - start,
        )
    finally:
        unregister_group(proc.pid)


# --------------------------------------------------------------------------- retries


_TRANSIENT_RE = re.compile(
    r"rate[ _-]?limit|too many requests|\b429\b|overloaded|at capacity|\b529\b|\b50[234]\b"
    r"|temporarily unavailable|service unavailable|bad gateway|gateway timeout"
    r"|stream idle timeout|context deadline exceeded"
    r"|ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|socket hang up|fetch failed"
    r"|network error|connection (?:reset|refused|error|closed|aborted)",
    re.IGNORECASE,
)
_AUTH_FAILURE_RE = re.compile(
    r"\b401\b|unauthori[sz]ed|authentication_error|(?:wrong|invalid|incorrect) api key"
    r"|not logged in|please (?:log ?in|run /login)",
    re.IGNORECASE,
)


def is_transient_error(text: str | None) -> bool:
    """True for failures worth retrying: rate limits, overload/unavailable, network errors.

    Authentication failures are never transient, even when the CLI's message also says
    "temporarily unavailable" (Grok wraps a 401 that way): a wrong key stays wrong. Process
    timeouts aren't transient either: retrying a call that already ran for the full timeout
    multiplies wall time for little gain.
    """
    if not text or _AUTH_FAILURE_RE.search(text):
        return False
    return _TRANSIENT_RE.search(text) is not None


async def retry_transient[T](
    attempt: Callable[[], Awaitable[T]],
    is_transient: Callable[[T], bool],
    *,
    label: str,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay_s: float = DEFAULT_RETRY_BASE_DELAY_S,
    describe: Callable[[T], str] = str,
) -> T:
    """Call ``attempt`` until it succeeds, fails non-transiently, or ``max_retries`` run out.

    Backoff doubles from ``base_delay_s``. There is no jitter: delays are small, retries are
    rare, and deterministic timing keeps runs easy to reason about.
    """
    result = await attempt()
    for retry in range(max_retries):
        if not is_transient(result):
            return result
        delay = base_delay_s * (2**retry)
        logger.warning(
            "%s: transient failure (retry %d/%d in %.1fs): %s",
            label,
            retry + 1,
            max_retries,
            delay,
            redact(describe(result))[:300],
        )
        await asyncio.sleep(delay)
        result = await attempt()
    return result


# --------------------------------------------------------------------------- redaction


_SECRET_NAME_RE = re.compile(
    r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?|_URL|_ENDPOINT|_HOST|_AUTH|_BASE"
    r"|_PAT|_PASSWD|_COOKIE|_SESSION|_JWT)$",
    re.IGNORECASE,
)
_SENSITIVE_NAME = _SECRET_NAME_RE
_SECRET_VALUE_RE = re.compile(
    r"\b(?:sk|xai|pk)-[A-Za-z0-9_\-]{12,}"
    r"|\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}"
    r"|(?i:Bearer)\s+[A-Za-z0-9._\-~+/=]{12,}"
    r"|(?i:Authorization:\s*Basic)\s+[A-Za-z0-9+/=]{8,}"
    r"|(?i:x-api-key:)\s*[^\s\"',;]{8,}"
    r"|\bxox[a-zA-Z]-[A-Za-z0-9-]{10,}"
    r"|\bAIza[0-9A-Za-z_-]{30,}"
    r"|\bya29\.[0-9A-Za-z_-]{20,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)
_INLINE_PATTERNS = _SECRET_VALUE_RE
_REDACTED = "<redacted>"
_LOCAL_HOSTS = frozenset({"localhost", "localhost.localdomain", "127.0.0.1", "::1", "0.0.0.0"})
_MIN_SECRET_CHARS = 8
_MIN_HOST_CHARS = 4


def url_hostname(value: str) -> str | None:
    """The host of ``value`` when it is a URL worth masking: at least ``_MIN_HOST_CHARS`` long,
    not all digits (masking ``1234`` would mask numbers everywhere) and not a loopback name or
    address (which names no private endpoint)."""
    if "://" not in value:
        return None
    try:
        host = urlsplit(value.strip()).hostname
    except ValueError:
        return None
    if (
        not host
        or len(host) < _MIN_HOST_CHARS
        or host.isdigit()
        or host in _LOCAL_HOSTS
        or host.endswith(".localhost")
    ):
        return None
    return host


def redact(text: str, env: Mapping[str, str] | None = None) -> str:
    """Mask secrets before text reaches a log, an error message or ``ModelResponse.raw``.

    Masks, in order:

    - the values of credential- and endpoint-looking environment variables (names ending in
      KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL(S), _URL, _ENDPOINT, _HOST, _AUTH or _BASE), and
      the JSON-escaped form of any such value holding a ``/`` (``https:\\/\\/...``), as a CLI's
      JSON error output would spell it;
    - the hostname of every such endpoint-named value that is a URL (``url_hostname``), as a
      whole word, so the host alone (or inside another URL, escaped or not) can't surface
      either. Only those variables: masking the host of any URL-valued variable turned a
      ``WORD_URL=http://test:8080`` into a mask over every ``test`` in a message;
    - common key and bearer-token shapes.

    Longer values go first, so a value that contains another is masked whole.
    """
    source = os.environ if env is None else env
    values: set[str] = set()
    hosts: set[str] = set()
    for name, value in source.items():
        if not _SECRET_NAME_RE.search(name):
            continue
        if len(value) >= _MIN_SECRET_CHARS:
            values.add(value)
            if "/" in value:
                values.add(value.replace("/", "\\/"))
        host = url_hostname(value)
        if host is not None:
            hosts.add(host)
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, _REDACTED)
    for host in sorted(hosts, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(host)}\b", _REDACTED, text)
    return _SECRET_VALUE_RE.sub(_REDACTED, text)


def tail(text: str, limit: int = 2000) -> str:
    """Last ``limit`` characters of ``text`` (errors are usually at the end of CLI output)."""
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]
