"""Seeded sampling helpers shared by the generator stages."""

from __future__ import annotations

import random
from collections.abc import Sequence


def weighted_sample[T](
    rng: random.Random, items: Sequence[T], weights: Sequence[float], k: int
) -> list[T]:
    """``k`` distinct items, weighted, without replacement, in input order.

    Efraimidis-Spirakis keys: an exact count drawn in proportion to the weights, which is how
    the generator turns "about 4% of people are terminated" into a count that never thins out
    by chance. Items with a non-positive weight are never picked.
    """
    keyed = sorted(
        ((rng.random() ** (1.0 / w), i) for i, w in enumerate(weights) if w > 0), reverse=True
    )
    return [items[i] for i in sorted(i for _, i in keyed[:k])]
