"""Subprocess plumbing for the subscription-CLI providers.

Everything a CLI provider does with the OS goes through a ``ProcessRunner`` (a coroutine taking a
``Command`` and returning a ``ProcessResult``). Production uses ``run_command``; tests inject a
fake runner, so no test ever spawns a real model CLI.

``run_command`` never raises for process-level failures: a missing binary, a crash or a timeout
all come back as a ``ProcessResult`` the provider turns into ``ModelResponse.error``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

logger = logging.getLogger("identitybench.providers")

DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_DELAY_S = 2.0


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


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the child and everything it spawned.

    The CLIs are Node/Rust launchers that fork workers; killing only the direct child would leave
    an orphan still talking to the model (and still billing) after the timeout.
    """
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


async def run_command(command: Command) -> ProcessResult:
    """Run ``command`` with captured output and a hard timeout."""
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

    stdin_bytes = command.stdin.encode() if command.stdin is not None else None
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_bytes), timeout=command.timeout_s
        )
    except TimeoutError:
        _kill_group(proc)
        with contextlib.suppress(Exception):
            await proc.wait()
        return ProcessResult(
            returncode=proc.returncode if proc.returncode is not None else -9,
            stdout="",
            stderr="",
            duration_s=time.perf_counter() - start,
            timed_out=True,
        )
    except asyncio.CancelledError:
        _kill_group(proc)
        raise
    _kill_stragglers(proc.pid)
    return ProcessResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout.decode(errors="replace"),
        stderr=stderr.decode(errors="replace"),
        duration_s=time.perf_counter() - start,
    )


_TRANSIENT_RE = re.compile(
    r"rate[ _-]?limit|too many requests|\b429\b|overloaded|\b529\b|\b50[234]\b"
    r"|temporarily unavailable|service unavailable|bad gateway|gateway timeout"
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


_SECRET_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)$", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(
    r"\b(?:sk|xai|sk-ant|pk|ghp|gho)-[A-Za-z0-9_\-]{12,}|Bearer\s+[A-Za-z0-9._\-~+/=]{12,}"
)
_REDACTED = "<redacted>"


def redact(text: str, env: Mapping[str, str] | None = None) -> str:
    """Mask secrets before text reaches a log, an error message or ``ModelResponse.raw``.

    Masks the values of credential-looking environment variables (names ending in KEY, TOKEN,
    SECRET, PASSWORD, CREDENTIAL(S)) plus common key and bearer-token shapes.
    """
    source = os.environ if env is None else env
    for name, value in source.items():
        if len(value) >= 8 and _SECRET_NAME_RE.search(name) and value in text:
            text = text.replace(value, _REDACTED)
    return _SECRET_VALUE_RE.sub(_REDACTED, text)


def tail(text: str, limit: int = 2000) -> str:
    """Last ``limit`` characters of ``text`` (errors are usually at the end of CLI output)."""
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]
