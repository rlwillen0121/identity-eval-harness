"""The subprocess layer, exercised with the Python interpreter as a harmless stand-in binary."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time

import pytest

from identitybench.providers._subprocess import (
    Command,
    is_transient_error,
    redact,
    retry_transient,
    run_command,
    tail,
)


def python(
    code: str, *, stdin: str | None = None, timeout_s: float = 30.0, cwd: str = "."
) -> Command:
    return Command(
        argv=(sys.executable, "-c", code),
        cwd=cwd,
        env={"PATH": os.environ.get("PATH", ""), "MARKER": "visible"},
        timeout_s=timeout_s,
        stdin=stdin,
    )


async def test_captures_stdout_stderr_exit_code_and_stdin(tmp_path) -> None:
    code = (
        "import os, sys; data = sys.stdin.read(); print(data.upper()); "
        "print(os.environ['MARKER'], file=sys.stderr); print(os.getcwd()); sys.exit(3)"
    )
    result = await run_command(python(code, stdin="héllo", cwd=str(tmp_path)))
    assert result.returncode == 3
    lines = result.stdout.splitlines()
    assert lines[0] == "HÉLLO"
    assert os.path.realpath(lines[1]) == os.path.realpath(tmp_path)
    assert result.stderr.strip() == "visible"
    assert not result.timed_out
    assert result.launch_error is None
    assert result.duration_s > 0


async def test_env_is_exactly_what_the_command_says() -> None:
    result = await run_command(python("import json, os; print(json.dumps(sorted(os.environ)))"))
    names = json.loads(result.stdout)
    assert "MARKER" in names
    assert "HOME" not in names


async def test_no_stdin_means_devnull() -> None:
    result = await run_command(python("import sys; print(repr(sys.stdin.read()))"))
    assert result.stdout.strip() == "''"


async def test_timeout_kills_the_process_group(tmp_path) -> None:
    # The child spawns a grandchild that would outlive a plain kill of the direct child.
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        f"open({str(pid_file)!r}, 'w').write(str(p.pid)); "
        "time.sleep(30)"
    )
    start = time.perf_counter()
    result = await run_command(python(code, timeout_s=1.0))
    assert result.timed_out
    assert time.perf_counter() - start < 10
    grandchild = int(pid_file.read_text())
    for _ in range(50):  # the kernel reaps asynchronously
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        os.kill(grandchild, signal.SIGKILL)
        pytest.fail("grandchild survived the timeout kill")


async def test_helpers_left_running_after_a_normal_exit_are_killed(tmp_path) -> None:
    # Like a CLI that starts a language server and exits: the helper drops the output pipes, so
    # communicate() returns, but it would keep running forever.
    pid_file = tmp_path / "helper.pid"
    code = (
        "import subprocess, sys; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))"
    )
    result = await run_command(python(code))
    assert result.returncode == 0
    assert not result.timed_out
    helper = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(helper, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        os.kill(helper, signal.SIGKILL)
        pytest.fail("a helper outlived the finished command")


async def test_missing_binary_is_a_launch_error_not_an_exception() -> None:
    command = Command(argv=("/nonexistent/claude-binary",), cwd=".", env={}, timeout_s=5)
    result = await run_command(command)
    assert result.launch_error is not None
    assert "could not start" in result.launch_error
    assert result.returncode == -1


@pytest.mark.parametrize(
    "text",
    [
        "API Error: 429 Too Many Requests",
        "rate_limit_error: slow down",
        "Rate limit reached for requests",
        "Overloaded",
        "API Error: 529",
        "HTTP 503 Service Unavailable",
        "502 Bad Gateway",
        "The service is temporarily unavailable",
        "Error: read ECONNRESET",
        "getaddrinfo ENOTFOUND api.example.com",
        "TypeError: fetch failed",
        "socket hang up",
        "network error while streaming",
        "connection reset by peer",
    ],
)
def test_transient_errors(text: str) -> None:
    assert is_transient_error(text)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "claude_cli: no result within the 600s timeout",
        "invalid_request_error: max_tokens too large",
        "authentication failed: please log in",
        "processed 4290 tokens",
        "model not found",
        "Unauthorized (401): missing or wrong API key. Authentication is temporarily unavailable",
        "authentication_error: invalid x-api-key (503 upstream)",
        "Not logged in · Please run /login",
    ],
)
def test_non_transient_errors(text: str | None) -> None:
    assert not is_transient_error(text)


async def test_retry_transient_retries_with_backoff_then_succeeds(monkeypatch, caplog) -> None:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("identitybench.providers._subprocess.asyncio.sleep", fake_sleep)
    outcomes = iter(["429 rate limit", "overloaded", "ok"])
    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        return next(outcomes)

    with caplog.at_level(logging.WARNING, logger="identitybench.providers"):
        result = await retry_transient(
            attempt, lambda r: r != "ok", label="test", max_retries=3, base_delay_s=2.0
        )
    assert result == "ok"
    assert calls == 3
    assert delays == [2.0, 4.0]
    assert "transient failure" in caplog.text


async def test_retry_transient_gives_up_after_max_retries(monkeypatch) -> None:
    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr("identitybench.providers._subprocess.asyncio.sleep", fake_sleep)
    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        return "503"

    result = await retry_transient(attempt, lambda r: True, label="test", max_retries=3)
    assert result == "503"
    assert calls == 4


async def test_retry_transient_does_not_retry_permanent_failures() -> None:
    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        return "bad request"

    assert await retry_transient(attempt, lambda r: False, label="test") == "bad request"
    assert calls == 1


def test_redact_masks_secret_env_values_and_key_shapes() -> None:
    env = {"MY_API_KEY": "abcdef123456", "SHORT_TOKEN": "abc", "PATH": "/usr/bin:/bin"}
    text = (
        "key=abcdef123456 short=abc path=/usr/bin:/bin "
        "sk-ant-api03-ABCDEFGHIJKLMNOP Bearer eyJhbGciOiJIUzI1NiJ9.payload"
    )
    redacted = redact(text, env)
    assert "abcdef123456" not in redacted
    assert "sk-ant-api03-ABCDEFGHIJKLMNOP" not in redacted
    assert "eyJhbGciOiJIUzI1NiJ9" not in redacted
    assert "short=abc" in redacted  # too short to be a credential worth masking
    assert "/usr/bin:/bin" in redacted  # not a credential-named variable


def test_tail_keeps_the_end() -> None:
    assert tail("short") == "short"
    long = "x" * 50 + "END"
    assert tail(long, 10) == "…" + long[-10:]
