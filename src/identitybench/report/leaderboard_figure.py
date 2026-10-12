"""The README's leaderboard, drawn as a table (SPEC §15: README charts are SVG exports).

GitHub can't style a Markdown table, so the README's headline table is this figure, with the
same numbers as text in a collapsed table under it for copy and paste and screen readers. Built
like a newspaper table rather than a spreadsheet:

- **One row per model, in the order given** (the README sorts by risk cost), with its name and,
  beneath it in small type, its vendor and any disclosure the name carries ("self-hosted").
  Rows carry no rank number: the order is one of point estimates, and the rows at the top whose
  sort key no test separates are bracketed in the left margin as a statistical tie. Hairlines
  separate the rows; the reference rows (the all-human status quo and the oracle) follow the
  models, set apart and muted, never ranked.
- **A safety-bar column**: a check for a model that clears the bar; for one that misses it, a
  cross and a strip of pips, one per criterion in the bar's order, filled for each criterion
  missed, so the verdict never rests on color and the pattern reads down the column.
- **Inline graphics on shared scales**: grounded accuracy as a bar on 0-100; wrongful approvals
  and unsafe plans as chips colored by severity on the scorecard's binned sequential ramp
  (``report.scorecard``), a printed 0.0 left unfilled so that only failures carry color, and
  every value whose own check misses the safety bar outlined (the classes don't line up with
  the bar's thresholds, and wrongful approvals are judged on their upper bound, not the point
  printed); cost per 1,000 decisions as a mark on a neutral log track with a tick at each
  decade, labelled once under the column, the mark's shape giving the cost's footing (a circle
  for an API charge, a diamond for an API-equivalent). A cost that isn't a price
  (``unmetered``, a CLI's ``≈`` estimate, ``n/a``) is one muted italic word in the price
  column, with no mark, since it has no place on a price scale. One small key above the table
  explains the chips, the safety pips and the marks.
- **Plain headers**, at most two lines, numbers right-aligned in tabular figures under them, and
  the better direction stated once, as a bracket over the columns where higher is better and
  over those where lower is.

Every number is the text the README's tables print, given by the caller; the figure only
places it. The ``<desc>`` states every row's values.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from identitybench.report.figure_kit import Canvas, Theme, text_width, truncate
from identitybench.report.scorecard import BINS, shortfall_bin

__all__ = [
    "WIDTH",
    "BoardColumn",
    "BoardCost",
    "BoardRow",
    "BoardValue",
    "cost_domain",
    "leaderboard_svg",
]

WIDTH = 820.0
_PAD = 24.0
_ROW = 34.0
_NAME, _VENDOR, _VALUE, _CHIP, _HEAD, _KEY, _SMALL, _WORD = (
    13.0,
    11.0,
    13.0,
    12.0,
    11.5,
    11.5,
    11.0,
    11.5,
)
"""Type sizes. Nothing is set below 11 px: GitHub scales the 820 px figure down to its README
column (about 700-770 px), so 11 px renders near 10 px there."""
_NAME_MAX = 160.0
_NAME_MIN = 96.0
_CHIP_H = 18.0
_CHIP_GAP = 8.0
_RING = 2.0
"""How far a failing value's outline stands off its chip: on the page, so it shows on every
fill (the scorecard's ring, squared to the chip)."""
_BAR_H = 8.0
_ICON = 6.5
_PIP_R, _PIP_PITCH, _PIP_GAP = 2.6, 8.0, 7.0
_GAP_MIN, _GAP_MAX = 8.0, 18.0
_BAR_MIN, _BAR_MAX, _BAR_FLOOR = 44.0, 96.0, 32.0
_TRACK_MIN, _TRACK_MAX, _TRACK_FLOOR = 40.0, 80.0, 32.0
_BIN_LABELS = ("≤1", "≤2.5", "≤5", "≤10", "≤25", ">25")
"""The legend's chips: the scorecard's classes past 0 (a printed 0.0 has no chip)."""

ColumnKind = Literal["bar", "severity", "number", "cost"]
CostKind = Literal["charge", "equivalent", "unmetered", "estimate", "none", "absent"]


@dataclass(frozen=True, slots=True)
class BoardValue:
    """One printed value: its text as the tables print it (``93.3``, ``n/a``, ``—``) and the
    number that text shows, in the column's unit (percentage points for a rate), or None."""

    text: str
    value: float | None = None


@dataclass(frozen=True, slots=True)
class BoardCost:
    """Cost per 1,000 decisions: the text the tables print without its footing mark
    (``$1.25``, ``unmetered``, ``≈$0.31``, ``n/a``, ``—``), the footing, and the dollars a mark
    is placed at (for an API charge or an API-equivalent only)."""

    text: str
    kind: CostKind
    usd: float | None = None

    @property
    def placed(self) -> bool:
        return self.kind in ("charge", "equivalent") and self.usd is not None and self.usd > 0


@dataclass(frozen=True, slots=True)
class BoardColumn:
    """A metric column. ``head`` is its header, one or two lines; ``keys`` the values its cells
    show (a severity column may show several side by side: requests and reviews), each named
    in the description by ``spoken``; ``emphasis`` sets its numbers in bold (the sort key)."""

    head: tuple[str, ...]
    kind: ColumnKind
    better: Literal["higher", "lower"]
    keys: tuple[str, ...]
    spoken: tuple[str, ...]
    unit: str = "%"
    emphasis: bool = False


@dataclass(frozen=True, slots=True)
class BoardRow:
    """A model (or a reference): its values by key, its cost, its vendor, and its verdict
    against the safety bar: True (clears), False (misses, with ``misses`` the criteria it
    misses, in the bar's order, named as in ``leaderboard_svg``'s ``criteria``) or None (not
    judged: a reference). ``failed`` holds the keys of the values whose own check misses the
    bar (each is outlined)."""

    name: str
    values: Mapping[str, BoardValue] = field(default_factory=dict[str, BoardValue])
    cost: BoardCost | None = None
    vendor: str | None = None
    clears: bool | None = None
    misses: tuple[str, ...] = ()
    failed: frozenset[str] = frozenset()
    reference: bool = False


def cost_domain(costs: Sequence[float]) -> tuple[float, float]:
    """The log track's ends: the powers of ten around the placed costs, at least a decade
    apart."""
    low = 10.0 ** math.floor(math.log10(min(costs)))
    high = 10.0 ** math.ceil(math.log10(max(costs)))
    return low, max(high, low * 10.0)


def _money(value: float) -> str:
    return f"${value:g}" if value >= 1 else f"${value:.2f}"


def _split_name(name: str) -> tuple[str, str | None]:
    """A display name's model and its parenthetical disclosure: ("Qwen3.8-27B", "4-bit,
    self-hosted")."""
    base, paren, rest = name.partition(" (")
    if not paren or not rest.rstrip(" †‡").endswith(")"):
        return name, None
    markers = rest[len(rest.rstrip(" †‡")) :]
    return base + markers, rest.rstrip(" †‡")[:-1]


def _short_vendor(vendor: str) -> str:
    """A vendor without its parenthetical: "Alibaba (Qwen)" -> "Alibaba"."""
    return vendor.partition(" (")[0]


def _second_lines(rows: Sequence[BoardRow], width: float) -> list[str | None]:
    """Under each name: its vendor and any disclosure ("Alibaba (Qwen) · self-hosted"). When
    a row's line doesn't fit ``width``, every row of that vendor names it in short ("Alibaba"),
    so rows of one vendor never differ; neither part is ever dropped."""

    def line(row: BoardRow, short: bool) -> str | None:
        vendor = row.vendor
        if vendor is not None and short:
            vendor = _short_vendor(vendor)
        parts = [p for p in (vendor, _split_name(row.name)[1]) if p]
        return " · ".join(parts) if parts else None

    tight = {
        row.vendor
        for row in rows
        if row.vendor is not None
        and text_width(line(row, False) or "", _VENDOR) > width
        and _short_vendor(row.vendor) != row.vendor
    }
    out: list[str | None] = []
    for row in rows:
        text = line(row, row.vendor in tight)
        out.append(None if text is None else truncate(text, width, _VENDOR))
    return out


@dataclass(slots=True)
class _Slot:
    """A column's place: its left edge and width, and for a value with a graphic (a bar or the
    cost track), the width of the value and of the graphic after it."""

    x: float
    w: float
    value_w: float = 0.0
    graphic_w: float = 0.0
    chip_w: float = 0.0


def _head_width(lines: Sequence[str]) -> float:
    return max((text_width(line, _HEAD, bold=True) for line in lines), default=0.0)


def _check(canvas: Canvas, cx: float, cy: float, *, key: bool = False) -> None:
    canvas.circle(cx, cy, _ICON, fill="good", extra="" if key else ' class="clears"')
    d = (
        f"M{cx - 3.1:.1f},{cy + 0.1:.1f}L{cx - 0.9:.1f},{cy + 2.4:.1f}"
        f"L{cx + 3.2:.1f},{cy - 2.5:.1f}"
    )
    canvas.path(d, stroke="page", width=1.8)


def _cross(canvas: Canvas, cx: float, cy: float, *, key: bool = False) -> None:
    canvas.circle(
        cx,
        cy,
        _ICON - 0.75,
        fill="none",
        stroke="alarm",
        width=1.5,
        extra="" if key else ' class="misses"',
    )
    r = 2.4
    d = (
        f"M{cx - r:.1f},{cy - r:.1f}L{cx + r:.1f},{cy + r:.1f}"
        f"M{cx + r:.1f},{cy - r:.1f}L{cx - r:.1f},{cy + r:.1f}"
    )
    canvas.path(d, stroke="alarm", width=1.5)


def _pip(canvas: Canvas, cx: float, cy: float, filled: bool) -> None:
    if filled:
        canvas.circle(cx, cy, _PIP_R, fill="alarm", extra=' class="missed"')
    else:
        canvas.circle(cx, cy, _PIP_R - 0.5, fill="none", stroke="neutral", width=1.0)


def _pips_width(n: int) -> float:
    return (n - 1) * _PIP_PITCH + 2 * _PIP_R if n else 0.0


def _outline(canvas: Canvas, x: float, y: float, w: float, h: float, *, key: bool = False) -> None:
    """A failing value's outline, ``_RING`` off the chip's box on every side."""
    classed = "" if key else ' class="fails"'
    canvas.rect(
        x - _RING,
        y - _RING,
        w + 2 * _RING,
        h + 2 * _RING,
        fill="none",
        rx=5.5,
        extra=f' stroke="{canvas.theme.alarm}" stroke-width="1"{classed}',
    )


def leaderboard_svg(
    rows: Sequence[BoardRow],
    columns: Sequence[BoardColumn],
    *,
    theme: Theme,
    title: str,
    subtitle: str,
    source: Sequence[str],
    badge: str | None = None,
    criteria: Sequence[str] = (),
    tie_rows: int = 0,
    tie_labels: Sequence[str] = (),
    key: str = "identitybench-leaderboard",
) -> str:
    """The table of ``rows`` (models, then references) by ``columns``, in their orders, with a
    safety-bar column when any row has a verdict. ``subtitle`` is the takeaway, ``source`` the
    small print, ``badge`` a short status top right. ``criteria`` names the safety bar's
    criteria in order, one pip each. ``tie_rows`` (at least two) brackets that many models at
    the top as a statistical tie, labelled with the first of ``tie_labels`` that fits."""
    canvas = Canvas(WIDTH, theme)
    top = canvas.frame(title=title, subtitle=subtitle, badge=badge, pad=_PAD)
    models = [r for r in rows if not r.reference]
    references = [r for r in rows if r.reference]
    judged = any(r.clears is not None for r in rows)
    pips = len(criteria) if judged and any(r.misses for r in models) else 0
    placed = [r.cost.usd for r in rows if r.cost is not None and r.cost.placed]
    domain = cost_domain([u for u in placed if u is not None]) if placed else None
    tie = tie_rows if 2 <= tie_rows <= len(models) else 0
    outlined = any(r.failed for r in models)

    # ---- Measure ----------------------------------------------------------------------------
    inner = WIDTH - 2 * _PAD
    name_w = max(
        [text_width(_split_name(r.name)[0], _NAME) for r in models]
        + [text_width("Model", _HEAD, bold=True)]
    )
    vendor_w = max(
        (
            text_width(" · ".join(p for p in (r.vendor, _split_name(r.name)[1]) if p), _VENDOR)
            for r in models
        ),
        default=0.0,
    )
    model_w = min(max(name_w, vendor_w), _NAME_MAX)
    safety_w = 0.0
    if judged:
        icons = 2 * _ICON + (_PIP_GAP + _pips_width(pips) if pips else 0.0)
        safety_w = max(icons, _head_width(("Safety", "bar")))
    slots: list[_Slot] = []
    for column in columns:
        head_w = _head_width(column.head)
        bold = column.emphasis
        if column.kind == "severity":
            texts = [r.values[k].text for r in rows for k in column.keys if k in r.values]
            chip = max([text_width(t, _CHIP, bold=True) + 12 for t in texts] + [34.0])
            cell = len(column.keys) * chip + (len(column.keys) - 1) * _CHIP_GAP
            slots.append(_Slot(0, max(cell, head_w), chip_w=chip))
        elif column.kind == "number":
            texts = [r.values[k].text for r in rows for k in column.keys if k in r.values]
            w = max([text_width(t, _VALUE, bold=bold) for t in texts] + [head_w])
            slots.append(_Slot(0, w))
        elif column.kind == "bar":
            texts = [r.values[k].text for r in rows for k in column.keys if k in r.values]
            value_w = max(text_width(t, _VALUE, bold=bold) for t in [*texts, "100.0"])
            slots.append(_Slot(0, 0, value_w=value_w, graphic_w=_BAR_MIN))
        else:
            costs = [r.cost for r in rows if r.cost is not None]
            value_w = max(
                [text_width(c.text, _VALUE) for c in costs if c.placed or c.kind == "absent"]
                + [text_width(c.text, _WORD) for c in costs if not c.placed and c.kind != "absent"]
                + [text_width("$0.00", _VALUE)]
            )
            slots.append(
                _Slot(0, 0, value_w=value_w, graphic_w=_TRACK_MIN if domain is not None else 0.0)
            )

    def graphic(column: BoardColumn) -> bool:
        return column.kind == "bar" or (column.kind == "cost" and domain is not None)

    def composite(slot: _Slot, column: BoardColumn) -> float:
        drawn = slot.value_w + (6 + slot.graphic_w if graphic(column) else 0.0)
        return max(drawn, _head_width(column.head))

    def fixed() -> float:
        return sum(
            composite(s, c) if c.kind in ("bar", "cost") else s.w
            for s, c in zip(slots, columns, strict=True)
        )

    n_gaps = len(columns) + (1 if judged else 0)

    def spare() -> float:
        return inner - model_w - safety_w - fixed() - n_gaps * _GAP_MIN

    # Short of width, the graphics give way first, down to their floors, then a long name.
    for slot, column in zip(slots, columns, strict=True):
        if spare() >= 0:
            break
        if graphic(column):
            floor = _BAR_FLOOR if column.kind == "bar" else _TRACK_FLOOR
            slot.graphic_w = max(floor, slot.graphic_w + spare())
    if spare() < 0:
        model_w = max(model_w + spare(), _NAME_MIN)
    # Grow the graphics toward their ideal widths, the cost track first (a log scale needs room
    # to label its decades), then the gaps.
    left = spare()
    for slot, column in sorted(
        zip(slots, columns, strict=True), key=lambda sc: sc[1].kind != "cost"
    ):
        if not graphic(column) or left <= 0:
            continue
        cap = _BAR_MAX if column.kind == "bar" else _TRACK_MAX
        before = composite(slot, column)
        slot.graphic_w = min(cap, slot.graphic_w + left)
        left -= composite(slot, column) - before
    gap = min(_GAP_MAX, _GAP_MIN + max(left, 0.0) / max(n_gaps, 1))

    model_x = _PAD
    cursor = model_x + model_w + gap
    safety_x = cursor
    if judged:
        cursor += safety_w + gap
    for slot, column in zip(slots, columns, strict=True):
        slot.x = cursor
        if column.kind in ("bar", "cost"):
            slot.w = composite(slot, column)
        cursor += slot.w + gap
    # A reference has no safety cell, so its label may run across that column.
    reference_w = (slots[0].x - gap if slots else WIDTH - _PAD) - model_x
    seconds = _second_lines(models, model_w)

    # ---- Key ----------------------------------------------------------------------------------
    y = top + 12
    items: list[list[tuple[str, str]]] = []
    if any(c.kind == "severity" for c in columns):
        group = [("label", "Failure rate, %"), *(("chip", b) for b in _BIN_LABELS)]
        if outlined:
            group.append(("outline", "misses the bar"))
        items.append(group)
    if judged:
        group = [("label", "Safety bar"), ("check", "clears"), ("cross", "misses")]
        if pips:
            group.append(("pips", " · ".join(criteria) + ", filled if missed"))
        items.append(group)
    kinds = {r.cost.kind for r in rows if r.cost is not None}
    words = {
        "unmetered": "unmetered: no per-call price",
        "estimate": "≈ CLI estimate, not a price",
        "none": "n/a: no usable cost",
    }
    if domain is not None or kinds & set(words):
        group = []
        if domain is not None:
            group.append(("label", f"Cost, log scale {_money(domain[0])}–{_money(domain[1])}"))
            if "charge" in kinds:
                group.append(("circle", "API charge"))
            if "equivalent" in kinds:
                group.append(("diamond", "API-equivalent"))
        else:
            group.append(("label", "Cost"))
        group += [("italic", text) for kind, text in words.items() if kind in kinds]
        items.append(group)

    def item_width(kind: str, text: str) -> float:
        if kind == "chip":
            return text_width(text, _SMALL) + 10
        if kind in ("circle", "diamond", "check", "cross"):
            return 16 + text_width(text, _KEY)
        if kind == "outline":
            return 26 + 6 + text_width(text, _KEY)
        if kind == "pips":
            return _pips_width(len(criteria)) + 6 + text_width(text, _KEY)
        return text_width(text, _KEY, bold=kind == "label")

    def spacing(kind: str) -> float:
        return 3.0 if kind == "chip" else 12.0

    def group_width(group: Sequence[tuple[str, str]]) -> float:
        widths = [item_width(k, t) for k, t in group]
        return sum(widths) + sum(spacing(k) for k, _ in group[1:])

    # Groups share a line when they fit; a group wider than the figure breaks between its items,
    # its later lines indented to its first item after the label.
    lines: list[tuple[float, list[list[tuple[str, str]]]]] = []
    for group in items:
        if lines and sum(group_width(g) + 28 for g in lines[-1][1]) + group_width(group) <= inner:
            lines[-1][1].append(group)
            continue
        indent = 0.0
        chunk: list[tuple[str, str]] = []
        for item in group:
            if chunk and indent + group_width([*chunk, item]) > inner:
                lines.append((indent, [chunk]))
                indent = group_width(group[:1]) + spacing(group[1][0]) if len(group) > 1 else 0.0
                chunk = []
            chunk.append(item)
        lines.append((indent, [chunk]))
    severity_steps: dict[str, int] = dict(zip(_BIN_LABELS, range(1, len(BINS) + 1), strict=True))
    for indent, line in lines:
        x = _PAD + indent
        baseline = y + 13
        mid = baseline - 4.0
        for group in line:
            for i, (kind, text) in enumerate(group):
                if i:
                    x += spacing(kind)
                if kind == "label":
                    box = canvas.text(x, baseline, text, size=_KEY, fill="ink2", weight=600)
                    x = box.x1
                elif kind == "chip":
                    w = item_width(kind, text)
                    step = severity_steps[text]
                    canvas.rect(x, baseline - 12, w, 16, fill=theme.heat[step], rx=3)
                    canvas.text(
                        x + w / 2,
                        baseline,
                        text,
                        size=_SMALL,
                        fill=theme.heat_ink[step],
                        anchor="middle",
                    )
                    x += w
                elif kind == "outline":
                    canvas.rect(x + _RING, baseline - 10, 26 - 2 * _RING, 12, fill="grid", rx=3)
                    _outline(canvas, x + _RING, baseline - 10, 26 - 2 * _RING, 12, key=True)
                    box = canvas.text(x + 32, baseline, text, size=_KEY, fill="muted")
                    x = box.x1
                elif kind in ("check", "cross"):
                    (_check if kind == "check" else _cross)(canvas, x + _ICON, mid, key=True)
                    box = canvas.text(x + 16, baseline, text, size=_KEY, fill="muted")
                    x = box.x1
                elif kind == "pips":
                    for p in range(len(criteria)):
                        _pip(canvas, x + _PIP_R + p * _PIP_PITCH, mid, False)
                    x += _pips_width(len(criteria)) + 6
                    box = canvas.text(x, baseline, text, size=_KEY, fill="muted")
                    x = box.x1
                elif kind in ("circle", "diamond"):
                    if kind == "circle":
                        canvas.circle(x + 4.5, mid, 4.0, fill="ink2")
                    else:
                        canvas.diamond(x + 4.5, mid, 5.0, fill="ink2")
                    box = canvas.text(x + 16, baseline, text, size=_KEY, fill="muted")
                    x = box.x1
                else:
                    box = canvas.text(
                        x, baseline, text, size=_KEY, fill="muted", extra=' font-style="italic"'
                    )
                    x = box.x1
            x += 28
        y += 22

    # ---- Header -------------------------------------------------------------------------------
    head_top = y + 10
    bracket_y = head_top + 2
    head_b = head_top + 46
    runs: list[tuple[str, int, int]] = []
    for j, column in enumerate(columns):
        if runs and runs[-1][0] == column.better:
            runs[-1] = (column.better, runs[-1][1], j)
        else:
            runs.append((column.better, j, j))
    for better, first, last in runs:
        x0, x1 = slots[first].x, slots[last].x + slots[last].w
        label = f"{better} is better"
        if text_width(label, _SMALL) + 16 > x1 - x0:
            continue
        canvas.line(x0, bracket_y + 4, x1, bracket_y + 4, stroke="axis")
        canvas.line(x0, bracket_y + 4, x0, bracket_y + 8, stroke="axis")
        canvas.line(x1, bracket_y + 4, x1, bracket_y + 8, stroke="axis")
        mid = (x0 + x1) / 2
        w = text_width(label, _SMALL) + 10
        canvas.rect(mid - w / 2, bracket_y - 6, w, 14, fill="page")
        canvas.text(
            mid,
            bracket_y + 7.5,
            label,
            size=_SMALL,
            fill="muted",
            anchor="middle",
            extra=' font-style="italic"',
        )

    def head(x: float, lines_: Sequence[str], anchor: str, fill: str = "ink2") -> None:
        for i, line in enumerate(lines_):
            baseline = head_b - 14 * (len(lines_) - 1 - i)
            canvas.text(x, baseline, line, size=_HEAD, fill=fill, anchor=anchor, weight=600)

    head(model_x, ("Model",), "start")
    if judged:
        head(safety_x, ("Safety", "bar"), "start")
    for slot, column in zip(slots, columns, strict=True):
        fill = "ink" if column.emphasis else "ink2"
        if column.kind in ("bar", "cost"):
            head(slot.x, column.head, "start", fill)
        else:
            head(slot.x + slot.w, column.head, "end", fill)
    rule_y = head_b + 9
    canvas.line(_PAD, rule_y, WIDTH - _PAD, rule_y, stroke="axis")

    # ---- Rows ---------------------------------------------------------------------------------
    def track(slot: _Slot) -> tuple[float, float]:
        """The cost track's ends, inset so a mark at either end stays on the track."""
        tx = slot.x + slot.value_w + 6
        return tx + 5, tx + slot.graphic_w - 5

    def track_x(slot: _Slot, usd: float) -> float:
        assert domain is not None
        lo, hi = (math.log10(v) for v in domain)
        t = (math.log10(usd) - lo) / (hi - lo)
        start, end = track(slot)
        return start + (end - start) * min(max(t, 0.0), 1.0)

    decades: list[float] = []  # the track's inner decades (its ends are the domain's)
    if domain is not None:
        lo, hi = (round(math.log10(v)) for v in domain)
        decades = [10.0**k for k in range(lo + 1, hi)]

    def draw_row(row: BoardRow, ry: float, second: str | None) -> None:
        cy = ry + _ROW / 2
        muted = row.reference
        name, _ = _split_name(row.name)
        name_y = ry + 15.0 if second else cy + 4.5
        ink = "ink2" if muted else "ink"
        style = ' font-style="italic"' if muted else ""
        canvas.text(
            model_x,
            name_y,
            truncate(name, reference_w if muted else model_w, _NAME),
            size=_NAME,
            fill=ink,
            weight=400 if muted else 500,
            extra=style,
        )
        if second:
            canvas.text(model_x, ry + 28.0, second, size=_VENDOR, fill="muted")
        if judged and row.clears is not None:
            if row.clears:
                _check(canvas, safety_x + _ICON, cy)
            else:
                _cross(canvas, safety_x + _ICON, cy)
                if pips:
                    px = safety_x + 2 * _ICON + _PIP_GAP + _PIP_R
                    for p, criterion in enumerate(criteria):
                        _pip(canvas, px + p * _PIP_PITCH, cy, criterion in row.misses)
        for slot, column in zip(slots, columns, strict=True):
            weight = 600 if column.emphasis and not muted else 400
            if column.kind == "number":
                v = row.values.get(column.keys[0], BoardValue("—"))
                canvas.text(
                    slot.x + slot.w,
                    cy + 4.5,
                    v.text,
                    size=_VALUE,
                    fill=ink,
                    anchor="end",
                    weight=weight,
                )
            elif column.kind == "severity":
                right = slot.x + slot.w
                for k, value_key in enumerate(reversed(column.keys)):
                    v = row.values.get(value_key, BoardValue("—"))
                    cx1 = right - k * (slot.chip_w + _CHIP_GAP)
                    cx0 = cx1 - slot.chip_w
                    step = shortfall_bin(v.value) if v.value is not None else 0
                    if value_key in row.failed and not muted:
                        _outline(canvas, cx0, cy - _CHIP_H / 2, slot.chip_w, _CHIP_H)
                    if step and not muted:
                        canvas.rect(
                            cx0,
                            cy - _CHIP_H / 2,
                            slot.chip_w,
                            _CHIP_H,
                            fill=theme.heat[step],
                            rx=4,
                        )
                        canvas.text(
                            (cx0 + cx1) / 2,
                            cy + 4.2,
                            v.text,
                            size=_CHIP,
                            fill=theme.heat_ink[step],
                            anchor="middle",
                            weight=600 if step >= 3 else 500,
                        )
                    else:
                        canvas.text(
                            (cx0 + cx1) / 2,
                            cy + 4.2,
                            v.text,
                            size=_CHIP,
                            fill="ink2" if v.value is not None else "muted",
                            anchor="middle",
                        )
            elif column.kind == "bar":
                v = row.values.get(column.keys[0], BoardValue("—"))
                value_right = slot.x + slot.value_w
                canvas.text(
                    value_right,
                    cy + 4.5,
                    v.text,
                    size=_VALUE,
                    fill=ink,
                    anchor="end",
                    weight=weight,
                )
                bx = value_right + 6
                if v.value is not None:
                    canvas.rect(bx, cy - _BAR_H / 2, slot.graphic_w, _BAR_H, fill="grid", rx=2)
                if v.value is not None and v.value > 0:
                    share = min(max(v.value / 100.0, 0.0), 1.0)
                    canvas.rect(
                        bx,
                        cy - _BAR_H / 2,
                        max(slot.graphic_w * share, 2.0),
                        _BAR_H,
                        fill="neutral" if muted else "accent",
                        rx=2,
                    )
            else:
                cost = row.cost or BoardCost("—", "absent")
                value_right = slot.x + slot.value_w
                if cost.placed and domain is not None and cost.usd is not None:
                    canvas.text(
                        value_right, cy + 4.5, cost.text, size=_VALUE, fill=ink, anchor="end"
                    )
                    tx = value_right + 6
                    canvas.line(tx, cy, tx + slot.graphic_w, cy, stroke="grid", width=2)
                    for decade in decades:
                        dx = track_x(slot, decade)
                        canvas.line(dx, cy - 2.5, dx, cy + 2.5, stroke="axis")
                    mx = track_x(slot, cost.usd)
                    if cost.kind == "equivalent":
                        canvas.diamond(mx, cy, 5.2, fill="ink2", extra=' data-cost="equivalent"')
                    else:
                        canvas.circle(mx, cy, 4.2, fill="ink2", extra=' data-cost="charge"')
                elif cost.kind == "absent":
                    canvas.text(
                        value_right, cy + 4.5, cost.text, size=_VALUE, fill="muted", anchor="end"
                    )
                else:  # a cost that isn't a price: one word, in the price column, no mark
                    canvas.text(
                        value_right,
                        cy + 4.5,
                        cost.text,
                        size=_WORD,
                        fill="muted",
                        anchor="end",
                        extra=' font-style="italic"',
                    )

    ry = rule_y
    for i, row in enumerate(models):
        if i:
            canvas.line(_PAD, ry, WIDTH - _PAD, ry, stroke="grid")
        draw_row(row, ry, seconds[i])
        ry += _ROW

    # The cost track's inner decades, labelled once under the models' rows when they fit.
    cost_slot = next(
        (s for s, c in zip(slots, columns, strict=True) if c.kind == "cost" and graphic(c)), None
    )
    if cost_slot is not None and decades and models:
        labels = [(track_x(cost_slot, d), _money(d)) for d in decades]

        def fits(chosen: Sequence[tuple[float, str]]) -> bool:
            edges = [
                (x - text_width(t, _SMALL) / 2, x + text_width(t, _SMALL) / 2) for x, t in chosen
            ]
            apart = all(b[0] - a[1] >= 4 for a, b in itertools.pairwise(edges))
            return apart and edges[-1][1] <= WIDTH - _PAD

        if fits(labels):
            for x, text in labels:
                canvas.text(x, ry + 13, text, size=_SMALL, fill="muted", anchor="middle")
            ry += 14
    if references:
        ry += 10
        canvas.line(_PAD, ry, WIDTH - _PAD, ry, stroke="axis")
        for i, row in enumerate(references):
            if i:
                canvas.line(_PAD, ry, WIDTH - _PAD, ry, stroke="grid")
            draw_row(row, ry, None)
            ry += _ROW
    canvas.line(_PAD, ry, WIDTH - _PAD, ry, stroke="axis")

    # The tie: a bracket in the left margin over the rows no test separates.
    if tie:
        y0, y1 = rule_y + 6, rule_y + tie * _ROW - 6
        bx = _PAD - 6.0  # in the page's margin, so the names keep the title's left edge
        canvas.path(
            f"M{bx + 3:.1f},{y0:.1f}L{bx:.1f},{y0:.1f}L{bx:.1f},{y1:.1f}L{bx + 3:.1f},{y1:.1f}",
            stroke="muted",
            width=1.0,
            extra=' class="tie"',
        )
        tie_text = next((t for t in tie_labels if text_width(t, _SMALL) <= y1 - y0 - 12), "")
        if tie_text:
            lx, ly = bx - 5.5, (y0 + y1) / 2
            canvas.text(
                lx,
                ly,
                tie_text,
                size=_SMALL,
                fill="muted",
                anchor="middle",
                extra=f' font-style="italic" transform="rotate(-90 {lx:.1f} {ly:.1f})"',
            )
    height = canvas.footer(ry + 24, source, _PAD)

    def described(row: BoardRow) -> str:
        parts: list[str] = []
        if row.reference:
            parts.append("reference, not ranked")
        if row.clears is True:
            parts.append("clears the safety bar")
        elif row.clears is False:
            parts.append(f"misses the safety bar on {', '.join(row.misses) or 'a criterion'}")
        for column in columns:
            if column.kind == "cost":
                cost = row.cost or BoardCost("—", "absent")
                footing = {
                    "charge": " (API charge)",
                    "equivalent": " (API-equivalent, not a charge)",
                    "estimate": " (CLI estimate, not a price)",
                }.get(cost.kind, "")
                parts.append(f"cost per 1,000 decisions {cost.text}{footing}")
                continue
            for value_key, spoken in zip(column.keys, column.spoken, strict=True):
                v = row.values.get(value_key, BoardValue("—"))
                unit = column.unit if v.value is not None else ""
                fails = " (misses the bar)" if value_key in row.failed else ""
                parts.append(f"{spoken} {v.text}{unit}{fails}")
        vendor = f" ({row.vendor})" if row.vendor else ""
        return f"{row.name}{vendor}: " + ", ".join(parts)

    noun = "model" if len(models) == 1 else "models"
    tied = (
        f" The first {tie} rows are bracketed: {tie_labels[0] if tie_labels else 'a tie'}."
        if tie
        else ""
    )
    desc = (
        f"Leaderboard table of {len(models)} {noun}"
        + (f" and {len(references)} reference rows" if references else "")
        + f". {subtitle}{tied} "
        + "; ".join(described(r) for r in [*models, *references])
        + f". {' '.join(source)}"
    )
    return canvas.render(height, title=title, desc=desc, key=key)
