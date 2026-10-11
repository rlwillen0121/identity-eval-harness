from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from identitybench.providers._cli import SYSTEM_BEGIN, SYSTEM_END
from identitybench.providers._subprocess import Command, ProcessResult
from identitybench.providers.cursor_cli import (
    CursorCliProvider,
    cursor_config_dir,
    resolve_cursor_binary,
)
from tests.providers.fakes import DECISION_SCHEMA, FakeRunner, make_request, ok

PAYLOAD = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 2100,
    "result": 'Sure. {"decision": "DENY", "rules": ["LIFE-001"]}',
    "session_id": "sess-1",
    "usage": {"inputTokens": 1200, "outputTokens": 40},
}


def provider(runner: FakeRunner, **kwargs) -> CursorCliProvider:
    return CursorCliProvider(
        runner=runner, version="2026.09.26", binary="cursor-agent", retry_base_delay_s=0, **kwargs
    )


async def test_argv_with_prompt_as_final_argument() -> None:
    runner = FakeRunner(ok(PAYLOAD))
    await provider(runner).complete(make_request("Decide REQ-1.", system="SYS"))
    command = runner.command
    assert command.argv == (
        "cursor-agent",
        "-p",
        "--trust",
        "--mode",
        "ask",
        "--output-format",
        "json",
        "--model",
        "auto",
        f"{SYSTEM_BEGIN}\nSYS\n{SYSTEM_END}\n\nDecide REQ-1.",
    )
    assert command.stdin is None
    assert runner.snapshots[0].cwd_entries == []


async def test_env_drops_cursor_endpoint_and_key(monkeypatch) -> None:
    monkeypatch.setenv("CURSOR_API_ENDPOINT", "https://gateway.example")
    monkeypatch.setenv("CURSOR_API_KEY", "key_123456789")
    runner = FakeRunner(ok(PAYLOAD))
    await provider(runner).complete(make_request())
    assert "CURSOR_API_ENDPOINT" not in runner.command.env
    assert "CURSOR_API_KEY" not in runner.command.env


async def test_leading_dash_prompt_cannot_become_an_option() -> None:
    runner = FakeRunner(ok(PAYLOAD))
    await provider(runner).complete(make_request("--help me", system=""))
    assert runner.command.argv[-1] == "\n--help me"


async def test_parses_result_and_usage_without_attributing_a_model() -> None:
    response = await provider(FakeRunner(ok(PAYLOAD))).complete(make_request())
    assert response.ok
    assert response.text == PAYLOAD["result"]
    assert response.parsed is None  # no native structured output: the runner extracts
    assert response.usage.input_tokens == 1200
    assert response.usage.output_tokens == 40
    assert response.served_model is None
    assert response.raw == {
        "subtype": "success",
        "duration_ms": 2100,
        "session_id": "sess-1",
        "ignored_params": ["max_output_tokens"],
    }


async def test_schema_and_effort_are_not_sent_and_effort_is_reported_ignored() -> None:
    runner = FakeRunner(ok(PAYLOAD))
    response = await provider(runner).complete(make_request(schema=DECISION_SCHEMA, effort="high"))
    assert "--json-schema" not in runner.command.argv
    assert response.raw is not None
    assert response.raw["ignored_params"] == ["max_output_tokens", "effort"]
    assert provider(runner).capabilities().native_structured_output is False


async def test_error_payload() -> None:
    payload = {
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "result": "Not authenticated",
    }
    response = await provider(FakeRunner(ok(payload, returncode=1))).complete(make_request())
    assert response.error is not None
    assert "Not authenticated" in response.error


