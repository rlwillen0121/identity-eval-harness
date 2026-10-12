"""The README's cost figure: grounded accuracy against cost per 1,000 decisions, one point per
model with a price, and the cost-quality frontier (SPEC §15).

**Only prices get a position.** The x axis is logarithmic and holds the models whose cost is an
API charge (metered from the dated pricing table, or billed by a gateway), drawn as circles,
and those with an API-equivalent (a subscription CLI's recorded tokens at the model's dated list
price, ``report.api_equivalent``), drawn as diamonds and marked ``◇`` in every label, so the two
footings never read as one. A legend under the plot names both. A CLI's own estimate is not a
price, an unmetered model's $0 is not a price either, and an unreported cost is
unknown: those models are listed under the plot with their grounded accuracy and what their
cost is, and are never drawn at a cost position. The accuracy axis is taken from the priced
models alone, so a model that isn't plotted never stretches it.

**The frontier** is taken over the priced models, charges and API-equivalents alike (both are
per-token list prices for the tokens the run used): a model is on it when no other priced model
costs no more and is at least as grounded, better on one of the two (point estimates as
printed, not a significance test). It is a staircase through those models, filled in the
accent color; the others are gray.

**Selective labels.** A crowded scatter can't name every point legibly, so names go to the
points that carry the story: the frontier, the least grounded and the most expensive model,
and every point while there are at most :data:`LABEL_ALL`. Every other point gets a number
beside its dot, keyed in a list under the plot in grounded-accuracy order; points whose dots
overlap share one number tag ("4,5"). A name or a number is placed only where

- it stays in the plot and keeps clear of every other name, number, dot, leader, frontier step
  and the ceiling line;
- it can't be read as another model's: a name, and a number beside its dot, is nearer its own
  dot than any other by a clear margin (:data:`NEAREST_MARGIN`); a number joined to its dot by
  a leader keeps :data:`LEADER_CLEARANCE` from every other dot;
- its leader, when it needs one, is at most :data:`LEADER_MAX` px long and crosses no text,
  no other leader, no other model's dot and no frontier step.

A name that finds no such place falls back to a number; the plot grows until every number
fits. Every text, dot and leader that belongs to a point carries ``data-point`` (its index
among the priced points, in the order given), so a test can check each of these rules on the
SVG alone.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass

from identitybench.report import format as fmt
from identitybench.report.figure_kit import (
    Box,
    Canvas,
    Segment,
    Theme,
    box_distance,
    fmt_share,
    label_box,
    segment_distance,
    segment_hits_box,
    segments_cross,
    shown_percent,
    text_width,
    truncate,
)

__all__ = [
    "LABEL_ALL",
    "LEADER_CLEARANCE",
    "LEADER_MAX",
    "NEAREST_MARGIN",
    "WIDTH",
    "CostPoint",
    "cost_frontier",
    "cost_quality_svg",
    "cost_ticks",
]

WIDTH = 820.0
_PAD = 24.0
_NAME, _NOTE, _TICK, _KEY = 13.0, 12.0, 12.0, 11.5
_DOT = 6.0
_ROW = 20.0
"""The key list's row pitch."""
_LABEL_MAX = 225.0
"""A plotted name is shortened beyond this width (the description keeps it whole)."""
LABEL_ALL = 8
"""Up to this many priced models, every point is named (where its name fits)."""
LEADER_MAX = 36.0
"""The longest leader a name or number may have, in px."""
NEAREST_MARGIN = 5.0
"""How much nearer a tag beside its dot (and any name) must be to its own dot than to any other
model's, in px."""
LEADER_CLEARANCE = 14.0
"""How far a number tag with a leader keeps from every other model's dot, in px: its leader
names its dot, and no other dot sits close enough to claim it."""
_CLUSTER = 9.0
"""Dots whose centers are closer than this overlap: they share one number tag."""
_GROW, _GROW_MAX = 80.0, 640.0
_DIAMOND = _DOT * 1.25
"""An API-equivalent's diamond, center to corner: about the area of a charge's circle."""
_EMPTY = "No model has a price yet."


