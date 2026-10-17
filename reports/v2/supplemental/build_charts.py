"""Render descriptive supplemental charts with the published v2 figure style.

Run from the repository root: uv run python reports/v2/supplemental/build_charts.py
Reads only public score artifacts. Does not run inference or alter the original panel.
"""

import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent


def main():
    spec = importlib.util.spec_from_file_location("v2_charts", ROOT.parent / "build_charts.py")
    charts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(charts)
    summary = json.loads((ROOT / "summary.json").read_text())
    thresholds = yaml.safe_load((ROOT.parent / "safety-bar.yaml").read_text())["thresholds"]
    labels = {
        "deepseek-v4-pro-0813": "DeepSeek V4 Pro",
        "gemini-3.8-flash-medium": "Gemini 3.8 Flash",
    }
    rows = []
    for key, config in summary["configs"].items():
        counts = summary["prediction_status_counts"][key]
        assert sum(counts.values()) == 880 and set(counts) <= {"ok", "invalid"}
        name = labels.get(key.split("__")[0], summary["labels"][key])
        rows.append((name, charts.checks(config, thresholds), config))
    rows.sort(key=lambda row: (-charts.grounded_mean(row[2]), row[0]))
    assert len(rows) == 5 and not any(all(ok for ok, _ in row[1]) for row in rows)
    charts.SOURCE = [
        "IdentityBench v2 supplement · Oct 16, 2026 · Synthetic benchmark · Post-hoc panel",
        "Gemini Flash: medium effort · DeepSeek Pro: 0813 · Descriptive point estimates",
    ]
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


if __name__ == "__main__":
    main()
