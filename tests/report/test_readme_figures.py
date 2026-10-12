"""The README figures' renderers: the hero dot plot, the leaderboard table, the cost against
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
    BoardColumn,
    BoardCost,
    BoardRow,
    BoardValue,
    cost_domain,
    leaderboard_svg,
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


def test_the_heat_ramps_are_monotonic_and_their_text_is_legible() -> None:
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
# The leaderboard
# --------------------------------------------------------------------------------------------

BOARD_COLUMNS = (
    BoardColumn(("Grounded", "accuracy"), "bar", "higher", ("grounded",), ("grounded accuracy",)),
    BoardColumn(
        ("Correct", "automation"), "number", "higher", ("automation",), ("correct automation",)
    ),
    BoardColumn(
        ("Wrongful approvals", "requests / reviews"),
        "severity",
        "lower",
        ("war:req", "war:rev"),
        ("wrongful approvals on requests", "wrongful approvals on reviews"),
    ),
    BoardColumn(("Unsafe", "plans"), "severity", "lower", ("unsafe",), ("unsafe plans",)),
    BoardColumn(("Risk", "cost"), "number", "lower", ("nrc",), ("risk cost",), "", True),
    BoardColumn(("Cost per", "1k decisions"), "cost", "lower", ("cost",), ("cost",)),
)
_KINDS = ("charge", "equivalent", "charge", "unmetered", "charge", "equivalent", "none")
CRITERIA = ("wrongful approvals", "unsafe plans", "harmful flips", "invalid outputs")
"""The safety bar's criteria, one pip each."""
_MISSES = (
    ("wrongful approvals", "unsafe plans", "harmful flips", "invalid outputs"),
    ("unsafe plans",),
    ("invalid outputs",),
    ("unsafe plans", "invalid outputs"),
    ("harmful flips",),
)


def _pct(share: float) -> BoardValue:
    shown = round(share * 100, 1)
    return BoardValue(f"{shown:.1f}", shown)


def _board_rows(n: int, seed: int = 11) -> list[BoardRow]:
    """``n`` models with spread values, every cost footing and safety verdict, the planned
    display names and vendors, then the two reference rows."""
    rng = random.Random(seed)
    rows: list[BoardRow] = []
    for i in range(n):
        kind = _KINDS[i % len(_KINDS)]
        usd = round(10 ** rng.uniform(-0.7, 1.8), 2)
        cost = (
            BoardCost(f"${usd:,.2f}", kind, usd)
            if kind in ("charge", "equivalent")
            else BoardCost("unmetered" if kind == "unmetered" else "n/a", kind)
        )
        war = [0.0 if rng.random() < 0.5 else rng.uniform(0.001, 0.7) for _ in range(2)]
        clears = i % 5 == 0
        misses = () if clears else _MISSES[i % len(_MISSES)]
        unsafe = 0.0 if clears else rng.uniform(0.0, 0.4)
        failed = {k for k, w in (("war:req", war[0]), ("war:rev", war[1])) if w > 0.02}
        if "wrongful approvals" in misses and not failed:
            failed.add("war:rev")  # judged on the bound: a small value can miss
        if "unsafe plans" in misses:
            failed.add("unsafe")
        rows.append(
            BoardRow(
                NAMES[i % len(NAMES)],
                {
                    "grounded": _pct(rng.uniform(0.25, 1.0)),
                    "automation": _pct(rng.uniform(0.4, 0.802)),
                    "war:req": _pct(war[0]),
                    "war:rev": _pct(war[1]),
                    "unsafe": _pct(unsafe),
                    "nrc": BoardValue(f"{0.198 + i * 0.08:.3f}", 0.198 + i * 0.08),
                },
                cost,
                VENDORS[i % len(VENDORS)],
                clears,
                misses,
                frozenset() if clears else frozenset(failed),
            )
        )
    dash = BoardValue("—")
    rows += [
        BoardRow(
            "Human-review reference",
            {
                "grounded": dash,
                "automation": dash,
                "war:req": _pct(0.0),
                "war:rev": _pct(0.0),
                "unsafe": dash,
                "nrc": BoardValue("1.000", 1.0),
            },
            BoardCost("—", "absent"),
            reference=True,
        ),
        BoardRow(
            "Oracle reference",
            {
                "grounded": _pct(1.0),
                "automation": _pct(0.802),
                "war:req": _pct(0.0),
                "war:rev": _pct(0.0),
                "unsafe": _pct(0.0),
                "nrc": BoardValue("0.198", 0.198),
            },
            BoardCost("—", "absent"),
            reference=True,
        ),
    ]
    return rows


