"""Render descriptive supplemental charts with the published v2 figure style.

Run from the repository root: uv run python reports/v2/supplemental/build_charts.py
Reads only public score artifacts. Does not run inference or alter the original panel.
"""

import argparse
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main(*, export_png: bool = False):
    spec = importlib.util.spec_from_file_location("v2_charts", ROOT.parent / "build_charts.py")
    charts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(charts)
    rows = charts.load_all_rows()
    out = ROOT / "charts"
    out.mkdir(exist_ok=True)
    for theme in charts.THEMES.values():
        figures = {
            "capability": charts.capabilities(rows, theme),
            "safety": charts.heatmap(rows, theme),
            **{
                kind: charts.metric_map(rows, theme, kind)
                for kind in ("scorecard", "lifecycle", "robustness")
            },
            **{task: charts.decisions(rows, theme, task) for task in ("requests", "reviews")},
        }
        for name, svg in figures.items():
            (out / f"{name}-{theme.name}.svg").write_text(svg)

    # Keep dark-mode compatibility files white and render the standalone B-style
    # story set from the same published summary and raw-precision safety checks.
    for light in out.glob("*-light.svg"):
        dark = out / light.name.replace("-light.svg", "-dark.svg")
        if dark.exists():
            dark.write_text(light.read_text())
    b_style_spec = importlib.util.spec_from_file_location(
        "identitybench_v2_b_style", ROOT.parent / "b_style_charts.py"
    )
    assert b_style_spec is not None and b_style_spec.loader is not None
    b_style = importlib.util.module_from_spec(b_style_spec)
    sys.modules[b_style_spec.name] = b_style
    b_style_spec.loader.exec_module(b_style)
    b_style.render_combined_panel(
        ROOT.parent / "summary.json",
        ROOT / "summary.json",
        out,
        export_png=export_png,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--png",
        action="store_true",
        help="also rasterize B-style PNG exports using sips or rsvg-convert",
    )
    main(export_png=parser.parse_args().png)
