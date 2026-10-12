"""The README's hero figure: per model, two horizontal bars (decision accuracy and grounded
accuracy), in a light and a dark variant (SPEC §15: README charts are SVG exports).

GitHub shows a README image through ``<img>``, where a stylesheet's ``prefers-color-scheme``
follows the operating system rather than the page's theme, so the README embeds both variants
with ``<picture>`` and each file has fixed colors (:data:`THEMES`). Each is drawn on its
theme's GitHub page color, so it is seamless there, and still readable when the browser's
color scheme and the GitHub theme disagree (the media query follows the former).

The same rules as ``report.svg``:

- **Deterministic.** No clock or randomness; coordinates with one decimal; rows in the order
  given. The same rows give the same bytes.
- **Accessible.** ``role="img"`` with a ``<title>`` and a ``<desc>`` that states every value;
  identity is never carried by color alone (every pair of bars sits beside its model's name,
  and the legend names both series in text).
- **Honest marks.** Bars start at zero on a fixed 0-100% axis, so a saturated metric reads as
  saturated; an undefined value is drawn as the text n/a, never as a zero-length bar.

Colors are the validated categorical order of ``report.svg`` (blue, then orange), each stepped
for its theme, with recessive hairline gridlines.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

from identitybench.report.svg import _bar_path, _f, _text_width

__all__ = ["THEMES", "HeroRow", "Theme", "hero_svg"]

WIDTH = 820.0
_FONT = 'system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif'
_BAR_H = 13.0
_BAR_GAP = 4.0
_GROUP_GAP = 20.0
_TICKS = (0.0, 0.25, 0.5, 0.75, 1.0)
_RIGHT = 76.0
"""Room right of the 100% gridline for a value label (``100.0%`` at 13 px), with margin
for a wider fallback font than the estimate assumes (Verdana is about 15% wider)."""


@dataclass(frozen=True, slots=True)
class Theme:
    """Fixed colors for one page theme."""

    name: str
    ink: str
    """Titles and model names."""
    ink2: str
    """Subtitle, legend and value labels."""
    muted: str
    """Tick labels and the footnote."""
    grid: str
    axis: str
    series: tuple[str, str]
    badge: str
    """The interim badge's outline."""
    page: str
    """The background: GitHub's page color for the theme, so the figure is seamless there and
    still readable where the browser's color scheme and GitHub's theme disagree."""


THEMES: dict[str, Theme] = {
    "light": Theme(
        name="light",
        ink="#1f2328",
        ink2="#52514e",
        muted="#6e6d68",
        grid="#e7e6e0",
        axis="#c3c2b7",
        series=("#2a78d6", "#eb6834"),
        badge="#d0cfc8",
        page="#ffffff",
    ),
    "dark": Theme(
        name="dark",
        ink="#f0efec",
        ink2="#c3c2b7",
        muted="#9a9890",
        grid="#2a2c30",
        axis="#43464c",
        series=("#3987e5", "#d95926"),
        badge="#43464c",
        page="#0d1117",
    ),
}
"""``light`` for GitHub's light themes (a white page), ``dark`` for its dark ones."""


@dataclass(frozen=True, slots=True)
class HeroRow:
    """One model: its two values as shares (0 to 1), or None when undefined."""

    name: str
    values: tuple[float | None, float | None]


