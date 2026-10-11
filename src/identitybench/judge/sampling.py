"""Deterministic stratified selection for judge campaigns and labeling sheets.

Two allocation policies, because the two uses want different things:

- ``proportional`` (judge sampling): each stratum's share of the sample follows its share of
  the population, but every non-empty stratum gets at least one pick when the sample is large
  enough. Without that floor, the rare incorrect decisions (the ones whose persuasive rationales
  matter most, SPEC §13.2) could round down to nothing.
- ``balanced`` (labeling sheets): strata get equal shares, capped by their size, with the excess
  passed on to strata that still have room (water-filling). SPEC §13.3 stratifies the 200 labels
  by model, decision and correctness so validation covers each cell, not the majority one.

Within a stratum, members are put in a fixed order and drawn with ``random.Random`` seeded from
``(salt, seed, stratum)``, so a selection is reproducible, independent of input order and
independent of the other strata. Selections come back in input order, and
``stratified_sample`` also returns each pick's inverse inclusion probability, so means over a
judge sample can be weighted back to the population it was drawn from.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from typing import Literal

from identitybench.runner.dataset import SampleSpec

__all__ = [
    "Allocation",
    "allocate",
    "stratified_pick",
    "stratified_sample",
    "target_size",
]

Allocation = Literal["proportional", "balanced"]


def target_size(spec: SampleSpec, population: int) -> int:
    """How many units ``spec`` selects from ``population``.

    Same semantics as experiment sampling (``runner.dataset``): a float in (0, 1] is a fraction
    (rounded half up, never 0 for a non-empty population), an int >= 0 a count capped at the
    population.
    """
    if isinstance(spec, bool):
        raise TypeError("sample spec must be a fraction or a count, not a bool")
    if isinstance(spec, int):
        if spec < 0:
            raise ValueError(f"sample count must be >= 0, got {spec}")
        return min(spec, population)
    if not 0.0 < spec <= 1.0:
        raise ValueError(f"sample fraction must be in (0, 1], got {spec}")
    if population == 0:
        return 0
    return max(1, min(population, math.floor(spec * population + 0.5)))


def _proportional(sizes: Sequence[int], n: int) -> list[int]:
    nonempty = [i for i, size in enumerate(sizes) if size > 0]
    if n < len(nonempty):
        # Too few picks for one per stratum: the largest strata get one each.
        chosen = sorted(nonempty, key=lambda i: (-sizes[i], i))[:n]
        return [1 if i in chosen else 0 for i in range(len(sizes))]
    total = sum(sizes)
    quotas = [n * size / total for size in sizes]
    alloc = [
        min(size, max(1, math.floor(q))) if size else 0
        for size, q in zip(sizes, quotas, strict=True)
    ]
    while sum(alloc) > n:
        # Shrink the stratum most over its quota (never below the floor of one).
        i = max(
            (i for i in nonempty if alloc[i] > 1),
            key=lambda i: (alloc[i] - quotas[i], -i),
        )
        alloc[i] -= 1
    while sum(alloc) < n:
        # Grow the stratum most under its quota that still has members left.
        i = max(
            (i for i in nonempty if alloc[i] < sizes[i]),
            key=lambda i: (quotas[i] - alloc[i], sizes[i], -i),
        )
        alloc[i] += 1
    return alloc


def _balanced(sizes: Sequence[int], n: int) -> list[int]:
    alloc = [0] * len(sizes)
    remaining = n
    while remaining > 0:
        open_strata = [i for i, size in enumerate(sizes) if alloc[i] < size]
        share = remaining // len(open_strata)
        if share == 0:
            # Fewer picks left than open strata: the strata with the most room get one each.
            by_room = sorted(open_strata, key=lambda i: (alloc[i] - sizes[i], -sizes[i], i))
            for i in by_room[:remaining]:
                alloc[i] += 1
            break
        for i in open_strata:
            take = min(share, sizes[i] - alloc[i])
            alloc[i] += take
            remaining -= take
    return alloc


def allocate(sizes: Sequence[int], n: int, how: Allocation) -> list[int]:
    """Split ``n`` picks across strata of ``sizes`` (see the module docstring).

    ``n`` must not exceed the total size. Leftover picks go to the stratum furthest below its
    quota (proportional) or with the most room left (balanced); ties go to the larger stratum,
    then the earlier one. The result depends only on ``sizes`` (in order) and ``n``, so callers
    pass strata in a canonical (sorted) order.
    """
    if any(size < 0 for size in sizes):
        raise ValueError("stratum sizes must be non-negative")
    if not 0 <= n <= sum(sizes):
        raise ValueError(f"cannot pick {n} from {sum(sizes)} units")
    if n == 0:
        return [0] * len(sizes)
    return _proportional(sizes, n) if how == "proportional" else _balanced(sizes, n)


def stratified_sample[T](
    items: Sequence[T],
    n: int,
    *,
    stratum: Callable[[T], str],
    order: Callable[[T], str],
    seed: int,
    salt: str,
    how: Allocation = "proportional",
) -> list[tuple[T, float]]:
    """``n`` of ``items`` with their weights, stratified by ``stratum``, in input order.

    An item's weight is its stratum's size over the picks the stratum got: the inverse of its
    inclusion probability. Means over a sample must use it, because the floor of one pick per
    stratum (and balanced allocation) over-represents small strata; an unweighted mean would
    drift toward them. ``order`` gives each item a unique, stable sort key (an ID), which fixes
    the population a stratum's generator draws from whatever order ``items`` arrive in.
    """
    groups: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        groups.setdefault(stratum(item), []).append(index)
    keys = sorted(groups)
    alloc = allocate([len(groups[key]) for key in keys], n, how)
    weights: dict[int, float] = {}
    for key, k in zip(keys, alloc, strict=True):
        members = sorted(groups[key], key=lambda i: order(items[i]))
        for index in random.Random(f"{salt}|{seed}|{key}").sample(members, k):
            weights[index] = len(members) / k
    return [(item, weights[index]) for index, item in enumerate(items) if index in weights]


def stratified_pick[T](
    items: Sequence[T],
    n: int,
    *,
    stratum: Callable[[T], str],
    order: Callable[[T], str],
    seed: int,
    salt: str,
    how: Allocation = "proportional",
) -> list[T]:
    """``stratified_sample`` without the weights, for selections no mean is taken over (the
    labeling sheets, which aim to cover every stratum, not to estimate a population mean)."""
    picked = stratified_sample(
        items, n, stratum=stratum, order=order, seed=seed, salt=salt, how=how
    )
    return [item for item, _ in picked]