@dataclass(frozen=True, slots=True)
class CostPoint:
    """One model: grounded accuracy (a share, or None when undefined), its cost per 1,000
    decisions when it has a price (else None), whether that price is an API-equivalent rather
    than an API charge (``equivalent``), and ``note``, what its cost is when it has no price (a
    CLI's estimate, unmetered, not reported), listed beside it."""

    name: str
    quality: float | None
    cost: float | None = None
    note: str | None = None
    equivalent: bool = False

    @property
    def cost_text(self) -> str:
        """The price as labels print it: ``$1.25``, or ``◇$1.25`` for an API-equivalent."""
        return (fmt.EQUIVALENT if self.equivalent else "") + fmt.usd(self.cost)


def cost_frontier(points: Sequence[CostPoint]) -> list[CostPoint]:
    """The priced points no other priced point beats on both cost and grounded accuracy (as
    printed), cheapest first."""
    priced = [p for p in points if p.cost is not None and p.quality is not None]

    def beaten(p: CostPoint) -> bool:
        assert p.cost is not None and p.quality is not None
        q = shown_percent(p.quality)
        for o in priced:
            assert o.cost is not None and o.quality is not None
            oq = shown_percent(o.quality)
            if o.cost <= p.cost and oq >= q and (o.cost < p.cost or oq > q):
                return True
        return False

    return sorted((p for p in priced if not beaten(p)), key=lambda p: (p.cost, p.name))


_LADDER = (1.0, 2.0, 5.0)


def _ladder(exponent: int) -> list[float]:
    return [m * 10.0**exponent for m in _LADDER]


def cost_ticks(costs: Sequence[float]) -> tuple[float, float, list[float]]:
    """A log axis for ``costs`` (all positive): its ends on the 1-2-5 ladder, spanning at least
    a decade with room either side of the points, and its tick values."""
    low, high = min(costs) / 1.5, max(costs) * 1.5
    values = [v for e in range(-6, 7) for v in _ladder(e)]
    start = max((v for v in values if v <= low), default=values[0])
    end = min((v for v in values if v >= high), default=values[-1])
    grow_low = True
    while end / start < 10.0 - 1e-9:
        if grow_low and start > values[0]:
            start = max(v for v in values if v < start)
        elif end < values[-1]:
            end = min(v for v in values if v > end)
        grow_low = not grow_low
    ticks = [v for v in values if start * (1 - 1e-9) <= v <= end * (1 + 1e-9)]
    if len(ticks) > 9:
        ticks = [v for v in ticks if math.isclose(v / 10.0 ** math.floor(math.log10(v)), 1.0)]
    return start, end, ticks


def _accuracy_ticks(qualities: Sequence[float]) -> list[float]:
    """The accuracy axis (printed percentages) for the plotted points: up to 100, from a
    round step below the lowest (and no higher than 90, so a tight cluster isn't magnified),
    with at most six intervals."""
    low = min(qualities, default=90.0)
    for step in (1.0, 2.0, 5.0, 10.0, 20.0):
        start = math.floor((low - max(step * 0.4, 1.0)) / step) * step
        start = max(min(start, 90.0 - 90.0 % step), 0.0)  # at least 10 points: no magnifying
        if (100.0 - start) / step <= 6:
            break
    return [start + step * i for i in range(round((100.0 - start) / step) + 1)]


def _money(value: float) -> str:
    """A tick label: ``$5``, ``$0.50``, ``$20``."""
    return f"${value:g}" if value >= 1 else f"${value:.2f}".rstrip("0").rstrip(".")


# --------------------------------------------------------------------------------------------
# Label placement
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Tag:
    """A placed name or number: the points it names, its text box, its baseline and its
    leader (from the dot's ring to the box), if it sits away from its dot."""

    owners: tuple[int, ...]
    box: Box
    baseline: float
    leader: Segment | None
    number: bool
    stacked: bool = False
    """A name over its cost (two lines), when the name and cost on one line found no room."""


_SIDE_GAP = 4.0
_OFFSETS = (0.0, 8.0, 16.0, 26.0, 36.0)


