"""Number formatting shared by every report surface (Markdown, HTML, README, chart labels).

One place decides how a number reads, so the README table, the report and the charts never
disagree on rounding. The conventions:

- **n/a** for an undefined value (an empty denominator, SPEC §12.2), never 0 or a dash.
- Rates as percentages with one decimal; intervals in parentheses with an en dash,
  ``12.3% (8.1–16.9)``. Parentheses, unlike brackets, mean nothing to Markdown, so a README
  table stays readable as plain text.
- A Clopper–Pearson bound is spelled out as a one-sided bound with its counts,
  ``(CP95 ≤ 4.1%, 0/73)``, so it can't be mistaken for a two-sided interval. A cell has no room
  for the rest of what SPEC §12.8 says every bound states (its estimand, units and failure
  event), so every table that shows bounds is followed by :func:`bound_notes`, which spells
  them out from the scorer's ``Bound`` records (operating points, whose records carry no
  estimand, use ``common.operating_bound_note`` in the same :func:`bound_note` form). A target
  is printed exactly (:func:`pct_exact`: a 1.25% target never reads 1.2%), and a bound printed
  beside it gets enough decimals to show its side of it (:func:`pct_against`).
- A defined value with no interval (fewer than two resampled units carry its denominator)
  says so and names its n, ``0.0% (no interval: 1 request twin pair)``, so a bare number never
  reads as exact (SPEC §1: every number ships with its n).
- Ratios (NRC) with three decimals; per-event means with two.
- Costs in dollars (:func:`usd`); an API-equivalent cost, which is a list price and not a
  charge, carries :data:`EQUIVALENT` (``◇$1.25``), and a CLI's own estimate ``≈``.

Every function is pure and locale-independent, so outputs are byte-identical across machines.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from identitybench.scoring.metrics import Bound, Metric

__all__ = [
    "EQUIVALENT",
    "NA",
    "bound",
    "bound_note",
    "bound_notes",
    "collapsed",
    "count",
    "interval",
    "metric",
    "number",
    "pct",
    "pct_against",
    "pct_exact",
    "plural",
    "pp",
    "seconds",
    "spelled",
    "units",
    "usd",
    "words",
]

NA = "n/a"

EQUIVALENT = "◇"
"""Marks an API-equivalent cost (the tokens a subscription CLI or an unmetered endpoint recorded, at
list price, ``report.api_equivalent``) wherever one is printed, as ``≈`` marks a CLI's own estimate;
the cost figure draws such a point as a diamond."""


def pct(value: float | None, digits: int = 1) -> str:
    """``0.123`` -> ``12.3%``."""
    return NA if value is None else f"{value * 100:.{digits}f}%"


def _exact_places(value: float, digits: int, max_digits: int) -> int:
    """The fewest decimals, from ``digits`` up to ``max_digits``, at which ``value`` as a
    percentage reads back as itself (to float noise: 0.07 * 100 is 7.000000000000001)."""
    scaled = value * 100
    for places in range(digits, max_digits):
        if math.isclose(float(f"{scaled:.{places}f}"), scaled, rel_tol=1e-9, abs_tol=1e-12):
            return places
    return max_digits


def pct_exact(value: float, digits: int = 1, max_digits: int = 6) -> str:
    """``value`` as a percentage with the fewest decimals (at least ``digits``) that print it
    exactly: ``0.02`` -> ``2.0%``, ``0.0125`` -> ``1.25%``. For a target a label compares a
    number with (``ScoreSettings.war_target`` is a free float): :func:`pct` would print a 1.25%
    target as 1.2% (half-even), and a 1.24% bound "meeting" it would then read as above it."""
    return pct(value, _exact_places(value, digits, max_digits))


def pct_against(value: float, reference: float, digits: int = 1, max_digits: int = 6) -> str:
    """``value`` as a percentage with the fewest decimals (at least ``digits``, and at least
    as many as :func:`pct_exact` prints ``reference`` with) at which it no longer reads as
    equal to ``reference`` at the same precision. A label that states how a number compares
    with a target needs this: at one decimal a 2.004% bound prints as 2.0% next to "above the
    2.0% target", and a 1.977% bound as 2.0% next to "meets WAR ≤ 2.0%". The target is printed
    exactly (:func:`pct_exact`), so once the two renderings differ their order is the true
    order (rounding is monotone) and the comparison the label states is visible. A value equal
    to ``reference`` keeps the starting precision."""
    places = max(digits, _exact_places(reference, digits, max_digits))
    while (
        places < max_digits
        and value != reference
        and f"{value * 100:.{places}f}" == f"{reference * 100:.{places}f}"
    ):
        places += 1
    return pct(value, places)


_SPELLED = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def spelled(n: int) -> str:
    """A count in a sentence: spelled out below ten (``four``), else in figures (``1,870``)."""
    return _SPELLED[n] if 0 <= n < len(_SPELLED) else f"{n:,}"


def number(value: float | None, digits: int = 3) -> str:
    return NA if value is None else f"{value:.{digits}f}"


def count(value: float) -> str:
    """A count that is a float only because it was summed as one (metric denominators)."""
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.2f}"


def plural(noun: str, n: float) -> str:
    """``noun`` for exactly one, its plural otherwise (the unit names ``scoring`` uses:
    "base request case", "review item", "campaign", "family", "JML event")."""
    if n == 1:
        return noun
    if noun.endswith("y") and noun[-2:-1] not in ("a", "e", "i", "o", "u"):
        return noun[:-1] + "ies"
    return noun + "s"


def units(n: float, noun: str) -> str:
    """``8 base request cases``, ``1 family``."""
    return f"{count(n)} {plural(noun, n)}"


def interval(
    point: float | None,
    low: float | None,
    high: float | None,
    *,
    percent: bool = True,
    digits: int | None = None,
) -> str:
    """``point (low–high)``, the point alone when no interval exists, or n/a."""
    if point is None:
        return NA
    places = digits if digits is not None else (1 if percent else 3)
    scale = 100.0 if percent else 1.0
    suffix = "%" if percent else ""
    text = f"{point * scale:.{places}f}{suffix}"
    if low is None or high is None:
        return text
    return f"{text} ({low * scale:.{places}f}–{high * scale:.{places}f})"


def collapsed(m: Metric | None) -> bool:
    """A defined metric whose interval has no width, such as ``0.0% (0.0–0.0)``: every
    bootstrap resample gave the same value, which with no observed events (or only events) is
    what a cluster bootstrap does, not evidence of certainty (SPEC §12.8). Reports say so
    wherever one is shown."""
    return m is not None and m.point is not None and m.low == m.point and m.high == m.point


def bound(b: Bound, digits: int = 1) -> str:
    """``CP95 ≤ 4.1%, 0/73``: the one-sided upper bound at its confidence level, with k/n."""
    level = round((1.0 - b.alpha) * 100)
    return f"CP{level} ≤ {pct(b.upper, digits)}, {b.k}/{b.n}"


def bound_note(entries: Iterable[tuple[int, str, str, str]]) -> str | None:
    """``Exact bounds: CP95 bound on <estimand>; units: <units>; failure event: <event>`` for
    each distinct (level, estimand, units, failure event) in first-seen order; None for none.
    ``units`` is the singular unit name, as ``scoring`` records it."""
    seen = dict.fromkeys(entries)
    if not seen:
        return None
    return "Exact bounds: " + " · ".join(
        f"CP{level} bound on {estimand}; units: {plural(units, 2)}; failure event: {event}"
        for level, estimand, units, event in seen
    )


def bound_notes(metrics: Iterable[Metric | None]) -> str | None:
    """What the exact bounds among ``metrics`` are bounds on, taken verbatim from the scorer's
    ``Bound`` records (SPEC §12.8: every bound states its estimand, its units and its failure
    event), one entry per distinct bound in first-seen order; None when none carries a bound.
    Only bounds a cell would show (on a defined metric, see :func:`metric`) are described."""
    return bound_note(
        (round((1.0 - b.alpha) * 100), b.estimand, b.units, b.failure_event)
        for m in metrics
        if m is not None and m.point is not None and (b := m.bound) is not None
    )


def metric(
    m: Metric | None, *, percent: bool = True, digits: int | None = None, show_bound: bool = True
) -> str:
    """A metric with its interval, and its exact bound when it carries one. A defined metric
    without an interval names its n instead (the denominator, in the metric's units), since the
    point alone would read as exact."""
    if m is None:
        return NA
    text = interval(m.point, m.low, m.high, percent=percent, digits=digits)
    if m.point is not None and (m.low is None or m.high is None):
        text = f"{text} (no interval: {units(m.denominator, m.units)})"
    if show_bound and m.bound is not None and m.point is not None:
        text = f"{text} ({bound(m.bound)})"
    return text


def pp(value: float | None, low: float | None, high: float | None) -> str:
    """A difference of rates in percentage points: ``+3.2 pp (-1.0 to +7.4)``."""
    if value is None:
        return NA
    text = f"{value * 100:+.1f} pp"
    if low is None or high is None:
        return text
    return f"{text} ({low * 100:+.1f} to {high * 100:+.1f})"


def usd(value: float | None) -> str:
    """Dollars: two decimals, or four below a cent so a cheap model doesn't read as free."""
    if value is None:
        return NA
    if value == 0:
        return "$0.00"
    if abs(value) < 0.01:
        return f"${value:.4f}"
    return f"${value:,.2f}"


def seconds(value: float | None) -> str:
    return NA if value is None else f"{value:.2f} s"


def words(items: Sequence[str], joiner: str = "and") -> str:
    """``a``, ``a and b``, ``a, b and c`` (no serial comma), or with ``joiner="or"``."""
    if len(items) <= 2:
        return f" {joiner} ".join(items)
    return ", ".join(items[:-1]) + f" {joiner} " + items[-1]
