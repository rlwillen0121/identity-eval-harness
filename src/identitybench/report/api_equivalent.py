"""Pricing conventions shared by v4.1 study reports."""

from collections.abc import Mapping
from types import MappingProxyType

from identitybench.pricing import CacheTtl

CACHE_TTL: Mapping[str, CacheTtl] = MappingProxyType({"claude_cli": "1h"})


SELF_HOSTED: frozenset[str] = frozenset({"qwen3.8-27b-exl3-4bit", "qwen3.8-flash-next"})
