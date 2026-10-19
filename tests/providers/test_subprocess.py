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
    TRUNCATION_MARKER,
    Command,
    is_transient_error,
    kill_all_live,
    live_groups,
    redact,
    register_group,
    retry_transient,
    run_command,
    tail,
    unregister_group,
    url_hostname,
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


# Fake tokens in the real formats, assembled at run time so no literal looks like a live secret
# to a scanner: GitHub's classic prefixes take 36 base62 characters, fine-grained PATs
# ``github_pat_`` plus 82 characters with an underscore inside.
_BODY36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_FAKE_TOKENS = [
    *(prefix + "_" + _BODY36 for prefix in ("ghp", "gho", "ghu", "ghs", "ghr")),
    "github_pat_" + "11ABCDEFG0" + _BODY36[:12] + "_" + (_BODY36 + "AB")[:59],
    "sk-" + "or-v1-" + _BODY36,
    "sk-" + "ant-api03-" + _BODY36,
    "sk-" + "proj-" + _BODY36,
    "xai-" + _BODY36,
    "pk-" + _BODY36,
]


@pytest.mark.parametrize("token", _FAKE_TOKENS, ids=lambda t: t.split("_")[0].split("-")[0])
def test_redact_masks_each_key_shape(token: str) -> None:
    redacted = redact(f"stderr: auth failed for {token} (401)", env={})
    assert token not in redacted
    assert _BODY36[:20] not in redacted
    assert redacted == "stderr: auth failed for <redacted> (401)"


@pytest.mark.parametrize(
    "text",
    [
        "ghp-" + _BODY36,  # a hyphen is not GitHub's format
        "ghp_short123",  # too short to be a token
        "see the github_pat_ docs",
        "skip-this-flag",
    ],
)
def test_redact_leaves_lookalikes(text: str) -> None:
    assert redact(text, env={}) == text


def test_tail_keeps_the_end() -> None:
    assert tail("short") == "short"
    long = "x" * 50 + "END"
    assert tail(long, 10) == "…" + long[-10:]


# --------------------------------------------------------------------------- output caps


async def test_captured_output_is_capped_with_a_marker(monkeypatch) -> None:
    monkeypatch.setattr("identitybench.providers._subprocess.MAX_CAPTURE_BYTES", 1000)
    code = "import sys; sys.stdout.write('o' * 5000); sys.stderr.write('e' * 5000)"
    result = await run_command(python(code))
    assert result.returncode == 0
    assert not result.timed_out
    assert result.stdout == "o" * 1000 + TRUNCATION_MARKER
    assert result.stderr == "e" * 1000 + TRUNCATION_MARKER


async def test_output_within_the_cap_is_untouched(monkeypatch) -> None:
    monkeypatch.setattr("identitybench.providers._subprocess.MAX_CAPTURE_BYTES", 1000)
    result = await run_command(python("import sys; sys.stdout.write('o' * 1000)"))
    assert result.stdout == "o" * 1000
    assert TRUNCATION_MARKER not in result.stdout


async def test_a_child_that_floods_output_before_reading_stdin_does_not_deadlock() -> None:
    # Pipes hold 64 KiB: the child fills stdout before it would read stdin, while the parent has
    # more stdin than fits in the pipe. Draining both streams alongside the write keeps it moving.
    code = "import sys; sys.stdout.write('x' * 300000); sys.stdout.flush(); sys.stdin.read()"
    result = await run_command(python(code, stdin="s" * 300000, timeout_s=30))
    assert result.returncode == 0
    assert len(result.stdout) == 300000


# --------------------------------------------------------------------------- live process groups


def test_live_group_registry_and_kill_all(monkeypatch) -> None:
    assert not live_groups()
    killed: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        killed.append((pgid, sig))
        if pgid == 70002:
            raise ProcessLookupError  # already gone: not an error

    monkeypatch.setattr(os, "killpg", fake_killpg)
    for pgid in (70001, 70002, 70003):
        register_group(pgid)
    unregister_group(70003)
    assert live_groups() == {70001, 70002}
    assert kill_all_live() == 2
    assert killed == [(70001, signal.SIGKILL), (70002, signal.SIGKILL)]
    assert not live_groups()
    assert kill_all_live() == 0


async def test_run_command_registers_the_group_while_the_child_runs() -> None:
    assert not live_groups()
    task = asyncio.create_task(run_command(python("import time; time.sleep(1)", timeout_s=30)))
    for _ in range(200):
        if live_groups():
            break
        await asyncio.sleep(0.02)
    else:
        task.cancel()
        pytest.fail("the child's process group was never registered")
    (pgid,) = live_groups()
    assert pgid > 0
    result = await task
    assert result.returncode == 0
    assert not live_groups()


