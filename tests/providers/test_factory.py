from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from identitybench.providers.anthropic_api import AnthropicProvider
from identitybench.providers.base import GenerationParams, Message, ModelRequest, Provider, Role
from identitybench.providers.claude_cli import ClaudeCliProvider
from identitybench.providers.codex_cli import CodexCliProvider
from identitybench.providers.cursor_cli import CursorCliProvider
from identitybench.providers.factory import PROVIDER_KINDS, ModelSpec, make_provider
from identitybench.providers.grok_cli import GrokCliProvider
from identitybench.providers.openai_compat import OpenAICompatProvider
from identitybench.providers.replay import ReplayProvider
from identitybench.runner.cache import key_for_request


@pytest.fixture
def replay_file(tmp_path) -> str:
    path = tmp_path / "responses.jsonl"
    path.write_text(json.dumps({"key": "k", "response": {"text": "x"}}) + "\n", encoding="utf-8")
    return str(path)


def test_provider_kinds() -> None:
    assert PROVIDER_KINDS == (
        "anthropic",
        "anthropic_compatible",
        "openai_compatible",
        "claude_cli",
        "codex_cli",
        "grok_cli",
        "cursor_cli",
        "replay",
    )


def test_spec_defaults_label_and_generation_params() -> None:
    spec = ModelSpec(provider="anthropic", model="claude-opus-5-5")
    assert spec.max_output_tokens == 4096
    assert spec.display_label == "anthropic:claude-opus-5-5"
    assert spec.generation_params() == GenerationParams()
    labelled = ModelSpec(
        provider="anthropic",
        model="claude-opus-5-5",
        label="Opus 5.5 (high)",
        effort="high",
        temperature=0.0,
        max_output_tokens=2048,
    )
    assert labelled.display_label == "Opus 5.5 (high)"
    assert labelled.generation_params() == GenerationParams(
        max_output_tokens=2048, temperature=0.0, effort="high"
    )