def _spots(px: float, py: float, w: float, h: float) -> list[tuple[float, Box]]:
    """Boxes ``w`` x ``h`` around the point, nearest first: beside it (right preferred, then
    left), above and below it, then further out in every direction."""
    out: list[tuple[float, Box]] = []
    r = _DOT
    xs: list[tuple[float, float]] = []  # (box x0, penalty)
    for e in _OFFSETS:
        xs.append((px + r + _SIDE_GAP + e, 0.0))
        xs.append((px - r - _SIDE_GAP - e - w, 1.5))
    for f in (0.15, 0.5, 0.85):
        xs.append((px - w * f, 2.5))
    clear = r + 2 + h / 2  # the box's center offset that just clears the dot
    dys = sorted(
        {0.0, 7.0, -7.0, clear, -clear}
        | {s * (clear + e) for e in (8.0, 16.0, 26.0) for s in (1, -1)}
        | {s * e for e in (14.0, 22.0, 30.0) for s in (1, -1)},
        key=abs,
    )
    for x0, penalty in xs:
        for dy in dys:
            box = Box(x0, py + dy - h / 2, x0 + w, py + dy + h / 2)
            out.append((box_distance(px, py, box) + penalty, box))
    out.sort(key=lambda c: (c[0], c[1].y0, c[1].x0))
    return out


def _leader_for(px: float, py: float, box: Box) -> Segment | None:
    """The leader from the dot at (px, py) (a group's center) to ``box``, or None when the box
    sits beside the dot."""
    if box_distance(px, py, box) <= _DOT + _SIDE_GAP + 3.0:
        return None
    ex = min(max(px, box.x0), box.x1)
    ey = min(max(py, box.y0), box.y1)
    dx, dy = ex - px, ey - py
    length = math.hypot(dx, dy) or 1.0
    sx, sy = px + dx / length * (_DOT + 2.0), py + dy / length * (_DOT + 2.0)
    return (sx, sy, ex, ey)


class _Layout:
    """Places tags around ``dots`` (point centers) without breaking the module's rules."""

    def __init__(
        self,
        dots: Sequence[tuple[float, float]],
        bounds: Box,
        blocked: Sequence[Box],
        steps: Sequence[Segment],
    ) -> None:
        self.dots = list(dots)
        self.bounds = bounds
        self.blocked = list(blocked)
        """Boxes nothing may overlap: the ceiling line and its label."""
        self.steps = list(steps)
        """The frontier's segments."""
        self.tags: list[_Tag] = []
        self.dot_boxes = [
            Box(x - _DOT - 1, y - _DOT - 1, x + _DOT + 1, y + _DOT + 1) for x, y in dots
        ]

    def _clear(
        self,
        owners: tuple[int, ...],
        box: Box,
        leader: Segment | None,
        *,
        number: bool,
        strict: bool,
    ) -> bool:
        if not box.inside(self.bounds):
            return False
        if any(box.overlaps(b, 2.0) for b in self.blocked):
            return False
        if any(box.overlaps(t.box, 3.0) for t in self.tags):
            return False
        if any(box.overlaps(d, 1.5) for d in self.dot_boxes):
            return False
        grown = Box(box.x0 - 2, box.y0 - 2, box.x1 + 2, box.y1 + 2)
        if any(t.leader is not None and segment_hits_box(t.leader, grown) for t in self.tags):
            return False
        if any(segment_hits_box(s, grown) for s in self.steps):
            return False
        others = [box_distance(x, y, box) for j, (x, y) in enumerate(self.dots) if j not in owners]
        if number and leader is not None and strict:
            if any(d < LEADER_CLEARANCE for d in others):
                return False
        else:
            own = min(box_distance(*self.dots[i], box) for i in owners)
            margin = NEAREST_MARGIN if strict else 0.0
            if any(d < own + margin for d in others):
                return False
        if leader is not None:
            length = math.hypot(leader[2] - leader[0], leader[3] - leader[1])
            if length > LEADER_MAX * (1 if strict else 2):
                return False
            if any(segment_hits_box(leader, b) for b in self.blocked):
                return False
            if any(segment_hits_box(leader, t.box) for t in self.tags):
                return False
            for t in self.tags:
                if t.leader is not None and segments_cross(leader, t.leader):
                    return False
            for j, (x, y) in enumerate(self.dots):
                if j not in owners and segment_distance(x, y, leader) < _DOT + 2.5:
                    return False
            if any(segments_cross(leader, s) for s in self.steps):
                return False
        return True

    def place(
        self,
        owners: tuple[int, ...],
        w: float,
        h: float,
        *,
        number: bool,
        strict: bool = True,
        stacked: bool = False,
    ) -> bool:
        """Place a tag ``w`` x ``h`` for ``owners``; False when no position keeps the rules.
        A name must be nearer its own dot than any other by :data:`NEAREST_MARGIN`; so must a
        number beside its dot, while a number with a leader keeps :data:`LEADER_CLEARANCE` from
        every other dot. Not ``strict`` (a last resort, once the plot has grown all it may), a
        tag only needs to be nearer its own dot than any other and may have a leader twice as
        long."""
        xs = [self.dots[i][0] for i in owners]
        ys = [self.dots[i][1] for i in owners]
        px, py = sum(xs) / len(xs), sum(ys) / len(ys)
        for _, box in _spots(px, py, w, h):
            leader = _leader_for(px, py, box)
            if self._clear(owners, box, leader, number=number, strict=strict):
                baseline = box.y0 + (_NAME if stacked else h) * 0.78
                self.tags.append(_Tag(owners, box, baseline, leader, number, stacked))
                return True
        return False


