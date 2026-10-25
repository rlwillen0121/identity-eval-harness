"""v4.1 runner: config fingerprint covers the CLI turn protocol."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from identitybench.providers.base import Provider
from identitybench.v41.runner import (
    NO_THOUGHT_TURN_PROTOCOL,
    V41ModelEntryConfig,
    _uses_no_thought_schema,
    compute_v41_config_fingerprint,
)


def _fp(turn_protocol: str | None = None) -> str:
    entry = V41ModelEntryConfig(provider="claude_cli", model="claude-sonnet-5-5")
    return compute_v41_config_fingerprint(
        entry,
        entry.to_run_config(max_turns=12, max_calls_per_turn=8),
        seed=20260301,
        dataset_sha256="0" * 64,
        endpoint_identity="claude_cli:claude-sonnet-5-5",
        turn_protocol=turn_protocol,
    )


def test_config_fingerprint_covers_turn_protocol() -> None:
    """Resume must not reuse records written under the older "thought" turn protocol."""
    fp = _fp()
    assert len(fp) == 64 and fp == _fp(None)
    assert _fp(NO_THOUGHT_TURN_PROTOCOL) != fp
    assert _fp(NO_THOUGHT_TURN_PROTOCOL) == _fp(NO_THOUGHT_TURN_PROTOCOL)


def _provider(kind: str, model: str, *, native_tools: bool) -> Provider:
    caps = SimpleNamespace(native_tools=native_tools)
    stub: Any = SimpleNamespace(kind=kind, model=model, capabilities=lambda: caps)
    return cast(Provider, stub)


def test_no_thought_schema_selection() -> None:
    assert _uses_no_thought_schema(_provider("claude_cli", "claude-opus-5-5", native_tools=False))
    assert _uses_no_thought_schema(_provider("cursor_cli", "claude-fable-5-1", native_tools=False))
    assert not _uses_no_thought_schema(_provider("codex_cli", "gpt-6-astra", native_tools=False))
    assert not _uses_no_thought_schema(
        _provider("anthropic_api", "claude-fable-5-1", native_tools=True)
    )
