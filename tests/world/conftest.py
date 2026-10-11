"""Shared world fixtures: building a world is the expensive part, so build each once."""

from __future__ import annotations

import pytest

from identitybench.world.entities import World
from identitybench.world.generator import GeneratedWorld, generate_world
from identitybench.world.query import WorldIndex

SEED = 7
ENTERPRISE_SEED = 5


@pytest.fixture(scope="session")
def startup_generated() -> GeneratedWorld:
    return generate_world("startup", SEED)


@pytest.fixture(scope="session")
def midmarket_generated() -> GeneratedWorld:
    return generate_world("midmarket", SEED)


@pytest.fixture(scope="session")
def enterprise_generated() -> GeneratedWorld:
    """Only requested by tests marked ``slow`` (an enterprise build takes several seconds)."""
    return generate_world("enterprise", ENTERPRISE_SEED)


@pytest.fixture(scope="session")
def startup_world(startup_generated: GeneratedWorld) -> World:
    return startup_generated.world


@pytest.fixture(scope="session")
def midmarket_world(midmarket_generated: GeneratedWorld) -> World:
    return midmarket_generated.world


@pytest.fixture(scope="session")
def midmarket_index(midmarket_world: World) -> WorldIndex:
    return WorldIndex(midmarket_world)


@pytest.fixture(
    scope="session",
    params=["startup", "midmarket", pytest.param("enterprise", marks=pytest.mark.slow)],
)
def generated(request: pytest.FixtureRequest) -> GeneratedWorld:
    """Parametrized over every profile; the enterprise variants are marked ``slow``."""
    return request.getfixturevalue(f"{request.param}_generated")


@pytest.fixture(scope="session")
def world(generated: GeneratedWorld) -> World:
    return generated.world
