"""Shared pieces of the README figures (``report.hero``, ``report.cost_quality`` and
``report.scorecard``): two fixed color themes, a text-width estimate, a frame (title,
subtitle, status badge, source line) and the geometry the cost chart's label placement uses
to keep labels from colliding, clipping or pointing at the wrong mark however many models a
leaderboard holds.

GitHub shows a README image through ``<img>``, where a stylesheet's ``prefers-color-scheme``
follows the operating system rather than the page's theme, so the README embeds a light and a
dark file per figure with ``<picture>`` and each file has fixed colors (:data:`THEMES`). Each
figure is drawn on its theme's GitHub page color, so it is seamless there.

The rules every figure follows, as ``report.svg`` does:

- **Deterministic.** No clock or randomness; coordinates with one decimal; the same rows give
  the same bytes.
- **Accessible.** ``role="img"`` with a ``<title>`` and a ``<desc>`` that states every value.
  Identity is never carried by color alone: every mark sits beside its model's name, or (in a
  crowded cost chart) beside a number keyed to the name in a list under the plot.
- **Measured, never clipped.** Text is laid out with :func:`text_width`, a per-character
  estimate of a system sans (Helvetica's advance widths) padded by :data:`SAFE` for wider
  fallback faces, and every text element carries its size and weight as attributes, so a test
  can rebuild every label's box from the SVG alone (:func:`text_boxes`) and check that none
  overlap or leave the canvas.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

__all__ = [
    "FONT",
    "SAFE",
    "THEMES",
    "Box",
    "Canvas",
    "Segment",
    "Theme",
    "box_distance",
    "fmt_share",
    "label_box",
    "segment_distance",
    "segment_hits_box",
    "segments_cross",
    "shown_percent",
    "text_boxes",
    "text_width",
    "truncate",
    "wrap",
]

FONT = (
    'system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif'
)
SAFE = 1.12
"""Layout slack on :func:`text_width`: SF Pro and DejaVu Sans run 4-10% wider than the
Helvetica widths the estimate uses."""


@dataclass(frozen=True, slots=True)
class Theme:
    """Fixed colors for one GitHub page theme."""

    name: str
    page: str
    """The background: GitHub's page color, so a figure is seamless on it."""
    ink: str
    """Titles, model names and the numbers that matter."""
    ink2: str
    """Subtitles, secondary numbers and column heads."""
    muted: str
    """Ticks, vendors, leader lines and the source line (at least 4.5:1 on the page and on a
    row stripe)."""
    band: str
    """Row stripes."""
    grid: str
    axis: str
    accent: str
    """Grounded accuracy, and the cost frontier."""
    warm: str
    """The gap between decision and grounded accuracy."""
    neutral: str
    """Decision accuracy rings, and models off the cost frontier: a deliberate gray, so the
    accent carries the emphasis (each also differs by shape or position, never by hue alone),
    at least 3:1 on the page and on a row stripe. The accent and warm pair passes the dataviz
    palette validator (lightness band, chroma, all-pairs CVD and normal-vision separation, 3:1
    contrast) on each theme's page."""
    alarm: str
    """The scorecard's ring around a nonzero safety failure (a wrongful approval, an unsafe
    plan, a harmful flip), drawn on the page outside the cell so it shows on every fill."""
    badge: str
    heat: tuple[str, ...]
    """The scorecard's sequential ramp, from no shortfall to the largest, evenly stepped in
    OKLCH lightness (receding into the page at zero in both themes)."""
    heat_ink: tuple[str, ...]
    """The text color on each heat step (WCAG contrast at least 4.5:1)."""
    missing: str
    """The dashed outline of a scorecard cell with no value (n/a, not scored): drawn unfilled,
    so it never reads as the no-shortfall step, which is filled."""


_LIGHT_HEAT = ("#f6f8fa", "#fae5c3", "#f9c68c", "#f7a059", "#ea763c", "#c8412b", "#a72629")
_DARK_HEAT = ("#161b22", "#502824", "#783729", "#a24725", "#ca6112", "#ea8a18", "#fcba43")

