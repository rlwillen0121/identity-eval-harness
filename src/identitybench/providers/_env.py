"""Which environment variable holds the API key for a configured endpoint (SPEC §11.1)."""

from __future__ import annotations

VENDOR_KEY_ENVS = frozenset({"OPENAI_API_KEY", "ANTHROPIC_API_KEY"})
"""Real vendor credentials: never sent to an endpoint unless configured for it explicitly."""

_URL_SUFFIXES = ("_BASE_URL", "_URL")


def paired_api_key_env(base_url_env: str) -> str | None:
    """The key variable conventionally paired with a base-URL variable.

    ``LOCAL_LLM_BASE_URL`` pairs with ``LOCAL_LLM_API_KEY`` (as in ``.env.example``) and
    ``GATEWAY_URL`` with ``GATEWAY_API_KEY``. A pairing that lands on a vendor key
    (``ANTHROPIC_BASE_URL`` -> ``ANTHROPIC_API_KEY`` for a self-hosted Messages server) counts as
    none, so a custom URL never receives a real vendor credential by naming accident. ``None``
    also when the name has neither suffix.
    """
    for suffix in _URL_SUFFIXES:
        prefix = base_url_env.removesuffix(suffix)
        if prefix and prefix != base_url_env:
            paired = f"{prefix}_API_KEY"
            return None if paired in VENDOR_KEY_ENVS else paired
    return None
