"""Make cost tests independent of the caller's environment.

``default_pricing()`` looks at ``$IDENTITYBENCH_PRICING`` and ``./pricing.yaml`` and caches the
result per process, so without this a stray env var or a different working directory changes
which prices the tests see.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from identitybench.runner.cost import PRICING_ENV, SOURCE_PRICING_PATH, default_pricing


@pytest.fixture(autouse=True)
def _repo_pricing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(PRICING_ENV, raising=False)
    monkeypatch.chdir(SOURCE_PRICING_PATH.parent)
    default_pricing.cache_clear()
    yield
    default_pricing.cache_clear()
