"""Model specs (one ``models:`` entry of an experiment config, SPEC §16) and the provider factory.

Endpoint and constructor fields that can't apply to a provider kind are rejected rather than
ignored: a ``base_url_env`` on a CLI provider, an ``effort`` for Cursor or a misspelled ``extra``
option is a config mistake that would otherwise silently run a different setup than the one
recorded in the manifest. ``extra`` values are type-checked, never coerced: YAML's
``reasoning_effort_param: "false"`` is a string, and ``bool("false")`` is True.

Generation settings a provider can't honour (``temperature`` and ``max_output_tokens`` for the
CLI kinds, ``temperature`` for models that reject it, ``effort`` for a server configured with
``reasoning_effort_param: false``) are accepted, so one experiment-wide
``params`` block can cover every model; the provider lists them in ``raw["ignored_params"]`` of
each response instead.

``extra`` holds constructor options, which never reach the request, with one exception:
``openai_compatible``'s ``provider_routing`` (OpenRouter's ``provider`` preference object) is
sent in every request body, and the provider folds it into its endpoint identity, so it is part
of every cache key like any other setting that can change the answer.

Where a provider would otherwise apply a hidden default effort (``codex_cli``), the spec's
generation parameters carry that default explicitly, so the cache key and manifest record the
effort the model actually ran at.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from identitybench.providers.anthropic_api import AnthropicProvider
from identitybench.providers.base import GenerationParams, Provider
from identitybench.providers.claude_cli import ClaudeCliProvider
from identitybench.providers.codex_cli import DEFAULT_EFFORT as CODEX_DEFAULT_EFFORT
from identitybench.providers.codex_cli import CodexCliProvider
from identitybench.providers.cursor_cli import CursorCliProvider
from identitybench.providers.grok_cli import GrokCliProvider
from identitybench.providers.openai_compat import (
    OpenAICompatProvider,
    provider_routing_problem,
)
from identitybench.providers.replay import ReplayProvider

ProviderKind = Literal[
    "anthropic",
    "anthropic_compatible",
    "openai_compatible",
    "claude_cli",
    "codex_cli",
    "grok_cli",
    "cursor_cli",
    "replay",
]
PROVIDER_KINDS: tuple[str, ...] = get_args(ProviderKind)

_API_KINDS = frozenset({"anthropic", "anthropic_compatible", "openai_compatible"})
_CLI_KINDS = frozenset({"claude_cli", "codex_cli", "grok_cli", "cursor_cli"})

_ALLOWED_EXTRA: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"max_retries"}),
    "anthropic_compatible": frozenset({"max_retries"}),
    "openai_compatible": frozenset(
        {"max_retries", "reasoning_effort_param", "max_tokens_param", "provider_routing"}
    ),
    "claude_cli": frozenset({"binary"}),
    "codex_cli": frozenset({"binary"}),
    "grok_cli": frozenset({"binary"}),
    "cursor_cli": frozenset({"binary"}),
    "replay": frozenset({"path"}),
}
"""Provider constructor options accepted in ``ModelSpec.extra``, per kind."""

_MAX_TOKENS_PARAMS = ("max_completion_tokens", "max_tokens")


def _extra_problem(name: str, value: object) -> str | None:
    """Why ``extra[name] = value`` is invalid, or None if it's fine."""
    if name == "max_retries":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return "must be a non-negative integer"
    elif name == "reasoning_effort_param":
        if not isinstance(value, bool):
            return "must be true or false"
    elif name == "max_tokens_param":
        if value not in _MAX_TOKENS_PARAMS:
            return f"must be one of {list(_MAX_TOKENS_PARAMS)}"
    elif name == "provider_routing":
        return provider_routing_problem(value)
    elif not isinstance(value, str) or not value.strip():  # binary, path
        return "must be a non-empty string"
    return None


DEFAULT_EFFORT: dict[str, str] = {"codex_cli": CODEX_DEFAULT_EFFORT}
"""Effort a provider kind runs at when the spec sets none (kinds not listed use the model's own
default, which the provider reports nothing about)."""


