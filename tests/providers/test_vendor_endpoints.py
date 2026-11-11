"""Vendor credentials require an explicit opt-in before following custom endpoints."""

import pytest
from pydantic import ValidationError

from identitybench.providers.factory import ModelSpec, make_provider


@pytest.mark.parametrize(
    ("kind", "variable", "host"),
    [
        ("anthropic", "ANTHROPIC_BASE_URL", "api.anthropic.com"),
        ("openai_compatible", "OPENAI_BASE_URL", "api.openai.com"),
    ],
)
@pytest.mark.parametrize("url", ["https://gateway.example/v1", "https://{host}.evil.example"])
def test_vendor_override_requires_opt_in(monkeypatch, kind, variable, host, url):
    monkeypatch.setenv(variable, url.format(host=host))
    with pytest.raises(ValueError, match="allow_custom_endpoint"):
        make_provider(ModelSpec(provider=kind, model="test"))


@pytest.mark.parametrize(
    ("kind", "variable", "host"),
    [
        ("anthropic", "ANTHROPIC_BASE_URL", "api.anthropic.com"),
        ("openai_compatible", "OPENAI_BASE_URL", "api.openai.com"),
    ],
)
def test_opt_in_is_visible_and_separates_endpoint_identity(monkeypatch, kind, variable, host):
    monkeypatch.setenv(variable, f"https://{host}")
    default = make_provider(ModelSpec(provider=kind, model="test"))
    spec = ModelSpec(provider=kind, model="test", extra={"allow_custom_endpoint": True})
    opted_in = make_provider(spec)
    assert opted_in.endpoint_identity().startswith("custom-endpoint-opt-in:")
    assert opted_in.endpoint_identity() != default.endpoint_identity()
    monkeypatch.setenv(variable, "https://gateway.example")
    assert make_provider(spec).endpoint_identity() != opted_in.endpoint_identity()


@pytest.mark.parametrize("kind", ["anthropic", "openai_compatible"])
@pytest.mark.parametrize("value", ["true", 1, None])
def test_opt_in_is_a_strict_boolean(kind, value):
    with pytest.raises(ValidationError, match="must be true or false"):
        ModelSpec(provider=kind, model="test", extra={"allow_custom_endpoint": value})


@pytest.mark.parametrize("kind", ["anthropic_compatible", "openai_compatible"])
def test_explicit_compatible_endpoint_does_not_need_opt_in(monkeypatch, kind):
    monkeypatch.setenv("TEST_BASE_URL", "https://gateway.example")
    provider = make_provider(ModelSpec(provider=kind, model="test", base_url_env="TEST_BASE_URL"))
    assert not provider.endpoint_identity().startswith("custom-endpoint-opt-in:")
