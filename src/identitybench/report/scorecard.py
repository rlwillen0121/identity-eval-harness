"""The README's scorecard: a heatmap of models against the headline metrics, each cell printing
the number the README tables print and colored by how far that number is from perfect
(SPEC §15).

**One color direction for every metric.** Accuracy is better high and WAR better low, so a
cell is colored by its *shortfall*: points between its printed value and the metric's ideal
(100% accuracy, 0% WAR, unsafe plans and harmful flips, the oracle's own scores). A perfect
cell recedes into the page and the further a model is from perfect, the stronger the color,
whichever way the metric points. The heads still carry the metric's direction.

**Binned, on a published scale.** Shortfalls fall into seven classes, 0, up to 1, 2.5, 5, 10
and 25 points, and beyond 25, drawn from a single-hue sequential ramp stepped evenly in OKLCH
lightness (``figure_kit.THEMES``), with the scale as a legend. The bins grow geometrically so
that a 1.7-point WAR, which matters, isn't lost next to a 49-point grounding shortfall, and the
shortfall is taken from the printed value, so a cell that reads 0.0 is never colored.

**Safety failures never recede.** One scale serves accuracy points and safety-rate points, and
its first class (up to 1 point) sits close to perfect, so a nonzero wrongful approval rate,
unsafe plan rate or harmful flip rate is also ringed in the alarm color, on the page outside
the cell, so it shows on every fill, and set in bold. The legend states the rule.

**Missing is not perfect.** A cell with no value (n/a, or ``—`` not scored) is drawn unfilled
with a dashed outline, never in the no-shortfall color, and the legend shows it when one
occurs.

Rows are drawn in the order given (the README uses the hero's), and the text in each cell has
at least 4.5:1 contrast with its fill.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from identitybench.report.figure_kit import (
    Canvas,
    Theme,
    fmt_share,
    shown_percent,
    text_width,
    truncate,
)

__all__ = [
    "BINS",
    "WIDTH",
    "ScoreCell",
    "ScoreColumn",
    "ScoreRow",
    "scorecard_svg",
    "shortfall",
    "shortfall_bin",
]

WIDTH = 820.0
_PAD = 24.0
_ROW = 28.0
_NAME, _CELL, _HEAD, _SUB = 13.5, 12.5, 12.5, 11.5
_NAME_MAX = 232.0
_GROUP_GAP = 12.0

BINS: tuple[float, ...] = (0.0, 1.0, 2.5, 5.0, 10.0, 25.0)
"""Upper bounds (points short of perfect, inclusive) of the first six classes; the seventh
holds everything beyond the last."""
_BIN_LABELS = ("0", "≤1", "≤2.5", "≤5", "≤10", "≤25", ">25")


@dataclass(frozen=True, slots=True)
class ScoreColumn:
    """One metric column: its group head (the metric, with its direction), its own head (the
    task) and the metric's ideal value as a share (1.0 or 0.0)."""

    group: str
    label: str
    ideal: float
    short: str | None = None
    """A shorter head ("Req" for "Requests"), used for every column when some full head
    wouldn't fit its column (many columns beside a long model name); None: the full head."""


_SHORT_LABELS: Mapping[str, str] = {
    "Requests": "Req",
    "Reviews": "Rev",
    "JML plans": "JML",
    "Req + rev": "Req+rev",
}
"""Default short heads for the task labels the README uses."""


def _labels(columns: Sequence[ScoreColumn], width: float) -> list[str]:
    """The column heads: every full head if each fits ``width``, else every short one."""
    full = [c.label for c in columns]
    if all(text_width(label, _SUB) + 6 <= width for label in full):
        return full
    return [c.short or _SHORT_LABELS.get(c.label, c.label) for c in columns]


@dataclass(frozen=True, slots=True)
class ScoreCell:
    """A share (0 to 1), None when undefined (n/a), or not scored on the task."""

    value: float | None
    scored: bool = True


@dataclass(frozen=True, slots=True)
class ScoreRow:
    name: str
    cells: tuple[ScoreCell, ...]


def shortfall(value: float, ideal: float) -> float:
    """Points between a printed value and the ideal."""
    return round(abs(shown_percent(ideal) - shown_percent(value)), 1)


def shortfall_bin(points: float) -> int:
    """The class (0 to 6) of a shortfall in points."""
    return next((i for i, bound in enumerate(BINS) if points <= bound), len(BINS))


def _alarm_ring(canvas: Canvas, x: float, y: float, w: float, h: float) -> None:
    """The ring around a nonzero safety failure: a rounded outline just inside the slot."""
    canvas.rect(
        x + 0.75,
        y + 0.75,
        w - 1.5,
        h - 1.5,
        fill="none",
        rx=5,
        extra=f' stroke="{canvas.theme.alarm}" stroke-width="1.5" class="alarm"',
    )


def _missing_cell(canvas: Canvas, x: float, y: float, w: float, h: float) -> None:
    """A cell with no value: unfilled, with a dashed outline."""
    canvas.rect(
        x,
        y,
        w,
        h,
        fill="none",
        rx=4,
        extra=(
            f' stroke="{canvas.theme.missing}" stroke-width="1" stroke-dasharray="3 2"'
            ' class="missing"'
        ),
    )


