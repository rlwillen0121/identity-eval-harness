"""The README figures' renderers: the hero dot plot, the thesis graphic, the cost against
grounded accuracy chart and the scorecard heatmap. Each is checked for what it shows (every value
as the tables print it, rows in the order given, marks where the values put them) and for
layout at leaderboard scale: with the 26 models the v1 leaderboard plans (their real display
names and vendors), long names and clustered values, no two labels overlap, no label covers a
mark and nothing leaves the canvas."""

from __future__ import annotations

import colorsys
import dataclasses
import itertools
import math
import random
import re
import xml.etree.ElementTree as ET
from typing import Any

import pytest

from identitybench.report.cost_quality import (
    LABEL_ALL,
    LEADER_CLEARANCE,
    LEADER_MAX,
    CostPoint,
    cost_frontier,
    cost_quality_svg,
    cost_ticks,
)
from identitybench.report.figure_kit import (
    THEMES,
    Box,
    balanced,
    text_boxes,
    text_width,
    wrap,
)
from identitybench.report.hero import HeroRow, hero_svg
from identitybench.report.leaderboard_figure import (
    ThesisGroup,
    ThesisRow,
    _lines,  # pyright: ignore[reportPrivateUsage]
    _tokens,  # pyright: ignore[reportPrivateUsage]
    headline,
    thesis_svg,
)
from identitybench.report.scorecard import (
    BINS,
    ScoreCell,
    ScoreColumn,
    ScoreRow,
    scorecard_svg,
    shortfall,
    shortfall_bin,
)

NS = "{http://www.w3.org/2000/svg}"
PLANNED = 26
"""The model rows the v1 leaderboard plans."""
NAMES = (
    "Qwen3.8-Flash-Next (self-hosted)",
    "Claude Opus 5.5",
    "Claude Sonnet 5.5",
    "Claude Haiku 5.5",
    "GPT-6.1 Sol",
    "GPT-6 Astra",
    "GPT-6 Luna",
    "DeepSeek V4 Pro",
    "DeepSeek V4.1 Flash",
    "Mistral Large 4",
    "GLM-5.3 Flash",
    "Gemini 3.8 Flash",
    "Gemini 3.5 Flash-Lite",
    "Hunyuan 3",
    "MiMo-V2.6-Flash",
    "MiMo-V2.6-Pro",
    "Mercury 2.5",
    "Solar Pro 4",
    "Muse Glimmer 30B",
    "Ling 3.0 Flash",
    "Ternary Bonsai 2 27B",
    "MiniMax M3",
    "Granite 4.2 8B",
    "Qwen3.8 Flash",
    "Qwen3.8 Omni Flash",
    "Qwen3.8-27B (4-bit, self-hosted)",
    "model-with-a-very-long-label-v2-preview-0611 ‡",
)
"""The planned rows' display names (the longest first, so every size includes it), then a label
long enough to be shortened."""
_DOT = 6.0
VENDORS = (
    "Alibaba (Qwen)",
    "Anthropic",
    "OpenAI",
    "DeepSeek",
    "Mistral AI",
    "Z.ai",
    "Google",
    "Tencent",
    "Xiaomi",
    "Inception",
    "Upstage",
    "Meta",
    "inclusionAI",
    "Prism ML",
    "MiniMax",
    "IBM",
)
SOURCE = ["Synthetic rows.", "Source: synthetic · run 2026-10-12"]


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _parse(svg: str) -> ET.Element:
    root = ET.fromstring(svg)
    assert root.tag == f"{NS}svg"
    return root


def _texts(svg: str) -> list[str]:
    """Every text in order, a label's ``<tspan>`` note as its own entry after it."""
    out: list[str] = []
    for t in _parse(svg).iter(f"{NS}text"):
        out += [t.text or "", *(span.text or "" for span in t.iter(f"{NS}tspan"))]
    return out


def _desc(svg: str) -> str:
    desc = _parse(svg).find(f"{NS}desc")
    assert desc is not None and desc.text is not None
    return desc.text


def _circles(svg: str) -> list[tuple[float, float, float, str]]:
    return [
        (
            float(c.get("cx") or 0),
            float(c.get("cy") or 0),
            float(c.get("r") or 0),
            c.get("fill") or "",
        )
        for c in _parse(svg).iter(f"{NS}circle")
    ]


def _text_at(svg: str, text: str) -> list[ET.Element]:
    return [t for t in _parse(svg).iter(f"{NS}text") if t.text == text]


def assert_legible(svg: str) -> None:
    """No two text boxes overlap, no text covers a circle, and everything is on the canvas."""
    root = _parse(svg)
    width, height = float(root.get("width") or 0), float(root.get("height") or 0)
    canvas = Box(0, 0, width, height)
    boxes = text_boxes(svg)
    for text, box in boxes:
        assert box.inside(canvas), f"{text!r} leaves the canvas: {box}"
    for (a, box_a), (b, box_b) in itertools.combinations(boxes, 2):
        assert not box_a.overlaps(box_b), f"{a!r} overlaps {b!r}"
    for cx, cy, r, _ in _circles(svg):
        mark = Box(cx - r, cy - r, cx + r, cy + r)
        for text, box in boxes:
            assert not box.overlaps(mark), f"{text!r} covers the mark at ({cx}, {cy})"


def _hero_rows(n: int, seed: int = 7) -> list[HeroRow]:
    rng = random.Random(seed)
    rows: list[HeroRow] = []
    for i in range(n):
        decision = min(1.0, max(0.4, rng.gauss(0.96, 0.05)))
        grounded = max(0.3, decision - abs(rng.gauss(0.06, 0.06)))
        if i < 4:  # a tight cluster at the ceiling
            decision, grounded = 1.0, 0.996 - i * 0.001
        rows.append(HeroRow(NAMES[i % len(NAMES)], (decision, grounded), VENDORS[i % len(VENDORS)]))
    return sorted(rows, key=lambda r: -(r.values[1] or 0.0))


def _hero(rows: list[HeroRow] | None = None, theme: str = "light", **overrides: Any) -> str:
    options: dict[str, Any] = {
        "theme": THEMES[theme],
        "title": "Models decide right, but can't always show why",
        "subtitle": "8 of 9 models reach 95% decision accuracy; 2 reach 95% grounded accuracy.",
        "source": SOURCE,
        "badge": "Interim · 9 of 20 models",
        "reference": (1.0, "Oracle ceiling (100%)"),
    }
    options.update(overrides)
    if rows is None:
        rows = [
            HeroRow('model <a> & "b"', (0.995, 0.934), "Anthropic"),
            HeroRow("undefined", (0.8, None)),
            HeroRow("same", (1.0, 1.0), "OpenAI"),
        ]
    return hero_svg(rows, **options)


# --------------------------------------------------------------------------------------------
# The kit
# --------------------------------------------------------------------------------------------


def test_text_width_is_per_character_and_padded() -> None:
    assert text_width("iiii", 12) < text_width("MMMM", 12)
    assert text_width("100.0", 13, bold=True) > text_width("100.0", 13)
    assert text_width("abc", 24) == pytest.approx(2 * text_width("abc", 12))
    width = text_width("one two", 12) + 0.1
    lines = wrap("one two three four", width, 12)
    assert lines[0] == "one two" and " ".join(lines) == "one two three four"
    assert all(text_width(line, 12) <= width for line in lines)


def test_a_subtitle_wraps_into_even_lines_never_an_orphan() -> None:
    text = (
        "12 of 20 models reach 95% decision accuracy; 3 reach 95% grounded accuracy. Sorted "
        "by grounded accuracy."
    )
    plain, even = wrap(text, 772, 15), balanced(text, 772, 15)
    assert len(plain) == 2 and len(plain[-1].split()) == 1  # greedy: one word on its own
    # The last sentence takes its own line when that keeps the line count.
    assert even == [
        "12 of 20 models reach 95% decision accuracy; 3 reach 95% grounded accuracy.",
        "Sorted by grounded accuracy.",
    ]
    # Without a sentence to break at, the lines narrow until the last holds a third.
    clause = (
        "Along the frontier, GPT-6.1 Sol ($11.22) is 57.2 points of grounded accuracy above "
        "Mercury 2.5 ($0.24), at 47.3x the price."
    )
    lines = balanced(clause, 772, 15)
    assert len(lines) == len(wrap(clause, 772, 15)) and " ".join(lines) == clause
    assert text_width(lines[-1], 15) >= 772 / 3
    assert balanced("short", 772, 15) == ["short"]


