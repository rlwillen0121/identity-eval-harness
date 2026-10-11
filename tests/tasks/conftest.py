"""Session-scoped worlds for every task-builder test module.

Generating a world is deterministic but not free (midmarket takes about a second), so each
profile is built once per test session and shared. Builders must treat the worlds as read-only:
``World`` isn't frozen, and a test that mutated one would leak into every other test.

Peer stats need canonical titles, which only a freshly generated world has, so these fixtures
come straight from ``build_world`` rather than from a JSON round trip.
"""

from __future__ import annotations

import pytest

from identitybench.world.entities import World
from identitybench.world.generator import build_world

SEED = 7


@pytest.fixture(scope="session")
def midmarket_world() -> World:
    """``build_world("midmarket", 7)``: about 2,000 identities, nested roles, SOX apps."""
    return build_world("midmarket", SEED)


@pytest.fixture(scope="session")
def startup_world() -> World:
    """``build_world("startup", 7)``: about 150 identities, flat roles."""
    return build_world("startup", SEED)
