"""Editorial v2 figures for the published overview, capability and safety panels.

The v1 chart primitives remain the source for the detailed task figures.  This module owns
the larger three-panel layout approved for the v2 publication surface so the original and
supplemental panels share one deterministic renderer in both page themes.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from identitybench.report.figure_kit import THEMES, Canvas, Theme, wrap

WIDTH = 1600.0
PAD = 86.0
SOURCE_LINE = "SOURCE Published IdentityBench v2 scores · Results snapshot b50c123"
HEADING_FONT = ' font-family="Georgia, Times New Roman, serif"'


def _editorial_theme(theme: Theme) -> Theme:
    """Map the shared GitHub themes to the approved warm editorial palette."""
    if theme.name == "dark":
        return replace(
            THEMES["dark"],
            page="#162126",
            ink="#F3F0E8",
            ink2="#C0CBD0",
            muted="#9CABB1",
            band="#1C2D37",
            grid="#34454A",
            axis="#45606B",
            accent="#75BDCF",
            bar="#6F7F82",
            bar_ink="#162126",
            safe="#91C9AA",
            safe_ink="#162126",
            safe_band="#203E35",
            alarm="#F4A08F",
            neutral="#B8C3C7",
            badge="#45606B",
        )
    return replace(
        THEMES["light"],
        page="#F7F5F1",
        ink="#272B28",
        ink2="#6C736B",
        muted="#6C736B",
        band="#F0EEE8",
        grid="#DEDED5",
        axis="#DEDED5",
        accent="#3C756C",
        bar="#A9AFA6",
        bar_ink="#272B28",
        safe="#3C756C",
        safe_ink="#F7F5F1",
        safe_band="#E4EEE6",
        alarm="#925C43",
        neutral="#A9AFA6",
        badge="#A9AFA6",
    )


def _failure_band(theme: Theme) -> str:
    return "#4A3026" if theme.name == "dark" else "#EEE1D7"


def _failure_text(theme: Theme) -> str:
    return "#F4A08F" if theme.name == "dark" else "#925C43"


def _mean_grounding(config: dict[str, Any]) -> float:
    return (
        config["requests"]["grounding"]["grounded_accuracy"]["point"]
        + config["reviews"]["grounding"]["grounded_accuracy"]["point"]
    ) / 2


def _ordered(rows: list[tuple[str, list[tuple[bool, str]], dict[str, Any]]]):
    """Use one global accuracy order for every editorial panel."""
    return sorted(rows, key=lambda row: (-_mean_grounding(row[2]), row[0]))


class _Plate:
    def __init__(self, theme: Theme, panel: str, figure: int) -> None:
        self.theme = _editorial_theme(theme)
        self.canvas = Canvas(WIDTH, self.theme)
        self.panel = panel
        self.figure = figure

    def heading(self, title: str, subtitle: str) -> int:
        c = self.canvas
        c.text(PAD, 55, f"IDENTITYBENCH  /  V2 {self.panel.upper()}", size=17, weight=700)
        c.text(
            WIDTH - PAD,
            55,
            "16 OCTOBER 2026",
            size=15,
            fill="muted",
            anchor="end",
        )
        c.line(PAD, 77, WIDTH - PAD, 77, stroke="grid")
        y = 147
        title_lines = title.split("|")
        for line in title_lines:
            c.text(PAD, y, line, size=50, weight=400, extra=HEADING_FONT)
            y += 53
        y -= 13
        for line in wrap(subtitle, WIDTH - 2 * PAD, 21):
            c.text(PAD, y, line, size=21, fill="ink2")
            y += 28
        return 248 + 53 * (len(title_lines) - 1)

    def notes(self, bottom: int, lines: list[str], title: str, desc: str, key: str) -> str:
        c = self.canvas
        c.line(PAD, bottom, WIDTH - PAD, bottom, stroke="axis")
        y = bottom + 34
        for note in lines:
            for line in wrap(note, WIDTH - 2 * PAD, 15):
                c.text(PAD, y, line, size=15, fill="muted")
                y += 25
        c.text(PAD, y, SOURCE_LINE, size=13, fill="muted")
        c.text(WIDTH - PAD, y, f"{self.figure:02d}", size=13, fill="muted", anchor="end")
        return c.render(
            y + 40,
            title=title.replace("|", " "),
            desc=desc,
            key=key,
        )


def _dot(c: Canvas, x: float, y: float, passed: bool) -> None:
    if passed:
        c.circle(x, y, 4, fill="safe")
    else:
        c.circle(x, y, 4, fill="none", stroke="muted", width=1.7)


def overview(
    rows: list[tuple[str, list[tuple[bool, str]], dict[str, Any]]],
    theme: Theme,
    *,
    panel: str,
    subtitle: str,
    source: list[str],
) -> str:
    """Render grounded accuracy with a separate, explicit safety verdict column."""
    rows = _ordered(rows)
    plate = _Plate(theme, panel, 1)
    c = plate.canvas
    title = "Strong scores do not guarantee|clearing every safety check."
    top = plate.heading(title, subtitle)
    x0, span = 440.0, 815.0
    bottom = top + 42 + len(rows) * 61
    c.text(x0, top, "Grounded accuracy", size=19, weight=700)
    c.text(x0 + span, top, "Higher is better", size=15, fill="muted", anchor="end")
    c.text(1342, top, "Safety screen", size=19, weight=700)
    for tick in (0, 25, 50, 75, 100):
        x = x0 + span * tick / 100
        c.line(x, top + 27, x, bottom, stroke="grid")
        c.text(
            x,
            bottom + 28,
            f"{tick}{'%' if tick == 100 else ''}",
            size=14,
            fill="muted",
            anchor="middle",
        )
    described: list[str] = []
    for i, (name, cells, config) in enumerate(rows):
        y = top + 60 + i * 61
        passed = all(ok for ok, _ in cells)
        value = _mean_grounding(config)
        c.text(PAD, y + 7, name, size=22)
        c.rect(x0, y - 15, span * value, 30, fill="safe" if passed else "bar")
        c.text(x0 + span * value + 10, y + 7, f"{100 * value:.1f}", size=20, weight=700)
        _dot(c, 1350, y, passed)
        c.text(
            1371,
            y + 5,
            "PASS" if passed else "MISS",
            size=16,
            fill="safe" if passed else "muted",
            weight=700,
        )
        described.append(
            f"{name}: {100 * value:.1f}% mean grounded accuracy; "
            f"safety {'pass' if passed else 'miss'}"
        )
    return plate.notes(
        bottom + 58,
        [
            "Grounded accuracy: equal-weight mean of request and review grounding. "
            "Safety requires all seven checks.",
            *source,
        ],
        title,
        ". ".join(described),
        "v2-editorial-overview",
    )


def capability(
    rows: list[tuple[str, list[tuple[bool, str]], dict[str, Any]]],
    theme: Theme,
    *,
    panel: str,
    source: list[str],
) -> str:
    """Render request, review and lifecycle capability with a separate safety verdict."""
    rows = _ordered(rows)
    plate = _Plate(theme, panel, 2)
    c = plate.canvas
    title = "Capability by task"
    top = plate.heading(title, "Three task scores. One separate safety verdict.")
    start, stride, span = 440.0, 300.0, 200.0
    bottom = top + 65 + len(rows) * 68
    heads = [
        ("Access requests", "Grounded accuracy"),
        ("Access reviews", "Grounded accuracy"),
        ("Lifecycle plans", "Exact plans"),
    ]
    for j, (label, sub) in enumerate(heads):
        x = start + j * stride
        c.text(x, top, label, size=19, weight=700)
        c.text(x, top + 25, sub, size=15, fill="muted")
        for tick in (0, 50, 100):
            tx = x + span * tick / 100
            c.line(tx, top + 47, tx, bottom, stroke="grid")
            c.text(
                tx,
                bottom + 26,
                f"{tick}{'%' if tick == 100 else ''}",
                size=13,
                fill="muted",
                anchor="middle",
            )
    c.text(1371, top, "Safety", size=19, weight=700)
    described: list[str] = []
    for i, (name, cells, config) in enumerate(rows):
        y = top + 82 + i * 68
        passed = all(ok for ok, _ in cells)
        values = [
            config[t]["grounding"]["grounded_accuracy"]["point"] for t in ("requests", "reviews")
        ]
        values.append(config["jml"]["exact_timeline_match"]["point"])
        c.text(PAD, y + 7, name, size=21)
        for j, value in enumerate(values):
            x = start + j * stride
            c.rect(x, y - 14, span * value, 28, fill="accent")
            c.text(x + span * value + 8, y + 6, f"{100 * value:.1f}", size=17, weight=700)
        _dot(c, 1379, y, passed)
        c.text(
            1396,
            y + 5,
            "PASS" if passed else "MISS",
            size=14,
            fill="safe" if passed else "muted",
            weight=700,
        )
        described.append(
            f"{name}: "
            + ", ".join(f"{heads[j][0]} {100 * value:.1f}%" for j, value in enumerate(values))
            + f"; safety {'pass' if passed else 'miss'}"
        )
    return plate.notes(
        bottom + 58,
        [
            "Grounding: 251 request cases / 652 review items · Lifecycle: 120 plans · "
            "Each task has its own denominator.",
            *source,
        ],
        title,
        ". ".join(described),
        "v2-editorial-capability",
    )


def safety(
    rows: list[tuple[str, list[tuple[bool, str]], dict[str, Any]]],
    theme: Theme,
    heads: list[tuple[str, str, str]],
    *,
    panel: str,
    source: list[str],
) -> str:
    """Render the seven threshold checks, highlighting only cells that miss."""
    rows = _ordered(rows)
    plate = _Plate(theme, panel, 3)
    c = plate.canvas
    title = "Where the safety screen breaks"
    top = plate.heading(title, "Every criterion must pass. Colored cells mark missed thresholds.")
    start, stride = 436.0, 151.0
    bottom = top + 100 + len(rows) * 61
    for j, labels in enumerate(heads):
        x = start + j * stride + 65
        for k, label in enumerate(labels):
            c.text(
                x,
                top + k * 23,
                label,
                size=14 if k == 0 else 15,
                fill="ink" if k == 0 else "muted",
                weight=700 if k == 0 else 400,
                anchor="middle",
            )
    described: list[str] = []
    for i, (name, cells, _config) in enumerate(rows):
        y = top + 91 + i * 61
        c.text(PAD, y + 7, name, size=21)
        c.line(PAD, y + 30, WIDTH - PAD, y + 30, stroke="grid")
        for j, (passed, value) in enumerate(cells):
            x = start + j * stride
            if not passed:
                c.rect(x, y - 19, 132, 39, fill=_failure_band(plate.theme))
            c.text(
                x + 66,
                y + 7,
                value if passed else f"× {value}",
                size=18,
                fill="ink" if passed else _failure_text(plate.theme),
                weight=400 if passed else 700,
                anchor="middle",
            )
        described.append(
            name
            + ": "
            + "; ".join(
                f"{' '.join(heads[j][:2])} {value}, {'pass' if passed else 'miss'}"
                for j, (passed, value) in enumerate(cells)
            )
        )
    return plate.notes(
        bottom + 17,
        [
            "Wrongful approval uses the frozen bound/screen rule. *Review screen is not a "
            "full-population confidence guarantee.",
            "× = threshold missed · All values are percentages · Values and verdicts use "
            "the frozen safety-bar calculation.",
            *source,
        ],
        title,
        ". ".join(described),
        "v2-editorial-safety",
    )
