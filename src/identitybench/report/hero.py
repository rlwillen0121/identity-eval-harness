"""The README's hero figure: a dumbbell chart, one row per model, with a dot for grounded
accuracy and a ring for decision accuracy joined by a line, so the gap between deciding right
and showing why is the first thing a reader sees (SPEC §15: README charts are SVG exports).

Built like a chart-table: the model's name and vendor on the left, the dumbbell on a shared
percentage axis in the middle, and the three numbers it draws (grounded, decision and the gap
between them) in fixed columns on the right, whose heads name the two marks, so no legend is
needed and no label can collide with a mark, however many rows there are. Rows are drawn in
the order given (the README sorts by grounded accuracy); every other row is striped so a row
can be followed from name to numbers. The oracle ceiling is a labeled hairline, never a row.

The axis needn't start at zero (dots mark positions, not lengths): it starts at the 10% step
below the lowest value, so a saturated metric still reads as saturated against 100%.

Numbers are printed as the README table prints them (a percentage to one decimal); the gap is
the difference of the two printed numbers, so a reader's subtraction always checks out.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from identitybench.report.figure_kit import (
    Canvas,
    Theme,
    fmt_share,
    shown_percent,
    text_width,
    truncate,
)

__all__ = ["WIDTH", "HeroRow", "gap_text", "hero_svg"]

WIDTH = 820.0
_PAD = 24.0
_ROW = 29.0
_NAME, _VENDOR, _VALUE, _HEAD, _TICK = 14.0, 11.5, 14.0, 12.5, 12.0
_NAME_MAX = 210.0
_RING = 5.0
"""The decision ring's radius."""


@dataclass(frozen=True, slots=True)
class HeroRow:
    """One model: (decision accuracy, grounded accuracy) as shares (0 to 1), or None when
    undefined, and its vendor (a short muted label), if known."""

    name: str
    values: tuple[float | None, float | None]
    vendor: str | None = None


def gap_text(decision: float | None, grounded: float | None) -> str:
    """Decision minus grounded accuracy in points, from the printed (rounded) numbers."""
    if decision is None or grounded is None:
        return "n/a"
    return f"{shown_percent(decision) - shown_percent(grounded):.1f}"


def _domain(rows: Sequence[HeroRow]) -> tuple[float, list[float]]:
    values = [shown_percent(v) for r in rows for v in r.values if v is not None]
    low = min(values, default=0.0)
    start = min(max(math.floor((low - 2.0) / 10.0) * 10.0, 0.0), 90.0)
    step = 10.0 if 100.0 - start <= 60.0 else 20.0
    ticks = [start + step * i for i in range(int((100.0 - start) / step) + 1)]
    if ticks[-1] < 100.0:
        ticks.append(100.0)
    return start / 100.0, [t / 100.0 for t in ticks]