class ModelSpec(BaseModel):
    """One model configuration under test."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: ProviderKind
    model: str = Field(min_length=1)
    label: str | None = None
    """Display name in reports; defaults to ``provider:model``."""
    base_url_env: str | None = None
    """Environment variable holding the base URL (API kinds only; never a literal URL)."""
    api_key_env: str | None = None
    cache_namespace: str | None = None
    """Bump when a self-hosted deployment changes behind the same URL and name (SPEC §11.4)."""
    effort: str | None = None
    """Reasoning effort; None means the provider kind's default (``DEFAULT_EFFORT``)."""
    temperature: float | None = None
    max_output_tokens: int = Field(default=4096, gt=0)
    timeout_s: float | None = Field(default=None, gt=0)
    concurrency: int | None = Field(default=None, gt=0)
    """Per-model concurrency limit for the runner (None = the experiment default)."""
    extra: dict[str, Any] = Field(default_factory=dict)
    """Provider constructor options; see ``_ALLOWED_EXTRA`` for what each kind accepts."""

    @model_validator(mode="after")
    def _check_fields_apply(self) -> ModelSpec:
        kind = self.provider
        if kind not in _API_KINDS:
            for name in ("base_url_env", "api_key_env", "cache_namespace"):
                if getattr(self, name) is not None:
                    raise ValueError(f"{name} does not apply to provider {kind!r}")
        if kind == "anthropic" and self.base_url_env is not None:
            raise ValueError("provider 'anthropic' takes no base_url_env; use anthropic_compatible")
        if kind == "anthropic_compatible" and self.base_url_env is None:
            raise ValueError("provider 'anthropic_compatible' requires base_url_env")
        if kind == "cursor_cli" and self.effort is not None:
            raise ValueError("provider 'cursor_cli' has no effort setting")
        if kind == "replay" and "path" not in self.extra:
            raise ValueError("provider 'replay' requires extra.path (a responses.jsonl file)")
        unknown = set(self.extra) - _ALLOWED_EXTRA[kind]
        if unknown:
            allowed = sorted(_ALLOWED_EXTRA[kind]) or "none"
            raise ValueError(f"unknown extra options for {kind!r}: {sorted(unknown)} ({allowed=})")
        for name, value in self.extra.items():
            problem = _extra_problem(name, value)
            if problem is not None:
                raise ValueError(f"extra.{name} {problem} (got {value!r})")
        return self

    @property
    def display_label(self) -> str:
        return self.label or f"{self.provider}:{self.model}"

    @property
    def effective_effort(self) -> str | None:
        """The effort requests run at: the spec's own, else the kind's pinned default."""
        return self.effort if self.effort is not None else DEFAULT_EFFORT.get(self.provider)

    def generation_params(self) -> GenerationParams:
        """The per-request generation settings this spec implies."""
        return GenerationParams(
            max_output_tokens=self.max_output_tokens,
            temperature=self.temperature,
            effort=self.effective_effort,
        )


def make_provider(spec: ModelSpec) -> Provider:
    """Build the provider a spec describes. Generation settings travel per request
    (``spec.generation_params()``); CLI providers also take ``effort`` as their default."""
    kind = spec.provider
    extra = spec.extra
    # Only options the spec actually sets are passed, so each provider keeps its own defaults.
    timeout: dict[str, Any] = {"timeout_s": spec.timeout_s} if spec.timeout_s is not None else {}
    # ``extra`` values were type-checked by the spec's validator.
    binary: dict[str, Any] = {"binary": extra["binary"]} if "binary" in extra else {}
    retries: dict[str, Any] = (
        {"max_retries": extra["max_retries"]} if "max_retries" in extra else {}
    )

    if kind in ("anthropic", "anthropic_compatible"):
        # api_key_env stays None when unset: each provider then decides whether the vendor key
        # applies, which it never does for a self-hosted base URL.
        return AnthropicProvider(
            spec.model,
            api_key_env=spec.api_key_env,
            base_url_env=spec.base_url_env,
            cache_namespace=spec.cache_namespace,
            **timeout,
            **retries,
        )
    if kind == "openai_compatible":
        options: dict[str, Any] = {
            name: extra[name]
            for name in ("reasoning_effort_param", "max_tokens_param", "provider_routing")
            if name in extra
        }
        return OpenAICompatProvider(
            spec.model,
            api_key_env=spec.api_key_env,
            base_url_env=spec.base_url_env or "OPENAI_BASE_URL",
            cache_namespace=spec.cache_namespace,
            **timeout,
            **retries,
            **options,
        )
    if kind == "claude_cli":
        return ClaudeCliProvider(spec.model, effort=spec.effort, **timeout, **binary)
    if kind == "codex_cli":
        return CodexCliProvider(spec.model, effort=spec.effective_effort, **timeout, **binary)
    if kind == "grok_cli":
        return GrokCliProvider(spec.model, effort=spec.effort, **timeout, **binary)
    if kind == "cursor_cli":
        return CursorCliProvider(spec.model, **timeout, **binary)
    return ReplayProvider(extra["path"], model=spec.model)