async def test_cancellation_kills_the_group_and_reaps_the_child(tmp_path) -> None:
    pid_file = tmp_path / "child.pid"
    code = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    task = asyncio.create_task(run_command(python(code, timeout_s=60)))
    for _ in range(200):
        if pid_file.is_file() and pid_file.read_text():
            break
        await asyncio.sleep(0.02)
    else:
        task.cancel()
        pytest.fail("the child never started")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    child = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)  # succeeds for a zombie too, so this also checks the reap
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        os.kill(child, signal.SIGKILL)
        pytest.fail("the child survived cancellation")
    assert not live_groups()


# --------------------------------------------------------------------------- endpoint redaction


def test_redact_masks_endpoint_named_values_and_their_json_escaped_form() -> None:
    env = {
        "OPENCODE_GO_BASE_URL": "https://go.opencode.example/v1",
        "CURSOR_API_ENDPOINT": "https://gw.cursor.example:8443",
        "ISC_TENANT_URL": "https://tenant.identitynow.example",
        "DB_HOST": "db.internal.example",
        "SVC_AUTH": "basic-auth-blob",
        "API_BASE": "https://base.example/api",
    }
    text = (
        'url=https://go.opencode.example/v1 json="https:\\/\\/go.opencode.example\\/v1" '
        "ep=https://gw.cursor.example:8443 tenant=https://tenant.identitynow.example "
        "host=db.internal.example auth=basic-auth-blob base=https://base.example/api"
    )
    redacted = redact(text, env)
    for value in env.values():
        assert value not in redacted
    assert "https:\\/\\/go.opencode.example\\/v1" not in redacted
    assert "go.opencode.example" not in redacted
    assert redacted.count("<redacted>") == 7


def test_redact_masks_the_hostname_of_an_endpoint_named_variable_and_its_escaped_form() -> None:
    env = {
        "LOCAL_LLM_BASE_URL": "https://gateway.corp.example/v1/chat",
        "LOCAL_URL": "http://localhost:8000/v1",
        "LOOPBACK_URL": "http://127.0.0.1:11434",
        "SHORT_URL": "http://ab/x",
        "DEV_ENDPOINT": "http://app.localhost:3000",
        "PATH": "/usr/bin:/bin",
    }
    text = (
        "failed: gateway.corp.example refused; "
        'json="https:\\/\\/gateway.corp.example\\/other\\/path"; '
        "cname gateway.corp.example.internal; "
        "localhost ok; 127.0.0.1 ok; ab ok; app.localhost ok; /usr/bin"
    )
    redacted = redact(text, env)
    assert "gateway.corp.example" not in redacted
    assert redacted.startswith("failed: <redacted> refused; ")
    assert 'json="https:\\/\\/<redacted>\\/other\\/path"' in redacted  # the host inside another URL
    assert "cname <redacted>.internal" in redacted
    assert "localhost ok; 127.0.0.1 ok; ab ok; app.localhost ok; /usr/bin" in redacted


def test_redact_leaves_the_host_of_a_url_whose_variable_is_not_endpoint_named() -> None:
    """Masking the host of every URL-valued variable turned a stray ``WORD=http://test:8080``
    into a mask over every ``test`` in a message; only endpoint-named variables count."""
    env = {"SOME_SETTING": "https://gateway.corp.example/v1/chat", "WORD": "http://test:8080"}
    text = "the test suite tested gateway.corp.example"
    assert redact(text, env) == text


def test_redact_masks_hosts_as_whole_words_and_never_an_all_digit_host() -> None:
    env = {"WORD_URL": "http://test:8080", "PORTISH_URL": "http://1234:80"}
    text = 'the test suite tested {"count": 1234} testing contest'
    redacted = redact(text, env)
    assert redacted == 'the <redacted> suite tested {"count": 1234} testing contest'
    assert redact("endpoint http://1234:80 down", env) == "endpoint <redacted> down"


@pytest.mark.parametrize(
    ("value", "host"),
    [
        ("https://gateway.corp.example/v1", "gateway.corp.example"),
        ("http://user:pw@host.example:8080/path", "host.example"),
        ("  https://padded.example  ", "padded.example"),
        ("http://localhost:8000", None),
        ("http://localhost.localdomain:8000", None),
        ("http://app.localhost:3000", None),
        ("http://127.0.0.1", None),
        ("http://[::1]:8000", None),
        ("http://ab/", None),
        ("http://1234:80", None),
        ("http://10.0.0.5:8000", "10.0.0.5"),
        ("/usr/bin:/bin", None),
        ("not a url", None),
        ("http://[bad", None),
    ],
)
def test_url_hostname(value: str, host: str | None) -> None:
    assert url_hostname(value) == host