def test_the_theme_colors_are_ordered_and_their_text_is_legible() -> None:
    def luminance(color: str) -> float:
        def channel(c: float) -> float:
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

        r, g, b = (channel(int(color[i : i + 2], 16) / 255) for i in (1, 3, 5))
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def contrast(a: str, b: str) -> float:
        high, low = sorted((luminance(a), luminance(b)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    light, dark = THEMES["light"], THEMES["dark"]
    steps = [luminance(c) for c in light.heat]
    assert steps == sorted(steps, reverse=True)  # light: darker with the shortfall
    steps = [luminance(c) for c in dark.heat]
    assert steps == sorted(steps)  # dark: brighter with the shortfall
    for theme in (light, dark):
        assert len(theme.heat) == len(theme.heat_ink) == len(BINS) + 1
        for fill, ink in zip(theme.heat, theme.heat_ink, strict=True):
            assert contrast(fill, ink) >= 4.5, (theme.name, fill, ink)
        # Small muted text (vendors, ticks, leaders) on the page and on a row stripe, and the
        # gray marks (decision rings, off-frontier dots, the alarm ring) as graphics.
        for surface in (theme.page, theme.band):
            assert contrast(theme.muted, surface) >= 4.5, (theme.name, surface)
            assert contrast(theme.neutral, surface) >= 3.0, (theme.name, surface)
        assert contrast(theme.alarm, theme.page) >= 3.0, theme.name
        # The thesis graphic's bars as graphics, the values printed inside them, and its accent
        # as text (the group label, the named model).
        for fill, ink in ((theme.bar, theme.bar_ink), (theme.safe, theme.safe_ink)):
            assert contrast(fill, theme.page) >= 3.0, (theme.name, fill)
            assert contrast(fill, ink) >= 4.5, (theme.name, fill, ink)
        assert contrast(theme.safe, theme.page) >= 4.5, theme.name
        # The worst classes read as red in both themes (a dark ramp that brightens into yellow
        # would make the worst value look like a caution): hue within 20 degrees of red.
        for color in theme.heat[-2:]:
            r, g, b = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
            hue = colorsys.rgb_to_hsv(r, g, b)[0] * 360
            assert hue <= 20 or hue >= 340, (theme.name, color, hue)


# --------------------------------------------------------------------------------------------
# The hero
# --------------------------------------------------------------------------------------------


def test_the_hero_is_accessible_and_states_every_value() -> None:
    svg = _hero()
    root = _parse(svg)
    assert root.get("role") == "img"
    assert root.get("aria-labelledby") == (
        "identitybench-results-light-title identitybench-results-light-desc"
    )
    desc = _desc(svg)
    assert 'model <a> & "b": grounded accuracy 93.4%, decision accuracy 99.5%' in desc
    assert "undefined: grounded accuracy n/a, decision accuracy 80.0%" in desc
    texts = _texts(svg)
    for text in ("93.4", "99.5", "80.0", "n/a", "Oracle ceiling (100%)", "Grounded", "Decision"):
        assert text in texts


def test_the_hero_prints_the_tables_numbers_and_never_joins_the_two_marks() -> None:
    """The metrics have different denominators, so the hero draws no line between a model's
    two marks and prints no gap: the difference is not a count of decisions lacking evidence."""
    rows = _hero_rows(PLANNED)
    svg = _hero(rows)
    texts = _texts(svg)
    for row in rows:
        decision, grounded = row.values
        assert decision is not None and grounded is not None
        assert f"{round(grounded * 100, 1):.1f}" in texts
        assert f"{round(decision * 100, 1):.1f}" in texts
    assert "Gap" not in texts and "pts" not in texts
    horizontal = [
        line
        for line in _parse(svg).iter(f"{NS}line")
        if line.get("y1") == line.get("y2") and line.get("x1") != line.get("x2")
    ]
    assert horizontal == []  # gridlines and the ceiling are vertical; nothing joins the marks
    assert "joined" not in _desc(svg)


def test_hero_rows_are_drawn_in_the_order_given() -> None:
    rows = _hero_rows(PLANNED)
    texts = list(_parse(_hero(rows)).iter(f"{NS}text"))

    def drawn(name: str) -> ET.Element:
        """The row's name as drawn (a long one shortened with an ellipsis)."""
        return next(
            t
            for t in texts
            if t.text == name
            or ((t.text or "").endswith("…") and name.startswith((t.text or "")[:-1]))
        )

    ys = [float(drawn(r.name).get("y") or 0) for r in rows]
    assert ys == sorted(ys) and len(set(ys)) == len(ys)


def test_hero_marks_sit_on_one_axis_and_the_ring_is_drawn_over_the_dot() -> None:
    svg = _hero()
    circles = _circles(svg)
    accent = THEMES["light"].accent
    dots = [c for c in circles if c[3] == accent and c[2] == 6.0]
    rings = [c for c in circles if c[3] == "none" and c[2] == 5.0]
    assert len(dots) == 2 and len(rings) == 3  # the undefined grounded value has no dot
    # "same" scores 100 on both: its ring is centered on its dot and comes after it.
    same_dot = next(d for d in dots if any(abs(d[1] - r[1]) < 0.1 and d[0] == r[0] for r in rings))
    ceiling = [
        line
        for line in _parse(svg).iter(f"{NS}line")
        if line.get("x1") == line.get("x2") and line.get("stroke") == THEMES["light"].ink2
    ]
    assert len(ceiling) == 1 and float(ceiling[0].get("x1") or 0) == same_dot[0]
    order = [c for c in circles if c[0] == same_dot[0] and abs(c[1] - same_dot[1]) < 0.1]
    assert [c[3] for c in order] == [accent, "none"]


def test_the_hero_axis_starts_at_the_ten_point_step_below_the_lowest_value() -> None:
    ticks = [t for t in _texts(_hero()) if re.fullmatch(r"\d+%", t)]
    assert ticks[0] == "70%" and ticks[-1] == "100%"  # lowest value 80.0
    low = _hero([HeroRow("x", (0.6, 0.51))])
    assert next(t for t in _texts(low) if re.fullmatch(r"\d+%", t)) == "40%"


@pytest.mark.parametrize("n", [1, 9, 20, PLANNED])
def test_the_hero_grows_with_its_rows_and_stays_legible(n: int) -> None:
    svg = _hero(_hero_rows(n))
    assert_legible(svg)
    heights = [float(_parse(_hero(_hero_rows(k))).get("height") or 0) for k in (5, 6)]
    assert heights[1] - heights[0] == pytest.approx(29.0)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_hero_is_legible_with_every_planned_model_in_either_theme(theme: str) -> None:
    rows = _hero_rows(PLANNED, seed=3)
    svg = _hero(rows, theme)
    assert_legible(svg)
    # Every planned display name is shown whole (only the over-long test label is shortened).
    texts = _texts(svg)
    assert all(name in texts for name in NAMES[:PLANNED])


def test_the_hero_themes_differ_only_in_color() -> None:
    light, dark = _hero(theme="light"), _hero(theme="dark")
    assert light == _hero(theme="light")  # deterministic
    strip = re.compile(r"#[0-9a-f]{6}|-(light|dark)-")
    assert strip.sub("", light) == strip.sub("", dark)
    assert THEMES["dark"].page in dark and THEMES["light"].page in light


def test_hero_without_badge_or_reference() -> None:
    texts = _texts(_hero(badge=None, reference=None))
    assert not any("Interim" in t or "Oracle" in t for t in texts)


# --------------------------------------------------------------------------------------------
# Cost against grounded accuracy
# --------------------------------------------------------------------------------------------


def _cost_points(n: int, seed: int) -> list[CostPoint]:
    """``n`` models: a quarter with no API price, some exact duplicates (same accuracy, a cent
    apart) and the rest spread over three decades of cost."""
    rng = random.Random(seed)
    notes = ("≈$21.31 CLI estimate", "cost not reported", "unmetered")
    points: list[CostPoint] = []
    for i in range(n):
        quality = max(0.4, min(1.0, rng.gauss(0.92, 0.06)))
        if i % 4 == 0:
            points.append(CostPoint(NAMES[i % len(NAMES)], quality, None, notes[i % 3]))
        elif i % 5 == 0:  # near-duplicates: same accuracy, a cent apart
            points.append(CostPoint(NAMES[i % len(NAMES)], 0.93, 8.0 + i * 0.01))
        else:
            points.append(CostPoint(NAMES[i % len(NAMES)], quality, 10 ** rng.uniform(-1.2, 1.8)))
    return points


def _named_cases() -> dict[str, list[CostPoint]]:
    """The layouts that broke the first placer, at the planned leaderboard's size: every model
    priced in a $4-$7 cluster, a $0.02-$300 spread with ties at one accuracy, and a realistic
    mix."""
    rng = random.Random(11)
    dense = [
        CostPoint(NAMES[i], rng.uniform(0.90, 0.95), rng.uniform(4, 7)) for i in range(PLANNED)
    ]
    wide: list[CostPoint] = []
    for i in range(PLANNED):
        if i % 3 == 2:
            wide.append(CostPoint(NAMES[i], rng.uniform(0.6, 1.0), None, "cost not reported"))
        else:
            quality = rng.choice([0.933, 0.933, rng.uniform(0.6, 1.0)])
            wide.append(CostPoint(NAMES[i], quality, 10 ** rng.uniform(-1.7, 2.5)))
    realistic = [
        CostPoint(
            NAMES[i],
            min(1.0, rng.gauss(0.93, 0.04)),
            None if i in (3, 4, 5) else round(10 ** rng.uniform(-0.3, 1.6), 2),
            "cost not reported" if i in (3, 4, 5) else None,
        )
        for i in range(PLANNED)
    ]
    return {"dense": dense, "wide": wide, "realistic": realistic}


def _cost(points: list[CostPoint], theme: str = "light", **overrides: Any) -> str:
    options: dict[str, Any] = {
        "theme": THEMES[theme],
        "title": "Grounded accuracy against API cost",
        "subtitle": "A takeaway.",
        "source": SOURCE,
        "badge": "Interim · 9 of 20 models",
        "reference": (1.0, "Oracle ceiling (100%)"),
    }
    options.update(overrides)
    return cost_quality_svg(points, **options)


def _box_distance(x: float, y: float, box: Box) -> float:
    return math.hypot(max(box.x0 - x, 0.0, x - box.x1), max(box.y0 - y, 0.0, y - box.y1))


def _through(seg: tuple[float, float, float, float], box: Box) -> bool:
    """Whether the segment runs through ``box``'s interior (sampled every 0.5 px)."""
    x1, y1, x2, y2 = seg
    steps = max(int(math.hypot(x2 - x1, y2 - y1) / 0.5), 1)
    for i in range(steps + 1):
        x, y = x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps
        if box.x0 + 0.5 < x < box.x1 - 0.5 and box.y0 + 0.5 < y < box.y1 - 0.5:
            return True
    return False


def _cross(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    def side(p: tuple[float, ...], q: tuple[float, ...], x: float, y: float) -> float:
        return (q[0] - p[0]) * (y - p[1]) - (q[1] - p[1]) * (x - p[0])

    a0, a1, b0, b1 = a[:2], a[2:], b[:2], b[2:]
    return side(b0, b1, *a0) * side(b0, b1, *a1) < 0 and side(a0, a1, *b0) * side(a0, a1, *b1) < 0


def _marks(svg: str) -> dict[str | None, tuple[float, float]]:
    """Each priced point's mark center by ``data-point``: a charge's circle or an
    API-equivalent's diamond (a path whose first corner is straight above its center)."""
    root = _parse(svg)
    marks: dict[str | None, tuple[float, float]] = {
        c.get("data-point"): (float(c.get("cx") or 0), float(c.get("cy") or 0))
        for c in root.iter(f"{NS}circle")
        if c.get("data-point") is not None
    }
    for path in root.iter(f"{NS}path"):
        if path.get("class") == "equivalent":
            corners = [
                tuple(map(float, xy.split(",")))
                for xy in re.findall(r"[\d.]+,[\d.]+", path.get("d") or "")
            ]
            (top_x, _), (_, right_y) = corners[0], corners[1]
            marks[path.get("data-point")] = (top_x, right_y)
    return marks


def assert_cost_legible(svg: str, points: list[CostPoint]) -> None:
    """Everything :func:`assert_legible` checks, and the cost chart's own rules, read from the
    SVG alone (``data-point`` ties each name, number, leader and dot to its point):

    - every leader is at most ``LEADER_MAX`` long and runs through no other point's text and no
      other point's dot, and no two leaders cross;
    - a name, or a number beside its dot, is nearer its own dot than any other model's; a number
      joined by a leader keeps ``LEADER_CLEARANCE`` from every other dot;
    - every priced point is identified: named on the chart, or numbered, with that number in
      the list under the plot beside its name.
    """
    assert_legible(svg)
    root = _parse(svg)
    boxes = text_boxes(svg)
    texts = list(root.iter(f"{NS}text"))
    dots = _marks(svg)

    def segment(line: ET.Element) -> tuple[float, float, float, float]:
        x1, y1, x2, y2 = (float(line.get(k) or 0) for k in ("x1", "y1", "x2", "y2"))
        return (x1, y1, x2, y2)

    leaders = [
        (segment(line), set((line.get("data-point") or "").split()))
        for line in root.iter(f"{NS}line")
        if line.get("class") == "leader"
    ]
    for seg, owners in leaders:
        assert math.hypot(seg[2] - seg[0], seg[3] - seg[1]) <= LEADER_MAX + 0.5
        for t, (text, box) in zip(texts, boxes, strict=True):
            if set((t.get("data-point") or "").split()) != owners:
                assert not _through(seg, box), f"a leader runs through {text!r}"
        for key, (x, y) in dots.items():
            if key not in owners:
                dot = Box(x - _DOT, y - _DOT, x + _DOT, y + _DOT)
                assert not _through(seg, dot), f"the leader of {owners} crosses dot {key}"
    for (a, _), (b, _) in itertools.combinations(leaders, 2):
        assert not _cross(a, b), "two leaders cross"
    tags: dict[str, tuple[str, Box]] = {}
    for t, (text, box) in zip(texts, boxes, strict=True):
        key = t.get("data-point")
        if key is None:
            continue
        if key in tags:  # a name and its cost: one label
            old_text, old = tags[key]
            box = Box(
                min(old.x0, box.x0), min(old.y0, box.y0), max(old.x1, box.x1), max(old.y1, box.y1)
            )
            text = f"{old_text} {text}"
        tags[key] = (text, box)
    with_leader = {frozenset(owners) for _, owners in leaders}
    for key, (text, box) in tags.items():
        owners = set(key.split())
        own = min(_box_distance(*dots[o], box) for o in owners)
        number = re.fullmatch(r"[\d,]+", text) is not None
        for other, (x, y) in dots.items():
            if other in owners:
                continue
            if number and frozenset(owners) in with_leader:
                assert _box_distance(x, y, box) >= LEADER_CLEARANCE - 0.5, (text, other)
            else:
                assert _box_distance(x, y, box) > own, f"{text!r} is nearer dot {other}"
    # Every priced point is named, or numbered and keyed by that number.
    priced = [p for p in points if p.cost is not None and p.quality is not None]
    named = {k for k, (text, _) in tags.items() if not re.fullmatch(r"[\d,]+", text)}
    assert len(dots) == len(priced)
    plain = _texts(svg)
    for i, p in enumerate(priced):
        if str(i) in {o for k in named for o in k.split()}:
            continue
        tag = next(t for k, (t, _) in tags.items() if str(i) in k.split())
        assert re.fullmatch(r"[\d,]+", tag), p.name
        numbers = tag.split(",")
        listed = [plain[j + 1] for j, t in enumerate(plain[:-1]) if t in numbers]
        assert any(p.name.startswith(n.rstrip("…")) for n in listed), p.name


def test_the_frontier_keeps_the_points_no_cheaper_one_beats() -> None:
    a = CostPoint("a", 0.90, 1.0)
    b = CostPoint("b", 0.95, 4.0)
    c = CostPoint("c", 0.93, 5.0)  # beaten by b: dearer and less grounded
    d = CostPoint("d", 0.99, 9.0)
    e = CostPoint("e", 0.99, 12.0)  # beaten by d: same accuracy, dearer
    unpriced = CostPoint("unpriced", 1.0, None, "unmetered")  # never on the frontier
    assert cost_frontier([c, e, unpriced, d, b, a]) == [a, b, d]
    # Accuracy is compared as printed: 0.9301 and 0.9304 both read 93.0.
    assert cost_frontier([CostPoint("x", 0.9301, 2.0), CostPoint("y", 0.9304, 3.0)])[-1].name == "x"


def test_cost_ticks_span_a_decade_on_the_one_two_five_ladder() -> None:
    start, end, ticks = cost_ticks([5.70, 9.65])
    assert (start, end) == (2.0, 20.0)
    assert ticks == [2.0, 5.0, 10.0, 20.0]
    start, end, ticks = cost_ticks([0.004, 80.0])
    assert start <= 0.004 / 1.5 and end >= 80 * 1.5
    assert all(start <= t <= end for t in ticks) and len(ticks) <= 9


def _priced_dots(svg: str) -> list[tuple[float, float, float, str]]:
    return [c for c in _circles(svg) if c[2] == _DOT]


def test_priced_points_sit_on_the_log_axis_and_the_rest_are_listed_under_it() -> None:
    points = [
        CostPoint("cheap", 0.92, 1.0),
        CostPoint("mid", 0.90, 10.0),
        CostPoint("dear", 0.97, 100.0),
        CostPoint("cli", 0.93, None, "≈$21.31 CLI estimate"),
        CostPoint("unknown", 0.51, None, "cost not reported"),
    ]
    svg = _cost(points)
    priced = sorted(_priced_dots(svg), key=lambda c: c[0])
    assert len(priced) == 3
    xs = [c[0] for c in priced]
    assert xs[1] - xs[0] == pytest.approx(xs[2] - xs[1], abs=0.2)  # equal decades
    assert all(c[3] != THEMES["light"].page for c in priced)
    # Higher accuracy is higher up.
    y = {round(c[0]): c[1] for c in priced}
    assert y[round(xs[2])] < y[round(xs[0])] < y[round(xs[1])]
    texts = _texts(svg)
    costs = sorted(t for t in texts if re.fullmatch(r"\$\d+\.\d\d", t))
    assert costs == ["$1.00", "$10.00", "$100.00"]  # all three named, each with its cost
    # The unpriced models are a list under the plot, never at a cost position: a ring glyph,
    # the name, the grounded accuracy and what the cost is, most grounded first.
    assert "No price" in texts and "not on the cost axis" in texts
    i = texts.index("cli")
    assert texts[i : i + 3] == ["cli", "93.0%", "≈$21.31 CLI estimate"]
    assert texts.index("unknown") > i and "cost not reported" in texts
    axis_title = next(t for t in _parse(svg).iter(f"{NS}text") if t.text and "log scale" in t.text)
    listed = next(t for t in _parse(svg).iter(f"{NS}text") if t.text == "cli")
    assert float(listed.get("y") or 0) > float(axis_title.get("y") or 0)
    rings = [c for c in _circles(svg) if c[3] == "none"]
    assert len(rings) == 2


def test_api_equivalents_are_diamonds_on_the_same_axis_with_a_legend() -> None:
    points = [
        CostPoint("charged", 0.80, 1.0),
        CostPoint("listed", 0.95, 10.0, equivalent=True),
        CostPoint("dear", 0.97, 100.0),
        CostPoint("local", 0.90, None, "unmetered"),
    ]
    svg = _cost(points)
    root = _parse(svg)
    diamonds = [p for p in root.iter(f"{NS}path") if p.get("class") == "equivalent"]
    assert [d.get("data-point") for d in diamonds] == ["1"]
    circles = [c for c in root.iter(f"{NS}circle") if c.get("data-point") is not None]
    assert sorted(c.get("data-point") for c in circles) == ["0", "2"]
    marks = _marks(svg)
    assert marks["0"][0] < marks["1"][0] < marks["2"][0]  # one log axis for both footings
    texts = _texts(svg)
    assert "◇$10.00" in texts and "$1.00" in texts  # the label carries the mark too
    assert "API charge (billed or metered)" in texts
    assert any(t.startswith("API-equivalent, marked ◇") for t in texts)
    assert "listed: grounded accuracy 95.0%, $10.00 API-equivalent" in _desc(svg)
    assert "Frontier: charged, listed and dear." in _desc(svg)
    assert_cost_legible(svg, points)
    # Without an API-equivalent there is no legend to read.
    plain = _texts(_cost([CostPoint("a", 0.9, 1.0), CostPoint("b", 0.95, 3.0)]))
    assert "API charge (billed or metered)" not in plain


@pytest.mark.parametrize("case", ["dense", "wide", "realistic"])
def test_a_crowd_with_api_equivalents_stays_legible(case: str) -> None:
    points = [
        dataclasses.replace(p, equivalent=p.cost is not None and i % 3 == 0)
        for i, p in enumerate(_named_cases()[case])
    ]
    for theme in THEMES:
        assert_cost_legible(_cost(points, theme), points)


def test_the_accuracy_axis_comes_from_the_priced_models_only() -> None:
    priced = [CostPoint(f"m{i}", 0.92 - i * 0.01, 2.0 + 3 * i) for i in range(4)]
    unpriced = CostPoint("far below", 0.51, None, "cost not reported")
    ticks = [t for t in _texts(_cost([*priced, unpriced])) if re.fullmatch(r"\d+", t)]
    assert ticks[0] == "88" and ticks[-1] == "100"  # not stretched down to 51
    # A tight cluster near the top isn't magnified: the axis spans at least ten points.
    top = [CostPoint(f"t{i}", 0.996 + i * 0.001, 2.0 + i) for i in range(3)]
    ticks = [t for t in _texts(_cost(top)) if re.fullmatch(r"\d+", t)]
    assert int(ticks[0]) <= 90


def test_the_frontier_is_blue_and_off_frontier_points_are_gray() -> None:
    points = [CostPoint("a", 0.95, 1.0), CostPoint("b", 0.90, 2.0), CostPoint("c", 0.99, 8.0)]
    svg = _cost(points)
    fills = sorted(c[3] for c in _priced_dots(svg))
    light = THEMES["light"]
    assert fills == sorted([light.accent, light.accent, light.neutral])
    assert len(list(_parse(svg).iter(f"{NS}path"))) == 1  # the frontier staircase
    assert "Frontier: a and c." in _desc(svg)


def test_a_few_points_are_all_named_and_a_crowd_names_only_its_story() -> None:
    few = [CostPoint(NAMES[i], 0.80 + i * 0.03, 10 ** (i * 0.5)) for i in range(5)]
    texts = _texts(_cost(few))
    assert all(p.name in texts for p in few)
    assert "Numbered on the chart" not in texts
    crowd = _named_cases()["realistic"]
    svg = _cost(crowd)
    priced = [p for p in crowd if p.cost is not None]
    story = {p.name for p in cost_frontier(crowd)}
    story.add(min(priced, key=lambda p: p.quality or 0.0).name)
    story.add(max(priced, key=lambda p: p.cost or 0.0).name)
    labels = [t for t in _parse(svg).iter(f"{NS}text") if t.get("data-point") is not None]
    named = {t.text for t in labels if t.text and not re.fullmatch(r"[\d,$.]+", t.text)}
    assert named and named <= story and len(named) < LABEL_ALL
    assert "Numbered on the chart" in _texts(svg)


@pytest.mark.parametrize("case", ["dense", "wide", "realistic"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_crowded_cost_charts_keep_every_rule(case: str, theme: str) -> None:
    points = _named_cases()[case]
    svg = _cost(points, theme)
    assert_cost_legible(svg, points)
    # A crowd doesn't need a giant plot: every number fits at the base size.
    assert float(_parse(svg).get("height") or 0) < 1000


@pytest.mark.parametrize("seed", range(46))
def test_every_point_is_identified_without_collisions_at_any_size(seed: int) -> None:
    n = 4 + seed % (PLANNED - 3)  # 4 to 26 models
    points = _cost_points(n, seed)
    svg = _cost(points, "dark" if seed % 2 else "light")
    assert_cost_legible(svg, points)
    # No text is struck through by the ceiling line.
    ceiling = next(
        line
        for line in _parse(svg).iter(f"{NS}line")
        if line.get("y1") == line.get("y2")
        and line.get("stroke") == THEMES["dark" if seed % 2 else "light"].ink2
    )
    y = float(ceiling.get("y1") or 0)
    x0, x1 = float(ceiling.get("x1") or 0), float(ceiling.get("x2") or 0)
    for text, box in text_boxes(svg):
        assert not box.overlaps(Box(x0, y - 0.5, x1, y + 0.5)), text


def test_overlapping_dots_share_one_number() -> None:
    twins = [CostPoint("twin a", 0.93, 8.00), CostPoint("twin b", 0.93, 8.01)]
    others = [CostPoint(NAMES[i], 0.80 + i * 0.01, 0.5 * (i + 1)) for i in range(8)]
    points = [*twins, *others]
    svg = _cost(points)
    assert_cost_legible(svg, points)
    shared = [
        t.text for t in _parse(svg).iter(f"{NS}text") if (t.get("data-point") or "").count(" ")
    ]
    assert len(shared) == 1 and re.fullmatch(r"\d+,\d+", shared[0] or "")


def test_unplaceable_points_are_named_and_no_price_means_no_axis() -> None:
    svg = _cost([CostPoint("none", None, 3.0), CostPoint("cli", 0.9, None, "unmetered")])
    texts = " ".join(_texts(svg))
    assert "Not shown (grounded accuracy n/a): none." in texts
    assert "No model has a price yet." in texts
    assert "log scale" not in texts and "$1" not in texts  # no invented axis
    assert_legible(svg)


def test_the_cost_chart_is_deterministic_and_themed() -> None:
    points = _cost_points(12, 1)
    light, dark = _cost(points), _cost(points, "dark")
    assert light == _cost(points)
    strip = re.compile(r"#[0-9a-f]{6}|-(light|dark)-")
    assert strip.sub("", light) == strip.sub("", dark)


# --------------------------------------------------------------------------------------------
# The scorecard
# --------------------------------------------------------------------------------------------

COLUMNS = (
    ScoreColumn("Accuracy ↑", "Requests", 1.0),
    ScoreColumn("Accuracy ↑", "Reviews", 1.0),
    ScoreColumn("Accuracy ↑", "JML plans", 1.0),
    ScoreColumn("Grounded ↑", "Req + rev", 1.0),
    ScoreColumn("WAR ↓", "Requests", 0.0),
    ScoreColumn("WAR ↓", "Reviews", 0.0),
    ScoreColumn("Unsafe ↓", "JML plans", 0.0),
    ScoreColumn("Harmful flips ↓", "Requests", 0.0),
    ScoreColumn("Harmful flips ↓", "Reviews", 0.0),
)


def _score_rows(n: int, seed: int = 5) -> list[ScoreRow]:
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        cells = []
        for c in COLUMNS:
            value = (
                rng.uniform(0.5, 1.0) if c.ideal else rng.choice([0.0, 0.0, rng.uniform(0, 0.3)])
            )
            cells.append(ScoreCell(value))
        if i == 3:
            cells[2] = ScoreCell(None, scored=False)
        if i == 4:
            cells[0] = ScoreCell(None)
        rows.append(ScoreRow(NAMES[i % len(NAMES)], tuple(cells)))
    return rows


def _scorecard(rows: list[ScoreRow], theme: str = "light") -> str:
    return scorecard_svg(
        rows,
        COLUMNS,
        theme=THEMES[theme],
        title="Where each model falls short",
        subtitle="A takeaway.",
        source=SOURCE,
        badge="Interim · 9 of 20 models",
    )


def test_shortfall_is_taken_from_the_printed_value_and_binned() -> None:
    assert shortfall(0.99996, 1.0) == 0.0  # prints 100.0: no shortfall, no color
    assert shortfall(0.0004, 0.0) == 0.0  # prints 0.0
    assert shortfall(0.933, 1.0) == 6.7
    assert shortfall(0.232, 0.0) == 23.2
    expected = {0.0: 0, 0.1: 1, 1.0: 1, 1.1: 2, 2.5: 2, 5.0: 3, 9.9: 4, 10.0: 4, 25.0: 5, 25.1: 6}
    assert {p: shortfall_bin(p) for p in expected} == expected


def _cells(svg: str) -> list[ET.Element]:
    """The filled cell rects (a plain cell, or a ringed one's fill inside its ring)."""
    return [r for r in _parse(svg).iter(f"{NS}rect") if r.get("height") in ("25.0", "22.5")]


def test_color_follows_distance_from_perfect_whichever_way_the_metric_points() -> None:
    rows = [ScoreRow("m", (ScoreCell(0.95), ScoreCell(0.05)))]
    columns = (ScoreColumn("Accuracy ↑", "Requests", 1.0), ScoreColumn("WAR ↓", "Requests", 0.0))
    svg = scorecard_svg(
        rows, columns, theme=THEMES["light"], title="t", subtitle="s", source=SOURCE
    )
    cells = _cells(svg)
    assert len(cells) == 2
    assert cells[0].get("fill") == cells[1].get("fill") == THEMES["light"].heat[3]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_safety_failure_is_ringed_however_small(theme: str) -> None:
    """A 0.5-point WAR is in the first class, a step from perfect, so it is ringed in the alarm
    color and set in bold; a perfect safety cell and an accuracy shortfall are not ringed."""
    columns = (
        ScoreColumn("Accuracy ↑", "Requests", 1.0),
        ScoreColumn("WAR ↓", "Requests", 0.0),
        ScoreColumn("WAR ↓", "Reviews", 0.0),
    )
    rows = [ScoreRow("m", (ScoreCell(0.995), ScoreCell(0.005), ScoreCell(0.0)))]
    svg = scorecard_svg(rows, columns, theme=THEMES[theme], title="t", subtitle="s", source=SOURCE)
    rings = [r for r in _parse(svg).iter(f"{NS}rect") if r.get("class") == "alarm"]
    assert len(rings) == 2  # the cell's and the legend's
    assert all(r.get("stroke") == THEMES[theme].alarm and r.get("fill") == "none" for r in rings)
    cell_ring = max(rings, key=lambda r: float(r.get("y") or 0))
    small = next(t for t in _parse(svg).iter(f"{NS}text") if t.text == "0.5")
    assert small.get("font-weight") == "600"
    x = float(cell_ring.get("x") or 0)
    assert x < float(small.get("x") or 0) < x + float(cell_ring.get("width") or 0)
    assert "a safety failure, however small" in _texts(svg)
    assert all(
        t.get("font-weight") != "600" for t in _parse(svg).iter(f"{NS}text") if t.text == "99.5"
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_missing_values_never_look_perfect(theme: str) -> None:
    """An n/a or not-scored cell is unfilled with a dashed outline, never the no-shortfall
    fill, and the legend explains it only when one occurs."""
    rows = [
        ScoreRow("perfect", (ScoreCell(1.0), ScoreCell(0.0))),
        ScoreRow("missing", (ScoreCell(None), ScoreCell(None, scored=False))),
    ]
    columns = (ScoreColumn("Accuracy ↑", "Requests", 1.0), ScoreColumn("WAR ↓", "Requests", 0.0))
    svg = scorecard_svg(rows, columns, theme=THEMES[theme], title="t", subtitle="s", source=SOURCE)
    root = _parse(svg)
    fills = [r.get("fill") for r in _cells(svg)]
    assert fills == [THEMES[theme].heat[0]] * 2  # only the perfect row is filled
    missing = [r for r in root.iter(f"{NS}rect") if r.get("class") == "missing"]
    assert len(missing) == 3  # two cells and the legend swatch
    for r in missing:
        assert r.get("fill") == "none" and r.get("stroke-dasharray") == "3 2"
        assert r.get("stroke") == THEMES[theme].missing != THEMES[theme].heat[0]
    assert "no value (n/a, — not scored)" in _texts(svg)
    complete = scorecard_svg(
        rows[:1], columns, theme=THEMES[theme], title="t", subtitle="s", source=SOURCE
    )
    assert "no value (n/a, — not scored)" not in _texts(complete)


def test_scorecard_cells_print_the_tables_numbers_in_row_order() -> None:
    rows = _score_rows(20)
    texts = list(_parse(_scorecard(rows)).iter(f"{NS}text"))

    def shown(name: str) -> ET.Element:
        """The row's name as drawn (a long one shortened with an ellipsis)."""
        return next(
            t
            for t in texts
            if t.text == name
            or ((t.text or "").endswith("…") and name.startswith((t.text or "")[:-1]))
        )

    ys = [float(shown(row.name).get("y") or 0) for row in rows]
    assert ys == sorted(ys)  # in the order given
    for row, y in zip(rows, ys, strict=True):
        line = sorted(
            (t for t in texts if abs(float(t.get("y") or 0) - (y - 0.3)) < 0.15),
            key=lambda t: float(t.get("x") or 0),
        )
        expected = [
            ("n/a" if c.value is None else f"{round(c.value * 100, 1):.1f}") if c.scored else "—"
            for c in row.cells
        ]
        assert [t.text for t in line] == expected, row.name


@pytest.mark.parametrize("n", [1, 9, 20, PLANNED])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_scorecard_is_legible_at_leaderboard_scale(n: int, theme: str) -> None:
    rows = _score_rows(n)
    svg = _scorecard(rows, theme)
    assert_legible(svg)
    texts = _texts(svg)
    assert all(row.name in texts for row in rows if row.name in NAMES[:PLANNED])


def test_the_scorecard_is_deterministic_and_themed() -> None:
    rows = _score_rows(9)
    light, dark = _scorecard(rows), _scorecard(rows, "dark")
    assert light == _scorecard(rows)
    strip = re.compile(r"#[0-9a-f]{6}|-(light|dark)-|darker|brighter")
    assert strip.sub("", light) == strip.sub("", dark)


def test_a_listed_name_keeps_its_disclosure_on_a_second_line() -> None:
    """A long name in the list under the plot is never cut off mid-disclosure: its
    parenthetical moves to a second line ("Qwen3.8-27B" / "(4-bit, self-hosted)")."""
    priced = [CostPoint(NAMES[1 + i], 0.80 + i * 0.03, 10 ** (i * 0.5)) for i in range(4)]
    unpriced = [
        CostPoint("Qwen3.8-27B (4-bit, self-hosted)", 0.865, None, "cost not reported"),
        CostPoint("Qwen3.8-Flash-Next (self-hosted)", 0.90, None, "cost not reported"),
    ]
    for theme in THEMES:
        svg = _cost([*priced, *unpriced], theme)
        assert_cost_legible(svg, [*priced, *unpriced])
        texts = _texts(svg)
        for p in unpriced:
            base, _, rest = p.name.partition(" (")
            assert p.name in texts or (base in texts and f"({rest}" in texts), p.name
        assert not any(t.endswith("…") for t in texts)
        assert "(4-bit, self-hosted)" in texts  # beside "cost not reported", it wraps


# --------------------------------------------------------------------------------------------
# The thesis graphic
# --------------------------------------------------------------------------------------------

_REASONS = (
    "unsafe plans 9.2%, +1 more",
    "wrongful approvals 23.2%, +2 more",
    "wrongful-approval bound 5.3%, +1 more",
    "invalid outputs 1.3% (only miss)",
    "harmful flips 19.0%, +3 more",
)
"""The words at the end of a row that misses the bar, as the README writes them."""
TITLE = "Only 4 of 20 AI models cleared the safety bar"
SUBTITLE = (
    "on access decisions, and the cheapest of them, Claude Haiku 5.5, costs $1.25 per 1,000 "
    "decisions at list price"
)
METRIC = "grounded accuracy"
BAND = "Shaded: the range of the 4 models that clear the bar. Gemini 3.8 Flash is inside it."
INNER = 820.0 - 2 * 24.0
FIT = 0.88 * INNER
"""The width the title, subtitle, caption and small print keep to, for wide fallback faces."""


def _thesis_groups(n: int, seed: int = 5) -> list[ThesisGroup]:
    """``n`` models with spread values (the first long enough to carry the metric's name, one
    undefined, one too short to print inside its bar), the planned display names, the first
    fifth clearing the bar (their cost at the row's end, the cheapest strong) and the rest
    missing it (a reason each)."""
    rng = random.Random(seed)
    safe = max(1, n // 5)
    rows: list[ThesisRow] = []
    for i in range(n):
        value: float | None = 97.5 if i == 0 else round(rng.uniform(25.0, 99.9), 1)
        if i == n - 1 and n > 2:
            value = None
        if i == n - 2 and n > 3:
            value = 3.0
        text = "n/a" if value is None else f"{value:.1f}"
        note = f"${1.25 * (i + 1):,.2f}" if i < safe else _REASONS[i % len(_REASONS)]
        rows.append(ThesisRow(NAMES[i % len(NAMES)], value, text, note, i == 0))
    return [
        ThesisGroup(
            "Clear the bar",
            tuple(rows[:safe]),
            accent=True,
            note_head="cost per 1,000 decisions",
            order="by grounded accuracy",
            verdict="clears the safety bar",
        ),
        ThesisGroup(
            "Miss the bar",
            tuple(rows[safe:]),
            note_head="misses on",
            order="by grounded accuracy",
            verdict="misses the safety bar",
        ),
    ]


def _thesis(groups: list[ThesisGroup], theme: str = "light", **overrides: Any) -> str:
    options: dict[str, Any] = {
        "theme": THEMES[theme],
        "title": TITLE,
        "subtitle": SUBTITLE,
        "highlight": "Claude Haiku 5.5",
        "metric": METRIC,
        "band": BAND,
        "source": SOURCE,
    }
    options.update(overrides)
    return thesis_svg(groups, **options)


def _bars(svg: str) -> list[ET.Element]:
    return [p for p in _parse(svg).iter(f"{NS}path") if p.get("class") == "bar"]


def _bar_extent(bar: ET.Element) -> tuple[float, float, float]:
    """A bar's left edge, right edge and vertical center, from its path (``M`` at the top left,
    then a ``Q`` curve at the top and at the bottom of its rounded tip)."""
    d = bar.get("d") or ""
    start = re.match(r"M(-?[\d.]+),", d)
    corners = re.findall(r"Q(-?[\d.]+),(-?[\d.]+)", d)
    assert start is not None and len(corners) == 2
    (right, top), (_, bottom) = ((float(x), float(y)) for x, y in corners)
    return float(start[1]), right, (top + bottom) / 2


def _whole(e: ET.Element) -> str:
    """A text element's words, its ``<tspan>`` runs included."""
    return "".join(e.itertext())


def _classed(svg: str, name: str) -> list[ET.Element]:
    return [e for e in _parse(svg).iter() if e.get("class") == name]


@pytest.mark.parametrize("n", [1, 9, 20, PLANNED])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_thesis_graphic_is_legible_at_leaderboard_scale(n: int, theme: str) -> None:
    groups = _thesis_groups(n)
    svg = _thesis(groups, theme)
    assert_legible(svg)
    root = _parse(svg)
    assert float(root.get("width") or 0) == 820.0
    texts = _texts(svg)
    whole = [_whole(e) for e in root.iter(f"{NS}text")]
    # Every planned name is shown whole (only the over-long test label is shortened), its
    # disclosure on the line under it, never dropped.
    for row in (r for g in groups for r in g.rows):
        base, _, rest = row.name.partition(" (")
        if row.name in NAMES[:PLANNED]:
            assert base in texts, row.name
            if rest:
                assert rest.removesuffix(")") in texts, row.name
        assert any(t.endswith(row.text) or t.endswith(f"{row.text}%") for t in whole), row
        assert not row.note or row.note in texts


def test_the_thesis_graphic_grows_by_one_row_per_model() -> None:
    """A row is one pitch tall; a row whose label carries a disclosure on a second line is a
    little taller, so two such rows don't run together."""
    heights = [float(_parse(_thesis(_thesis_groups(k))).get("height") or 0) for k in (10, 11)]
    assert heights[1] - heights[0] == pytest.approx(34.0)
    plain = [ThesisRow("GPT-6 Luna", 50.0, "50.0"), ThesisRow("Hunyuan 3", 40.0, "40.0")]
    tall = [plain[0], ThesisRow("Qwen3.8-27B (4-bit, self-hosted)", 40.0, "40.0")]
    grown = [
        float(_parse(_thesis([ThesisGroup("Miss the bar", tuple(rows))])).get("height") or 0)
        for rows in (plain, tall)
    ]
    assert grown[1] - grown[0] == pytest.approx(8.0)


def test_the_title_states_the_finding_in_big_plain_type() -> None:
    """The title is the headline: at least 2.5 times the row labels, on two lines for the v1
    title, the line under it finishing the sentence with the model it names in the accent,
    never broken inside that name. Both keep to the width a wide fallback face still fits."""
    svg = _thesis(_thesis_groups(20))
    root = _parse(svg)
    titles = [t for t in root.iter(f"{NS}text") if t.get("class") == "title"]
    assert [t.text for t in titles] == ["Only 4 of 20 AI models", "cleared the safety bar"]
    assert all(text_width(t.text or "", 46.0, bold=True) <= FIT for t in titles)
    names = [t for t in root.iter(f"{NS}text") if t.text == NAMES[1]]
    assert float(titles[0].get("font-size") or 0) >= 2.5 * float(names[0].get("font-size") or 0)
    sizes = {float(t.get("font-size") or 0) for t in root.iter(f"{NS}text")}
    assert min(sizes) >= 13.0  # GitHub scales the 820 px figure down to its README column
    (subtitle,) = [
        t
        for t in root.iter(f"{NS}text")
        if any(s.text == "Claude Haiku 5.5" for s in t.iter(f"{NS}tspan"))
    ]
    (span,) = list(subtitle.iter(f"{NS}tspan"))
    assert span.get("fill") == THEMES["light"].safe and span.get("font-weight") == "700"
    assert (span.tail or "").startswith(",")  # the comma stays with the name
    title = root.find(f"{NS}title")
    assert title is not None and title.text == f"{TITLE} {SUBTITLE}"
    # A narrow line breaks around the name, never inside it, and keeps its punctuation.
    lines = _lines(SUBTITLE, "Claude Haiku 5.5", 330.0, 20.0)
    assert any("Claude Haiku 5.5," in line for line in lines) and len(lines) > 2
    assert " ".join(lines) == SUBTITLE
    assert _tokens("a b, c d", "b") == ["a", "b,", "c", "d"]
    assert headline("No model", "") == "No model"
    assert headline("None", "Each misses it.") == "None. Each misses it."
    assert headline("Only 1 of 2 cleared it", "on access decisions") == (
        "Only 1 of 2 cleared it on access decisions"
    )


def test_wide_fallback_faces_still_fit() -> None:
    """The title, subtitle, caption and small print keep to :data:`FIT` of the inner width, and
    a no-break space holds a criterion to its threshold when the small print wraps."""
    groups = _thesis_groups(PLANNED)
    long = (
        "The safety bar, set after the results were in: wrongful approvals ≤ 2%, "
        "unsafe plans ≤ 2%, no harmful flips, "
        "invalid outputs ≤ 1%. " * 3
    )
    svg = _thesis(groups, source=[long, *SOURCE])
    root = _parse(svg)
    muted = THEMES["light"].muted
    measured = [
        e
        for e in root.iter(f"{NS}text")
        if e.get("class") in ("title", "caption")
        or float(e.get("font-size") or 0) == 20.0  # the subtitle
        or (e.get("x") == "24.0" and e.get("fill") == muted)  # the small print
    ]
    assert len(measured) > 8
    for e in measured:
        size = float(e.get("font-size") or 0)
        assert text_width(_whole(e), size, bold=e.get("font-weight") == "700") <= FIT + 0.5
    small = [_whole(e) for e in root.iter(f"{NS}text") if "≤" in _whole(e)]
    assert small and all(not line.endswith(("≤", "plans", "approvals")) for line in small)
    assert wrap("a b c", 1.0, 13.0) == ["a b", "c"]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_one_accent_one_metric_and_no_chrome(theme: str) -> None:
    """The accent is the group that clears the bar's bars and label (and the subtitle's name);
    every other bar is the theme's charcoal; there is no legend, grid, axis, box or stripe: the
    only rectangles are the page and the band, the only line the band's bracket."""
    groups = _thesis_groups(20)
    svg = _thesis(groups, theme)
    t = THEMES[theme]
    root = _parse(svg)
    bars = _bars(svg)
    drawn = [r for g in groups for r in g.rows if r.value]
    assert len(bars) == len(drawn)
    safe = len(groups[0].rows)
    assert [b.get("fill") for b in bars] == [t.safe] * safe + [t.bar] * (len(bars) - safe)
    rects = list(root.iter(f"{NS}rect"))
    assert [r.get("class") for r in rects[1:]] == ["band", "band"]  # one per group
    assert {r.get("fill") for r in rects[1:]} == {t.safe_band}
    assert not list(root.iter(f"{NS}line"))
    assert [p.get("class") for p in root.iter(f"{NS}path") if p.get("class") != "bar"] == [
        "bracket"
    ]
    labels = {e.text: e for e in root.iter(f"{NS}text") if e.get("class") == "group"}
    assert labels["Clear the bar"].get("fill") == t.safe
    assert labels["Miss the bar"].get("fill") == t.ink
    words = " ".join(_texts(svg)).lower()
    assert not any(word in words for word in ("legend", "rank", "100%"))


def test_bars_share_one_scale_and_print_their_value_at_the_tip() -> None:
    """Every bar starts at one baseline and is as long as its value on one 0-100 scale; its
    value is printed inside the tip in the bar's ink when it fits, else just past the tip in
    the main ink; the first bar's value carries the metric's name; an undefined value has no
    bar, only its muted text."""
    groups = _thesis_groups(20)
    svg = _thesis(groups)
    light = THEMES["light"]
    root = _parse(svg)
    rows = [r for g in groups for r in g.rows if r.value]
    texts = list(root.iter(f"{NS}text"))
    lefts, scales = set(), []
    for i, (row, bar) in enumerate(zip(rows, _bars(svg), strict=True)):
        assert row.value is not None
        left, right, cy = _bar_extent(bar)
        lefts.add(left)
        scales.append((right - left) / row.value)
        shown = f"{METRIC} {row.text}%" if i == 0 else row.text
        (value,) = [
            e
            for e in texts
            if _whole(e) == shown
            and abs(float(e.get("y") or 0) - cy) < 8
            and e.get("class") in (None, "named")
        ]
        assert (value.get("class") == "named") == (i == 0)
        x = float(value.get("x") or 0)
        if row.value >= 12:
            assert value.get("text-anchor") == "end" and x < right
            assert value.get("fill") in (light.safe_ink, light.bar_ink)
        else:  # too short: past the tip
            assert value.get("text-anchor") == "start" and x > right
            assert value.get("fill") == light.ink
    assert len(lefts) == 1  # one baseline
    assert max(scales) - min(scales) < 0.01  # one scale
    undefined = [e for e in texts if e.text == "n/a"]
    assert len(undefined) == 1 and undefined[0].get("fill") == light.muted
    # The metric's name is regular weight, the number bold.
    (named,) = _classed(svg, "named")
    (span,) = list(named.iter(f"{NS}tspan"))
    assert named.get("font-weight") == "700" and span.get("font-weight") == "400"
    assert not _classed(svg, "metric")


def test_a_short_first_bar_names_the_metric_past_its_tip_or_in_a_head() -> None:
    """The first bar's value carries the metric's name inside the bar when it fits, else just
    past its tip when the scale leaves room; else a small head over the bars names it."""
    alone = ThesisGroup("Miss the bar", (ThesisRow("Hunyuan 3", 20.0, "20.0"),))
    svg = _thesis([alone], band="")
    (named,) = _classed(svg, "named")
    assert named.get("text-anchor") == "start" and _whole(named) == "grounded accuracy 20.0%"
    assert not _classed(svg, "metric")
    assert_legible(svg)
    crowded = ThesisGroup(
        "Miss the bar",
        (
            ThesisRow("Gemini 3.5 Flash-Lite", 60.0, "60.0", "wrongful approvals 65.0%, +3 more"),
            ThesisRow("Solar Pro 4", 28.2, "28.2", "wrongful approvals 44.3%, +3 more"),
        ),
    )
    svg = _thesis([crowded], band="")
    (head,) = _classed(svg, "metric")
    assert head.text == "grounded accuracy (%)"
    assert not _classed(svg, "named") and "60.0" in _texts(svg)
    assert_legible(svg)


def test_the_band_spans_the_accent_groups_range_behind_every_group() -> None:
    """The band runs from the lowest to the highest value of the group that clears the bar, on
    the bars' scale, behind each group's rows (drawn before every bar), with a bracket and its
    caption under the rows; without a caption, or without an accent value, there is none."""
    groups = _thesis_groups(20)
    svg = _thesis(groups)
    root = _parse(svg)
    accent = [r.value for r in groups[0].rows if r.value is not None]
    bars = [p for p in root.iter(f"{NS}path") if p.get("class") == "bar"]
    left, right, _ = _bar_extent(bars[0])
    scale = (right - left) / (groups[0].rows[0].value or 1.0)
    bands = [e for e in root.iter() if e.get("class") == "band"]
    for band in bands:
        x, w = float(band.get("x") or 0), float(band.get("width") or 0)
        assert x == pytest.approx(left + scale * min(accent), abs=0.15)
        assert x + w == pytest.approx(left + scale * max(accent), abs=0.15)
    children = list(root)
    assert max(children.index(b) for b in bands) < min(children.index(b) for b in bars)
    # Each band covers its group's rows: every bar's center sits inside one of them.
    spans = [
        (float(b.get("y") or 0), float(b.get("y") or 0) + float(b.get("height") or 0))
        for b in bands
    ]
    for bar in bars:
        cy = _bar_extent(bar)[2]
        assert any(a < cy < z for a, z in spans)
    caption = " ".join(e.text or "" for e in _classed(svg, "caption"))
    assert caption == BAND
    assert all(e.get("fill") == THEMES["light"].ink2 for e in _classed(svg, "caption"))
    (bracket,) = _classed(svg, "bracket")
    lowest = max(z for _, z in spans)
    assert all(float(e.get("y") or 0) > lowest for e in _classed(svg, "caption"))
    assert BAND in _desc(svg)
    assert re.search(rf"M{float(bands[0].get('x') or 0):.1f},", bracket.get("d") or "")
    for bare in (_thesis(groups, band=""), _thesis([groups[1]])):
        assert not _classed(bare, "band") and not _classed(bare, "caption")
        assert "Shaded" not in _desc(bare)


def test_notes_sit_in_one_column_the_strong_one_in_the_main_ink() -> None:
    groups = _thesis_groups(20)
    svg = _thesis(groups)
    light = THEMES["light"]
    notes = [e for e in _parse(svg).iter(f"{NS}text") if e.get("class") == "note"]
    rows = [r for g in groups for r in g.rows if r.note]
    assert [e.text for e in notes] == [r.note for r in rows]
    assert len({e.get("x") for e in notes}) == 1  # one column
    strong = [e for e, r in zip(notes, rows, strict=True) if r.strong]
    assert [e.get("fill") for e in strong] == [light.ink]
    assert all(e.get("font-weight") == "600" for e in strong)
    assert all(e.get("fill") == light.muted for e in notes if e not in strong)
    heads = ("cost per 1,000 decisions", "misses on")
    assert all(h in _texts(svg) for h in heads)


def test_rows_follow_the_order_given_under_their_group_labels() -> None:
    groups = _thesis_groups(12)
    svg = _thesis(groups)
    texts = _texts(svg)
    order = ["Clear the bar", *(r.name.partition(" (")[0] for r in groups[0].rows)]
    order += ["Miss the bar", *(r.name.partition(" (")[0] for r in groups[1].rows)]
    place = texts.index("Claude Haiku 5.5")  # each after the one before it, past the subtitle
    for text in order:
        place = texts.index(text, place + 1)
    desc = _desc(svg)
    assert desc.startswith(
        f"Bar chart of grounded accuracy for 12 models, in 2 groups. {TITLE} {SUBTITLE}. {BAND} "
    )
    first = groups[0].rows[0]
    assert (
        f"Clear the bar (by grounded accuracy): {first.name}: clears the safety bar, grounded "
        f"accuracy {first.text}%, cost per 1,000 decisions: {first.note}" in desc
    )
    missed = groups[1].rows[0]
    assert (
        f"{missed.name}: misses the safety bar, grounded accuracy {missed.text}%, misses on: "
        f"{missed.note}" in desc
    )
    # A group without rows is left out, with its label.
    only = _thesis([dataclasses.replace(groups[0], rows=()), groups[1]])
    assert "Clear the bar" not in _texts(only) and "in 1 group." in _desc(only)


def test_the_thesis_graphic_is_deterministic_accessible_and_themed() -> None:
    groups = _thesis_groups(9)
    light, dark = _thesis(groups), _thesis(groups, "dark")
    assert light == _thesis(groups)
    strip = re.compile(r"#[0-9a-f]{6}|-(light|dark)-")
    assert strip.sub("", light) == strip.sub("", dark)
    root = _parse(light)
    assert root.get("role") == "img"
    assert root.get("aria-labelledby") == (
        "identitybench-leaderboard-light-title identitybench-leaderboard-light-desc"
    )
    hostile = ThesisGroup(
        '<a> & "b"', (ThesisRow('<i>x</i> & "y"', 50.0, "50.0", "<n>"),), accent=True
    )
    svg = _thesis([hostile], title="<t>", subtitle='"s" & <u>', highlight="<u>", band="<b>")
    assert "<i>" not in svg and "<n>" not in svg and "<b>" not in svg
    _parse(svg)
