"""The Markdown report (SPEC §15, M2): ``report.md`` plus its charts as SVG files.

``write_markdown_report`` loads the runs, builds the shared document (``report.sections``) and
renders it here: sections become ``##`` headings with stable ``<a id>`` anchors, groups nest as
deeper headings, tables become pipe tables, figures are written to ``charts/<key>.svg`` and
linked, and untrusted free text goes into fenced code blocks it cannot escape
(``report.markdown_text``). The output depends only on the runs, so the same runs always give
the same bytes. Rewriting a report in the same directory removes the chart files an earlier
report wrote and this one doesn't (a configuration dropped since, say), so ``charts/`` never
holds a stale chart next to ``report.md``; files that aren't report charts are left alone.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from identitybench.report.charts import FIGURE_PREFIXES
from identitybench.report.document import (
    Block,
    Document,
    Facts,
    Figure,
    Group,
    Section,
    Table,
    Text,
    Verbatim,
)
from identitybench.report.inputs import RunInput, ValidationInput, load_report_runs
from identitybench.report.markdown_text import escape_cell, escape_inline, fenced
from identitybench.report.sections import DEFAULT_GALLERY_SIZE, build_document

__all__ = ["CHARTS_DIR", "MARKDOWN_FILE", "render_markdown", "write_markdown_report"]

MARKDOWN_FILE = "report.md"
CHARTS_DIR = "charts"

_LABELS = {
    "primary": "Primary metrics (SPEC §12.8).",
    "exploratory": "Exploratory.",
    "mixed": "Primary metrics are marked ★; everything else is exploratory.",
}


def _table(table: Table) -> list[str]:
    lines: list[str] = []
    if table.caption:
        lines += [escape_inline(table.caption), ""]
    lines.append("| " + " | ".join(escape_cell(c) for c in table.columns) + " |")
    lines.append(
        "|"
        + "|".join("---:" if table.is_numeric(i) else "---" for i in range(len(table.columns)))
        + "|"
    )
    lines += ["| " + " | ".join(escape_cell(c) for c in row) + " |" for row in table.rows]
    return lines


def _block(block: Block, depth: int) -> list[str]:
    if isinstance(block, Text):
        text = escape_inline(block.text)
        if block.tone == "warning":
            return [f"> **Warning:** {text}"]
        if block.tone == "note":
            return [f"> {text}"]
        return [text]
    if isinstance(block, Table):
        return _table(block)
    if isinstance(block, Facts):
        return [
            f"- **{escape_inline(label)}:** {escape_inline(value)}" for label, value in block.items
        ]
    if isinstance(block, Verbatim):
        return [f"**{escape_inline(block.label)}**", "", fenced(block.text)]
    if isinstance(block, Figure):
        return [f"![{escape_inline(block.description)}]({CHARTS_DIR}/{block.key}.svg)"]
    assert isinstance(block, Group)
    heading = (
        f"{'#' * depth} {escape_inline(block.title)}"
        if depth <= 6
        else f"**{escape_inline(block.title)}**"
    )
    return [heading, "", *_blocks(block.blocks, depth + 1)]


def _blocks(blocks: Iterable[Block], depth: int) -> list[str]:
    lines: list[str] = []
    for block in blocks:
        lines += _block(block, depth)
        lines.append("")
    return lines


def _section(section: Section) -> list[str]:
    lines = [f'<a id="{section.key}"></a>', "", f"## {escape_inline(section.title)}", ""]
    if section.label is not None:
        lines += [f"*{_LABELS[section.label]}*", ""]
    return lines + _blocks(section.blocks, 3)


def render_markdown(document: Document) -> str:
    """The document as Markdown, with figures linked as ``charts/<key>.svg``."""
    lines = [f"# {escape_inline(document.title)}", ""]
    lines += _blocks(document.intro, 3)
    lines += ["## Contents", ""]
    lines += [f"- [{escape_inline(s.title)}](#{s.key})" for s in document.sections]
    lines.append("")
    for section in document.sections:
        lines += _section(section)
    # Nested blocks each end with a blank line; one blank line between blocks is enough.
    compact = [line for i, line in enumerate(lines) if line or (i > 0 and lines[i - 1])]
    while compact and not compact[-1]:
        compact.pop()
    return "\n".join(compact) + "\n"


def write_markdown_report(
    runs: Iterable[RunInput],
    out_dir: Path | str,
    *,
    title: str | None = None,
    judge_validation: ValidationInput | None = None,
    dataset_dir: Path | str | None = None,
    gallery_size: int = DEFAULT_GALLERY_SIZE,
) -> Path:
    """Write ``report.md`` and ``charts/*.svg`` for ``runs`` under ``out_dir``; return the
    report's path.

    ``runs`` are scored run directories or loaded ``ReportRun``s. ``judge_validation`` (a
    ``JudgeValidationReport``, its summary, or a path to its ``report.json``) gates the judge's
    dimensions; without it no judge score is shown (SPEC §13.3). It also applies to a loaded
    run that carries no validation of its own (``load_report_runs``). ``dataset_dir`` overrides
    where run datasets are looked for, and is refused alongside a loaded run.
    """
    loaded = load_report_runs(runs, dataset_dir=dataset_dir, judge_validation=judge_validation)
    document = build_document(loaded, title=title, gallery_size=gallery_size)
    out = Path(out_dir)
    charts = out / CHARTS_DIR
    charts.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()
    for figure in document.figures():
        name = f"{figure.key}.svg"
        (charts / name).write_text(figure.svg, encoding="utf-8", newline="\n")
        written.add(name)
    for stale in sorted(charts.glob("*.svg")):
        if stale.name not in written and stale.is_file() and stale.stem.startswith(FIGURE_PREFIXES):
            stale.unlink()
    path = out / MARKDOWN_FILE
    path.write_text(render_markdown(document), encoding="utf-8", newline="\n")
    return path