THEMES: dict[str, Theme] = {
    "light": Theme(
        name="light",
        page="#ffffff",
        ink="#1f2328",
        ink2="#59636e",
        muted="#656d76",
        band="#f6f8fa",
        grid="#eaeef2",
        axis="#d1d9e0",
        accent="#2a78d6",
        warm="#eb6834",
        neutral="#7d8590",
        alarm="#c8412b",
        badge="#d1d9e0",
        heat=_LIGHT_HEAT,
        heat_ink=("#59636e", "#1f2328", "#1f2328", "#1f2328", "#1f2328", "#ffffff", "#ffffff"),
        missing="#8c959f",
    ),
    "dark": Theme(
        name="dark",
        page="#0d1117",
        ink="#f0f6fc",
        ink2="#9198a1",
        muted="#7d8590",
        band="#151b23",
        grid="#21262d",
        axis="#3d444d",
        accent="#4493f8",
        warm="#d95926",
        neutral="#6e7681",
        alarm="#f0883e",
        badge="#3d444d",
        heat=_DARK_HEAT,
        heat_ink=("#9198a1", "#f0f6fc", "#f0f6fc", "#f0f6fc", "#0d1117", "#0d1117", "#0d1117"),
        missing="#6e7681",
    ),
}
"""``light`` for GitHub's light themes (a white page), ``dark`` for its dark ones."""


# --------------------------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------------------------


def shown_percent(share: float) -> float:
    """A share (0 to 1) as the README tables print it: a percentage rounded to one decimal."""
    return round(share * 100, 1)


def fmt_share(share: float | None) -> str:
    """``0.9333`` -> ``93.3`` (the README tables' text), ``n/a`` for an undefined value."""
    return "n/a" if share is None else f"{shown_percent(share):.1f}"


# --------------------------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------------------------

# Helvetica advance widths (1/1000 em) for ASCII 32-126.
_ASCII = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 "
    "1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556 "
    "333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 "
    "556 556 333 500 278 556 500 722 500 500 500 334 260 334 584"
)
_WIDTHS: dict[str, int] = {chr(32 + i): int(w) for i, w in enumerate(_ASCII.split())}
_WIDTHS.update(
    {
        " ": 278,
        "·": 278,
        "≈": 584,
        "–": 556,
        "—": 1000,
        "−": 584,
        "…": 1000,
        "†": 556,
        "‡": 556,
        "≤": 584,
        "≥": 584,
        "×": 584,
        "◇": 800,
        "✓": 800,
        "✗": 800,
    }
)
_DEFAULT_WIDTH = 650
_BOLD = 1.06
"""Helvetica Bold runs about 6% wider than the regular weight."""


def text_width(text: str, size: float, *, bold: bool = False) -> float:
    """Estimated advance width of ``text`` at ``size`` px, padded by :data:`SAFE`."""
    units = sum(_WIDTHS.get(ch, _DEFAULT_WIDTH) for ch in text)
    return units / 1000 * size * SAFE * (_BOLD if bold else 1.0)