def _clusters(dots: Sequence[tuple[float, float]]) -> list[tuple[int, ...]]:
    """Groups of points whose dots overlap (centers within :data:`_CLUSTER`), each in index
    order; a point alone is a group of one."""
    parent = list(range(len(dots)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in itertools.combinations(range(len(dots)), 2):
        if math.dist(dots[i], dots[j]) < _CLUSTER:
            parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(dots)):
        groups.setdefault(find(i), []).append(i)
    return sorted((tuple(g) for g in groups.values()), key=lambda g: g[0])


# --------------------------------------------------------------------------------------------
# The figure
# --------------------------------------------------------------------------------------------


def _name_text(p: CostPoint) -> tuple[str, str]:
    """A named point's label: its name (shortened if very long) and its cost."""
    assert p.cost is not None
    return truncate(p.name, _LABEL_MAX, _NAME), p.cost_text


def _name_width(p: CostPoint) -> float:
    name, cost = _name_text(p)
    return text_width(name, _NAME) + 6 + text_width(cost, _NOTE)


_STACK = 14.0
"""The second line's offset in a stacked name (its cost under it)."""


def _stacked_width(p: CostPoint) -> float:
    name, cost = _name_text(p)
    return max(text_width(name, _NAME), text_width(cost, _NOTE))


@dataclass(frozen=True, slots=True)
class _Row:
    """A row of the list under the plot."""

    glyph: str
    """A number (a numbered point) or "" (a model with no API price: a hollow ring)."""
    name: str
    quality: float | None
    cost: str


def _key_list(
    canvas: Canvas,
    top: float,
    sections: Sequence[tuple[str, str, Sequence[_Row]]],
) -> float:
    """The list under the plot: each section (head, note, rows) in order, flowed into two
    columns of about equal height; returns the y below it."""
    items: list[tuple[str, object]] = []
    for head, note, rows in sections:
        if not rows:
            continue
        if items:
            items.append(("gap", None))
        items.append(("head", (head, note)))
        items += [("row", row) for row in rows]
    if not items:
        return top
    heights = [8.0 if kind == "gap" else _ROW for kind, _ in items]
    total = sum(heights)
    split, best = len(items), total
    if sum(1 for kind, _ in items if kind == "row") > 4:
        for k in range(1, len(items)):
            if items[k - 1][0] == "head" or items[k][0] == "gap":
                continue  # never leave a head at a column's foot, or start one with a gap
            left = sum(heights[:k])
            imbalance = abs(total - 2 * left)
            if imbalance < best:
                split, best = k, imbalance
    columns = [items[:split], items[split:]] if split < len(items) else [items]
    gap = 28.0
    col_w = (WIDTH - 2 * _PAD - gap) / 2
    bottom = top
    for c, column in enumerate(columns):
        x = _PAD + c * (col_w + gap)
        rows = [item for kind, item in column if kind == "row"]
        cost_w = max([text_width(r.cost, _NOTE) for r in rows if isinstance(r, _Row)] + [0.0])
        value_w = text_width("100.0%", _NAME)
        name_x = x + 24
        room = max(col_w - 24 - 12 - value_w - 12 - cost_w, 60.0)
        widest = max([text_width(r.name, _NAME) for r in rows if isinstance(r, _Row)] + [0.0])
        name_w = min(widest, room)
        value_right = name_x + name_w + 12 + value_w
        y = top
        for kind, item in column:
            if kind == "gap":
                y += 8.0
                continue
            baseline = y + 14
            if kind == "head":
                assert isinstance(item, tuple)
                head, note = item
                box = canvas.text(x, baseline, str(head), size=_TICK, fill="ink2", weight=600)
                if note:
                    canvas.text(box.x1 + 8, baseline, str(note), size=_TICK, fill="muted")
            else:
                assert isinstance(item, _Row)
                if item.glyph:
                    canvas.text(
                        x + 16,
                        baseline,
                        item.glyph,
                        size=_KEY,
                        fill="ink2",
                        anchor="end",
                        weight=600,
                    )
                else:
                    canvas.circle(
                        x + 10, baseline - 4.3, 4.5, fill="none", stroke="ink2", width=1.5
                    )
                lines = _name_lines(item.name, name_w)
                canvas.text(name_x, baseline, lines[0], size=_NAME, weight=500)
                if len(lines) > 1:
                    canvas.text(name_x, baseline + _ROW - 3, lines[1], size=_NOTE, fill="ink2")
                canvas.text(
                    value_right,
                    baseline,
                    f"{fmt_share(item.quality)}%" if item.quality is not None else "n/a",
                    size=_NAME,
                    anchor="end",
                )
                canvas.text(value_right + 12, baseline, item.cost, size=_NOTE, fill="ink2")
                y += (len(lines) - 1) * (_ROW - 3)
            y += _ROW
        bottom = max(bottom, y)
    return bottom


def _name_lines(name: str, width: float) -> list[str]:
    """A listed model's name in ``width``: whole on one line if it fits; else, for a name with a
    parenthetical ("Qwen3.8-27B (4-bit, self-hosted)"), the parenthetical on a second, smaller
    line, so a disclosure is never cut off; else shortened with an ellipsis."""
    if text_width(name, _NAME) <= width:
        return [name]
    base, paren, rest = name.partition(" (")
    if paren:
        return [truncate(base, width, _NAME), truncate(f"({rest}", width, _NOTE)]
    return [truncate(name, width, _NAME)]


def _legend(canvas: Canvas, top: float, *, frontier: bool) -> float:
    """The marks' key, one row under the plot: a circle for an API charge, a diamond for an
    API-equivalent; returns the y below it."""
    baseline = top + 14
    x = _PAD
    canvas.circle(x + 6, baseline - 4.3, _DOT, fill="neutral", stroke="page", width=2.0)
    box = canvas.text(x + 18, baseline, "API charge (billed or metered)", size=_TICK, fill="ink2")
    x = box.x1 + 22
    canvas.diamond(x + 7, baseline - 4.3, _DIAMOND, fill="neutral", stroke="page", width=2.0)
    box = canvas.text(
        x + 20,
        baseline,
        f"API-equivalent, marked {fmt.EQUIVALENT} (list price × a subscription CLI's tokens)",
        size=_TICK,
        fill="ink2",
    )
    if frontier:
        canvas.text(box.x1 + 16, baseline, "blue: frontier", size=_TICK, fill="muted")
    return top + _ROW + 10


def cost_quality_svg(
    points: Sequence[CostPoint],
    *,
    theme: Theme,
    title: str,
    subtitle: str,
    source: Sequence[str],
    badge: str | None = None,
    reference: tuple[float, str] | None = None,
    key: str = "identitybench-cost",
) -> str:
    """Grounded accuracy (y) against cost per 1,000 decisions (log x) for the priced points
    (circles for API charges, diamonds for API-equivalents), and the rest listed under the plot
    (see the module docstring). Points without a grounded accuracy can't be placed and are named
    in the description and the source line."""
    canvas = Canvas(WIDTH, theme)
    top = canvas.frame(title=title, subtitle=subtitle, badge=badge, pad=_PAD)
    placed = [p for p in points if p.quality is not None]
    priced = [p for p in placed if p.cost is not None]
    unpriced = sorted(
        (p for p in placed if p.cost is None), key=lambda p: (-(p.quality or 0.0), p.name)
    )
    frontier = cost_frontier(points)
    on_frontier = {id(p) for p in frontier}
    numbers: dict[int, int] = {}

    if priced:
        y_ticks = _accuracy_ticks([shown_percent(p.quality or 0.0) for p in priced])
        y_start = y_ticks[0]
        x_start, x_end, x_ticks = cost_ticks([p.cost or 1.0 for p in priced])
        axis_x = _PAD + 34
        plot_x0, plot_x1 = axis_x + 10, WIDTH - _PAD - 8
        plot_top = top + 44

        def x(cost: float) -> float:
            span = math.log10(x_end) - math.log10(x_start)
            return plot_x0 + (plot_x1 - plot_x0) * (math.log10(cost) - math.log10(x_start)) / span

        def layout(
            plot_h: float, *, last: bool = False
        ) -> tuple[_Layout, tuple[float, float, str] | None]:
            """Every tag for a plot ``plot_h`` tall (or as many as fit) and where the ceiling's
            label goes; ``last`` places a number that finds no room by the strict rules by the
            relaxed ones."""
            bottom = plot_top + plot_h

            def yv(share: float) -> float:
                return bottom - (plot_h - 12) * (shown_percent(share) - y_start) / (100.0 - y_start)

            dots = [(x(p.cost or 1.0), yv(p.quality or 0.0)) for p in priced]
            blocked: list[Box] = []
            ceiling = None
            if reference is not None and y_start <= shown_percent(reference[0]) <= 100.0:
                ry = yv(reference[0])
                for spot in (
                    (plot_x0 + 4, ry - 6, "start"),
                    (plot_x1 - 4, ry - 6, "end"),
                    (plot_x0 + 4, ry + 15, "start"),
                    (plot_x1 - 4, ry + 15, "end"),
                ):
                    box = label_box(spot[0], spot[1], reference[1], _TICK, spot[2], False)
                    if not any(box_distance(dx, dy, box) < _DOT + 3 for dx, dy in dots):
                        ceiling = spot
                        blocked.append(box)
                        break
                blocked.append(Box(plot_x0, ry - 0.75, plot_x1, ry + 0.75))
            steps: list[Segment] = []
            index = {id(p): i for i, p in enumerate(priced)}
            for a, b in itertools.pairwise(frontier):
                (ax, ay), (bx, by) = dots[index[id(a)]], dots[index[id(b)]]
                steps += [(ax, ay, bx, ay), (bx, ay, bx, by)]
            bounds = Box(plot_x0 + 2, plot_top - 8, plot_x1, bottom - 3)
            lay = _Layout(dots, bounds, blocked, steps)

            groups = _clusters(dots)
            singles = [g[0] for g in groups if len(g) == 1]
            story = set(singles)
            if len(singles) > LABEL_ALL:
                lowest = min(singles, key=lambda i: (priced[i].quality or 0.0, priced[i].name))
                dearest = max(singles, key=lambda i: (priced[i].cost or 0.0, priced[i].name))
                story = {i for i in singles if id(priced[i]) in on_frontier} | {lowest, dearest}
            order = sorted(
                story,
                key=lambda i: (
                    id(priced[i]) not in on_frontier,
                    -(priced[i].quality or 0.0),
                    priced[i].name,
                ),
            )
            for i in order:
                if not lay.place((i,), _name_width(priced[i]), _NAME, number=False):
                    w = _stacked_width(priced[i])
                    lay.place((i,), w, _NAME + _STACK, number=False, stacked=True)
            named = {t.owners[0] for t in lay.tags}
            keyed = sorted(
                (i for i in range(len(priced)) if i not in named),
                key=lambda i: (-(priced[i].quality or 0.0), priced[i].name),
            )
            numbers.clear()
            numbers.update({i: n for n, i in enumerate(keyed, start=1)})
            for group in sorted(
                (g for g in groups if g[0] not in named), key=lambda g: min(numbers[i] for i in g)
            ):
                text = ",".join(str(numbers[i]) for i in sorted(group, key=numbers.__getitem__))
                w = text_width(text, _KEY, bold=True)
                if not lay.place(group, w, _KEY, number=True) and last:
                    lay.place(group, w, _KEY, number=True, strict=False)  # pragma: no cover
            return lay, ceiling

        def complete(lay: _Layout) -> bool:
            return len(lay.tags) == len(_clusters(lay.dots))

        base_h = max(240.0, min(440.0, 150.0 + 16.0 * len(priced)))
        plot_h = base_h
        lay, ceiling = layout(plot_h)
        while not complete(lay) and plot_h < base_h + _GROW_MAX:
            plot_h += _GROW  # a crowded plot grows until every number finds room
            lay, ceiling = layout(plot_h)
        if not complete(lay):  # pragma: no cover - the tests' densest plots never get here
            lay, ceiling = layout(plot_h, last=True)
        plot_bottom = plot_top + plot_h

        def yv(share: float) -> float:
            return plot_bottom - (plot_h - 12) * (shown_percent(share) - y_start) / (
                100.0 - y_start
            )

        # Axes and grid.
        canvas.text(
            _PAD, plot_top - 22, "Grounded accuracy, %", size=_TICK, fill="ink2", weight=600
        )
        for tick in y_ticks:
            ty = yv(tick / 100.0)
            canvas.line(plot_x0, ty, plot_x1, ty, stroke="grid")
            canvas.text(axis_x, ty + 4, f"{tick:g}", size=_TICK, fill="muted", anchor="end")
        for tick in x_ticks:
            tx = x(tick)
            canvas.line(tx, plot_top + 4, tx, plot_bottom, stroke="grid")
            canvas.text(
                tx, plot_bottom + 18, _money(tick), size=_TICK, fill="muted", anchor="middle"
            )
        canvas.line(plot_x0, plot_bottom, plot_x1, plot_bottom, stroke="axis")
        canvas.text(
            (plot_x0 + plot_x1) / 2,
            plot_bottom + 38,
            "Cost per 1,000 decisions, US$ (log scale)",
            size=_TICK,
            fill="ink2",
            anchor="middle",
            weight=600,
        )
        if reference is not None and y_start <= shown_percent(reference[0]) <= 100.0:
            ry = yv(reference[0])
            canvas.line(plot_x0, ry, plot_x1, ry, stroke="ink2", width=1.0)
            if ceiling is not None:
                canvas.text(
                    ceiling[0], ceiling[1], reference[1], size=_TICK, fill="ink2", anchor=ceiling[2]
                )

        # The frontier, then leaders, then the dots over them, then their names and numbers.
        index = {id(p): i for i, p in enumerate(priced)}
        if len(frontier) > 1:
            first = lay.dots[index[id(frontier[0])]]
            steps = [f"M{first[0]:.1f},{first[1]:.1f}"]
            for p in frontier[1:]:
                px, py = lay.dots[index[id(p)]]
                steps.append(f"H{px:.1f}V{py:.1f}")
            canvas.path("".join(steps), stroke="accent", width=2.0, extra=' stroke-opacity="0.55"')
        for tag in lay.tags:
            if tag.leader is not None:
                canvas.line(
                    *tag.leader,
                    stroke="muted",
                    width=1.0,
                    extra=f' data-point="{" ".join(map(str, tag.owners))}" class="leader"',
                )
        for i, (p, (px, py)) in enumerate(zip(priced, lay.dots, strict=True)):
            fill = "accent" if id(p) in on_frontier else "neutral"
            mark = f' data-point="{i}"' + (' class="equivalent"' if p.equivalent else "")
            if p.equivalent:
                canvas.diamond(px, py, _DIAMOND, fill=fill, stroke="page", width=2.0, extra=mark)
            else:
                canvas.circle(px, py, _DOT, fill=fill, stroke="page", width=2.0, extra=mark)
        for tag in lay.tags:
            owners = " ".join(str(i) for i in tag.owners)
            if tag.number:
                text = ",".join(
                    str(numbers[i]) for i in sorted(tag.owners, key=numbers.__getitem__)
                )
                canvas.text(
                    tag.box.x0,
                    tag.baseline,
                    text,
                    size=_KEY,
                    fill="ink2",
                    weight=600,
                    extra=f' data-point="{owners}"',
                )
                continue
            p = priced[tag.owners[0]]
            name, cost_text = _name_text(p)
            mark = f' data-point="{owners}"'
            # Flush toward the dot (right-aligned when left of it), so a font wider than the
            # estimate grows away from the dot, never into it.
            left = tag.box.x1 <= lay.dots[tag.owners[0]][0]
            tx, anchor = (tag.box.x1, "end") if left else (tag.box.x0, "start")
            if tag.stacked:
                canvas.text(
                    tx, tag.baseline, name, size=_NAME, anchor=anchor, weight=500, extra=mark
                )
                canvas.text(
                    tx,
                    tag.baseline + _STACK,
                    cost_text,
                    size=_NOTE,
                    fill="ink2",
                    anchor=anchor,
                    extra=mark,
                )
                continue
            canvas.labeled(
                tx,
                tag.baseline,
                name,
                cost_text,
                size=_NAME,
                note_size=_NOTE,
                weight=500,
                anchor=anchor,
                extra=mark,
            )
        list_top = plot_bottom + 58
        if any(p.equivalent for p in priced):
            list_top = _legend(canvas, list_top, frontier=len(frontier) > 1)
    else:  # no axis to draw: a placeholder scale would invent one
        canvas.text(_PAD, top + 18, _EMPTY, size=_NAME, fill="muted")
        list_top = top + 34

    keyed_rows = [
        _Row(str(numbers[i]), p.name, p.quality, p.cost_text)
        for i, p in sorted(
            ((i, p) for i, p in enumerate(priced) if i in numbers), key=lambda c: numbers[c[0]]
        )
    ]
    band_rows = [_Row("", p.name, p.quality, p.note or "no price") for p in unpriced]
    list_bottom = _key_list(
        canvas,
        list_top,
        [
            ("Numbered on the chart", "", keyed_rows),
            ("No price", "not on the cost axis", band_rows),
        ],
    )

    missing = [p.name for p in points if p.quality is None]
    lines = list(source)
    if missing:
        lines.insert(0, f"Not shown (grounded accuracy n/a): {fmt.words(missing)}.")
    height = canvas.footer(max(list_bottom + 22, list_top + 8), lines, _PAD)

    def described(p: CostPoint) -> str:
        if p.cost is None:
            cost = p.note or "no price"
        else:
            cost = f"{fmt.usd(p.cost)} {'API-equivalent' if p.equivalent else 'API cost'}"
        return f"{p.name}: grounded accuracy {fmt_share(p.quality)}%, {cost}"

    desc = (
        "Scatter plot of grounded accuracy against cost per 1,000 decisions on a log axis, for "
        "the models with a price: circles are API charges, diamonds API-equivalents (list "
        "price × the tokens a subscription CLI recorded); models without a price are listed "
        f"under it with their grounded accuracy. {subtitle} "
        f"Frontier: {fmt.words([p.name for p in frontier]) or 'none'}. "
        + "; ".join(described(p) for p in points)
        + f". {' '.join(lines)}"
    )
    return canvas.render(height, title=title, desc=desc, key=key)
