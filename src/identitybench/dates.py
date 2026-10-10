"""Date and duration semantics shared by the oracle, the JML simulator and the scorers.

SPEC §6.3:
- Dates only, no times. ``as_of`` is "today".
- A source is valid on day d iff ``granted_on <= d`` and (``expires_on`` is None or
  ``expires_on >= d``). Something that expires on D is valid through the end of D.
- A grant of N days (N a positive integer) runs from as_of through as_of + N - 1.
- Effective duration of an approval = min(requested, every applicable cap); None = standing (∞).
- A cap that must end on or before date E is E - as_of + 1 days.
- ``IMMEDIATE`` in a plan means effective on the as-of date.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

IMMEDIATE = "IMMEDIATE"


def is_valid_on(granted_on: date, expires_on: date | None, d: date) -> bool:
    """True iff a membership/assignment/control is valid (started and unexpired) on day ``d``."""
    if granted_on > d:
        return False
    return expires_on is None or expires_on >= d


def days_since(earlier: date, later: date) -> int:
    """Whole days from ``earlier`` to ``later`` (``later - earlier``)."""
    return (later - earlier).days


def cap_ending_on(end: date, as_of: date) -> int:
    """Duration cap (days) for a grant that must end on or before ``end``: ``end - as_of + 1``."""
    return (end - as_of).days + 1


def effective_duration(requested: int | None, caps: Iterable[int | None]) -> int | None:
    """min(requested, caps...); None means unbounded. Returns None only if all are None."""
    values = [v for v in [requested, *caps] if v is not None]
    return min(values) if values else None


def grant_end(as_of: date, duration_days: int | None) -> date | None:
    """Last valid day of a grant of ``duration_days`` starting on ``as_of`` (None = standing)."""
    if duration_days is None:
        return None
    return as_of + timedelta(days=duration_days - 1)


def parse_effective(value: str, as_of: date) -> date:
    """Parse a plan operation's ``effective`` field (``IMMEDIATE`` or ISO date)."""
    if value.strip().upper() == IMMEDIATE:
        return as_of
    return date.fromisoformat(value.strip())
