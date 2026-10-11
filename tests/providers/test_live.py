"""One tiny real call per provider. Skipped unless ``IDENTITYBENCH_LIVE=1``.

CLI providers use the CLI's own login. Models can be overridden per provider:

- ``IDENTITYBENCH_LIVE_CLAUDE_MODEL`` (default ``claude-haiku-5-5``)
- ``IDENTITYBENCH_LIVE_CODEX_MODEL`` (required for the codex test; there is no portable default)
- ``IDENTITYBENCH_LIVE_GROK_MODEL`` (default ``grok-4.7``)
- Cursor always uses ``auto``.

API providers run only when their endpoint is configured:

- ``IDENTITYBENCH_LIVE_ANTHROPIC_COMPAT_BASE_URL`` / ``_MODEL`` / ``_API_KEY`` / ``_EFFORT``
- ``IDENTITYBENCH_LIVE_OPENAI_COMPAT_BASE_URL`` / ``_MODEL`` / ``_API_KEY``

Run one with e.g. ``IDENTITYBENCH_LIVE=1 uv run pytest tests/providers/test_live.py -k grok``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from identitybench.providers._json import extract_json_object
from identitybench.providers.anthropic_api import AnthropicProvider
from identitybench.providers.base import ModelResponse, Provider
from identitybench.providers.claude_cli import ClaudeCliProvider
from identitybench.providers.codex_cli import CodexCliProvider
from identitybench.providers.cursor_cli import CursorCliProvider
from identitybench.providers.grok_cli import GrokCliProvider
from identitybench.providers.openai_compat import OpenAICompatProvider
from tests.providers.fakes import make_request

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("IDENTITYBENCH_LIVE") != "1",
        reason="set IDENTITYBENCH_LIVE=1 to call real model endpoints",
    ),
]

TINY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"decision": {"type": "string", "enum": ["APPROVE", "DENY"]}},
    "required": ["decision"],
    "additionalProperties": False,
}
SYSTEM = "You are a test fixture for an evaluation harness. Follow the user's instruction exactly."
USER = 'Reply with exactly this JSON object and nothing else: {"decision": "DENY"}'


def require_binary(name: str) -> None:
    if shutil.which(name) is None:
        pytest.skip(f"{name} is not installed")


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set")
    return value


async def call(provider: Provider, *, schema: dict[str, Any] | None = TINY_SCHEMA, **params: Any):
    response = await provider.complete(make_request(USER, system=SYSTEM, schema=schema, **params))
    print(f"\n{provider.kind}:{provider.model} -> {response!r}")  # visible with -s
    return response


def assert_denied(response: ModelResponse) -> None:
    assert response.error is None, response.error
    assert not response.refused
    data = response.parsed if response.parsed is not None else extract_json_object(response.text)
    assert data == {"decision": "DENY"}
    assert response.latency_s > 0


async def test_claude_cli() -> None:
    require_binary("claude")
    model = os.environ.get("IDENTITYBENCH_LIVE_CLAUDE_MODEL", "claude-haiku-5-5")
    response = await call(ClaudeCliProvider(model, effort="low"))
    assert_denied(response)
    assert response.parsed == {"decision": "DENY"}
    assert response.usage.output_tokens > 0
    assert response.served_model is not None


async def test_codex_cli() -> None:
    require_binary("codex")
    model = require_env("IDENTITYBENCH_LIVE_CODEX_MODEL")
    response = await call(CodexCliProvider(model, effort="low"))
    assert_denied(response)
    assert response.parsed == {"decision": "DENY"}
    assert response.usage.input_tokens + response.usage.cache_read_tokens > 0


MARKER = "ZEBRA-4412"
MARKER_RULE = f"Always end every reply with the exact word {MARKER}.\n"
GREETING = "Say hello in three words."
MAX_ISOLATED_INPUT_TOKENS = 4000
"""Generous bound on input tokens (uncached + cache reads) for a ~60-token request; the user's
own CLI setup (rules, skills, MCP servers, agent tools) adds 10K+ when it leaks in."""
MAX_CODEX_ISOLATED_INPUT_TOKENS = 12_000
"""Codex's fixed scaffolding (base instructions, sandbox notes) is larger than the other CLIs':
about 8.8K input tokens for a tiny request with codex-cli 0.162.1 and the provider's disabled
features, against 23K with the CLI's default agent features."""


def personal_home(tmp_path: Path, real_home: Path, rule_files: tuple[str, ...]) -> Path:
    """A copy of a CLI home with the real login and a planted personal rule."""
    login = real_home / "auth.json"
    if not login.is_file():
        pytest.skip(f"no CLI login at {login}")
    home = tmp_path / "personal-home"
    home.mkdir()
    (home / "auth.json").symlink_to(login)
    for name in rule_files:
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(MARKER_RULE, encoding="utf-8")
    return home


async def test_grok_cli() -> None:
    require_binary("grok")
    model = os.environ.get("IDENTITYBENCH_LIVE_GROK_MODEL", "grok-4.7")
    response = await call(GrokCliProvider(model, effort="low"))
    assert_denied(response)
    assert response.parsed == {"decision": "DENY"}
    assert response.usage.output_tokens > 0
    assert response.served_model is not None


