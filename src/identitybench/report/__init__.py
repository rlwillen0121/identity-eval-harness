"""Reports for scored runs (SPEC §15): Markdown, static HTML, SVG charts and the README table.

- ``write_markdown_report(runs, out_dir)``: ``report.md`` plus ``charts/*.svg``.
- ``write_html_report(runs, out_dir)``: a self-contained ``report.html`` with Vega-Lite charts
  that fall back to inline SVG.
- ``leaderboard_table(runs, task=..., allow_mixed=False)`` and ``update_readme(path, table)``:
  the README leaderboard between its ``<!-- leaderboard:start -->`` /
  ``<!-- leaderboard:end -->`` markers; runs not measured on the same cases, or scored under a
  different loss matrix, interval level or small-n threshold, are refused
  (``IncomparableRunsError``) unless ``allow_mixed``.
- ``report.svg``: the dependency-free chart builders, usable on their own.

``runs`` are scored run directories (``runs/<run_id>`` with ``scores/summary.json``) or
``ReportRun`` objects (``load_report_run``). Both reports render one shared document
(``report.sections.build_document``), so they always carry the same content.
"""

from identitybench.report.html import write_html_report
from identitybench.report.inputs import (
    JudgeSummary,
    ReportRun,
    ValidationSummary,
    judge_gate,
    load_report_run,
    load_report_runs,
)
from identitybench.report.leaderboard import (
    README_END,
    README_START,
    IncomparableRunsError,
    MarkerError,
    leaderboard_rows,
    leaderboard_table,
    update_readme,
)
from identitybench.report.markdown import write_markdown_report
from identitybench.report.sections import build_document
from identitybench.report.svg import (
    accuracy_heatmap_svg,
    bar_chart_svg,
    calibration_svg,
    nrc_bars_svg,
    selective_risk_svg,
    war_automation_svg,
)

__all__ = [
    "README_END",
    "README_START",
    "IncomparableRunsError",
    "JudgeSummary",
    "MarkerError",
    "ReportRun",
    "ValidationSummary",
    "accuracy_heatmap_svg",
    "bar_chart_svg",
    "build_document",
    "calibration_svg",
    "judge_gate",
    "leaderboard_rows",
    "leaderboard_table",
    "load_report_run",
    "load_report_runs",
    "nrc_bars_svg",
    "selective_risk_svg",
    "update_readme",
    "war_automation_svg",
    "write_html_report",
    "write_markdown_report",
]
