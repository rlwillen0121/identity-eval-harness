"""Deterministic stratified selection."""

from __future__ import annotations

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from identitybench.judge.sampling import allocate, stratified_pick, stratified_sample, target_size


def test_target_size() -> None:
    assert target_size(0.5, 10) == 5
    assert target_size(0.25, 10) == 3  # 2.5 rounds half up
    assert target_size(0.01, 10) == 1  # never empty from a fraction
    assert target_size(1.0, 0) == 0
    assert target_size(7, 5) == 5
    assert target_size(0, 5) == 0
    with pytest.raises(TypeError):
        target_size(True, 5)
    with pytest.raises(ValueError):
        target_size(-1, 5)
    with pytest.raises(ValueError):
        target_size(1.5, 5)


def test_proportional_gives_every_stratum_at_least_one() -> None:
    assert allocate([90, 9, 1], 10, "proportional") == [8, 1, 1]
    assert allocate([5, 5], 4, "proportional") == [2, 2]
    # Equal remainders (1.5 vs 3.5): the larger stratum gets the extra pick.
    assert allocate([0, 3, 7], 5, "proportional") == [0, 1, 4]


def test_proportional_with_fewer_picks_than_strata_takes_the_largest() -> None:
    assert allocate([1, 5, 3], 2, "proportional") == [0, 1, 1]


def test_balanced_water_fills() -> None:
    assert allocate([100, 100, 100], 30, "balanced") == [10, 10, 10]
    assert allocate([2, 100, 100], 30, "balanced") == [2, 14, 14]
    assert allocate([2, 3, 100], 30, "balanced") == [2, 3, 25]
    assert allocate([5, 5, 5], 4, "balanced") == [2, 1, 1]


def test_allocate_rejects_impossible_requests() -> None:
    with pytest.raises(ValueError):
        allocate([1, 2], 4, "balanced")
    with pytest.raises(ValueError):
        allocate([-1, 2], 1, "proportional")
    assert allocate([3, 4], 0, "proportional") == [0, 0]


@given(
    sizes=st.lists(st.integers(min_value=0, max_value=30), min_size=1, max_size=8),
    data=st.data(),
    how=st.sampled_from(["proportional", "balanced"]),
)
def test_allocations_sum_to_n_within_capacity(
    sizes: list[int], data: st.DataObject, how: str
) -> None:
    n = data.draw(st.integers(min_value=0, max_value=sum(sizes)))
    alloc = allocate(sizes, n, how)  # type: ignore[arg-type]
    assert sum(alloc) == n
    assert all(0 <= a <= s for a, s in zip(alloc, sizes, strict=True))
    if how == "proportional" and n >= sum(1 for s in sizes if s):
        assert all(a >= 1 for a, s in zip(alloc, sizes, strict=True) if s)


ITEMS = [f"{group}-{i:02d}" for group in "abc" for i in range(10)]


def _pick(items: list[str], n: int, seed: int = 0) -> list[str]:
    return stratified_pick(items, n, stratum=lambda x: x[0], order=lambda x: x, seed=seed, salt="t")


def test_pick_is_deterministic_and_independent_of_input_order() -> None:
    first = _pick(ITEMS, 9)
    shuffled = ITEMS[:]
    random.Random(3).shuffle(shuffled)
    assert sorted(_pick(shuffled, 9)) == sorted(first) == sorted(_pick(ITEMS, 9))
    assert [x[0] for x in first].count("a") == 3


def test_pick_returns_input_order_and_depends_on_seed() -> None:
    shuffled = ITEMS[:]
    random.Random(5).shuffle(shuffled)
    picked = _pick(shuffled, 12)
    assert picked == [x for x in shuffled if x in set(picked)]
    assert any(sorted(_pick(ITEMS, 9, seed=s)) != sorted(_pick(ITEMS, 9)) for s in range(1, 5))


def test_sample_weights_are_inverse_inclusion_probabilities() -> None:
    items = [f"a-{i:02d}" for i in range(18)] + ["b-00", "b-01"]
    picked = stratified_sample(
        items, 4, stratum=lambda x: x[0], order=lambda x: x, seed=0, salt="t"
    )
    weights = {x: w for x, w in picked}
    # 18 + 2 with 4 picks: proportional gives a 3.6 and b 0.4, the floor gives b one pick.
    assert sorted(weights.values()) == [2.0, 6.0, 6.0, 6.0]
    assert all(weights[x] == (6.0 if x[0] == "a" else 2.0) for x in weights)
    assert sum(weights.values()) == len(items)
    assert [x for x, _ in picked] == _pick(items, 4)


@given(
    sizes=st.lists(st.integers(min_value=1, max_value=12), min_size=1, max_size=5),
    data=st.data(),
)
def test_sample_weights_add_up_to_the_population(sizes: list[int], data: st.DataObject) -> None:
    """Once every stratum has a pick, the weights estimate the population size exactly."""
    items = [f"{chr(97 + g)}-{i:02d}" for g, size in enumerate(sizes) for i in range(size)]
    n = data.draw(st.integers(min_value=len(sizes), max_value=len(items)))
    picked = stratified_sample(
        items, n, stratum=lambda x: x[0], order=lambda x: x, seed=1, salt="h"
    )
    assert len(picked) == n
    assert sum(w for _, w in picked) == pytest.approx(len(items))
