"""The README figures' renderers: the hero dot plot, the cost against grounded accuracy chart and
the scorecard heatmap. Each is checked for what it shows (every value as the tables print it,
rows in the order given, marks where the values put them) and for layout at leaderboard scale:
with the 26 models the v1 leaderboard plans (their real display names and vendors), long names
and clustered values, no two labels overlap, no label covers a mark and nothing leaves the
canvas."""

from __future__ import annotations

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
from identitybench.report.figure_kit import THEMES, Box, text_boxes, text_width, wrap
from identitybench.report.hero import HeroRow, hero_svg
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