def test_spec_from_experiment_yaml_shape() -> None:
    spec = ModelSpec.model_validate(
        {
            "provider": "openai_compatible",
            "model": "<served-model-name>",
            "base_url_env": "LOCAL_LLM_BASE_URL",
            "cache_namespace": "deploy-1",
        }
    )
    assert spec.base_url_env == "LOCAL_LLM_BASE_URL"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"provider": "gemini", "model": "m"}, "provider"),
        ({"provider": "anthropic", "model": ""}, "model"),
        ({"provider": "anthropic", "model": "m", "typo_field": 1}, "typo_field"),
        ({"provider": "anthropic", "model": "m", "max_output_tokens": 0}, "max_output_tokens"),
        ({"provider": "anthropic", "model": "m", "base_url_env": "X"}, "anthropic_compatible"),
        ({"provider": "anthropic_compatible", "model": "m"}, "requires base_url_env"),
        ({"provider": "claude_cli", "model": "m", "base_url_env": "X"}, "does not apply"),
        ({"provider": "codex_cli", "model": "m", "cache_namespace": "x"}, "does not apply"),
        ({"provider": "cursor_cli", "model": "auto", "effort": "high"}, "no effort"),
        ({"provider": "replay", "model": "m"}, "extra.path"),
        ({"provider": "grok_cli", "model": "m", "extra": {"binray": "x"}}, "binray"),
        # extra values are type-checked, never coerced.
        (
            {
                "provider": "openai_compatible",
                "model": "m",
                "extra": {"reasoning_effort_param": "false"},
            },
            "reasoning_effort_param must be true or false",
        ),
        (
            {"provider": "openai_compatible", "model": "m", "extra": {"max_tokens_param": "max"}},
            "max_tokens_param must be one of",
        ),
        ({"provider": "anthropic", "model": "m", "extra": {"max_retries": "3"}}, "non-negative"),
        ({"provider": "anthropic", "model": "m", "extra": {"max_retries": -1}}, "non-negative"),
        ({"provider": "anthropic", "model": "m", "extra": {"max_retries": True}}, "non-negative"),
        ({"provider": "grok_cli", "model": "m", "extra": {"binary": 7}}, "non-empty string"),
        ({"provider": "claude_cli", "model": "m", "extra": {"binary": " "}}, "non-empty string"),
        ({"provider": "replay", "model": "m", "extra": {"path": None}}, "non-empty string"),
        ({"provider": "anthropic", "model": "m", "concurrency": 0}, "concurrency"),
        (
            {"provider": "openai_compatible", "model": "m", "extra": {"provider_routing": []}},
            "provider_routing must be a non-empty mapping",
        ),
        (
            {
                "provider": "openai_compatible",
                "model": "m",
                "extra": {"provider_routing": {"allow_fallbacks": "false"}},
            },
            "provider_routing allow_fallbacks must be true or false",
        ),
        # Routing is OpenRouter's request field; no other kind sends it.
        (
            {"provider": "anthropic", "model": "m", "extra": {"provider_routing": {"order": []}}},
            "unknown extra options",
        ),
    ],
)
def test_invalid_specs_are_rejected(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ModelSpec.model_validate(data)


def test_specs_are_immutable() -> None:
    spec = ModelSpec(provider="anthropic", model="m")
    with pytest.raises(ValidationError):
        spec.model = "other"  # type: ignore[misc]


def test_make_anthropic_providers(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_URL", "http://localhost:8000")
    native = make_provider(ModelSpec(provider="anthropic", model="claude-opus-5-5"))
    assert isinstance(native, AnthropicProvider)
    assert native.kind == "anthropic"
    local = make_provider(
        ModelSpec(
            provider="anthropic_compatible",
            model="local-model",
            base_url_env="LOCAL_URL",
            api_key_env="LOCAL_KEY",
            cache_namespace="d1",
            timeout_s=30,
            extra={"max_retries": 1},
        )
    )
    assert isinstance(local, AnthropicProvider)
    assert local.kind == "anthropic_compatible"
    assert local._api_key_env == "LOCAL_KEY"
    assert local._timeout_s == 30
    assert local._max_retries == 1
    assert local._cache_namespace == "d1"


def test_make_openai_provider(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
    p = make_provider(
        ModelSpec(
            provider="openai_compatible",
            model="local-model",
            base_url_env="LOCAL_LLM_BASE_URL",
            extra={"reasoning_effort_param": False, "max_tokens_param": "max_tokens"},
        )
    )
    assert isinstance(p, OpenAICompatProvider)
    assert p._base_url == "http://localhost:8000/v1"
    assert p._reasoning_effort_param is False
    assert p._max_tokens_param == "max_tokens"
    default = make_provider(ModelSpec(provider="openai_compatible", model="gpt-test"))
    assert isinstance(default, OpenAICompatProvider)
    assert default._api_key_env == "OPENAI_API_KEY"
    assert default._base_url_env == "OPENAI_BASE_URL"


ROUTING: dict[str, Any] = {
    "order": ["Baidu", "Alibaba"],
    "allow_fallbacks": False,
    "require_parameters": True,
}


def openrouter_spec(**extra: Any) -> ModelSpec:
    return ModelSpec(
        provider="openai_compatible",
        model="deepseek/deepseek-v4-pro-0813",
        base_url_env="OPENROUTER_BASE_URL",
        extra=extra,
    )


def test_routing_preference_reaches_the_request_and_the_cache_key(monkeypatch) -> None:
    """A different routing can change which host answers, so it must change the cache key."""
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    routed = make_provider(openrouter_spec(provider_routing=ROUTING))
    plain = make_provider(openrouter_spec())
    assert isinstance(routed, OpenAICompatProvider) and isinstance(plain, OpenAICompatProvider)
    request = ModelRequest(system="s", messages=(Message(Role.user, "u"),))
    assert routed.request_params(request)["extra_body"] == {"provider": ROUTING}
    assert "extra_body" not in plain.request_params(request)

    def key(p: Provider) -> str:
        return key_for_request(p.kind, p.model, p.endpoint_identity(), request, 0)

    assert routed.endpoint_identity() != plain.endpoint_identity()
    assert key(routed) != key(plain)


SPEC_16_MODELS: list[dict[str, Any]] = [
    {
        "provider": "openai_compatible",
        "model": "<openai-model-id>",
        "base_url_env": "OPENAI_BASE_URL",
    },
    {
        "provider": "openai_compatible",
        "model": "<served-model-name>",
        "base_url_env": "LOCAL_LLM_BASE_URL",
        "cache_namespace": "deploy-1",
    },
]
"""The ``models:`` entries of the SPEC §16 example config that use openai_compatible."""


def test_spec_16_self_hosted_entry_never_receives_a_real_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai-key-123")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.delenv("LOCAL_LLM_API_KEY", raising=False)
    vendor, local = (make_provider(ModelSpec.model_validate(m)) for m in SPEC_16_MODELS)
    assert isinstance(vendor, OpenAICompatProvider)
    assert isinstance(local, OpenAICompatProvider)
    assert vendor._get_client().api_key == "sk-real-openai-key-123"
    local_client = local._get_client()
    assert str(local_client.base_url).startswith("http://localhost:8000/v1")
    assert local_client.api_key == "not-set"


def test_spec_16_self_hosted_entry_uses_the_env_example_key(monkeypatch) -> None:
    """``.env.example`` documents LOCAL_LLM_API_KEY next to LOCAL_LLM_BASE_URL."""
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "local-key-123")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai-key-123")
    local = make_provider(ModelSpec.model_validate(SPEC_16_MODELS[1]))
    assert isinstance(local, OpenAICompatProvider)
    assert local._get_client().api_key == "local-key-123"


def test_anthropic_compatible_without_api_key_env_never_receives_a_real_key(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_URL", "http://localhost:8000")
    monkeypatch.delenv("LOCAL_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "real-oauth-token")
    p = make_provider(
        ModelSpec(provider="anthropic_compatible", model="local-model", base_url_env="LOCAL_URL")
    )
    assert isinstance(p, AnthropicProvider)
    client = p._get_client()
    assert client.api_key == "not-set"
    assert client.auth_token is None
    native = make_provider(ModelSpec(provider="anthropic", model="claude-opus-5-5"))
    assert isinstance(native, AnthropicProvider)
    assert native._api_key_env == "ANTHROPIC_API_KEY"


def test_codex_default_effort_is_explicit_in_the_generation_params() -> None:
    spec = ModelSpec(provider="codex_cli", model="gpt-x")
    assert spec.effective_effort == "medium"
    assert spec.generation_params().effort == "medium"
    provider = make_provider(spec)
    assert isinstance(provider, CodexCliProvider)
    assert provider.effort == "medium"
    high = ModelSpec(provider="codex_cli", model="gpt-x", effort="high")
    assert high.generation_params().effort == "high"
    # Kinds without a pinned default leave effort to the model.
    assert ModelSpec(provider="claude_cli", model="m").generation_params().effort is None


def test_make_cli_providers() -> None:
    claude = make_provider(
        ModelSpec(provider="claude_cli", model="claude-haiku-5-5", effort="low", timeout_s=60)
    )
    assert isinstance(claude, ClaudeCliProvider)
    assert (claude.effort, claude.timeout_s, claude.binary) == ("low", 60, "claude")

    codex = make_provider(ModelSpec(provider="codex_cli", model="gpt-x"))
    assert isinstance(codex, CodexCliProvider)
    assert (codex.effort, codex.timeout_s) == ("medium", 900)

    grok = make_provider(
        ModelSpec(provider="grok_cli", model="grok-4.7", extra={"binary": "/opt/grok"})
    )
    assert isinstance(grok, GrokCliProvider)
    assert grok.binary == "/opt/grok"

    cursor = make_provider(ModelSpec(provider="cursor_cli", model="auto"))
    assert isinstance(cursor, CursorCliProvider)
    assert cursor.model == "auto"


def test_make_replay_provider(replay_file: str) -> None:
    p = make_provider(
        ModelSpec(provider="replay", model="claude-opus-5-5", extra={"path": replay_file})
    )
    assert isinstance(p, ReplayProvider)
    assert p.model == "claude-opus-5-5"
    assert len(p) == 1


@pytest.mark.parametrize("kind", PROVIDER_KINDS)
def test_every_kind_builds_a_provider(kind: str, replay_file: str, monkeypatch) -> None:
    monkeypatch.setenv("SOME_URL", "http://localhost:1")
    data: dict[str, Any] = {"provider": kind, "model": "m"}
    if kind == "anthropic_compatible":
        data["base_url_env"] = "SOME_URL"
    if kind == "replay":
        data["extra"] = {"path": replay_file}
    p = make_provider(ModelSpec.model_validate(data))
    assert isinstance(p, Provider)
    assert p.kind == kind