def _board(rows: list[BoardRow], theme: str = "light", **overrides: Any) -> str:
    options: dict[str, Any] = {
        "theme": THEMES[theme],
        "title": "Leaderboard, ranked by risk cost",
        "subtitle": "2 of 9 models clear the safety bar; model-a is the cheapest of them.",
        "source": SOURCE,
        "badge": "Interim · 9/26 models",
        "criteria": CRITERIA,
    }
    options.update(overrides)
    return leaderboard_svg(rows, BOARD_COLUMNS, **options)


@pytest.mark.parametrize("n", [1, 9, 20, PLANNED])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_leaderboard_is_legible_at_leaderboard_scale(n: int, theme: str) -> None:
    rows = _board_rows(n)
    svg = _board(rows, theme)
    assert_legible(svg)
    root = _parse(svg)
    assert float(root.get("width") or 0) == 820.0
    texts = _texts(svg)
    # Every planned name is shown whole (only the over-long test label is shortened), its
    # disclosure on the line under it.
    for row in rows[:n]:
        base, _, rest = row.name.partition(" (")
        if row.name in NAMES[:PLANNED]:
            assert base in texts, row.name
            if rest:
                assert any(t.startswith(rest.rstrip(")")) or rest.rstrip(")") in t for t in texts)


def test_the_leaderboard_grows_by_one_row_per_model() -> None:
    heights = [float(_parse(_board(_board_rows(k))).get("height") or 0) for k in (5, 6)]
    assert heights[1] - heights[0] == pytest.approx(34.0)


def test_leaderboard_rows_follow_the_order_given_unnumbered_with_references_apart() -> None:
    """Rows carry no rank number: the order is one of point estimates (a tie bracket says where
    no test separates them)."""
    rows = _board_rows(9)
    svg = _board(rows)
    texts = list(_parse(svg).iter(f"{NS}text"))
    names = [r.name.partition(" (")[0] for r in rows]  # a disclosure goes on the line under
    drawn = [t for t in texts if t.text in names]
    ys = [float(t.get("y") or 0) for t in drawn]
    assert [t.text for t in drawn] == names and ys == sorted(ys)
    assert not any((t.text or "").isdigit() for t in texts)  # no rank column
    for reference in ("Human-review reference", "Oracle reference"):
        name = next(t for t in drawn if t.text == reference)
        assert name.get("font-style") == "italic"
        assert name.get("fill") == THEMES["light"].ink2  # muted, set apart
    desc = _desc(svg)
    assert "Human-review reference: reference, not ranked, grounded accuracy —, " in desc
    assert f"{rows[0].name} ({VENDORS[0]}): clears the safety bar, grounded " in desc
    assert "misses the safety bar on unsafe plans" in desc
    assert "rank" not in desc.replace("not ranked", "")
    places = [desc.index(f"{r.name} (") for r in rows if not r.reference]
    assert places == sorted(places)


def _bracket(svg: str) -> list[ET.Element]:
    return [t for t in _parse(svg).iter(f"{NS}text") if "rotate(-90" in (t.get("transform") or "")]


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_tie_at_the_top_is_bracketed_in_the_margin(theme: str) -> None:
    labels = ("no significant difference in risk cost", "tied on risk cost", "tied")
    rows = _board_rows(PLANNED)
    svg = _board(rows, theme, tie_rows=12, tie_labels=labels)
    assert_legible(svg)  # the vertical label's box is turned with it
    (label,) = _bracket(svg)
    assert label.text == labels[0] and label.get("font-style") == "italic"
    root = _parse(svg)
    names = {t.text: t for t in root.iter(f"{NS}text")}
    first = names[rows[0].name.partition(" (")[0]]
    assert float(label.get("x") or 0) < float(first.get("x") or 0) == 24.0  # in the margin
    (bracket,) = [p for p in root.iter(f"{NS}path") if p.get("class") == "tie"]
    ys = [float(v) for v in re.findall(r",(-?[\d.]+)", bracket.get("d") or "")]
    tops = [float(names[r.name.partition(" (")[0]].get("y") or 0) for r in rows[:13]]
    assert min(ys) < tops[0] and tops[11] < max(ys) < tops[12]  # the first 12 rows only
    assert "The first 12 rows are bracketed: no significant difference in risk cost." in _desc(svg)
    # A short tie takes a shorter label; no tie, or a tie of one, draws nothing.
    short = _board(rows, theme, tie_rows=2, tie_labels=labels)
    assert [t.text for t in _bracket(short)] == ["tied"]
    assert_legible(short)
    for tie in (0, 1, PLANNED + 5):
        assert not _bracket(_board(rows, theme, tie_rows=tie, tie_labels=labels))


