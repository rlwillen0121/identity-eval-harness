"""Dependency-free SVG charts, built as strings (SPEC §15: README charts are SVG exports).

Charts are part of the published record, so they follow the same rules as the numbers:

- **Deterministic.** No randomness, no clock, coordinates printed with one decimal, every
  collection drawn in the order the caller passes; the same data gives the same bytes.
- **Accessible.** Every chart is ``role="img"`` with a ``<title>`` and a ``<desc>`` that states
  the data in words, labelled by both (``aria-labelledby``). Identity is never carried by color
  alone: points and bars are named in text, and a legend accompanies every second series.
- **Self-contained.** No external fonts or scripts: a system font stack and an embedded
  stylesheet with a dark-mode variant, so a chart reads the same as a README image, as a file
  opened directly, and inlined in the HTML report. Class names carry an ``ibc-`` prefix and
  element IDs the chart's key, so many charts inlined in one page can't collide with each other
  or with the page.
- **Readable at README width** (720 px wide, 12 px text).

Undefined values (n/a) are drawn as text, never as a zero-length mark that would read as 0.
A long name is shortened before its markers (``suffix``: `` (baseline) ‡``), never through them.

Colors follow a validated categorical order (blue, then orange) for series, a muted gray for
SPEC §14 baselines, and a single-hue blue ramp for magnitudes (the heatmap).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from html import escape

from identitybench.report import format as fmt

__all__ = [
    "WIDTH",
    "BarRow",
    "CurvePoint",
    "HeatCell",
    "OperatingMark",
    "ReliabilityBin",
    "ScatterPoint",
    "accuracy_heatmap_svg",
    "bar_chart_svg",
    "calibration_svg",
    "nrc_bars_svg",
    "selective_risk_svg",
    "war_automation_svg",
    "war_target_label",
]

WIDTH = 720
_CHAR_W = 6.6
"""Approximate advance of one 12 px character in a system sans, for layout only."""
_LABEL_MAX = 38
_HEAT_RAMP: tuple[str, ...] = (
    "#cde2fb",
    "#9ec5f4",
    "#6da7ec",
    "#3987e5",
    "#256abf",
    "#184f95",
    "#0d366b",
)
_DARK_FROM = 3
"""Heat steps from this index on are dark enough to need white text; lighter steps take
near-black text that, unlike the page ink, stays dark in dark mode, since the ramp doesn't
change with the theme."""

_STYLE = (
    ".ibc-surface{fill:#fcfcfb}.ibc-ink{fill:#0b0b0b}.ibc-ink2{fill:#52514e}"
    ".ibc-muted{fill:#898781}.ibc-on-dark{fill:#ffffff}.ibc-on-light{fill:#0b0b0b}"
    ".ibc-na{fill:#f0efec}"
    ".ibc-grid{stroke:#e1e0d9;stroke-width:1;fill:none}"
    ".ibc-axis{stroke:#c3c2b7;stroke-width:1;fill:none}"
    ".ibc-ref{stroke:#898781;stroke-width:1;fill:none}"
    ".ibc-s1{fill:#2a78d6}.ibc-s1-line{stroke:#2a78d6;fill:none}"
    ".ibc-s2{fill:#eb6834}.ibc-s2-line{stroke:#eb6834;fill:none}"
    ".ibc-base{fill:#898781}.ibc-base-line{stroke:#898781;fill:none}"
    ".ibc-line{stroke-width:2;stroke-linejoin:round;stroke-linecap:round}"
    ".ibc-whisker{stroke-width:1.5}.ibc-ring{stroke:#fcfcfb;stroke-width:2}"
    ".ibc-bound{stroke-width:1.5;stroke-dasharray:4 3}"
    ".ibc-frontier{fill:none;stroke:#eb6834;stroke-width:2}"
    ".ibc-ceiling{fill:#fcfcfb;stroke:#52514e;stroke-width:2}"
    ".ibc-leader{stroke:#c3c2b7;stroke-width:1}"
    + "".join(f".ibc-h{i}{{fill:{c}}}" for i, c in enumerate(_HEAT_RAMP))
    + '.ibc-chart text{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;font-size:12px}'
    ".ibc-chart .ibc-title{font-size:14px;font-weight:600}.ibc-chart .ibc-small{font-size:10px}"
    "@media (prefers-color-scheme:dark){"
    ".ibc-surface{fill:#1a1a19}.ibc-ink{fill:#f0efec}.ibc-ink2{fill:#c3c2b7}"
    ".ibc-na{fill:#383835}.ibc-grid{stroke:#2c2c2a}.ibc-axis{stroke:#383835}"
    ".ibc-s1{fill:#3987e5}.ibc-s1-line{stroke:#3987e5}"
    ".ibc-s2{fill:#d95926}.ibc-s2-line{stroke:#d95926}.ibc-frontier{stroke:#d95926}"
    ".ibc-ceiling{fill:#1a1a19;stroke:#c3c2b7}"
    ".ibc-ring{stroke:#1a1a19}.ibc-leader{stroke:#383835}}"
)


# --------------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScatterPoint:
    """One configuration on the WAR vs correct automation plane. ``war_bound`` is the exact
    one-sided upper bound on WAR when the metric carries one (with few events the bootstrap
    whisker collapses toward the point, and the bound is what says how high WAR may be);
    ``ceiling`` marks the oracle, drawn as a reference rather than ranked."""

    name: str
    war: float
    automation: float
    war_low: float | None = None
    war_high: float | None = None
    automation_low: float | None = None
    automation_high: float | None = None
    baseline: bool = False
    frontier: bool = False
    suffix: str = ""
    war_bound: float | None = None
    ceiling: bool = False


@dataclass(frozen=True, slots=True)
class BarRow:
    """One row of a horizontal bar chart: one value per series, and an interval for a
    single-series chart. ``suffix`` (markers) is appended after any shortening of ``name``."""

    name: str
    values: tuple[float | None, ...]
    low: float | None = None
    high: float | None = None
    baseline: bool = False
    suffix: str = ""


@dataclass(frozen=True, slots=True)
class HeatCell:
    """One family × tier cell. ``n`` counts the units the small-n flag is decided on (cases, or
    campaigns for reviews); ``items`` is the review items behind it when units are clustered."""

    value: float | None
    n: int
    small_n: bool = False
    items: int | None = None


@dataclass(frozen=True, slots=True)
class CurvePoint:
    x: float
    y: float | None


@dataclass(frozen=True, slots=True)
class OperatingMark:
    """The operating point measured on the scored split (SPEC §12.7). ``label`` is drawn beside
    the mark and says whether the point is certified or empirical; ``status`` is the full
    sentence for the description; ``bound`` is the exact upper bound on WAR, drawn as a dashed
    whisker so a point under the target line can't read as meeting it when its bound doesn't."""

    coverage: float
    war: float | None
    label: str
    status: str
    bound: float | None = None


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    """Mean confidence and accuracy of the answers in one confidence bin."""

    confidence: float
    accuracy: float
    n: int


# --------------------------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------------------------


def _f(value: float) -> str:
    text = f"{value:.1f}"
    return "0.0" if text == "-0.0" else text


def _esc(text: str) -> str:
    return escape(text, quote=True)


def _truncate(text: str, limit: int = _LABEL_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _label(name: str, suffix: str, limit: int = _LABEL_MAX) -> str:
    """``name`` shortened to leave room for ``suffix``, which is always kept whole."""
    return _truncate(name, max(limit - len(suffix), 8)) + suffix


def _text_width(text: str, size: float = 12.0) -> float:
    return len(text) * _CHAR_W * size / 12.0


class _Canvas:
    """Accumulates SVG elements; :meth:`render` wraps them with the accessible header."""

    def __init__(
        self,
        key: str,
        width: float,
        height: float,
        title: str,
        desc: str,
        *,
        note: str | None = None,
    ) -> None:
        self.key = key
        self.width = width
        self.height = height
        self.title = title
        self.desc = desc if note is None else f"{desc} {note}"
        self.note = note
        """A caveat drawn in small type under the title (and appended to the description), so
        the chart carries it when shown alone."""
        self.parts: list[str] = []

    def line(self, x1: float, y1: float, x2: float, y2: float, cls: str) -> None:
        self.parts.append(
            f'<line x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}" class="{cls}"/>'
        )

    def rect(self, x: float, y: float, w: float, h: float, cls: str) -> None:
        self.parts.append(
            f'<rect x="{_f(x)}" y="{_f(y)}" width="{_f(w)}" height="{_f(h)}" class="{cls}"/>'
        )

    def path(self, d: str, cls: str) -> None:
        self.parts.append(f'<path d="{d}" class="{cls}"/>')

    def circle(self, cx: float, cy: float, r: float, cls: str) -> None:
        self.parts.append(f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(r)}" class="{cls}"/>')

    def text(
        self, x: float, y: float, text: str, cls: str = "ibc-ink", *, anchor: str = "start"
    ) -> None:
        self.parts.append(
            f'<text x="{_f(x)}" y="{_f(y)}" text-anchor="{anchor}" class="{cls}">'
            f"{_esc(text)}</text>"
        )

    def render(self) -> str:
        title_id, desc_id = f"{self.key}-title", f"{self.key}-desc"
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" class="ibc-chart" role="img" '
            f'aria-labelledby="{_esc(title_id)} {_esc(desc_id)}" '
            f'width="{_f(self.width)}" height="{_f(self.height)}" '
            f'viewBox="0 0 {_f(self.width)} {_f(self.height)}">'
            f'<title id="{_esc(title_id)}">{_esc(self.title)}</title>'
            f'<desc id="{_esc(desc_id)}">{_esc(self.desc)}</desc>'
            f"<style>{_STYLE}</style>"
        )
        background = (
            f'<rect width="{_f(self.width)}" height="{_f(self.height)}" class="ibc-surface"/>'
        )
        fits = max(int((self.width - 32) / (_CHAR_W * 14 / 12) + 1e-6), 8)
        heading = (
            f'<text x="16.0" y="24.0" class="ibc-ink ibc-title">'
            f"{_esc(_truncate(self.title, fits))}</text>"
        )
        if self.note is not None:
            note_fits = max(int((self.width - 32) / (_CHAR_W * 10 / 12) + 1e-6), 8)
            heading += (
                f'<text x="16.0" y="42.0" class="ibc-muted ibc-small">'
                f"{_esc(_truncate(self.note, note_fits))}</text>"
            )
        return head + background + heading + "".join(self.parts) + "</svg>\n"


def _nice_top(value: float) -> float:
    """The smallest of 1, 2, 2.5, 5 × 10^k at or above ``value`` (1 for nothing positive)."""
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    for mantissa in (1.0, 2.0, 2.5, 5.0, 10.0):
        top = mantissa * 10.0**exponent
        if top >= value * (1 - 1e-9):
            return top
    return 10.0 ** (exponent + 1)


def _ticks(top: float) -> list[float]:
    return [top * i / 5 for i in range(6)]


def _pct_tick(value: float) -> str:
    scaled = value * 100
    return f"{scaled:.0f}%" if abs(scaled - round(scaled)) < 1e-6 else f"{scaled:.1f}%"


def war_target_label(target: float) -> str:
    """The text beside the WAR target line, ``WAR target 2%``: the selective-risk SVG and its
    Vega-Lite twin (``report.vega``) both draw it, so a static reader of either can tell what
    the reference line is. The target is printed exactly (``fmt.pct_exact``, without a
    trailing ``.0`` like the axis ticks), so a 1.25% target never reads 1.2% beside an
    operating point whose bound meets it."""
    return f"WAR target {fmt.pct_exact(target, digits=0)}"


@dataclass(frozen=True, slots=True)
class _Plot:
    """A rectangular plotting area with linear x and y scales from 0."""

    left: float
    top: float
    right: float
    bottom: float
    x_max: float
    y_max: float

    def x(self, value: float) -> float:
        return self.left + (self.right - self.left) * min(max(value, 0.0), self.x_max) / self.x_max

    def y(self, value: float) -> float:
        span = self.bottom - self.top
        return self.bottom - span * min(max(value, 0.0), self.y_max) / self.y_max


def _axes(
    canvas: _Canvas,
    plot: _Plot,
    *,
    x_label: str,
    y_label: str,
    x_tick: Callable[[float], str],
    y_tick: Callable[[float], str],
) -> None:
    """Hairline grid at every tick, tick labels, a baseline and the axis titles."""
    for value in _ticks(plot.y_max):
        y = plot.y(value)
        canvas.line(plot.left, y, plot.right, y, "ibc-grid")
        canvas.text(plot.left - 6, y + 4, y_tick(value), "ibc-muted", anchor="end")
    for value in _ticks(plot.x_max):
        x = plot.x(value)
        canvas.line(x, plot.top, x, plot.bottom, "ibc-grid")
        canvas.text(x, plot.bottom + 16, x_tick(value), "ibc-muted", anchor="middle")
    canvas.line(plot.left, plot.bottom, plot.right, plot.bottom, "ibc-axis")
    canvas.text(
        (plot.left + plot.right) / 2, plot.bottom + 34, x_label, "ibc-ink2", anchor="middle"
    )
    canvas.text(plot.left, plot.top - 10, y_label, "ibc-ink2")


def _diamond(canvas: _Canvas, cx: float, cy: float, r: float, cls: str) -> None:
    canvas.path(
        f"M{_f(cx)},{_f(cy - r)} L{_f(cx + r)},{_f(cy)} L{_f(cx)},{_f(cy + r)} "
        f"L{_f(cx - r)},{_f(cy)} Z",
        cls,
    )


def _legend(canvas: _Canvas, x: float, y: float, entries: Sequence[tuple[str, str, str]]) -> None:
    """Entries ``(kind, class, label)``; kind is ``dot``, ``line``, ``dash`` (a dashed line),
    ``ring`` (a line through a ring: the frontier) or ``diamond``."""
    cursor = x
    for kind, cls, label in entries:
        if kind == "dot":
            canvas.circle(cursor + 5, y - 4, 5, cls)
        elif kind == "ring":
            canvas.line(cursor, y - 4, cursor + 14, y - 4, f"{cls} ibc-line")
            canvas.circle(cursor + 7, y - 4, 5, "ibc-frontier")
        elif kind == "diamond":
            _diamond(canvas, cursor + 6, y - 4, 6, cls)
        elif kind == "dash":
            canvas.line(cursor, y - 4, cursor + 14, y - 4, f"{cls} ibc-bound")
        else:
            canvas.line(cursor, y - 4, cursor + 14, y - 4, f"{cls} ibc-line")
        canvas.text(cursor + 18, y, label, "ibc-ink2")
        cursor += 18 + _text_width(label) + 18


def _place_labels(
    anchors: Sequence[tuple[float, float, str]],
    *,
    left_edge: float,
    right_edge: float,
    bottom_edge: float,
    height: float = 14.0,
) -> list[tuple[float, float, str, str]]:
    """Label positions ``(x, y, text, anchor)`` near each point, so that no two labels overlap
    and none runs past the edges (below ``bottom_edge`` it would sit on the axis tick labels).

    Each label tries three spots: right of its point, left of it, and centered above it. In
    each spot it moves down past the labels in its way or, when that would cross the bottom
    edge, up past them instead; the spot that needs the least movement wins (right, then left,
    then above on ties), so a label stays as close to its point as the others allow. Points are
    visited top to bottom, then left to right, so the result depends only on the data."""
    placed: list[tuple[float, float, float, float]] = []
    out: list[tuple[float, float, str, str]] = []

    def blocker(left: float, width: float, top: float) -> tuple[float, float, float, float] | None:
        return next(
            (
                box
                for box in placed
                if left < box[0] + box[2]
                and box[0] < left + width
                and top < box[1] + box[3]
                and box[1] < top + height
            ),
            None,
        )

    def settle(left: float, width: float, start: float) -> float:
        top = start
        while (box := blocker(left, width, top)) is not None:
            top = box[1] + box[3]
        if top + height > bottom_edge:
            top = start
            while (box := blocker(left, width, top)) is not None:
                top = box[1] - height
        return top

    for x, y, text in sorted(anchors, key=lambda a: (a[1], a[0], a[2])):
        width = _text_width(text, 11)
        beside = min(y - 7, bottom_edge - height)
        spots = (
            (x + 9, x + 9, "start", beside, 0.0),
            (x - 9 - width, x - 9, "end", beside, 1.0),
            (x - width / 2, x, "middle", y - 9 - height, 6.0),
        )
        fitting = [s for s in spots if s[0] >= left_edge and s[0] + width <= right_edge]
        best: tuple[float, float, float, float, str] | None = None
        for left, text_x, anchor, start, penalty in fitting or spots[:1]:
            top = settle(left, width, start)
            cost = abs(top - start) + penalty
            if best is None or cost < best[0]:
                best = (cost, left, top, text_x, anchor)
        assert best is not None
        _, left, top, text_x, anchor = best
        placed.append((left, top, width, height))
        out.append((text_x, top + 11, text, anchor))
    return out


# --------------------------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------------------------


def war_automation_svg(points: Sequence[ScatterPoint], *, key: str, title: str) -> str:
    """WAR (y, lower is better) against correct automation (x, higher is better), with interval
    whiskers on both axes, a dashed whisker to WAR's exact upper bound where there is one, and
    the Pareto frontier: ringed points, joined when there are several (SPEC §12.2: always shown
    together; the configurations no other beats on both form the frontier). A ``ceiling`` point
    (the oracle) is a hollow diamond, labeled as the ceiling, and never ringed."""
    frontier = sorted((p for p in points if p.frontier), key=lambda p: (p.automation, p.war))
    ceilings = [p for p in points if p.ceiling]

    def named(p: ScatterPoint) -> str:
        return p.name + p.suffix

    described = "; ".join(
        f"{named(p)}: WAR {_pct_tick(p.war)}"
        + (f" (exact upper bound {_pct_tick(p.war_bound)})" if p.war_bound is not None else "")
        + f", correct automation {_pct_tick(p.automation)}"
        + (" (frontier)" if p.frontier else "")
        + (" (ceiling)" if p.ceiling else "")
        for p in points
    )
    desc = (
        f"Scatter of WAR against correct automation for {len(points)} configurations, with "
        "interval whiskers. Pareto frontier"
        + (" (the oracle ceiling excluded)" if ceilings else "")
        + f": {', '.join(named(p) for p in frontier) or 'none'}. {described}."
    )
    height = 420.0
    canvas = _Canvas(key, WIDTH, height, title, desc)
    y_top = max([0.05, *(max(p.war_high or p.war, p.war_bound or 0.0) for p in points)])
    plot = _Plot(
        left=64, top=76, right=WIDTH - 24, bottom=height - 52, x_max=1.0, y_max=_nice_top(y_top)
    )
    entries = [
        ("dot", "ibc-s1", "Model"),
        ("dot", "ibc-base", "Baseline"),
        ("ring", "ibc-s2-line", "Pareto frontier"),
    ]
    if ceilings:
        entries.append(("diamond", "ibc-ceiling", "Oracle ceiling"))
    if any(p.war_bound is not None for p in points):
        entries.append(("dash", "ibc-ref", "Exact WAR upper bound"))
    _legend(canvas, 16, 46, entries)
    _axes(
        canvas,
        plot,
        x_label="Correct automation (higher is better) →",
        y_label="WAR (lower is better)",
        x_tick=_pct_tick,
        y_tick=_pct_tick,
    )
    if len(frontier) >= 2:
        d = " ".join(
            f"{'M' if i == 0 else 'L'}{_f(plot.x(p.automation))},{_f(plot.y(p.war))}"
            for i, p in enumerate(frontier)
        )
        canvas.path(d, "ibc-s2-line ibc-line")
    for p in points:
        cls = "ibc-base" if p.baseline else "ibc-s1"
        x, y = plot.x(p.automation), plot.y(p.war)
        if p.war_bound is not None and p.war_bound > p.war:
            canvas.line(x, y, x, plot.y(p.war_bound), "ibc-ref ibc-bound")
        if p.war_low is not None and p.war_high is not None:
            canvas.line(x, plot.y(p.war_low), x, plot.y(p.war_high), f"{cls}-line ibc-whisker")
        if p.automation_low is not None and p.automation_high is not None:
            canvas.line(
                plot.x(p.automation_low), y, plot.x(p.automation_high), y, f"{cls}-line ibc-whisker"
            )
    for p in points:
        x, y = plot.x(p.automation), plot.y(p.war)
        if p.ceiling:
            _diamond(canvas, x, y, 7, "ibc-ceiling")
            continue
        if p.frontier:
            canvas.circle(x, y, 9, "ibc-frontier")
        canvas.circle(x, y, 5, f"{'ibc-base' if p.baseline else 'ibc-s1'} ibc-ring")
    labels = _place_labels(
        [
            (
                plot.x(p.automation),
                plot.y(p.war),
                _label(p.name, p.suffix + (" · ceiling" if p.ceiling else ""), 44),
            )
            for p in points
        ],
        left_edge=plot.left - 40,
        right_edge=WIDTH - 8,
        bottom_edge=plot.bottom - 2,
    )
    for x, y, text, anchor in labels:
        canvas.text(x, y, text, "ibc-ink ibc-small", anchor=anchor)
    return canvas.render()


def bar_chart_svg(
    rows: Sequence[BarRow],
    *,
    key: str,
    title: str,
    axis_label: str,
    fmt: Callable[[float], str],
    series: Sequence[str] = ("value",),
    reference: tuple[float, str] | None = None,
) -> str:
    """Horizontal bars in the given row order, one bar per series per row, with an interval
    whisker for single-series charts and an optional labeled reference line. Single-series
    bars are blue for models and gray for baselines; several series take the categorical
    order and get a legend."""
    multi = len(series) > 1
    if len(series) > 2:
        raise ValueError("at most two series fit the categorical order validated all-pairs")
    if any(len(row.values) != len(series) for row in rows):
        raise ValueError("every row needs one value per series")
    names = [_label(row.name, row.suffix) for row in rows]
    label_w = min(max([_text_width(n) for n in names] + [40.0]) + 8, 280.0)
    bar_h, gap = 16.0, 2.0
    row_h = len(series) * (bar_h + gap) + 12
    top = 72.0 if multi or reference else 52.0
    height = top + row_h * max(len(rows), 1) + 44
    peaks = [v for row in rows for v in (*row.values, row.high) if v is not None]
    if reference is not None:
        peaks.append(reference[0] * 1.2)
    plot = _Plot(
        left=16 + label_w,
        top=top,
        right=WIDTH - 84,
        bottom=height - 44,
        x_max=_nice_top(max(peaks, default=1.0)),
        y_max=1.0,
    )
    described = "; ".join(
        f"{row.name}{row.suffix}: "
        + ", ".join(
            f"{s} {fmt(v) if v is not None else 'n/a'}"
            if multi
            else (fmt(v) if v is not None else "n/a")
            for s, v in zip(series, row.values, strict=True)
        )
        + (
            f" (interval {fmt(row.low)} to {fmt(row.high)})"
            if not multi and row.low is not None and row.high is not None
            else ""
        )
        for row in rows
    )
    canvas = _Canvas(key, WIDTH, height, title, f"Horizontal bar chart, {axis_label}. {described}.")
    if multi:
        _legend(canvas, 16, 46, [("dot", f"ibc-s{i + 1}", s) for i, s in enumerate(series)])
    for value in _ticks(plot.x_max):
        x = plot.x(value)
        canvas.line(x, plot.top, x, plot.bottom, "ibc-grid")
        canvas.text(x, plot.bottom + 16, fmt(value), "ibc-muted", anchor="middle")
    canvas.text(
        (plot.left + plot.right) / 2, plot.bottom + 34, axis_label, "ibc-ink2", anchor="middle"
    )
    canvas.line(plot.left, plot.top, plot.left, plot.bottom, "ibc-axis")
    for index, (row, name) in enumerate(zip(rows, names, strict=True)):
        row_top = plot.top + 6 + index * row_h
        canvas.text(16, row_top + row_h / 2 - 2, name, "ibc-ink")
        for s, value in enumerate(row.values):
            y = row_top + s * (bar_h + gap)
            cls = f"ibc-s{s + 1}" if multi else ("ibc-base" if row.baseline else "ibc-s1")
            if value is None:
                canvas.text(plot.left + 6, y + 12, "n/a", "ibc-muted")
                continue
            x1 = plot.x(value)
            if x1 > plot.left:
                canvas.path(_bar_path(plot.left, x1, y, bar_h), cls)
            end = x1
            if not multi and row.low is not None and row.high is not None:
                lo, hi, mid = plot.x(row.low), plot.x(row.high), y + bar_h / 2
                canvas.line(lo, mid, hi, mid, "ibc-whisker ibc-ref")
                canvas.line(lo, mid - 4, lo, mid + 4, "ibc-ref")
                canvas.line(hi, mid - 4, hi, mid + 4, "ibc-ref")
                end = max(end, hi)
            canvas.text(end + 6, y + 12, fmt(value), "ibc-ink2")
    if reference is not None:
        x = plot.x(reference[0])
        canvas.line(x, plot.top - 4, x, plot.bottom, "ibc-ref")
        canvas.text(x, plot.top - 10, reference[1], "ibc-ink2", anchor="middle")
    return canvas.render()


def _bar_path(x0: float, x1: float, y: float, h: float) -> str:
    """A bar square at the baseline with a 4 px rounded data end."""
    r = min(4.0, (x1 - x0) / 2, h / 2)
    return (
        f"M{_f(x0)},{_f(y)} H{_f(x1 - r)} Q{_f(x1)},{_f(y)} {_f(x1)},{_f(y + r)} "
        f"V{_f(y + h - r)} Q{_f(x1)},{_f(y + h)} {_f(x1 - r)},{_f(y + h)} H{_f(x0)} Z"
    )


def nrc_bars_svg(rows: Sequence[BarRow], *, key: str, title: str) -> str:
    """NRC per configuration with 95% intervals and the all-human status quo at 1 (SPEC §12.3:
    NRC < 1 beats sending everything to a human, under these costs and this case mix)."""
    return bar_chart_svg(
        rows,
        key=key,
        title=title,
        axis_label="Normalized risk cost (lower is better; always_escalate = 1)",
        fmt=lambda v: f"{v:.2f}",
        reference=(1.0, "status quo = 1"),
    )


def _heat_text(cell: HeatCell, unit: str) -> str:
    """One cell in words, for the description."""
    assert cell.value is not None
    items = f", {cell.items} items" if cell.items is not None else ""
    small = ", small n" if cell.small_n else ""
    return f"{_pct_tick(cell.value)} over {cell.n} {unit}{items}{small}"


def accuracy_heatmap_svg(
    cells: Mapping[tuple[str, str], HeatCell],
    rows: Sequence[str],
    columns: Sequence[str],
    *,
    key: str,
    title: str,
    unit: str = "cases",
    small_n: int | None = None,
) -> str:
    """Accuracy by policy family (rows) × tier (columns) on a single-hue ramp, each cell
    labeled with its value and n (``†`` when small); a missing or n/a cell is gray. ``unit``
    names what n counts (plural: "cases", "campaigns") and ``small_n`` the flag's threshold,
    both stated in the legend so n and † read the same with or without the description."""
    cell_w, cell_h, gap = 92.0, 40.0, 2.0
    label_w = max([_text_width(r) for r in rows] + [40.0]) + 16
    left, top = 16 + label_w, 76.0
    title_w = min(max(32 + _text_width(title, 14), 380.0), float(WIDTH))
    width = max(left + len(columns) * (cell_w + gap) + 16, title_w)
    height = top + len(rows) * (cell_h + gap) + 74
    described = "; ".join(
        f"{r} {c}: "
        + (
            "n/a"
            if (cell := cells.get((r, c))) is None or cell.value is None
            else _heat_text(cell, unit)
        )
        for r in rows
        for c in columns
    )
    canvas = _Canvas(
        key, width, height, title, f"Heatmap of accuracy by policy family and tier. {described}."
    )
    for j, column in enumerate(columns):
        canvas.text(
            left + j * (cell_w + gap) + cell_w / 2, top - 10, column, "ibc-ink2", anchor="middle"
        )
    for i, row in enumerate(rows):
        y = top + i * (cell_h + gap)
        canvas.text(16, y + cell_h / 2 + 4, row, "ibc-ink")
        for j, column in enumerate(columns):
            x = left + j * (cell_w + gap)
            cell = cells.get((row, column))
            if cell is None or cell.value is None:
                canvas.rect(x, y, cell_w, cell_h, "ibc-na")
                canvas.text(x + cell_w / 2, y + cell_h / 2 + 4, "n/a", "ibc-muted", anchor="middle")
                continue
            step = min(int(cell.value * len(_HEAT_RAMP)), len(_HEAT_RAMP) - 1)
            canvas.rect(x, y, cell_w, cell_h, f"ibc-h{step}")
            ink = "ibc-on-dark" if step >= _DARK_FROM else "ibc-on-light"
            canvas.text(
                x + cell_w / 2, y + 17, _pct_tick(round(cell.value, 3)), ink, anchor="middle"
            )
            n_text = f"n={cell.n}" + ("†" if cell.small_n else "")
            canvas.text(x + cell_w / 2, y + 31, n_text, f"{ink} ibc-small", anchor="middle")
    legend_y = top + len(rows) * (cell_h + gap) + 18
    canvas.text(16, legend_y + 10, "0%", "ibc-muted")
    for i in range(len(_HEAT_RAMP)):
        canvas.rect(44 + i * 22, legend_y, 20, 12, f"ibc-h{i}")
    canvas.text(44 + len(_HEAT_RAMP) * 22 + 6, legend_y + 10, "100% accuracy", "ibc-muted")
    flag = f"† fewer than {small_n}" if small_n is not None else "† small n"
    canvas.text(16, legend_y + 30, f"n = {unit} per cell · {flag}", "ibc-muted")
    return canvas.render()


def _curve_path(plot: _Plot, points: Sequence[tuple[float, float]]) -> str:
    return " ".join(
        f"{'M' if i == 0 else 'L'}{_f(plot.x(x))},{_f(plot.y(y))}"
        for i, (x, y) in enumerate(points)
    )


_MAX_MARKERS = 25
"""Above this many points a curve is drawn as a line only, so markers don't smear into it."""


