"""The static HTML report (SPEC §15, M7): one self-contained ``report.html``.

It renders the same :class:`~identitybench.report.document.Document` as the Markdown report,
through the Jinja2 template in ``report/templates/``, so the two can't drift apart. Charts are
inlined as SVG; those with a Vega-Lite spec (the family × tier heatmap, calibration and
selective-risk curves, cost per 1,000 decisions, latency) are rendered by Vega-Embed when its
pinned CDN scripts load (each checked against its Subresource Integrity hash,
``vega.INTEGRITY``) and keep the inline SVG otherwise. Nothing else is fetched, so the file
works opened straight from disk.

**Canary.** Reports quote test cases and their gold answers (the failure gallery), so the page
carries the provenance canary (SPEC §17.4) like every dataset file: in an HTML comment at the
top of the file and in a visible footer.

**Escaping.** Autoescaping is on for every string. The only markup passed through is what this
package builds: chart SVG (``report.svg`` escapes every text node and attribute it writes) and
Vega-Lite specs, serialized as JSON with ``<``, ``>``, ``&`` and the JavaScript line separators
written as ``\\u`` escapes, so no value inside a spec (a configuration name, say) can close the
``<script>`` element that holds it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

from identitybench import CANARY
from identitybench.report.document import (
    Block,
    Document,
    Facts,
    Figure,
    Group,
    Table,
    Text,
    Verbatim,
)
from identitybench.report.inputs import RunInput, ValidationInput, load_report_runs
from identitybench.report.sections import DEFAULT_GALLERY_SIZE, build_document
from identitybench.report.vega import INTEGRITY, SCRIPTS

__all__ = ["HTML_FILE", "TEMPLATE", "render_html", "spec_json", "write_html_report"]

HTML_FILE = "report.html"
TEMPLATE = "report.html.j2"
_TEMPLATES = Path(__file__).parent / "templates"

_LABELS = {
    "primary": "primary",
    "exploratory": "exploratory",
    "mixed": "★ primary · rest exploratory",
}

_SCRIPT_SAFE = {
    "<": "\\u003c",
    ">": "\\u003e",
    "&": "\\u0026",
    " ": "\\u2028",
    " ": "\\u2029",
}


def spec_json(figure: Figure) -> Markup:
    """The figure's Vega-Lite spec as JSON that is safe inside a ``<script>`` element."""
    if figure.vega is None:
        raise ValueError(f"figure {figure.key} has no Vega-Lite spec")
    text = json.dumps(
        figure.vega, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    for char, escaped in _SCRIPT_SAFE.items():
        text = text.replace(char, escaped)
    return Markup(text)


def _svg_markup(figure: Figure) -> Markup:
    return Markup(figure.svg)


def _kind(block: Block) -> str:
    if isinstance(block, Text):
        return "text"
    if isinstance(block, Table):
        return "table"
    if isinstance(block, Facts):
        return "facts"
    if isinstance(block, Verbatim):
        return "verbatim"
    if isinstance(block, Figure):
        return "figure"
    assert isinstance(block, Group)
    return "group"


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.globals.update(kind=_kind, svg_markup=_svg_markup, spec_json=spec_json)
    return env


def render_html(document: Document) -> str:
    """The document as one HTML page (see the module docstring)."""
    template = _environment().get_template(TEMPLATE)
    return template.render(
        document=document,
        scripts=[(src, INTEGRITY[src]) for src in SCRIPTS],
        labels=_LABELS,
        canary=CANARY,
    )


def write_html_report(
    runs: Iterable[RunInput],
    out_dir: Path | str,
    *,
    title: str | None = None,
    judge_validation: ValidationInput | None = None,
    dataset_dir: Path | str | None = None,
    gallery_size: int = DEFAULT_GALLERY_SIZE,
) -> Path:
    """Write ``report.html`` for ``runs`` under ``out_dir`` and return its path. Arguments are
    those of ``report.markdown.write_markdown_report``."""
    loaded = load_report_runs(runs, dataset_dir=dataset_dir, judge_validation=judge_validation)
    document = build_document(loaded, title=title, gallery_size=gallery_size)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / HTML_FILE
    path.write_text(render_html(document), encoding="utf-8", newline="\n")
    return path
