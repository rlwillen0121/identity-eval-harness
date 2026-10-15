"""Model providers (SPEC §11.1).

- ``base``: the ``Provider`` contract and request/response types.
- ``anthropic_api``: Anthropic Messages API (``anthropic``, ``anthropic_compatible``).
- ``openai_compat``: OpenAI Chat Completions and compatible servers (``openai_compatible``).
- ``claude_cli``, ``codex_cli``, ``grok_cli``, ``agy_cli``, ``cursor_cli``: subscription CLIs.
- ``replay``: recorded responses by cache key.
- ``factory``: ``ModelSpec`` and ``make_provider``.

Nothing is re-exported here so importing ``identitybench.providers.base`` doesn't pull in the
vendor SDKs.
"""