def selective_risk_svg(
    points: Sequence[CurvePoint],
    *,
    key: str,
    title: str,
    target: float | None = None,
    operating: OperatingMark | None = None,
    note: str | None = None,
) -> str:
    """WAR (y) against coverage (x) as the auto-decide threshold varies (SPEC §12.7), with the
    WAR target as a reference line and the operating point measured on the scored split,
    labeled certified or empirical, with a dashed whisker up to its exact WAR bound. ``note``
    is a caveat drawn under the title (a prompt variant whose confidences are anchored)."""
    defined = sorted((p.x, p.y) for p in points if p.y is not None)
    peaks = [0.05, *(y for _, y in defined)]
    if target is not None:
        peaks.append(target * 2)
    if operating is not None:
        peaks += [v for v in (operating.war, operating.bound) if v is not None]
    peak = max(peaks)
    height = 360.0
    described_point = ""
    if operating is not None:
        war = _pct_tick(operating.war) if operating.war is not None else "n/a"
        bound = ""
        if operating.bound is not None:
            # As precise as the status after it, which states the bound's side of the target.
            shown = (
                _pct_tick(operating.bound)
                if target is None
                else fmt.pct_against(operating.bound, target)
            )
            bound = f", exact upper bound {shown}"
        described_point = (
            f"; operating point at coverage {_pct_tick(operating.coverage)}, WAR {war}"
            + bound
            + f", {operating.status}"
        )
    canvas = _Canvas(
        key,
        WIDTH,
        height,
        title,
        f"Selective-risk curve: WAR against coverage at {fmt.units(len(defined), 'threshold')}"
        + (f"; {war_target_label(target)}" if target is not None else "")
        + described_point
        + ". "
        + "; ".join(f"coverage {_pct_tick(x)}: WAR {_pct_tick(y)}" for x, y in defined)
        + ".",
        note=note,
    )
    plot = _Plot(
        left=64, top=64, right=WIDTH - 24, bottom=height - 52, x_max=1.0, y_max=_nice_top(peak)
    )
    _axes(
        canvas,
        plot,
        x_label="Coverage: share of the workload auto-decided →",
        y_label="WAR at the threshold (lower is better)",
        x_tick=_pct_tick,
        y_tick=_pct_tick,
    )
    if target is not None:
        y = plot.y(target)
        canvas.line(plot.left, y, plot.right, y, "ibc-ref")
        canvas.text(plot.right, y - 6, war_target_label(target), "ibc-ink2", anchor="end")
    if len(defined) >= 2:
        canvas.path(_curve_path(plot, defined), "ibc-s1-line ibc-line")
    if len(defined) <= _MAX_MARKERS:
        for x, y in defined:
            canvas.circle(plot.x(x), plot.y(y), 4, "ibc-s1 ibc-ring")
    if operating is not None and operating.war is not None:
        x, y = plot.x(operating.coverage), plot.y(operating.war)
        if operating.bound is not None and operating.bound > operating.war:
            top = plot.y(operating.bound)
            canvas.line(x, y, x, top, "ibc-s2-line ibc-bound")
            canvas.line(x - 4, top, x + 4, top, "ibc-s2-line ibc-whisker")
        canvas.circle(x, y, 6, "ibc-s2 ibc-ring")
        # Beside the bound's cap when there is one (clear of the curve and the target line,
        # and next to the number the label quotes), else just above the point.
        label_y = (
            plot.y(operating.bound) + 4
            if operating.bound is not None and operating.bound > operating.war
            else y - 10
        )
        width = _text_width(operating.label, 10)
        if x + 10 + width <= plot.right:
            canvas.text(x + 10, label_y, operating.label, "ibc-ink ibc-small")
        else:
            label_x = max(x - 10, plot.left + width)
            canvas.text(label_x, label_y, operating.label, "ibc-ink ibc-small", anchor="end")
    return canvas.render()


