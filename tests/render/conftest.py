"""Shared fixtures: the hand-built cases and cases sliced from two generated worlds."""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType

import pytest

from identitybench.render import fewshot as fewshot_module
from identitybench.schemas.case import Case
from identitybench.schemas.enums import Task
from identitybench.world.generator import build_world
from tests.render.fixtures import all_cases
from tests.render.world_cases import jml_cases, request_cases, review_cases


@pytest.fixture(scope="session")
def hand_cases() -> list[Case]:
    return all_cases()


@pytest.fixture(scope="session")
def world_cases() -> list[Case]:
    """Requests, campaigns and JML plans (leave starts, movers, leavers) from a startup and a
    midmarket world."""
    cases: list[Case] = []
    for profile, seed in (("startup", 11), ("midmarket", 5)):
        world = build_world(profile, seed)
        cases += request_cases(world, 25, seed)
        cases += review_cases(world, 2, seed)
        cases += jml_cases(world, 3, seed)
    return cases


SetCap = Callable[[Task, int], None]
"""``set_fewshot_cap(task, chars)``: patch one task's few-shot cap for the test."""


@pytest.fixture
def set_fewshot_cap(monkeypatch: pytest.MonkeyPatch) -> SetCap:
    """Patches ``FEWSHOT_MAX_CHARS`` for one task at a time, leaving the other tasks' caps as
    they are (the mapping is read-only, so the test swaps in an edited copy)."""

    def set_cap(task: Task, chars: int) -> None:
        caps = dict(fewshot_module.FEWSHOT_MAX_CHARS)
        caps[task] = chars
        monkeypatch.setattr(fewshot_module, "FEWSHOT_MAX_CHARS", MappingProxyType(caps))

    return set_cap