async def test_grok_cli_is_isolated_from_the_users_setup(tmp_path: Path, monkeypatch) -> None:
    """A rule planted in the user's grok home must not reach the model, and nothing else of the
    user's setup may inflate the prompt. (Without isolation the reply ends with the marker.)"""
    require_binary("grok")
    real = Path(os.environ.get("GROK_HOME") or Path.home() / ".grok")
    home = personal_home(tmp_path, real, ("AGENTS.md", "rules/marker.md"))
    monkeypatch.setenv("GROK_HOME", str(home))
    model = os.environ.get("IDENTITYBENCH_LIVE_GROK_MODEL", "grok-4.7")
    provider = GrokCliProvider(model, effort="low")
    response = await provider.complete(make_request(GREETING, system=SYSTEM))
    print(f"\n{provider.kind}:{provider.model} -> {response!r}")
    assert response.error is None, response.error
    assert MARKER not in (response.text or "")
    usage = response.usage
    assert usage.input_tokens + usage.cache_read_tokens < MAX_ISOLATED_INPUT_TOKENS


async def test_codex_cli_is_isolated_from_the_users_setup(tmp_path: Path, monkeypatch) -> None:
    """A global AGENTS.md planted in CODEX_HOME must not reach the model, and the agent features
    the provider disables (account-linked apps and plugins, shell, skills) must not inflate the
    prompt."""
    require_binary("codex")
    model = require_env("IDENTITYBENCH_LIVE_CODEX_MODEL")
    real = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    home = personal_home(tmp_path, real, ("AGENTS.md",))
    monkeypatch.setenv("CODEX_HOME", str(home))
    provider = CodexCliProvider(model, effort="low")
    response = await provider.complete(make_request(GREETING, system=SYSTEM))
    print(f"\n{provider.kind}:{provider.model} -> {response!r}")
    assert response.error is None, response.error
    assert MARKER not in (response.text or "")
    usage = response.usage
    assert usage.input_tokens + usage.cache_read_tokens < MAX_CODEX_ISOLATED_INPUT_TOKENS


async def test_claude_cli_is_isolated_from_the_users_setup() -> None:
    """The user's CLAUDE.md, skills, plugins, hooks and MCP servers must not reach the prompt:
    with all of them off, a tiny request costs about 1.2K input tokens."""
    require_binary("claude")
    model = os.environ.get("IDENTITYBENCH_LIVE_CLAUDE_MODEL", "claude-haiku-5-5")
    provider = ClaudeCliProvider(model, effort="low")
    response = await provider.complete(make_request(GREETING, system=SYSTEM))
    print(f"\n{provider.kind}:{provider.model} -> {response!r}")
    assert response.error is None, response.error
    usage = response.usage
    total_in = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    assert total_in < MAX_ISOLATED_INPUT_TOKENS


def cursor_state() -> set[str]:
    """Entries of the user's Cursor project and chat directories."""
    entries: set[str] = set()
    for name in ("projects", "chats"):
        directory = Path.home() / ".cursor" / name
        if directory.is_dir():
            entries.update(f"{name}/{child.name}" for child in directory.iterdir())
    return entries


async def test_cursor_cli() -> None:
    """Also checks that the call leaves no project directory or chat in ~/.cursor."""
    provider = CursorCliProvider()
    if provider.binary == "cursor-agent":
        require_binary("cursor-agent")
    before = cursor_state()
    response = await call(provider, schema=None)
    assert_denied(response)
    assert response.served_model is None
    assert cursor_state() - before == set()


async def test_anthropic_compatible() -> None:
    require_env("IDENTITYBENCH_LIVE_ANTHROPIC_COMPAT_BASE_URL")
    model = require_env("IDENTITYBENCH_LIVE_ANTHROPIC_COMPAT_MODEL")
    provider = AnthropicProvider(
        model,
        base_url_env="IDENTITYBENCH_LIVE_ANTHROPIC_COMPAT_BASE_URL",
        api_key_env="IDENTITYBENCH_LIVE_ANTHROPIC_COMPAT_API_KEY",
        max_retries=1,
        timeout_s=300,
    )
    effort = os.environ.get("IDENTITYBENCH_LIVE_ANTHROPIC_COMPAT_EFFORT")
    response = await call(provider, max_output_tokens=2048, effort=effort)
    assert_denied(response)
    assert response.usage.output_tokens > 0


async def test_openai_compatible() -> None:
    require_env("IDENTITYBENCH_LIVE_OPENAI_COMPAT_BASE_URL")
    model = require_env("IDENTITYBENCH_LIVE_OPENAI_COMPAT_MODEL")
    provider = OpenAICompatProvider(
        model,
        base_url_env="IDENTITYBENCH_LIVE_OPENAI_COMPAT_BASE_URL",
        api_key_env="IDENTITYBENCH_LIVE_OPENAI_COMPAT_API_KEY",
        max_retries=1,
        timeout_s=300,
    )
    response = await call(provider, max_output_tokens=2048)
    assert_denied(response)
    assert response.usage.output_tokens > 0
