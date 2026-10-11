from __future__ import annotations

import random
import re

import pytest

from identitybench.world.entities import TicketType
from identitybench.world.ids import IdFactory, IdSpaceExhausted

PATTERNS = {
    "identity": r"u_[1-9]\d{4}",
    "membership": r"rm_[1-9]\d{4}",
    "account": r"acct_[1-9]\d{4}",
    "assignment": r"as_[1-9]\d{4}",
    "usage": r"use_[1-9]\d{4}",
    "request": r"req_[1-9]\d{4}",
    "control": r"mc_\d{3,4}",
}


@pytest.mark.parametrize("kind", sorted(PATTERNS))
def test_formats(kind: str) -> None:
    ids = IdFactory(random.Random(1))
    for _ in range(200):
        assert re.fullmatch(PATTERNS[kind], getattr(ids, kind)())


def test_control_ids_span_three_and_four_digits() -> None:
    ids = IdFactory(random.Random(3))
    lengths = {len(ids.control()) - len("mc_") for _ in range(500)}
    assert lengths == {3, 4}


@pytest.mark.parametrize(
    ("ticket_type", "prefix"),
    [(TicketType.incident, "INC-"), (TicketType.change, "CHG-"), (TicketType.project, "PRJ-")],
)
def test_ticket_ids(ticket_type: TicketType, prefix: str) -> None:
    ids = IdFactory(random.Random(2))
    assert re.fullmatch(rf"{prefix}[1-9]\d{{4}}", ids.ticket(ticket_type))


def test_unique_across_prefixes_and_draws() -> None:
    ids = IdFactory(random.Random(5))
    drawn = [ids.identity() for _ in range(5000)] + [ids.usage() for _ in range(5000)]
    assert len(set(drawn)) == len(drawn) == len(ids)


def test_deterministic() -> None:
    a = IdFactory(random.Random(9))
    b = IdFactory(random.Random(9))
    assert [a.identity() for _ in range(50)] == [b.identity() for _ in range(50)]
    assert IdFactory(random.Random(10)).identity() != IdFactory(random.Random(9)).identity()


def test_random_not_sequential() -> None:
    ids = IdFactory(random.Random(4))
    numbers = [int(ids.identity()[2:]) for _ in range(100)]
    assert numbers != sorted(numbers)
    assert max(numbers) - min(numbers) > 50_000


def test_reserve() -> None:
    ids = IdFactory(random.Random(0), widths={"u_": 1})
    for n in range(1, 8):
        ids.reserve(f"u_{n}")
    with pytest.raises(ValueError, match="duplicate"):
        ids.reserve("u_3")
    assert "u_3" in ids
    ids.reserve("app_ledger")
    with pytest.raises(ValueError):
        ids.reserve("app_ledger")


def test_reserved_ids_are_never_issued() -> None:
    ids = IdFactory(random.Random(0), widths={"rm_": 2})
    reserved = {f"rm_{n}" for n in range(10, 60)}
    ids.reserve_all(sorted(reserved))
    issued = {ids.membership() for _ in range(20)}
    assert not issued & reserved


def test_exhaustion_is_reported() -> None:
    ids = IdFactory(random.Random(0), widths={"as_": 1})
    for _ in range(7):
        ids.assignment()
    with pytest.raises(IdSpaceExhausted):
        ids.assignment()


def test_generic_prefix_and_width() -> None:
    ids = IdFactory(random.Random(0))
    assert re.fullmatch(r"rv_[1-9]\d{3}", ids.new("rv_", 4))
    assert re.fullmatch(r"zz_[1-9]\d{4}", ids.new("zz_"))
    with pytest.raises(ValueError):
        ids.new("zz_", 0)