def calibration_svg(
    bins: Sequence[ReliabilityBin], *, key: str, title: str, note: str | None = None
) -> str:
    """Reliability diagram: accuracy against mean confidence per confidence bin, with the
    diagonal of perfect calibration. ``note`` is a caveat drawn under the title."""
    height = 360.0
    canvas = _Canvas(
        key,
        WIDTH,
        height,
        title,
        f"Reliability diagram over {len(bins)} confidence bins. "
        + "; ".join(
            f"mean confidence {_pct_tick(b.confidence)}: accuracy {_pct_tick(b.accuracy)} of {b.n}"
            for b in bins
        )
        + ".",
        note=note,
    )
    plot = _Plot(left=64, top=64, right=WIDTH - 24, bottom=height - 52, x_max=1.0, y_max=1.0)
    _axes(
        canvas,
        plot,
        x_label="Mean confidence in the bin →",
        y_label="Accuracy in the bin",
        x_tick=_pct_tick,
        y_tick=_pct_tick,
    )
    canvas.line(plot.x(0), plot.y(0), plot.x(1), plot.y(1), "ibc-ref")
    canvas.text(plot.x(0.97), plot.y(0.97) + 16, "perfect calibration", "ibc-muted", anchor="end")
    ordered = sorted((b.confidence, b.accuracy) for b in bins)
    if len(ordered) >= 2:
        canvas.path(_curve_path(plot, ordered), "ibc-s1-line ibc-line")
    for b in bins:
        canvas.circle(plot.x(b.confidence), plot.y(b.accuracy), 4, "ibc-s1 ibc-ring")
    return canvas.render()
