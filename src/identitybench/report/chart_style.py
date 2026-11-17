"""Shared drawing primitives for the v4.1 B-style charts."""

from __future__ import annotations

import base64
import html
import re
from pathlib import Path

from identitybench.report.figure_kit import THEMES

ICON_DIR = Path(__file__).with_name("brand_icons")

WIDTH = 2200


FONT = '-apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif'


BG = "#FFFFFF"


INK = "#111111"


INK2 = "#383A42"


MUTED = "#57606A"


RULE = "#D8DEE4"


GRID = "#EAEEF2"


BLUE = "#0072B2"


BLUE_PILL = "#EAF4FA"


SLATE_PILL = "#F0F2F5"


PASS_CELL_BG = "#F6F8FA"


PASS_CELL_INK = "#24292F"


MISS_CELL_BG = "#FBEAE3"


MISS_CELL_BORDER = "#D55E00"


MISS_CELL_INK = "#B33C00"


HEAT_FILLS = THEMES["light"].heat


HEAT_INKS = THEMES["light"].heat_ink


BIN_LABELS = ("0", "≤1", "≤2.5", "≤5", "≤10", "≤25", ">25")


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def fmt_pct(value: float) -> str:
    pct = value * 100.0
    if abs(pct - 100.0) < 0.05:
        return "100%"
    if abs(pct) < 0.05:
        return "0.0%"
    return f"{pct:.1f}%"


class Svg:
    def __init__(self, title: str, description: str, key: str, height: int) -> None:
        self.title = title
        self.description = description
        self.key = key
        self.height = height
        self.items: list[str] = []

    def add(self, item: str) -> None:
        self.items.append(item)

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill: str,
        *,
        rx: float = 0,
        stroke: str | None = None,
        width: float = 1,
    ) -> None:
        attrs = f'x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" fill="{fill}"'
        if rx:
            attrs += f' rx="{rx:g}"'
        if stroke:
            attrs += f' stroke="{stroke}" stroke-width="{width:g}"'
        self.add(f"<rect {attrs}/>")

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        stroke: str,
        width: float = 1,
        *,
        dash: str | None = None,
    ) -> None:
        attrs = (
            f'x1="{x1:g}" y1="{y1:g}" x2="{x2:g}" y2="{y2:g}" '
            f'stroke="{stroke}" stroke-width="{width:g}"'
        )
        if dash:
            attrs += f' stroke-dasharray="{dash}"'
        self.add(f"<line {attrs}/>")

    def text(
        self,
        x: float,
        y: float,
        value: str,
        size: int,
        fill: str = INK,
        *,
        bold: bool = False,
        weight: int | None = None,
        anchor: str = "start",
    ) -> None:
        font_weight = weight if weight is not None else (700 if bold else 400)
        attrs = (
            f'x="{x:g}" y="{y:g}" fill="{fill}" font-family=\'{FONT}\' '
            f'font-size="{size}" font-weight="{font_weight}" text-anchor="{anchor}"'
        )
        self.add(f"<text {attrs}>{esc(value)}</text>")

    def image(self, href: str, x: float, y: float, width: float, height: float) -> None:
        self.add(
            f'<image href="{href}" x="{x:g}" y="{y:g}" width="{width:g}" height="{height:g}" '
            'preserveAspectRatio="xMidYMid meet"/>'
        )

    def output(self) -> str:
        title_id = f"{self.key}-title"
        desc_id = f"{self.key}-description"
        body = "\n".join(self.items)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{self.height}" '
            f'viewBox="0 0 {WIDTH} {self.height}" role="img" '
            f'aria-labelledby="{title_id} {desc_id}">\n'
            f'<title id="{title_id}">{esc(self.title)}</title>\n'
            f'<desc id="{desc_id}">{esc(self.description)}</desc>\n'
            f"<style>text{{font-variant-numeric:tabular-nums}}</style>\n"
            f'<rect width="{WIDTH}" height="{self.height}" fill="{BG}"/>\n{body}\n</svg>\n'
        )


def _asset_data(path: Path) -> str:
    if not path.exists():
        return ""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    mime = "image/svg+xml" if path.suffix.lower() == ".svg" else "image/png"
    return f"data:{mime};base64,{encoded}"


def _icon_kind(name: str) -> tuple[str, str | None]:
    """Return a verified LobeHub brand asset for every plotted v3 provider."""
    lowered = name.lower()
    assets = (
        ("gpt", "openai.png"),
        ("claude", "anthropic.png"),
        ("gemini", "google-color.png"),
        ("muse", "meta-color.png"),
        ("grok", "grok.png"),
        ("deepseek", "deepseek-color.png"),
        ("glm", "zai.png"),
        ("qwen", "qwen-color.png"),
        ("kimi", "kimi-color.png"),
        ("minimax", "minimax-color.png"),
        ("mistral", "mistral-color.png"),
    )
    for prefix, filename in assets:
        if lowered.startswith(prefix) or prefix in lowered:
            return "image", _asset_data(ICON_DIR / filename)
    return "initials", None


def _initials(name: str) -> str:
    words = [word for word in re.split(r"[^A-Za-z0-9]+", name) if word]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def draw_icon(svg: Svg, name: str, y: float, *, x: float = 100) -> None:
    kind, data = _icon_kind(name)
    if kind == "image" and data:
        svg.image(data, x, y - 24, 44, 44)
        return
    svg.rect(x, y - 22, 44, 44, SLATE_PILL, rx=22, stroke=RULE, width=1.5)
    svg.text(x + 22, y + 6, _initials(name), 16, INK, bold=True, anchor="middle")


def add_footer(svg: Svg, note: str, fig_num: int, footer_top: float) -> None:
    svg.line(100, footer_top, WIDTH - 100, footer_top, RULE, 1.5)
    svg.text(100, footer_top + 38, note, 17, MUTED)
    svg.text(WIDTH - 100, footer_top + 38, f"FIG. 0{fig_num}", 18, INK, bold=True, anchor="end")


BINS: tuple[float, ...] = (0.0, 1.0, 2.5, 5.0, 10.0, 25.0)


def shortfall_bin(points: float) -> int:
    """The class (0 to 6) of a shortfall in points."""
    return next((i for i, bound in enumerate(BINS) if points <= bound), len(BINS))
