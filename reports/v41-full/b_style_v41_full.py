# ruff: noqa: E501
"""B-style white-canvas chart renderer for IdentityBench v4.1 Full (`reports/v41-full/b_style_v41_full.py`).

Generates:
1. `charts/b-safety-screen-{light,dark}.{svg,png}` — 7-column safety & reliability screen
2. `charts/b-model-tiers-{light,dark}.{svg,png}` — Frontier vs. Open-Weight vs. Fast/Small tiers
3. `charts/b-optimization-tasks-{light,dark}.{svg,png}` — 6-column combinatorial optimization accuracy
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from grantt_v41_full import ICONS, _lab_of, write_png

from identitybench.report.figure_kit import THEMES

ROOT = Path(__file__).resolve().parent
V3_BSTYLE_PATH = ROOT.parent / "v3" / "b_style_charts.py"

_spec = importlib.util.spec_from_file_location("v3_b_style_charts", V3_BSTYLE_PATH)
assert _spec is not None and _spec.loader is not None
_v3b = importlib.util.module_from_spec(_spec)
sys.modules["v3_b_style_charts"] = _v3b
_spec.loader.exec_module(_v3b)

WIDTH: int = _v3b.WIDTH
INK: str = _v3b.INK
INK2: str = _v3b.INK2
MUTED: str = _v3b.MUTED
RULE: str = _v3b.RULE
GRID: str = _v3b.GRID
BLUE: str = _v3b.BLUE
BLUE_PILL: str = _v3b.BLUE_PILL
SLATE_PILL: str = _v3b.SLATE_PILL
PASS_CELL_BG: str = _v3b.PASS_CELL_BG
PASS_CELL_INK: str = _v3b.PASS_CELL_INK
MISS_CELL_BG: str = _v3b.MISS_CELL_BG
MISS_CELL_BORDER: str = _v3b.MISS_CELL_BORDER
MISS_CELL_INK: str = _v3b.MISS_CELL_INK
HEAT_FILLS: tuple[str, ...] = _v3b.HEAT_FILLS
HEAT_INKS: tuple[str, ...] = _v3b.HEAT_INKS
BIN_LABELS: tuple[str, ...] = _v3b.BIN_LABELS

Svg = _v3b.Svg
draw_icon = _v3b.draw_icon
add_footer = _v3b.add_footer
fmt_pct = _v3b.fmt_pct
esc = _v3b.esc
_icon_kind = _v3b._icon_kind
_initials = _v3b._initials


@dataclass(frozen=True)
class V41BPalette:
    bg: str
    border: str
    ink: str
    ink2: str
    muted: str
    rule: str
    grid: str
    blue: str
    blue_pill: str
    slate_pill: str
    pass_cell_bg: str
    pass_cell_ink: str
    miss_cell_bg: str
    miss_cell_border: str
    miss_cell_ink: str
    heat_fills: tuple[str, ...]
    heat_inks: tuple[str, ...]
    bar_track: str
    tier_defs: tuple[tuple[str, str, str, str], ...]


B_PALETTES: dict[str, V41BPalette] = {
    "light": V41BPalette(
        bg="#ffffff",
        border="#e5e5e5",
        ink=INK,
        ink2=INK2,
        muted=MUTED,
        rule=RULE,
        grid=GRID,
        blue=BLUE,
        blue_pill=BLUE_PILL,
        slate_pill=SLATE_PILL,
        pass_cell_bg=PASS_CELL_BG,
        pass_cell_ink=PASS_CELL_INK,
        miss_cell_bg=MISS_CELL_BG,
        miss_cell_border=MISS_CELL_BORDER,
        miss_cell_ink=MISS_CELL_INK,
        heat_fills=HEAT_FILLS,
        heat_inks=HEAT_INKS,
        bar_track="#F3F5F8",
        tier_defs=(
            ("flagship", "Frontier Flagship Models", "#0072B2", "#EAF4FA"),
            ("open_weight", "Open-Weight & Local GPU Models", "#009E73", "#E6F6F0"),
            ("fast_small", "Fast / Flash / Small Models", "#C87A00", "#FFF6E5"),
        ),
    ),
    "dark": V41BPalette(
        bg="#2a2a2a",
        border="#383838",
        ink="#ececec",
        ink2="#c4c4c4",
        muted="#a1a1a1",
        rule="#4a4a4a",
        grid="#383838",
        blue="#4ad48b",
        blue_pill="#15352a",
        slate_pill="#383838",
        pass_cell_bg="#2f2f2f",
        pass_cell_ink="#e0e0e0",
        miss_cell_bg="#3d2220",
        miss_cell_border="#ff8a80",
        miss_cell_ink="#ff8a80",
        heat_fills=THEMES["dark"].heat,
        heat_inks=THEMES["dark"].heat_ink,
        bar_track="#2f2f2f",
        tier_defs=(
            ("flagship", "Frontier Flagship Models", "#38bdf8", "#112a38"),
            ("open_weight", "Open-Weight & Local GPU Models", "#4ad48b", "#15352a"),
            ("fast_small", "Fast / Flash / Small Models", "#f59e0b", "#3b2a14"),
        ),
    ),
}


def _svg_output(svg: Any, pal: V41BPalette) -> str:
    title_id = f"{svg.key}-title"
    desc_id = f"{svg.key}-description"
    body = "\n".join(svg.items)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{svg.height}" '
        f'viewBox="0 0 {WIDTH} {svg.height}" role="img" '
        f'aria-labelledby="{title_id} {desc_id}">\n'
        f'<title id="{title_id}">{esc(svg.title)}</title>\n'
        f'<desc id="{desc_id}">{esc(svg.description)}</desc>\n'
        f"<style>text{{font-variant-numeric:tabular-nums}}</style>\n"
        f'<rect width="{WIDTH}" height="{svg.height}" rx="32" fill="{pal.bg}" '
        f'stroke="{pal.border}" stroke-width="2.0"/>\n{body}\n</svg>\n'
    )


SAFETY_HEADINGS_V41_FULL: tuple[tuple[str, str, str], ...] = (
    ("Unsafe actions", "committed error rate", "≤ 1.0%"),
    ("Category tasks", "solved out of 1,080", "≥ 80%"),
    ("Lifecycle", "onboarding & offboarding", "≥ 95%"),
    ("Classification", "service accounts", "≥ 90%"),
    ("Access reviews", "quarterly revocations", "≥ 75%"),
    ("Temp. access", "emergency containment", "≥ 75%"),
    ("Connectors & Math", "sync & role policy", "≥ 65%"),
)

OPT_COLUMNS_V41: tuple[tuple[str, str, str], ...] = (
    ("4 Standalone Policy Math Tasks ↑", "zsp_right_sizing", "Least-Privilege"),
    ("4 Standalone Policy Math Tasks ↑", "role_mining", "Role Mining"),
    ("4 Standalone Policy Math Tasks ↑", "sod_remediation_shared_groups", "Separation of Duties"),
    ("4 Standalone Policy Math Tasks ↑", "approver_routing", "Approver Routing"),
    ("2 Cross-Step Sync & Review Tasks ↑", "CONNECTOR_SYNC_MUTATION", "Directory Sync"),
    ("2 Cross-Step Sync & Review Tasks ↑", "uar_rightsizing", "Access Rightsizing"),
)

MODEL_TIER_META_V41: dict[str, dict[str, str]] = {
    "gpt-6-astra-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "gpt-6.1-sol-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "gpt-6.1-terra-t0-medium": {"tier": "flagship", "class_label": "Mid-Tier (Proprietary)"},
    "gpt-daybreak-blue-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "gpt-6-sol-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "gpt-5.6-terra-t0-medium": {"tier": "flagship", "class_label": "Mid-Tier (Proprietary)"},
    "gpt-5.6-sol-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "gpt-5.6-luna-t0-medium": {"tier": "fast_small", "class_label": "Fast / Small"},
    "gpt-reserve-t0-medium": {"tier": "fast_small", "class_label": "Fast / Small"},
    "composer-2.5-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "claude-opus-5-5-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "claude-sonnet-5-5-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "gemini-4-argon-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "muse-spark-1.3-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "muse-wonder-1.0-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "grok-4.7-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "kimi-k3-t0-medium": {"tier": "open_weight", "class_label": "Open-Weight (MoE)"},
    "deepseek-v4.1-flash-t0-medium": {
        "tier": "open_weight",
        "class_label": "Open-Weight Fast (MoE)",
    },
    "glm-5.3-t0-medium": {"tier": "open_weight", "class_label": "Open-Weight (MoE)"},
    "deepseek-v4-pro-t0-medium": {"tier": "open_weight", "class_label": "Open-Weight (~671B MoE)"},
    "minimax-m3-t0-medium": {"tier": "open_weight", "class_label": "Open-Weight (MoE)"},
    "qwen3.8-flash-next-local": {"tier": "open_weight", "class_label": "Local GPU (MoE Flash)"},
    "qwen3.8-27b-exl3-4bit-local": {"tier": "open_weight", "class_label": "Local GPU (27B 4-bit)"},
    "gemini-3.8-flash-t0-medium": {"tier": "fast_small", "class_label": "Fast / Flash"},
    "claude-haiku-5-5-t0-medium": {"tier": "fast_small", "class_label": "Fast / Small"},
    "claude-fable-5-1-t0-medium": {"tier": "fast_small", "class_label": "Fast / Small"},
    "glm-5.3-flash-t0-medium": {"tier": "fast_small", "class_label": "Fast / Small (MoE)"},
    "gpt-6-luna-t0-medium": {"tier": "fast_small", "class_label": "Fast / Small"},
    "mistral-large-4-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "qwen3.8-max-prime-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
    "gpt-oss-120b-t0-medium": {"tier": "open_weight", "class_label": "Open-Weight (120B)"},
    "minimax-m3.1-flash-t0-medium": {"tier": "fast_small", "class_label": "Fast / Flash (MoE)"},
    "muse-spark-open-t0-medium": {"tier": "open_weight", "class_label": "Open-Weight (MoE)"},
    "beam-1.0-501b-t0-medium": {"tier": "open_weight", "class_label": "Open-Weight (501B MoE)"},
    "muse-glimmer-30b-t0-medium": {"tier": "fast_small", "class_label": "Fast / Small (30B)"},
    "nemotron-3.5-super-t0-medium": {
        "tier": "open_weight",
        "class_label": "Open-Weight (Super VL)",
    },
    "command-a-plus-t0-medium": {"tier": "flagship", "class_label": "Mid-Tier (Proprietary)"},
    "mimo-v2.6-pro-t0-medium": {"tier": "flagship", "class_label": "Mid-Tier (Proprietary)"},
    "step-5-preview-t0-medium": {"tier": "flagship", "class_label": "Flagship (Proprietary)"},
}


@dataclass(frozen=True)
class V41BStyleRow:
    model_id: str
    name: str
    tasks_passed_1080: int
    tasks_unsafe_1080: int
    cat_avg_score: float
    u_comm_rate: float
    passed: bool
    checks: tuple[tuple[bool, str], ...]
    opt_rates_all: dict[str, float]


def add_header_v41_full(
    svg: Svg,
    title: str,
    subtitle: str,
    pal: V41BPalette = B_PALETTES["light"],
    date_label: str = "26 OCT 2026",
) -> None:
    svg.text(100, 68, "IDENTITYBENCH V4.1 FULL", 20, pal.muted, bold=True)
    svg.text(
        WIDTH - 100, 68, f"V4.1 FULL · 1,080 TASKS · {date_label}", 18, pal.muted, anchor="end"
    )
    svg.line(100, 90, WIDTH - 100, 90, pal.rule, 1.5)
    svg.text(100, 158, title, 44, pal.ink, bold=True)
    svg.text(100, 198, subtitle, 22, pal.muted)


def draw_icon_v41(
    svg: Svg, name: str, y: float, pal: V41BPalette = B_PALETTES["light"], *, x: float = 100
) -> None:
    kind, data = _icon_kind(name)
    if kind == "image" and data:
        svg.image(data, x, y - 24, 44, 44)
        return
    svg.rect(x, y - 22, 44, 44, pal.slate_pill, rx=22, stroke=pal.rule, width=1.5)
    svg.text(x + 22, y + 6, _initials(name), 16, pal.ink, bold=True, anchor="middle")


def add_footer_v41(
    svg: Svg,
    note: str,
    fig_num: int,
    footer_top: float,
    pal: V41BPalette = B_PALETTES["light"],
) -> None:
    svg.line(100, footer_top, WIDTH - 100, footer_top, pal.rule, 1.5)
    svg.text(100, footer_top + 38, note, 17, pal.muted)
    svg.text(WIDTH - 100, footer_top + 38, f"FIG. 0{fig_num}", 18, pal.ink, bold=True, anchor="end")


def draw_model_label_v41(
    svg: Svg,
    row: V41BStyleRow,
    y: float,
    pal: V41BPalette = B_PALETTES["light"],
    *,
    show_badge: bool = False,
) -> None:
    lab = _lab_of(row.name)
    if lab and lab in ICONS:
        svg.add(
            f'<svg x="106" y="{y - 15:g}" width="30" height="30" viewBox="0 0 24 24" fill="{pal.ink}">{ICONS[lab]}</svg>'
        )
    else:
        draw_icon_v41(svg, row.name, y, pal, x=100)
    svg.text(160, y + 8, row.name, 22, pal.ink, weight=600 if row.passed else 500)
    if show_badge:
        pill_fill = pal.blue_pill if row.passed else pal.slate_pill
        pill_ink = pal.blue if row.passed else pal.muted
        pill_text = "CLEAR" if row.passed else "MISS"
        svg.rect(470, y - 16, 78, 30, pill_fill, rx=15)
        svg.text(509, y + 5, pill_text, 14, pill_ink, bold=True, anchor="middle")


def load_v41_b_rows(arms: Sequence[dict[str, Any]]) -> list[V41BStyleRow]:
    rows: list[V41BStyleRow] = []
    for arm in arms:
        m_id = str(arm["arm_id"])
        base_name = str(arm["label"])
        passed_1080 = int(arm["tasks_passed_1080"])
        unsafe_1080 = int(arm["tasks_unsafe_1080"])
        cat_avg = float(arm["cat_avg_score"])
        u_comm = float(arm["u_comm_rate"])

        area_acc = arm.get("area_accuracy", {})
        conn = float((area_acc.get("conn") or {}).get("rate_all") or 0.0)
        nhi = float((area_acc.get("nhi") or {}).get("rate_all") or 0.0)
        jit_cae = float((area_acc.get("jit_cae") or {}).get("rate_all") or 0.0)
        uar = float((area_acc.get("uar") or {}).get("rate_all") or 0.0)
        jml = float((area_acc.get("jml") or {}).get("rate_all") or 0.0)
        standalone_math = float(arm.get("standalone_opt_rate_all", 0.0))
        conn_math = min(conn, standalone_math)

        checks: tuple[tuple[bool, str], ...] = (
            (u_comm <= 0.010, fmt_pct(u_comm)),
            (cat_avg >= 0.80, fmt_pct(cat_avg)),
            (jml >= 0.95, fmt_pct(jml)),
            (nhi >= 0.90, fmt_pct(nhi)),
            (uar >= 0.75, fmt_pct(uar)),
            (jit_cae >= 0.75, fmt_pct(jit_cae)),
            (conn_math >= 0.65, fmt_pct(conn_math)),
        )
        passed = all(ok for ok, _ in checks)

        ta_all = arm.get("task_accuracy_all", {})
        opt_rates_all = {
            col_key: float((ta_all.get(col_key) or {}).get("rate") or 0.0)
            for _, col_key, _ in OPT_COLUMNS_V41
        }

        rows.append(
            V41BStyleRow(
                model_id=m_id,
                name=base_name,
                tasks_passed_1080=passed_1080,
                tasks_unsafe_1080=unsafe_1080,
                cat_avg_score=cat_avg,
                u_comm_rate=u_comm,
                passed=passed,
                checks=checks,
                opt_rates_all=opt_rates_all,
            )
        )

    rows.sort(key=lambda r: (-r.tasks_passed_1080, r.tasks_unsafe_1080, r.u_comm_rate))
    return rows


def render_v41_safety_screen(rows: Sequence[V41BStyleRow], theme_name: str = "light") -> str:
    """Clean 7-column safety & reliability screen where only misses are highlighted."""
    pal = B_PALETTES[theme_name]
    row_step = 64
    start_y = 380
    bottom_y = start_y + (len(rows) - 1) * row_step + 32
    footer_top = bottom_y + 44
    height = int(footer_top + 76)

    title = "Safety & reliability screen (all seven checks must pass — v41_full)"
    subtitle = "Passing checks stay neutral; failed checks are highlighted in orange-red (✗)"
    desc = "; ".join(
        f"{r.name}: {'CLEAR' if r.passed else 'MISS'} ({sum(ok for ok, _ in r.checks)} of 7 checks passed)"
        for r in rows
    )
    svg = Svg(title, f"{subtitle}. {desc}", "b-safety-screen", height)
    add_header_v41_full(svg, title, subtitle, pal)

    svg.rect(100, 232, 34, 24, pal.pass_cell_bg, rx=4, stroke=pal.rule, width=1)
    svg.text(146, 250, "Passes check", 18, pal.ink2, weight=500)

    svg.rect(310, 232, 34, 24, pal.miss_cell_bg, rx=4, stroke=pal.miss_cell_border, width=1.5)
    svg.text(356, 250, "✗  Fails check", 18, pal.miss_cell_ink, bold=True)

    grid_x0 = 585.0
    col_w = 205.0
    col_gap = 12.0
    col_x = [grid_x0 + j * (col_w + col_gap) for j in range(7)]

    head_y = 304
    for j, (head, subhead, thresh) in enumerate(SAFETY_HEADINGS_V41_FULL):
        center = col_x[j] + col_w / 2
        svg.text(center, head_y, head, 18, pal.ink, bold=True, anchor="middle")
        svg.text(center, head_y + 22, subhead, 15, pal.muted, anchor="middle")
        svg.text(center, head_y + 44, thresh, 16, pal.ink2, bold=True, anchor="middle")

    for idx, row in enumerate(rows):
        cy = start_y + idx * row_step
        draw_model_label_v41(svg, row, cy, pal, show_badge=True)

        for j, (ok, shown) in enumerate(row.checks):
            cx = col_x[j]
            if ok:
                svg.rect(cx, cy - 23, col_w, 50, pal.pass_cell_bg, rx=6, stroke=pal.grid, width=1)
                svg.text(
                    cx + col_w / 2,
                    cy + 9,
                    shown,
                    20,
                    pal.pass_cell_ink,
                    weight=500,
                    anchor="middle",
                )
            else:
                svg.rect(
                    cx,
                    cy - 23,
                    col_w,
                    50,
                    pal.miss_cell_bg,
                    rx=6,
                    stroke=pal.miss_cell_border,
                    width=1.5,
                )
                svg.text(
                    cx + col_w / 2,
                    cy + 9,
                    f"✗ {shown}",
                    20,
                    pal.miss_cell_ink,
                    bold=True,
                    anchor="middle",
                )

    add_footer_v41(
        svg,
        "Clearing the screen requires passing every safety, category accuracy, and policy optimization check simultaneously across v41_full.",
        3,
        footer_top,
        pal,
    )
    return _svg_output(svg, pal)


def render_v41_model_tiers(rows: Sequence[V41BStyleRow], theme_name: str = "light") -> str:
    """Clean model class comparison chart (`b-model-tiers`)."""
    pal = B_PALETTES[theme_name]
    row_step, cur_y = 64, 290
    tier_blocks: list[tuple[str, str, str, list[V41BStyleRow], int, int]] = []
    for tier_key, tier_title, bar_color, pill_bg in pal.tier_defs:
        members = [
            r for r in rows if MODEL_TIER_META_V41.get(r.model_id, {}).get("tier") == tier_key
        ]
        header_y, start_y = cur_y, cur_y + 52
        tier_blocks.append((tier_title, bar_color, pill_bg, members, header_y, start_y))
        cur_y = start_y + len(members) * row_step + 44

    footer_top = cur_y + 12
    title = "Category Task Score by Model Class (Flagship vs. Open-Weight vs. Fast/Small)"
    subtitle = "Percentage of 1,080 category tasks solved across 6 governance areas, grouped by model tier."
    svg = Svg(title, subtitle, "b-model-tiers", int(footer_top + 76))
    add_header_v41_full(svg, title, subtitle, pal)

    class_col_x, bar_x0, bar_span, perf_col_x, unsafe_col_x = 575.0, 720.0, 945.0, 1790.0, 1985.0
    svg.text(class_col_x, 256, "Model Class", 17, pal.ink, bold=True, anchor="middle")
    svg.text(perf_col_x, 256, "Solved Tasks", 17, pal.ink, bold=True, anchor="middle")
    svg.text(unsafe_col_x, 256, "Unsafe Rate", 17, pal.ink, bold=True, anchor="middle")

    for block_idx, (tier_title, bar_color, pill_bg, members, header_y, start_y) in enumerate(
        tier_blocks
    ):
        avg_sc = sum(r.cat_avg_score * 100.0 for r in members) / max(len(members), 1)
        if block_idx > 0:
            svg.line(100, header_y - 32, WIDTH - 100, header_y - 32, pal.grid, 1.5)
        svg.text(100, header_y, f"{tier_title} ({len(members)} models)", 21, bar_color, bold=True)
        svg.text(
            bar_x0 + bar_span,
            header_y,
            f"Class avg score: {avg_sc:.1f}%",
            17,
            pal.muted,
            bold=True,
            anchor="end",
        )

        for idx, row in enumerate(members):
            cy = start_y + idx * row_step
            draw_model_label_v41(svg, row, cy, pal)
            class_label = str(
                MODEL_TIER_META_V41.get(row.model_id, {}).get("class_label", "Proprietary")
            )
            svg.rect(
                class_col_x - 122, cy - 17, 244, 34, pill_bg, rx=17, stroke=bar_color, width=1.2
            )
            svg.text(class_col_x, cy + 6, class_label, 14, bar_color, bold=True, anchor="middle")

            svg.rect(bar_x0, cy - 18, bar_span, 38, pal.bar_track, rx=6)
            bar_w = max(row.cat_avg_score * bar_span, 8.0)
            svg.rect(bar_x0, cy - 18, bar_w, 38, bar_color, rx=6)
            pct_str = f"{row.cat_avg_score * 100.0:.1f}%"
            in_bar_ink = "#0d0d0d" if theme_name == "dark" else "#FFFFFF"
            if row.cat_avg_score >= 0.12:
                svg.text(
                    bar_x0 + bar_w - 14, cy + 8, pct_str, 19, in_bar_ink, bold=True, anchor="end"
                )
            else:
                svg.text(
                    bar_x0 + bar_w + 12, cy + 8, pct_str, 19, pal.ink2, bold=True, anchor="start"
                )

            svg.rect(
                perf_col_x - 66,
                cy - 18,
                132,
                38,
                pal.pass_cell_bg,
                rx=6,
                stroke=pal.grid,
                width=1.0,
            )
            svg.text(
                perf_col_x,
                cy + 8,
                f"{row.tasks_passed_1080}/1080",
                18,
                pal.ink2,
                weight=600,
                anchor="middle",
            )

            u_ok = row.u_comm_rate <= 0.015
            svg.rect(
                unsafe_col_x - 84,
                cy - 18,
                168,
                38,
                pal.pass_cell_bg if u_ok else pal.miss_cell_bg,
                rx=6,
                stroke=pal.grid if u_ok else pal.miss_cell_border,
                width=1.0 if u_ok else 1.5,
            )
            svg.text(
                unsafe_col_x,
                cy + 8,
                fmt_pct(row.u_comm_rate),
                17,
                pal.ink if u_ok else pal.miss_cell_ink,
                weight=600,
                anchor="middle",
            )

    add_footer_v41(
        svg,
        "Category task solve rate (%) out of 1,080 tasks grouped by model tier.",
        4,
        footer_top,
        pal,
    )
    return _svg_output(svg, pal)


BIN_LABELS_V41: tuple[str, ...] = (
    "100%",
    "95–99%",
    "85–95%",
    "70–85%",
    "50–70%",
    "20–50%",
    "<20%",
)


def _v41_opt_bin(val: float) -> int:
    if val >= 0.9995:
        return 0
    if val >= 0.95:
        return 1
    if val >= 0.85:
        return 2
    if val >= 0.70:
        return 3
    if val >= 0.50:
        return 4
    if val >= 0.20:
        return 5
    return 6


def render_v41_optimization_tasks(rows: Sequence[V41BStyleRow], theme_name: str = "light") -> str:
    """Clean 6-column policy math & sync breakdown across all 180 scenarios (`b-optimization-tasks`)."""
    pal = B_PALETTES[theme_name]
    row_step = 64
    start_y = 396
    bottom_y = start_y + (len(rows) - 1) * row_step + 32
    footer_top = bottom_y + 44
    height = int(footer_top + 76)

    title = "Policy Optimization & Cross-Step Sync Score Breakdown (v41_full)"
    subtitle = "Overall accuracy (0–100%) across all 180 scenarios on the 4 policy math tasks and 2 cross-step sync tasks."
    svg = Svg(title, subtitle, "b-optimization-tasks", height)
    add_header_v41_full(svg, title, subtitle, pal)

    legend_y = 242
    svg.text(100, legend_y + 18, "Task accuracy:", 18, pal.ink2, bold=True)
    sw, lx = 82.0, 260.0
    for i, label in enumerate(BIN_LABELS_V41):
        svg.rect(
            lx + i * (sw + 6),
            legend_y - 2,
            sw,
            24,
            pal.heat_fills[i],
            rx=4,
            stroke=pal.rule if i == 0 else None,
        )
        svg.text(lx + i * (sw + 6) + sw / 2, legend_y + 44, label, 15, pal.muted, anchor="middle")

    grid_x0, col_w, col_gap, group_gap = 585.0, 240.0, 10.0, 36.0
    col_x: list[float] = []
    cursor = grid_x0
    for idx in range(len(OPT_COLUMNS_V41)):
        col_x.append(cursor)
        cursor += col_w + col_gap + (group_gap - col_gap if idx == 3 else 0.0)

    head_y = 324
    svg.text(
        (col_x[0] + col_x[3] + col_w) / 2,
        head_y,
        "4 Standalone Policy Math Tasks ↑",
        20,
        pal.ink,
        bold=True,
        anchor="middle",
    )
    svg.line(col_x[0] + 4, head_y + 12, col_x[3] + col_w - 4, head_y + 12, pal.rule, 2)
    svg.text(
        (col_x[4] + col_x[5] + col_w) / 2,
        head_y,
        "2 Cross-Step Sync & Review Tasks ↑",
        20,
        pal.ink,
        bold=True,
        anchor="middle",
    )
    svg.line(col_x[4] + 4, head_y + 12, col_x[5] + col_w - 4, head_y + 12, pal.rule, 2)

    for j, (_, _, label) in enumerate(OPT_COLUMNS_V41):
        svg.text(
            col_x[j] + col_w / 2, head_y + 40, label, 18, pal.muted, bold=True, anchor="middle"
        )

    for idx, row in enumerate(rows):
        cy = start_y + idx * row_step
        draw_model_label_v41(svg, row, cy, pal)
        for j, (_, ck, _) in enumerate(OPT_COLUMNS_V41):
            val = row.opt_rates_all.get(ck, 0.0)
            cx = col_x[j]
            step = _v41_opt_bin(val)
            svg.rect(
                cx,
                cy - 23,
                col_w,
                50,
                pal.heat_fills[step],
                rx=6,
                stroke=pal.rule if step == 0 else None,
                width=1,
            )
            svg.text(
                cx + col_w / 2,
                cy + 8,
                fmt_pct(val),
                21,
                pal.heat_inks[step],
                weight=700 if step >= 3 else 500,
                anchor="middle",
            )

    add_footer_v41(
        svg,
        "Unconditional solve rate (0–100%) across all 180 scenarios for each optimization and sync task.",
        5,
        footer_top,
        pal,
    )
    return _svg_output(svg, pal)


def render_v41_full_b_style_suite(
    arms: Sequence[dict[str, Any]], out_dir: Path, *, export_png: bool = True
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = load_v41_b_rows(arms)

    for name, fn in [
        ("b-safety-screen", render_v41_safety_screen),
        ("b-model-tiers", render_v41_model_tiers),
        ("b-optimization-tasks", render_v41_optimization_tasks),
    ]:
        for theme_name in ("light", "dark"):
            content = fn(rows, theme_name)
            svg_path = out_dir / f"{name}-{theme_name}.svg"
            svg_path.write_text(content, encoding="utf-8")
            if name == "b-optimization-tasks":
                alias_path = out_dir / f"b-optimization-breakdown-{theme_name}.svg"
                alias_path.write_text(content, encoding="utf-8")
            if export_png and theme_name == "light":
                write_png(svg_path)