def wrap(text: str, width: float, size: float, *, bold: bool = False) -> list[str]:
    """``text`` broken at spaces into lines no wider than ``width`` (estimated)."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if current and text_width(candidate, size, bold=bold) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def truncate(text: str, width: float, size: float, *, bold: bool = False) -> str:
    """``text``, shortened with an ellipsis until it fits ``width``."""
    if text_width(text, size, bold=bold) <= width:
        return text
    while len(text) > 1 and text_width(text + "…", size, bold=bold) > width:
        text = text[:-1]
    return text.rstrip() + "…"


# --------------------------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    def overlaps(self, other: Box, gap: float = 0.0) -> bool:
        return not (
            self.x1 + gap <= other.x0
            or other.x1 + gap <= self.x0
            or self.y1 + gap <= other.y0
            or other.y1 + gap <= self.y0
        )

    def inside(self, other: Box) -> bool:
        return (
            self.x0 >= other.x0
            and self.x1 <= other.x1
            and self.y0 >= other.y0
            and self.y1 <= other.y1
        )


def label_box(x: float, y: float, text: str, size: float, anchor: str, bold: bool) -> Box:
    """The box a text element occupies: from its baseline ``y``, ascenders up to 0.78 em and
    descenders down to 0.22 em."""
    w = text_width(text, size, bold=bold)
    x0 = x if anchor == "start" else x - w if anchor == "end" else x - w / 2
    return Box(x0, y - size * 0.78, x0 + w, y + size * 0.22)


Segment = tuple[float, float, float, float]
"""A line segment, (x1, y1, x2, y2)."""


def box_distance(x: float, y: float, box: Box) -> float:
    """The distance from the point (x, y) to the nearest point of ``box`` (0 inside it)."""
    dx = max(box.x0 - x, 0.0, x - box.x1)
    dy = max(box.y0 - y, 0.0, y - box.y1)
    return math.hypot(dx, dy)


def segment_distance(x: float, y: float, seg: Segment) -> float:
    """The distance from the point (x, y) to the segment."""
    x1, y1, x2, y2 = seg
    dx, dy = x2 - x1, y2 - y1
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / length))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def segment_hits_box(seg: Segment, box: Box) -> bool:
    """Whether a stretch of the segment of nonzero length lies inside ``box`` (touching its
    edge at one point doesn't count), by Liang-Barsky clipping."""
    x1, y1, x2, y2 = seg
    dx, dy = x2 - x1, y2 - y1
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x1 - box.x0), (dx, box.x1 - x1), (-dy, y1 - box.y0), (dy, box.y1 - y1)):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return (t1 - t0) * math.hypot(dx, dy) > 0.5


def segments_cross(a: Segment, b: Segment) -> bool:
    """Whether two segments cross (a shared endpoint or a touch doesn't count)."""

    def side(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
        return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)

    d1 = side(b[0], b[1], b[2], b[3], a[0], a[1])
    d2 = side(b[0], b[1], b[2], b[3], a[2], a[3])
    d3 = side(a[0], a[1], a[2], a[3], b[0], b[1])
    d4 = side(a[0], a[1], a[2], a[3], b[2], b[3])
    return d1 * d2 < 0 and d3 * d4 < 0


# --------------------------------------------------------------------------------------------
# The canvas
# --------------------------------------------------------------------------------------------


_TITLE, _SUBTITLE, _SOURCE = 22.0, 15.0, 12.0
"""The frame's type sizes (px): every figure is shown at 1:1 in an 830 px README column."""


def _f(value: float) -> str:
    text = f"{value:.1f}"
    return "0.0" if text == "-0.0" else text


def _esc(text: str) -> str:
    return escape(text, quote=True)


