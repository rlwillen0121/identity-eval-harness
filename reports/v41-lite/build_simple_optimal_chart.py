# ruff: noqa: E501
"""Clean 5-setup comparison chart showing Setup (+ model breakdown), Skill badge, Tasks, Cost, and Speed."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from b_style_v41 import (  # noqa: E402
    BLUE,
    BLUE_PILL,
    INK,
    MISS_CELL_BG,
    MISS_CELL_BORDER,
    MISS_CELL_INK,
    MUTED,
    PASS_CELL_BG,
    PASS_CELL_INK,
    RULE,
    Svg,
    draw_icon,
    write_png,
)

SUMMARY_JSON = REPO_ROOT / "runs" / "v41-skill-orchestration" / "report_summary.json"
CHARTS_DIR = ROOT / "charts"
ARTIFACT_DIR = Path(
    "/Users/ryanwillen/.gemini/antigravity-cli/brain/a375266b-37e6-4fe1-9eca-0c726983784f"
)


def build_simple_optimal_chart() -> str:
    data = json.loads(SUMMARY_JSON.read_text(encoding="utf-8"))
    cells = {c["cell_id"]: c for c in data["cells"]}

    # (rank, cell_id, setup_title, models_sub, has_skill, is_failed)
    rows = [
        (
            "#1",
            "ultraswarm_claude-opus-5-5_exec__claude-sonnet-5-5_lead__claude-haiku-5-5_swarm__A9_ultra_swarm",
            "3-Layer Ultra Swarm",
            "1 Opus · 1 Sonnet · 10 Haiku",
            True,
            False,
        ),
        (
            "#2",
            "claude-opus-5-5__A3_solo_defined_skill",
            "Solo Agent",
            "1 Opus",
            True,
            False,
        ),
        (
            "#3",
            "team_claude-opus-5-5_boss__claude-sonnet-5-5_worker__A5_tiered_team",
            "Orchestrator + Worker",
            "1 Opus · 1 Sonnet · 1 Reviewer",
            True,
            False,
        ),
        (
            "#4",
            "swarm10_claude-opus-5-5_boss__claude-haiku-5-5_subagents__A8_10_agent_swarm",
            "10-Agent Swarm",
            "1 Opus · 10 Haiku",
            True,
            False,
        ),
        (
            "✗",
            "claude-opus-5-5__A1_solo_no_skill",
            "Solo Agent",
            "1 Opus",
            False,
            True,
        ),
    ]

    w = 1640
    rh = 74
    h = 240 + len(rows) * rh
    svg = Svg(
        "Agent Setup Comparison",
        "Setup, Skill, Tasks Solved, Cost, and Speed",
        "b-optimal-cost-speed",
        h,
    )
    svg.width = w

    svg.text(70, 88, "Agent Setup Comparison", 36, INK, bold=True)

    hy = 120
    svg.rect(70, hy, w - 140, 44, "#F3F5F8", rx=8, stroke="#D8DEE4", width=1)
    svg.text(94, hy + 28, "Setup", 16, INK, bold=True)
    svg.text(505, hy + 28, "Skill", 16, INK, bold=True, anchor="middle")
    svg.text(620, hy + 28, "Tasks Solved", 16, INK, bold=True)
    svg.text(1000, hy + 28, "Cost", 16, "#077A46", bold=True)
    svg.text(1320, hy + 28, "Speed", 16, BLUE, bold=True)

    ry = hy + 56
    max_cost = 45.0
    max_lat = 400.0

    for idx, (rank_lbl, cid, setup_lbl, models_sub, has_skill, is_failed) in enumerate(rows):
        c = cells[cid]
        tp = int(c["tasks_passed"])
        tp_pct = float(c["task_pass_pct"])
        cost_val = float(c["cost_usd"])
        cost_str = f"${cost_val:.2f}"
        lat_s = round(float(c["lat_mean_s"]))

        bg = "#FFFFFF" if idx % 2 == 0 else "#FAFBFD"
        svg.rect(70, ry, w - 140, rh - 8, bg, rx=8, stroke=RULE, width=1)

        if is_failed:
            r_bg, r_fg, r_bd = MISS_CELL_BG, MISS_CELL_INK, MISS_CELL_BORDER
            t_col, c_col, l_col = "#D55E00", "#D55E00", "#B33C00"
        elif tp == 72:
            r_bg, r_fg, r_bd = PASS_CELL_BG, PASS_CELL_INK, "#86EFAC"
            t_col, c_col, l_col = "#077A46", "#077A46", "#0072B2"
        else:
            r_bg, r_fg, r_bd = BLUE_PILL, BLUE, "#B6D8EE"
            t_col, c_col, l_col = "#2563EB", "#0D9488", "#2563EB"

        # Rank pill + Icon + Setup Name + 1-line model breakdown
        svg.rect(86, ry + 17, 46, 32, r_bg, rx=6, stroke=r_bd, width=1.2)
        svg.text(109, ry + 38, rank_lbl, 15, r_fg, bold=True, anchor="middle")
        draw_icon(svg, "claude", ry + 32, x=146)
        svg.text(192, ry + 29, setup_lbl, 17.5, INK, bold=True)
        svg.text(192, ry + 49, models_sub, 13.5, MUTED, weight=600)

        # Skill pill
        if has_skill:
            s_bg, s_fg, s_bd, s_txt = PASS_CELL_BG, PASS_CELL_INK, "#86EFAC", "✓ SKILL.md"
        else:
            s_bg, s_fg, s_bd, s_txt = MISS_CELL_BG, MISS_CELL_INK, MISS_CELL_BORDER, "✗ No Skill"
        svg.rect(440, ry + 18, 130, 30, s_bg, rx=6, stroke=s_bd, width=1.2)
        svg.text(505, ry + 38, s_txt, 14, s_fg, bold=True, anchor="middle")

        # Tasks Solved bar
        tx = 620
        tw = 200
        svg.rect(tx, ry + 22, tw, 22, "#EAEEF2", rx=5)
        svg.rect(tx, ry + 22, max(8, round((tp / 72.0) * tw)), 22, t_col, rx=5)
        svg.text(tx + tw + 12, ry + 39, f"{tp}/72 ({tp_pct:.0f}%)", 16, INK, bold=True)

        # Cost bar
        cx = 1000
        cw = 195
        svg.rect(cx, ry + 22, cw, 22, "#EAEEF2", rx=5)
        svg.rect(
            cx, ry + 22, max(8, round((min(cost_val, max_cost) / max_cost) * cw)), 22, c_col, rx=5
        )
        svg.text(cx + cw + 12, ry + 39, cost_str, 16, INK, bold=True)

        # Speed bar
        lx = 1320
        lw = 185
        svg.rect(lx, ry + 22, lw, 22, "#EAEEF2", rx=5)
        svg.rect(lx, ry + 22, max(8, round((min(lat_s, max_lat) / max_lat) * lw)), 22, l_col, rx=5)
        svg.text(lx + lw + 12, ry + 39, f"{lat_s}s", 16, INK, bold=True)

        ry += rh

    svg.height = ry + 24
    out = svg.output()
    out = out.replace('viewBox="0 0 2200', f'viewBox="0 0 {w}')
    out = out.replace('width="2200"', f'width="{w}"')
    return out


def main() -> None:
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    svg_path = CHARTS_DIR / "b-optimal-cost-speed.svg"
    svg_path.write_text(build_simple_optimal_chart(), encoding="utf-8")
    write_png(svg_path)
    png_path = CHARTS_DIR / "b-optimal-cost-speed.png"
    if ARTIFACT_DIR.exists():
        shutil.copy2(svg_path, ARTIFACT_DIR / svg_path.name)
        shutil.copy2(png_path, ARTIFACT_DIR / png_path.name)
    print(f"Wrote {svg_path} and {png_path}")


if __name__ == "__main__":
    main()
