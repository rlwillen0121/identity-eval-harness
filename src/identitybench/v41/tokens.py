"""Offline-compatible token counting for v4.1 generation budgets."""

from __future__ import annotations

from collections.abc import Callable

_TOKENIZER_FN: Callable[[str], int] | None = None


def _resolve_tokenizer() -> Callable[[str], int]:
    global _TOKENIZER_FN
    if _TOKENIZER_FN is not None:
        return _TOKENIZER_FN
    try:
        import tiktoken  # pyright: ignore[reportMissingImports]

        enc = tiktoken.get_encoding("o200k_base")

        def _encode_len(text: str) -> int:
            if not text:
                return 0
            return len(enc.encode(text))

        _TOKENIZER_FN = _encode_len
    except Exception:

        def _fallback_len(text: str) -> int:
            if not text:
                return 0
            return max(1, (len(text.encode("utf-8")) + 3) // 4)

        _TOKENIZER_FN = _fallback_len
    return _TOKENIZER_FN


def count_o200k_tokens(text: str) -> int:
    """Count tokens using `o200k_base` (with deterministic offline UTF-8 fallback)."""
    return _resolve_tokenizer()(text)