class Canvas:
    """Accumulates the elements of one figure, colors referenced by role (the theme's field
    names), and renders the document with its accessible header."""

    def __init__(self, width: float, theme: Theme) -> None:
        self.width = width
        self.theme = theme
        self.parts: list[str] = []

    def color(self, role: str) -> str:
        return str(getattr(self.theme, role))

    def text(
        self,
        x: float,
        y: float,
        text: str,
        *,
        size: float,
        fill: str = "ink",
        anchor: str = "start",
        weight: int = 400,
        extra: str = "",
    ) -> Box:
        """A text element; ``fill`` is a theme role or a literal color. Returns its box."""
        color = fill if fill.startswith("#") else self.color(fill)
        weight_attr = f' font-weight="{weight}"' if weight != 400 else ""
        self.parts.append(
            f'<text x="{_f(x)}" y="{_f(y)}" font-size="{size:g}"{weight_attr} '
            f'text-anchor="{anchor}" fill="{color}"{extra}>{_esc(text)}</text>'
        )
        return label_box(x, y, text, size, anchor, weight >= 600)

    def labeled(
        self,
        x: float,
        y: float,
        text: str,
        note: str,
        *,
        size: float,
        note_size: float,
        gap: float = 6.0,
        weight: int = 400,
        anchor: str = "start",
        extra: str = "",
    ) -> Box:
        """A name and its note (a cost) as one text element, the note a ``<tspan>`` in the
        secondary ink ``gap`` px on, so a font wider than the estimate pushes the note along
        rather than into the name; ``anchor="end"`` puts the run's end at ``x`` (a label left
        of its mark, so extra width grows away from the mark). Returns the box."""
        weight_attr = f' font-weight="{weight}"' if weight != 400 else ""
        span_weight = ' font-weight="400"' if weight != 400 else ""
        self.parts.append(
            f'<text x="{_f(x)}" y="{_f(y)}" font-size="{size:g}"{weight_attr} '
            f'text-anchor="{anchor}" fill="{self.theme.ink}"{extra}>{_esc(text)}'
            f'<tspan dx="{gap:g}" font-size="{note_size:g}"{span_weight} '
            f'fill="{self.theme.ink2}">{_esc(note)}</tspan></text>'
        )
        w = text_width(text, size, bold=weight >= 600) + gap + text_width(note, note_size)
        x0 = x - w if anchor == "end" else x
        return Box(x0, y - size * 0.78, x0 + w, y + size * 0.22)

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        *,
        stroke: str,
        width: float = 1.0,
        extra: str = "",
    ) -> None:
        color = stroke if stroke.startswith("#") else self.color(stroke)
        self.parts.append(
            f'<line x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}" stroke="{color}" '
            f'stroke-width="{width:g}"{extra}/>'
        )

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        *,
        fill: str,
        rx: float = 0.0,
        extra: str = "",
    ) -> None:
        color = fill if fill.startswith("#") or fill == "none" else self.color(fill)
        rounded = f' rx="{_f(rx)}"' if rx else ""
        self.parts.append(
            f'<rect x="{_f(x)}" y="{_f(y)}" width="{_f(w)}" height="{_f(h)}"{rounded} '
            f'fill="{color}"{extra}/>'
        )

    def circle(
        self,
        cx: float,
        cy: float,
        r: float,
        *,
        fill: str,
        stroke: str | None = None,
        width: float = 2.0,
        extra: str = "",
    ) -> None:
        color = fill if fill.startswith("#") or fill == "none" else self.color(fill)
        ring = ""
        if stroke is not None:
            ring_color = stroke if stroke.startswith("#") else self.color(stroke)
            ring = f' stroke="{ring_color}" stroke-width="{width:g}"'
        self.parts.append(
            f'<circle cx="{_f(cx)}" cy="{_f(cy)}" r="{_f(r)}" fill="{color}"{ring}{extra}/>'
        )

    def diamond(
        self,
        cx: float,
        cy: float,
        r: float,
        *,
        fill: str,
        stroke: str | None = None,
        width: float = 2.0,
        extra: str = "",
    ) -> None:
        """A square standing on a corner, ``r`` from its center to each corner: the cost
        figure's mark for an API-equivalent cost, beside the circle of an API charge."""
        color = fill if fill.startswith("#") or fill == "none" else self.color(fill)
        ring = ""
        if stroke is not None:
            ring_color = stroke if stroke.startswith("#") else self.color(stroke)
            ring = f' stroke="{ring_color}" stroke-width="{width:g}" stroke-linejoin="round"'
        d = (
            f"M{_f(cx)},{_f(cy - r)}L{_f(cx + r)},{_f(cy)}L{_f(cx)},{_f(cy + r)}"
            f"L{_f(cx - r)},{_f(cy)}Z"
        )
        self.parts.append(f'<path d="{d}" fill="{color}"{ring}{extra}/>')

    def path(self, d: str, *, stroke: str, width: float = 1.5, extra: str = "") -> None:
        color = stroke if stroke.startswith("#") else self.color(stroke)
        self.parts.append(
            f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width:g}" '
            f'stroke-linejoin="round" stroke-linecap="round"{extra}/>'
        )

    def frame(
        self,
        *,
        title: str,
        subtitle: str,
        badge: str | None,
        pad: float,
    ) -> float:
        """Title, subtitle (wrapped) and the status badge top right; returns the y below."""
        badge_w = text_width(badge, 12, bold=True) + 24 if badge is not None else 0.0
        title_lines = wrap(title, self.width - 2 * pad - badge_w - 24, _TITLE, bold=True)
        y = pad + 22
        for line in title_lines:
            self.text(pad, y, line, size=_TITLE, weight=650)
            y += 28
        if badge is not None:
            bx = self.width - pad - badge_w
            self.rect(
                bx,
                pad + 3,
                badge_w,
                24,
                fill="none",
                rx=12,
                extra=f' stroke="{self.theme.badge}" stroke-width="1"',
            )
            self.text(
                bx + badge_w / 2,
                pad + 19.5,
                badge,
                size=12,
                fill="ink2",
                anchor="middle",
                weight=600,
            )
        y -= 4
        for line in wrap(subtitle, self.width - 2 * pad, _SUBTITLE):
            self.text(pad, y, line, size=_SUBTITLE, fill="ink2")
            y += 21
        return y

    def footer(self, y: float, lines: Sequence[str], pad: float) -> float:
        """The source line(s), wrapped, from baseline ``y``; returns the figure's height."""
        for text in lines:
            for line in wrap(text, self.width - 2 * pad, _SOURCE):
                self.text(pad, y, line, size=_SOURCE, fill="muted")
                y += 17
        return y - 17 + pad

    def render(self, height: float, *, title: str, desc: str, key: str) -> str:
        title_id, desc_id = f"{key}-{self.theme.name}-title", f"{key}-{self.theme.name}-desc"
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" role="img" '
            f'aria-labelledby="{title_id} {desc_id}" width="{_f(self.width)}" '
            f'height="{_f(height)}" viewBox="0 0 {_f(self.width)} {_f(height)}" '
            f'font-family="{_esc(FONT)}">'
            f'<title id="{title_id}">{_esc(title)}</title>'
            f'<desc id="{desc_id}">{_esc(desc)}</desc>'
            "<style>text{font-variant-numeric:tabular-nums}</style>"
            f'<rect width="{_f(self.width)}" height="{_f(height)}" rx="10.0" '
            f'fill="{self.theme.page}"/>' + "".join(self.parts) + "</svg>\n"
        )