def hero_svg(
    rows: Sequence[HeroRow],
    *,
    theme: Theme,
    title: str,
    subtitle: str,
    source: Sequence[str],
    badge: str | None = None,
    reference: tuple[float, str] | None = None,
    key: str = "identitybench-results",
) -> str:
    """The dumbbell chart of ``rows`` in their order. ``subtitle`` is the one-sentence
    takeaway, ``source`` the small print under the chart (each string wraps to the width),
    ``badge`` a short status top right (an interim leaderboard) and ``reference`` a labeled
    value on the axis (the oracle ceiling), drawn as a hairline."""
    canvas = Canvas(WIDTH, theme)
    top = canvas.frame(title=title, subtitle=subtitle, badge=badge, pad=_PAD)

    name_x = _PAD + 10
    name_w = min(max([text_width(r.name, _NAME) for r in rows] + [60.0]), _NAME_MAX)
    vendors = [r.vendor for r in rows if r.vendor]
    vendor_x = name_x + name_w + 12
    vendor_w = max([text_width(v, _VENDOR) for v in vendors], default=0.0)
    gap_right = WIDTH - _PAD - 10
    decision_right = gap_right - 70
    grounded_right = decision_right - 88
    x0 = vendor_x + vendor_w + (24 if vendors else 12)
    x1 = grounded_right - 90
    start, ticks = _domain(rows)

    def x(share: float) -> float:
        return x0 + (x1 - x0) * (min(max(share, start), 1.0) - start) / (1.0 - start)

    # Column heads: line A names the marks (with the mark beside each name), line B the units
    # and the axis ticks.
    head_a = top + 18
    head_b = head_a + 16
    heads = (
        (grounded_right, "Grounded", "%", "dot"),
        (decision_right, "Decision", "%", "ring"),
        (gap_right, "Gap", "pts", "line"),
    )
    for right, name, unit, glyph in heads:
        box = canvas.text(right, head_a, name, size=_HEAD, fill="ink", anchor="end", weight=600)
        gx, gy = box.x0 - 11, head_a - 4.2
        if glyph == "dot":
            canvas.circle(gx, gy, 5.0, fill="accent")
        elif glyph == "ring":
            canvas.circle(gx, gy, 4.2, fill="none", stroke="neutral", width=2.0)
        else:
            canvas.line(
                gx - 7, gy, gx + 3, gy, stroke="warm", width=3.0, extra=' stroke-linecap="round"'
            )
        canvas.text(right, head_b, unit, size=_TICK, fill="muted", anchor="end")
    canvas.text(name_x, head_b, "Model", size=_TICK, fill="muted")
    if vendors:
        canvas.text(vendor_x, head_b, "Vendor", size=_TICK, fill="muted")
    for tick in ticks:
        canvas.text(
            x(tick), head_b, f"{tick * 100:.0f}%", size=_TICK, fill="muted", anchor="middle"
        )

    rows_top = head_b + 10
    rows_bottom = rows_top + _ROW * max(len(rows), 1)
    for i in range(len(rows)):
        if i % 2 == 0:
            canvas.rect(_PAD, rows_top + i * _ROW, WIDTH - 2 * _PAD, _ROW, fill="band", rx=6)
    for tick in ticks:
        if reference is not None and abs(tick - reference[0]) < 1e-9:
            continue
        canvas.line(x(tick), rows_top, x(tick), rows_bottom, stroke="grid")
    if reference is not None and start <= reference[0] <= 1.0:
        # Labeled on the heads' first line, left of the hairline, clear of every tick label.
        rx = x(reference[0])
        canvas.line(rx, rows_top - 2, rx, rows_bottom + 4, stroke="ink2", width=1.0)
        canvas.text(rx - 5, head_a, reference[1], size=_TICK, fill="ink2", anchor="end")

    for i, row in enumerate(rows):
        cy = rows_top + i * _ROW + _ROW / 2
        surface = "band" if i % 2 == 0 else "page"
        canvas.text(name_x, cy + 4.7, truncate(row.name, _NAME_MAX, _NAME), size=_NAME, weight=500)
        if row.vendor:
            canvas.text(vendor_x, cy + 4.0, row.vendor, size=_VENDOR, fill="muted")
        decision, grounded = row.values
        # The gap line stops at the ring's edge, and the ring (hollow) is drawn over the dot,
        # so a model whose two numbers coincide shows a dot inside a ring.
        if decision is not None and grounded is not None and x(decision) - x(grounded) > _RING:
            canvas.line(x(grounded), cy, x(decision) - _RING, cy, stroke="warm", width=3.0)
        if grounded is not None:
            canvas.circle(x(grounded), cy, 6.0, fill="accent", stroke=surface, width=2.0)
        if decision is not None:
            canvas.circle(x(decision), cy, _RING, fill="none", stroke="neutral", width=2.0)
        canvas.text(
            grounded_right, cy + 4.7, fmt_share(grounded), size=_VALUE, anchor="end", weight=650
        )
        canvas.text(
            decision_right, cy + 4.7, fmt_share(decision), size=_VALUE, fill="ink2", anchor="end"
        )
        canvas.text(
            gap_right,
            cy + 4.7,
            gap_text(decision, grounded),
            size=_VALUE,
            fill="ink2",
            anchor="end",
        )

    if len(rows) > 12:  # a tall chart repeats its ticks under the rows
        for tick in ticks:
            canvas.text(
                x(tick),
                rows_bottom + 18,
                f"{tick * 100:.0f}%",
                size=_TICK,
                fill="muted",
                anchor="middle",
            )
    bottom = rows_bottom + (34 if len(rows) > 12 else 20)
    height = canvas.footer(bottom + 6, source, _PAD)

    described = "; ".join(
        f"{r.name}: grounded accuracy {_pct(r.values[1])}, decision accuracy {_pct(r.values[0])}"
        for r in rows
    )
    desc = (
        f"Dot plot of {len(rows)} models on a percentage axis: for each, a dot for grounded "
        f"accuracy and a ring for decision accuracy, joined by a line whose length is the gap. "
        f"{subtitle} {described}. {' '.join(source)}"
    )
    return canvas.render(height, title=title, desc=desc, key=key)


def _pct(share: float | None) -> str:
    return "n/a" if share is None else f"{fmt_share(share)}%"