def _esc(text: str) -> str:
    return escape(text, quote=True)


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _wrap(text: str, width: float, size: float) -> list[str]:
    """``text`` broken at spaces into lines no wider than ``width`` (estimated)."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if current and _text_width(candidate, size) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


class _Parts:
    def __init__(self) -> None:
        self.items: list[str] = []

    def text(
        self,
        x: float,
        y: float,
        text: str,
        cls: str,
        *,
        anchor: str = "start",
    ) -> None:
        self.items.append(
            f'<text x="{_f(x)}" y="{_f(y)}" text-anchor="{anchor}" class="{cls}">'
            f"{_esc(text)}</text>"
        )

    def line(self, x1: float, y1: float, x2: float, y2: float, cls: str) -> None:
        self.items.append(
            f'<line x1="{_f(x1)}" y1="{_f(y1)}" x2="{_f(x2)}" y2="{_f(y2)}" class="{cls}"/>'
        )

    def raw(self, element: str) -> None:
        self.items.append(element)


# Type sizes (px), for layout estimates and the stylesheet alike.
_TITLE, _SUB, _NAME, _VALUE, _SMALL = 22.0, 14.0, 14.0, 13.0, 12.0


def _style(theme: Theme) -> str:
    s1, s2 = theme.series
    return (
        f"text{{font-family:{_FONT};font-size:{_VALUE:g}px;fill:{theme.ink2}}}"
        f".h-title{{font-size:{_TITLE:g}px;font-weight:600;fill:{theme.ink}}}"
        f".h-sub{{font-size:{_SUB:g}px;fill:{theme.ink2}}}"
        f".h-name{{font-size:{_NAME:g}px;font-weight:500;fill:{theme.ink}}}"
        ".h-value{font-variant-numeric:tabular-nums}"
        f".h-strong{{font-weight:600;fill:{theme.ink}}}"
        f".h-tick{{font-size:{_SMALL:g}px;fill:{theme.muted};font-variant-numeric:tabular-nums}}"
        f".h-note{{font-size:{_SMALL:g}px;fill:{theme.muted}}}"
        f".h-badge-text{{font-size:{_SMALL:g}px;font-weight:600;fill:{theme.ink2}}}"
        f".h-badge{{fill:none;stroke:{theme.badge};stroke-width:1}}"
        f".h-grid{{stroke:{theme.grid};stroke-width:1}}"
        f".h-axis{{stroke:{theme.axis};stroke-width:1}}"
        f".h-ref{{stroke:{theme.axis};stroke-width:1.5}}"
        f".h-s1{{fill:{s1}}}.h-s2{{fill:{s2}}}.h-page{{fill:{theme.page}}}"
    )


def hero_svg(
    rows: Sequence[HeroRow],
    *,
    theme: Theme,
    title: str,
    subtitle: str,
    series: tuple[str, str],
    note: str,
    badge: str | None = None,
    reference: tuple[float, str] | None = None,
    key: str = "identitybench-results",
) -> str:
    """Two bars per row on a shared 0-100% axis, in the rows' order.

    ``series`` names the two bars (the legend and the description); ``badge`` is a short
    status drawn top right (an interim leaderboard); ``subtitle`` and ``note`` (small print
    under the axis) wrap to the width; ``reference`` is a labeled value on the axis (the
    oracle ceiling), drawn as a rule with a label rather than as a bar, so it stays apart from
    the models. Drawn at 1:1 it reads at README size: :data:`WIDTH` px wide, 14 px names.
    """
    pad = 12.0
    badge_w = _text_width(badge, _SMALL) * 1.08 + 22 if badge is not None else 0.0
    sub_lines = _wrap(subtitle, WIDTH - 2 * pad, _SUB)
    note_lines = _wrap(note, WIDTH - 2 * pad, _SMALL)
    label_w = min(max([_text_width(r.name, _NAME) for r in rows] + [96.0]) + 18, 280.0)
    left = pad + label_w
    right = WIDTH - _RIGHT
    legend_y = 72.0 + 19 * len(sub_lines)
    top = legend_y + 48
    group_h = 2 * _BAR_H + _BAR_GAP + _GROUP_GAP
    bottom = top + max(len(rows), 1) * group_h - _GROUP_GAP + 14
    height = bottom + 52 + 17 * len(note_lines)

    def x(value: float) -> float:
        return left + (right - left) * min(max(value, 0.0), 1.0)

    described = "; ".join(
        f"{row.name}: "
        + ", ".join(
            f"{name} {_pct(v) if v is not None else 'n/a'}"
            for name, v in zip(series, row.values, strict=True)
        )
        for row in rows
    )
    desc = (
        f"Horizontal bar chart of {len(rows)} models, two bars each on a 0 to 100% axis: "
        f"{series[0]} and {series[1]}. {subtitle}. {described}. {note}"
    )
    parts = _Parts()
    parts.text(pad, 32, title, "h-title")
    for i, line in enumerate(sub_lines):
        parts.text(pad, 58 + 19 * i, line, "h-sub")
    if badge is not None:
        bx = WIDTH - pad - badge_w
        parts.raw(
            f'<rect x="{_f(bx)}" y="13.0" width="{_f(badge_w)}" height="26.0" rx="13.0" '
            'class="h-badge"/>'
        )
        parts.text(bx + badge_w / 2, 30.5, badge, "h-badge-text", anchor="middle")
    cursor = pad
    for i, name in enumerate(series):
        parts.raw(
            f'<rect x="{_f(cursor)}" y="{_f(legend_y - 11)}" width="13.0" height="13.0" '
            f'rx="3.0" class="h-s{i + 1}"/>'
        )
        parts.text(cursor + 20, legend_y, name, "h-sub")
        cursor += 20 + _text_width(name, _SUB) + 28
    # Value labels first, so the vertical rules can leave a gap where a label crosses them.
    labels: list[tuple[float, float, str, str]] = []
    bars: list[str] = []
    for index, row in enumerate(rows):
        group_top = top + index * group_h
        middle = group_top + _BAR_H + _BAR_GAP / 2
        parts.text(left - 14, middle + 5, row.name, "h-name", anchor="end")
        for s, value in enumerate(row.values):
            y = group_top + s * (_BAR_H + _BAR_GAP)
            baseline = y + _BAR_H / 2 + 4.5
            if value is None:
                labels.append((left + 6, baseline, "n/a", "h-tick"))
                continue
            end = x(value)
            if end > left:
                bars.append(f'<path d="{_bar_path(left, end, y, _BAR_H)}" class="h-s{s + 1}"/>')
            strong = " h-strong" if s == 1 else ""
            labels.append((end + 6, baseline, _pct(value), f"h-value{strong}"))

    def rule(rx: float, cls: str) -> None:
        """A vertical rule from the top of the plot to the axis, broken around labels."""
        blocked = sorted(
            (ly - 12, ly + 3)
            for lx, ly, text, _ in labels
            if lx - 3 <= rx <= lx + _text_width(text, _VALUE) + 3
        )
        y0 = top - 8
        for b0, b1 in blocked:
            if b0 > y0:
                parts.line(rx, y0, rx, b0, cls)
            y0 = max(y0, b1)
        if y0 < bottom:
            parts.line(rx, y0, rx, bottom, cls)

    for tick in _TICKS:
        tx = x(tick)
        if reference is None or tick != reference[0]:
            rule(tx, "h-axis" if tick == 0 else "h-grid")
        parts.text(tx, bottom + 20, f"{tick * 100:.0f}%", "h-tick", anchor="middle")
    if reference is not None:
        rx = x(reference[0])
        rule(rx, "h-ref")
        label_x = min(rx, WIDTH - pad - _text_width(reference[1], _SMALL) / 2)
        parts.text(label_x, top - 15, reference[1], "h-note", anchor="middle")
    for bar in bars:
        parts.raw(bar)
    for lx, ly, text, cls in labels:
        parts.text(lx, ly, text, cls)
    for i, line in enumerate(note_lines):
        parts.text(pad, bottom + 50 + 17 * i, line, "h-note")
    title_id, desc_id = f"{key}-{theme.name}-title", f"{key}-{theme.name}-desc"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" role="img" '
        f'aria-labelledby="{title_id} {desc_id}" width="{_f(WIDTH)}" height="{_f(height)}" '
        f'viewBox="0 0 {_f(WIDTH)} {_f(height)}">'
        f'<title id="{title_id}">{_esc(title)}</title>'
        f'<desc id="{desc_id}">{_esc(desc)}</desc>'
        f"<style>{_style(theme)}</style>"
        f'<rect width="{_f(WIDTH)}" height="{_f(height)}" rx="8.0" class="h-page"/>'
        + "".join(parts.items)
        + "</svg>\n"
    )