# --------------------------------------------------------------------------------------------
# Checks (used by the tests; cheap enough to keep with the layout they check)
# --------------------------------------------------------------------------------------------

_NS = "{http://www.w3.org/2000/svg}"


def text_boxes(svg: str) -> list[tuple[str, Box]]:
    """Every text element of a figure with the box :func:`text_width` gives it, a trailing
    ``<tspan>`` (a label's note) included."""
    root = ET.fromstring(svg)
    boxes: list[tuple[str, Box]] = []
    for t in root.iter(f"{_NS}text"):
        text = t.text or ""
        size = float(t.get("font-size") or 12)
        bold = int(t.get("font-weight") or 400) >= 600
        width = text_width(text, size, bold=bold)
        for span in t.iter(f"{_NS}tspan"):
            span_size = float(span.get("font-size") or size)
            span_bold = int(span.get("font-weight") or t.get("font-weight") or 400) >= 600
            width += float(span.get("dx") or 0) + text_width(
                span.text or "", span_size, bold=span_bold
            )
        x, y = float(t.get("x") or 0), float(t.get("y") or 0)
        anchor = t.get("text-anchor") or "start"
        x0 = x if anchor == "start" else x - width if anchor == "end" else x - width / 2
        boxes.append((text, Box(x0, y - size * 0.78, x0 + width, y + size * 0.22)))
    return boxes
