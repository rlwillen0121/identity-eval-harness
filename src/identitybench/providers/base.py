"""Provider contract (SPEC §11.1).

Every model access path implements ``Provider``: native APIs (Anthropic, OpenAI-compatible,
Anthropic-compatible), subscription CLIs (``claude -p``, ``codex exec``, ``grok -p``), the replay
provider and the baselines. The runner and scorers only ever see ``ModelRequest`` and
``ModelResponse``, so nothing downstream special-cases a provider.

A provider never raises for model-level failures. Transport, auth and parse problems come back
as a ``ModelResponse`` with ``error`` set (after the provider's own retries), which the runner
records and the scorer treats as INVALID.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from identitybench.schemas.case import Case


class Role(StrEnum):
    user = "user"
    assistant = "assistant"
    tool = "tool"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    """Provider-neutral transcript message.

    - ``user``: ``content`` is the user text.
    - ``assistant``: ``content`` is the assistant text (may be empty) plus any ``tool_calls``.
    - ``tool``: ``content`` is the tool result text for ``tool_call_id``.
    """

    role: Role
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class GenerationParams:
    max_output_tokens: int = 4096
    temperature: float | None = None
    """None means "don't send": several current models reject any non-default temperature."""
    effort: str | None = None
    """Reasoning effort (``low``/``medium``/``high``/``xhigh``/``max``); None = provider default."""
    extra: dict[str, Any] = field(default_factory=dict)
    """Provider-specific pass-through options (recorded in the manifest)."""


@dataclass(frozen=True)
class ModelRequest:
    system: str
    messages: tuple[Message, ...]
    output_schema: dict[str, Any] | None = None
    schema_name: str = "decision"
    tools: tuple[ToolSpec, ...] = ()
    params: GenerationParams = field(default_factory=GenerationParams)


@dataclass
class Usage:
    input_tokens: int = 0
    """Uncached input tokens."""
    output_tokens: int = 0
    """All output tokens, including reasoning tokens."""
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.reasoning_tokens += other.reasoning_tokens


@dataclass
class ModelResponse:
    text: str | None = None
    """Final assistant text (for structured output, the JSON text)."""
    parsed: dict[str, Any] | None = None
    """Structured output as a dict when the provider returns one natively; otherwise None and the
    runner extracts a JSON object from ``text``."""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    refused: bool = False
    """True when the model declined (e.g. Anthropic ``stop_reason == "refusal"``). Scored INVALID;
    never retried on a different model, because that would misattribute the answer."""
    usage: Usage = field(default_factory=Usage)
    latency_s: float = 0.0
    served_model: str | None = None
    reported_cost_usd: float | None = None
    """Cost the access path itself reports (e.g. CLI ``total_cost_usd``), if any."""
    error: str | None = None
    raw: dict[str, Any] | None = None
    """Small provider-specific payload kept for debugging (never secrets)."""

    @property
    def ok(self) -> bool:
        return self.error is None and not self.refused


@dataclass(frozen=True)
class ProviderCapabilities:
    native_structured_output: bool
    native_tools: bool
    supports_temperature: bool
    reports_usage: bool = True


@runtime_checkable
class Provider(Protocol):
    kind: str
    """Provider kind, e.g. ``anthropic``, ``claude_cli``, ``replay``."""
    model: str

    def endpoint_identity(self) -> str:
        """Non-secret identity of where requests go (SPEC §11.4). Never includes keys or raw
        private URLs; use ``hash_endpoint`` for URLs."""
        ...

    def capabilities(self) -> ProviderCapabilities: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


@runtime_checkable
class CaseAwareProvider(Protocol):
    """A provider that answers from the case itself rather than from a prompt: the SPEC §14
    baselines (``always_*``, ``peer_recommender``, ``surface_probe``, plan floors, ``oracle``).

    The runner calls ``decide`` instead of rendering a prompt and calling ``complete``. The
    response is an ordinary ``ModelResponse`` whose ``parsed`` holds a task output dict, so the
    parser and every scorer treat baselines exactly like models.
    """

    kind: str
    model: str

    def endpoint_identity(self) -> str: ...

    async def decide(self, case: Case) -> ModelResponse: ...


def hash_endpoint(*parts: str | None) -> str:
    """Short stable hash of endpoint parts (base URL, reported revision, cache namespace)."""
    digest = hashlib.sha256("\x1f".join(p or "" for p in parts).encode()).hexdigest()
    return digest[:16]