def test_leaderboard_cells_print_every_value_given() -> None:
    rows = _board_rows(PLANNED)
    svg = _board(rows)
    texts = _texts(svg)
    desc = _desc(svg)
    for row in rows:
        for key, value in row.values.items():
            assert value.text in texts, (row.name, key)
        assert row.cost is not None and row.cost.text in texts
        if not row.reference:
            assert f"risk cost {row.values['nrc'].text}" in desc
    # The sort key's numbers are set in bold, no other value is (the chips aside).
    values = [t for t in _parse(svg).iter(f"{NS}text") if t.get("font-size") == "13"]
    bold = sorted(t.text or "" for t in values if t.get("font-weight") == "600")
    assert bold == sorted(r.values["nrc"].text for r in rows if not r.reference)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_only_failures_carry_color_on_the_scorecards_scale(theme: str) -> None:
    """A printed 0.0 has no chip; any other rate sits on a chip in its scorecard class, with
    text that is legible on it (the heat ramp's ink)."""
    zero, small, large = _pct(0.0), _pct(0.004), _pct(0.493)
    row = BoardRow(
        "m",
        {
            "grounded": _pct(0.9),
            "automation": _pct(0.8),
            "war:req": zero,
            "war:rev": small,
            "unsafe": large,
            "nrc": BoardValue("0.300", 0.3),
        },
        BoardCost("$1.00", "charge", 1.0),
    )
    svg = _board([row], theme)
    t = THEMES[theme]
    chips = [r for r in _parse(svg).iter(f"{NS}rect") if r.get("height") == "18.0"]
    assert [c.get("fill") for c in chips] == [
        t.heat[shortfall_bin(0.4)],
        t.heat[shortfall_bin(49.3)],
    ]
    for text, step in (("0.4", shortfall_bin(0.4)), ("49.3", shortfall_bin(49.3))):
        element = next(e for e in _parse(svg).iter(f"{NS}text") if e.text == text)
        assert element.get("fill") == t.heat_ink[step]
    zero_text = [e for e in _parse(svg).iter(f"{NS}text") if e.text == "0.0"]
    assert zero_text and all(e.get("fill") == t.ink2 for e in zero_text)
    # The key shows every class past 0.
    assert all(label in _texts(svg) for label in ("≤1", "≤2.5", "≤5", "≤10", "≤25", ">25"))


def test_cost_marks_sit_on_one_log_track_and_their_shape_gives_the_footing() -> None:
    def row(name: str, cost: BoardCost) -> BoardRow:
        return BoardRow(name, {"grounded": _pct(0.9), "nrc": BoardValue("0.2", 0.2)}, cost)

    rows = [
        row("cheap", BoardCost("$0.24", "charge", 0.24)),
        row("listed", BoardCost("$11.22", "equivalent", 11.22)),
        row("dear", BoardCost("$54.96", "charge", 54.96)),
        row("local", BoardCost("unmetered", "unmetered")),
        row("guess", BoardCost("≈$0.50", "estimate")),
        row("unknown", BoardCost("n/a", "none")),
    ]
    columns = [c for c in BOARD_COLUMNS if c.kind in ("bar", "cost")]
    svg = leaderboard_svg(
        rows, columns, theme=THEMES["light"], title="t", subtitle="s", source=SOURCE
    )
    root = _parse(svg)
    circles = [c for c in root.iter(f"{NS}circle") if c.get("data-cost") == "charge"]
    diamonds = [p for p in root.iter(f"{NS}path") if p.get("data-cost") == "equivalent"]
    assert len(circles) == 2 and len(diamonds) == 1  # no position for a cost that isn't a price
    xs = sorted(float(c.get("cx") or 0) for c in circles)
    diamond_x = float((diamonds[0].get("d") or "M0,").split(",")[0][1:])
    assert xs[0] < diamond_x < xs[1]  # one log track for both footings
    assert cost_domain([0.24, 11.22, 54.96]) == (0.1, 100.0)
    assert cost_domain([2.0, 3.0]) == (1.0, 10.0)  # at least a decade
    texts = _texts(svg)
    for shown in ("unmetered", "≈$0.50", "n/a", "Cost, log scale $0.10–$100", "API charge"):
        assert shown in texts
    assert "API-equivalent" in texts and "unmetered: no per-call price" in texts
    assert "≈ CLI estimate, not a price" in texts and "n/a: no usable cost" in texts
    # One rule for every cost that isn't a price: a muted italic word, right-aligned in the
    # price column where the prices end, with no mark and no track.
    prices = [t for t in root.iter(f"{NS}text") if t.text in ("$0.24", "$11.22", "$54.96")]
    words = [t for t in root.iter(f"{NS}text") if t.text in ("unmetered", "≈$0.50", "n/a")]
    right = {t.get("x") for t in prices}
    assert len(right) == 1 and {t.get("x") for t in words} == right
    assert all(t.get("text-anchor") == "end" for t in [*prices, *words])
    assert all(
        t.get("font-style") == "italic" and t.get("fill") == THEMES["light"].muted for t in words
    )
    # The track is neutral, with no stem from its left end (a log scale has no zero), and a
    # faint tick at each inner decade, labelled once under the rows.
    lines = list(root.iter(f"{NS}line"))
    tracks = [ln for ln in lines if ln.get("stroke-width") == "2"]
    assert len(tracks) == 3 and {ln.get("stroke") for ln in tracks} == {THEMES["light"].grid}
    ticks = [
        ln
        for ln in lines
        if ln.get("x1") == ln.get("x2") and float(ln.get("y2") or 0) - float(ln.get("y1") or 0) == 5
    ]
    assert len(ticks) == 2 * 3  # $1 and $10 on each of the three tracks
    assert "$1" in texts and "$10" in texts
    desc = _desc(svg)
    assert "listed: grounded accuracy 90.0%, cost per 1,000 decisions $11.22 " in desc
    assert "(API-equivalent, not a charge)" in desc
    assert_legible(svg)