# --------------------------------------------------------------------------- per-call state


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for the user's ~/.cursor, holding one chat of the user's own."""
    directory = tmp_path / "user-cursor"
    own_chat = directory / "chats" / "0123abcd" / "sess-own"
    own_chat.mkdir(parents=True)
    (own_chat / "meta.json").write_text('{"cwd": "/Users/me/code"}', encoding="utf-8")
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(directory))
    monkeypatch.delenv("CURSOR_DATA_DIR", raising=False)
    return directory


def write_state_like_the_cli(command: Command) -> ProcessResult:
    """What the real CLI leaves per call: a project directory (trust marker, transcript) under
    ``$CURSOR_DATA_DIR/projects`` and a chat under ``<config>/chats/<md5 of the cwd>``."""
    project = Path(command.env["CURSOR_DATA_DIR"]) / "projects" / "mangled-work-dir"
    (project / "agent-transcripts").mkdir(parents=True)
    (project / ".workspace-trusted").write_text(
        json.dumps({"workspacePath": command.cwd}), encoding="utf-8"
    )
    (project / "agent-transcripts" / "sess-1.jsonl").write_text("the prompt", encoding="utf-8")
    digest = hashlib.md5(command.cwd.encode(), usedforsecurity=False).hexdigest()
    chat = cursor_config_dir(command.env) / "chats" / digest / "sess-1"
    chat.mkdir(parents=True)
    (chat / "meta.json").write_text(json.dumps({"cwd": command.cwd}), encoding="utf-8")
    (chat / "store.db").write_text("the prompt", encoding="utf-8")
    return ok(PAYLOAD)


async def test_per_call_state_is_not_left_behind(config_dir: Path) -> None:
    runner = FakeRunner(write_state_like_the_cli)
    response = await provider(runner).complete(make_request())
    assert response.ok
    command = runner.command
    data_dir = Path(command.env["CURSOR_DATA_DIR"])
    # The projects directory lives in the call's temporary directory and goes with it.
    assert data_dir.parent.parent == Path(command.cwd).parent
    assert not data_dir.exists()
    # The call's chat is removed; the user's own chat is untouched.
    assert sorted(p.name for p in (config_dir / "chats").iterdir()) == ["0123abcd"]
    assert (config_dir / "chats" / "0123abcd" / "sess-own" / "meta.json").is_file()


async def test_the_users_config_dir_and_xdg_variables_are_kept(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config directory holds the login, so neither it nor the variables that locate it may
    change (unlike the CLIs that get a private HOME)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", "/xdg")
    runner = FakeRunner(ok(PAYLOAD))
    await provider(runner).complete(make_request())
    env = runner.command.env
    assert env["CURSOR_CONFIG_DIR"] == str(config_dir)
    assert env["XDG_CONFIG_HOME"] == "/xdg"
    assert env["HOME"] == os.environ["HOME"]


async def test_failing_to_remove_the_chat_is_logged_not_raised(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_rmtree = shutil.rmtree
    chats = config_dir / "chats"

    def refuse_chats(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).parent == chats:
            raise PermissionError("read-only file system")
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", refuse_chats)
    response = await provider(FakeRunner(write_state_like_the_cli)).complete(make_request())
    assert response.ok
    assert "could not remove the Cursor chat history" in caplog.text


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"CURSOR_CONFIG_DIR": "/c", "XDG_CONFIG_HOME": "/x", "HOME": "/h"}, "/c"),
        ({"CURSOR_CONFIG_DIR": "  ", "XDG_CONFIG_HOME": "/x", "HOME": "/h"}, "/x/cursor"),
        ({"HOME": "/h"}, "/h/.cursor"),
    ],
)
def test_config_dir_lookup_order(env: dict[str, str], expected: str) -> None:
    assert cursor_config_dir(env) == Path(expected)


def _fake_bin(directory: Path, name: str, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "launcher"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    link = directory / name
    link.symlink_to(target)
    return link


def test_resolve_prefers_cursor_agent_on_path(tmp_path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = _fake_bin(bin_dir, "cursor-agent", tmp_path / "cursor-agent" / "v1")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert resolve_cursor_binary() == str(link)


def test_resolve_ignores_a_non_cursor_agent_binary(tmp_path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_bin(bin_dir, "agent", tmp_path / "grok" / "bin")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert resolve_cursor_binary() == "cursor-agent"


def test_resolve_accepts_a_cursor_agent_named_agent(tmp_path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    link = _fake_bin(bin_dir, "agent", tmp_path / "cursor-agent" / "v1")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert resolve_cursor_binary() == str(link)


def test_resolve_falls_back_to_local_bin(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True)
    link = _fake_bin(local_bin, "cursor-agent", home / ".local" / "share" / "cursor-agent")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(home))
    assert resolve_cursor_binary() == str(link)


@pytest.mark.skipif(os.name != "posix", reason="symlink-based fixture")
def test_default_binary_is_resolved_at_construction(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert CursorCliProvider(version="1").binary == "cursor-agent"
