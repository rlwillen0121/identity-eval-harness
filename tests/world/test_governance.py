"""Unit tests for control expiry planning (``world.governance``; SPEC §7.4 MIT-001/MIT-002).

The world tests check outcomes over whole worlds; these pin the allocation rules a sweep can
miss because the worlds that need them are rare.
"""

from __future__ import annotations

import random

from identitybench.world.governance import LIVE_EXPIRY_MIX, _expiry_buckets


def test_two_live_controls_keep_an_expired_one() -> None:
    """With two controlled live conflicts, 25% rounds to no expired control; the floor keeps
    one (the MIT-001 lookalike) and the renewal slot (MIT-002) still fits beside it."""
    for seed in range(20):
        assert sorted(_expiry_buckets([True, True], LIVE_EXPIRY_MIX, random.Random(seed), 1)) == [
            "expired",
            "renewal",
        ]
        assert sorted(_expiry_buckets([True, True], LIVE_EXPIRY_MIX, random.Random(seed))) == [
            "renewal",
            "valid",
        ]


def test_a_single_live_control_is_never_forced_to_expire() -> None:
    """One control can't be both the expired lookalike and the renewal case, so the floor
    leaves it to the shares (which give neither)."""
    assert _expiry_buckets([True], LIVE_EXPIRY_MIX, random.Random(0), 1) == ["valid"]


def test_expired_slots_move_to_pairs_with_room() -> None:
    """An expired slot that lands on a conflict too recent for an expired control swaps with a
    slot of a pair that has room; the renewal count never changes."""
    for seed in range(20):
        buckets = _expiry_buckets([False, True, False, False], LIVE_EXPIRY_MIX, random.Random(seed))
        assert buckets[1] == "expired"
        assert buckets.count("expired") == 1
        assert buckets.count("renewal") == 1


def test_expired_slots_fall_back_to_valid_without_room() -> None:
    for seed in range(20):
        buckets = _expiry_buckets([False, False], LIVE_EXPIRY_MIX, random.Random(seed), 1)
        assert sorted(buckets) == ["renewal", "valid"]