def test_the_safety_verdict_is_a_shape_and_pips_never_color_alone() -> None:
    """A check, or a cross with a pip per criterion in the bar's order, filled for each one
    missed; the key names the criteria."""
    rows = _board_rows(5)
    svg = _board(rows)
    root = _parse(svg)
    checks = [c for c in root.iter(f"{NS}circle") if c.get("class") == "clears"]
    crosses = [c for c in root.iter(f"{NS}circle") if c.get("class") == "misses"]
    assert len(checks) == sum(r.clears is True for r in rows)
    assert len(crosses) == sum(r.clears is False for r in rows)
    assert all(c.get("fill") == THEMES["light"].good for c in checks)
    assert all(c.get("fill") == "none" for c in crosses)  # a ring, not a filled dot
    filled = [c for c in root.iter(f"{NS}circle") if c.get("class") == "missed"]
    assert len(filled) == sum(len(r.misses) for r in rows)
    assert all(c.get("fill") == THEMES["light"].alarm for c in filled)
    for row, cross in zip([r for r in rows if r.clears is False], crosses, strict=True):
        cy = cross.get("cy")
        pips = sorted(
            (
                c
                for c in root.iter(f"{NS}circle")
                if c.get("cy") == cy and float(c.get("r") or 0) < 3
            ),
            key=lambda c: float(c.get("cx") or 0),
        )
        assert [c.get("class") == "missed" for c in pips] == [k in row.misses for k in CRITERIA]
    texts = _texts(svg)
    assert " · ".join(CRITERIA) + ", filled if missed" in texts
    assert "Safety" in texts and "bar" in texts and "clears" in texts and "misses" in texts


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_value_that_misses_the_bar_is_outlined_whatever_its_class(theme: str) -> None:
    """The severity classes don't line up with the bar's thresholds, and wrongful approvals are
    judged on their bound: the values whose own check fails are outlined on the page around
    the chip, so the outline shows on every fill, and the key says so."""
    zero, small, large = _pct(0.0), _pct(0.017), _pct(0.25)
    row = BoardRow(
        "m",
        {"war:req": zero, "war:rev": small, "unsafe": large, "nrc": BoardValue("0.3", 0.3)},
        BoardCost("$1.00", "charge", 1.0),
        clears=False,
        misses=("wrongful approvals", "unsafe plans"),
        failed=frozenset({"war:rev", "unsafe"}),
    )
    passing = BoardRow(
        "n",
        {"war:req": zero, "war:rev": small, "unsafe": _pct(0.017), "nrc": BoardValue("0.4", 0.4)},
        BoardCost("$2.00", "charge", 2.0),
        clears=True,
    )
    svg = _board([row, passing], theme)
    assert_legible(svg)
    t = THEMES[theme]
    outlines = [r for r in _parse(svg).iter(f"{NS}rect") if r.get("class") == "fails"]
    assert len(outlines) == 2 and all(r.get("stroke") == t.alarm for r in outlines)
    chips = [r for r in _parse(svg).iter(f"{NS}rect") if r.get("height") == "18.0"]
    for outline in outlines:  # each surrounds a chip, off it on every side
        x, y = float(outline.get("x") or 0), float(outline.get("y") or 0)
        assert any(
            float(c.get("x") or 0) - x == pytest.approx(2.0)
            and float(c.get("y") or 0) - y == pytest.approx(2.0)
            for c in chips
        )
    assert "misses the bar" in _texts(svg)
    desc = _desc(svg)
    assert "wrongful approvals on reviews 1.7% (misses the bar)" in desc
    assert desc.count("(misses the bar)") == 2
    # Without a failing value there is no outline and no key for it.
    clean = _board([passing], theme)
    assert "misses the bar" not in _texts(clean) and 'class="fails"' not in clean