def scorecard_svg(
    rows: Sequence[ScoreRow],
    columns: Sequence[ScoreColumn],
    *,
    theme: Theme,
    title: str,
    subtitle: str,
    source: Sequence[str],
    badge: str | None = None,
    key: str = "identitybench-scorecard",
) -> str:
    """The heatmap of ``rows`` (models) by ``columns`` (metrics), in their orders."""
    canvas = Canvas(WIDTH, theme)
    y = canvas.frame(title=title, subtitle=subtitle, badge=badge, pad=_PAD)

    safety = {j for j, c in enumerate(columns) if c.ideal == 0.0}
    missing = any(not c.scored or c.value is None for r in rows for c in r.cells)

    # The scale, then the two marks that aren't on it.
    legend_y = y + 14
    caption = canvas.text(
        _PAD, legend_y + 11, "Points short of perfect", size=_SUB, fill="ink2", weight=600
    )
    sw = 38.0
    lx = caption.x1 + 16
    for i, label in enumerate(_BIN_LABELS):
        edge = f' stroke="{theme.axis}" stroke-width="1"' if i == 0 else ""
        canvas.rect(lx + i * (sw + 2), legend_y, sw, 14, fill=theme.heat[i], rx=3, extra=edge)
        canvas.text(
            lx + i * (sw + 2) + sw / 2,
            legend_y + 29,
            label,
            size=_SUB,
            fill="muted",
            anchor="middle",
        )
    mx = lx + 7 * (sw + 2) + 24
    marks: list[tuple[str, str]] = []
    if safety:
        marks.append(("ring", "a safety failure, however small"))
    if missing:
        marks.append(("dash", "no value (n/a, — not scored)"))
    for k, (kind, label) in enumerate(marks):
        my = legend_y + 22 * k
        if kind == "ring":
            _alarm_ring(canvas, mx, my - 2, 28, 18)
            canvas.rect(mx + 2.5, my + 0.5, 23, 12, fill=theme.heat[1], rx=2.5)
        else:
            _missing_cell(canvas, mx + 1, my - 1, 26, 16)
        canvas.text(mx + 36, my + 11, label, size=_SUB, fill="muted")

    # Columns: groups of adjacent columns sharing a head.
    groups: list[tuple[str, list[int]]] = []
    for j, column in enumerate(columns):
        if groups and groups[-1][0] == column.group:
            groups[-1][1].append(j)
        else:
            groups.append((column.group, [j]))
    name_x = _PAD + 10
    name_w = min(max([text_width(r.name, _NAME) for r in rows] + [60.0]), _NAME_MAX)
    grid_x0 = name_x + name_w + 14
    available = WIDTH - _PAD - grid_x0 - _GROUP_GAP * (len(groups) - 1)
    cw = min(78.0, available / max(len(columns), 1))
    col_x: list[float] = []
    cursor = grid_x0
    for _, members in groups:
        for _ in members:
            col_x.append(cursor)
            cursor += cw
        cursor += _GROUP_GAP

    head_y = legend_y + 64
    for group, members in groups:
        gx0, gx1 = col_x[members[0]], col_x[members[-1]] + cw
        canvas.text((gx0 + gx1) / 2, head_y, group, size=_HEAD, weight=600, anchor="middle")
        canvas.line(gx0 + 3, head_y + 7, gx1 - 3, head_y + 7, stroke="axis")
    for j, label in enumerate(_labels(columns, cw)):
        canvas.text(col_x[j] + cw / 2, head_y + 23, label, size=_SUB, fill="ink2", anchor="middle")

    rows_top = head_y + 34
    for i, row in enumerate(rows):
        ry = rows_top + i * _ROW
        canvas.text(
            name_x,
            ry + _ROW / 2 + 4.5,
            truncate(row.name, _NAME_MAX, _NAME),
            size=_NAME,
            weight=500,
        )
        for j, (column, cell) in enumerate(zip(columns, row.cells, strict=True)):
            cx = col_x[j]
            if not cell.scored or cell.value is None:
                _missing_cell(canvas, cx + 2, ry + 2, cw - 4, _ROW - 4)
                canvas.text(
                    cx + cw / 2,
                    ry + _ROW / 2 + 4.2,
                    "n/a" if cell.scored else "—",
                    size=_CELL,
                    fill="muted",
                    anchor="middle",
                )
                continue
            points = shortfall(cell.value, column.ideal)
            step = shortfall_bin(points)
            alarm = j in safety and points > 0.0
            if alarm:  # ringed on the page, the fill drawn a little inside the ring
                _alarm_ring(canvas, cx, ry, cw, _ROW)
                canvas.rect(cx + 2.75, ry + 2.75, cw - 5.5, _ROW - 5.5, fill=theme.heat[step], rx=3)
            else:
                canvas.rect(cx + 1.5, ry + 1.5, cw - 3, _ROW - 3, fill=theme.heat[step], rx=4)
            canvas.text(
                cx + cw / 2,
                ry + _ROW / 2 + 4.2,
                fmt_share(cell.value),
                size=_CELL,
                fill=theme.heat_ink[step],
                anchor="middle",
                weight=600 if step >= 3 or alarm else 400,
            )
    rows_bottom = rows_top + _ROW * len(rows)
    height = canvas.footer(rows_bottom + 26, source, _PAD)

    def described(row: ScoreRow) -> str:
        parts = []
        for column, cell in zip(columns, row.cells, strict=True):
            text = (
                ("n/a" if cell.value is None else f"{fmt_share(cell.value)}%")
                if cell.scored
                else "not scored"
            )
            parts.append(f"{column.group.rstrip(' ↑↓')} {column.label} {text}")
        return f"{row.name}: " + ", ".join(parts)

    desc = (
        f"Heatmap of {len(rows)} models by {len(columns)} metrics, each cell colored by its "
        "distance from a perfect score. "
        f"{subtitle} " + "; ".join(described(r) for r in rows) + f". {' '.join(source)}"
    )
    return canvas.render(height, title=title, desc=desc, key=key)