def test_a_reference_label_is_never_shortened_and_graphics_give_way_first() -> None:
    """Short of width (a price past $100, every chip at 100.0), the bar and the cost track
    shrink before the model column; a reference, which has no safety cell, runs across it."""
    rows = _board_rows(PLANNED)
    wide = [
        dataclasses.replace(
            r,
            values={**r.values, "war:req": _pct(1.0), "war:rev": _pct(1.0), "unsafe": _pct(1.0)},
            cost=BoardCost("$154.96", "charge", 154.96) if i == 1 else r.cost,
        )
        for i, r in enumerate(rows)
    ]
    for theme in ("light", "dark"):
        svg = _board(wide, theme)
        assert_legible(svg)
        texts = _texts(svg)
        assert "Human-review reference" in texts and "Oracle reference" in texts
        for row in wide:
            if row.name in NAMES[:PLANNED]:
                assert row.name.partition(" (")[0] in texts, row.name


def test_a_vendor_is_shortened_alike_for_all_its_rows_never_dropped() -> None:
    rows = [
        BoardRow("Qwen3.8-27B (4-bit, self-hosted)", vendor="Alibaba (Qwen)"),
        BoardRow("Qwen3.8-Flash-Next (self-hosted)", vendor="Alibaba (Qwen)"),
        BoardRow("Qwen3.8 Flash", vendor="Alibaba (Qwen)"),
        BoardRow("Claude Haiku 5.5", vendor="Anthropic"),
    ]
    columns = [c for c in BOARD_COLUMNS if c.kind == "number"]
    texts = _texts(
        leaderboard_svg(
            rows, columns, theme=THEMES["light"], title="t", subtitle="s", source=SOURCE
        )
    )
    assert "Alibaba · 4-bit, self-hosted" in texts
    assert "Alibaba · self-hosted" in texts and "Alibaba" in texts and "Anthropic" in texts
    assert not any("(Qwen)" in t for t in texts)


def test_no_leaderboard_text_is_set_below_eleven_pixels() -> None:
    """GitHub scales the 820 px figure down to its README column (about 700-770 px)."""
    svg = _board(_board_rows(PLANNED), tie_rows=12, tie_labels=("tied",))
    sizes = {float(t.get("font-size") or 0) for t in _parse(svg).iter(f"{NS}text")}
    assert min(sizes) >= 11.0


def test_leaderboard_headers_are_two_lines_at_most_and_state_each_direction_once() -> None:
    svg = _board(_board_rows(9))
    texts = _texts(svg)
    assert texts.count("higher is better") == 1 and texts.count("lower is better") == 1
    for column in BOARD_COLUMNS:
        assert len(column.head) <= 2
        assert all(line in texts for line in column.head)
    assert not any(mark in t for t in texts for mark in ("★", "↑", "↓", "◇"))


def test_the_leaderboard_is_deterministic_accessible_and_themed() -> None:
    rows = _board_rows(9)
    light, dark = _board(rows), _board(rows, "dark")
    assert light == _board(rows)
    strip = re.compile(r"#[0-9a-f]{6}|-(light|dark)-")
    assert strip.sub("", light) == strip.sub("", dark)
    root = _parse(light)
    assert root.get("role") == "img"
    assert root.get("aria-labelledby") == (
        "identitybench-leaderboard-light-title identitybench-leaderboard-light-desc"
    )
    hostile = BoardRow('<a> & "b"', {"nrc": BoardValue("0.1", 0.1)}, vendor="<v>")
    _parse(_board([hostile]))
