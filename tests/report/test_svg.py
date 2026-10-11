"""SVG charts: well-formed, accessible, self-contained, deterministic, honest about n/a."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import pytest

from identitybench.report import svg
from identitybench.report.charts import operating_label, pareto_frontier
from identitybench.report.common import operating_status
from identitybench.schemas.enums import Task
from tests.report.builders import certification, operating_point

NS = "{http://www.w3.org/2000/svg}"


def _parse(text: str) -> ET.Element:
    root = ET.fromstring(text)
    assert root.tag == f"{NS}svg"
    return root


def _texts(root: ET.Element) -> list[str]:
    return [t.text or "" for t in root.iter(f"{NS}text")]


def _scatter() -> str:
    return svg.war_automation_svg(
        [
            svg.ScatterPoint(
                'model <a> & "b"', 0.1, 0.6, 0.05, 0.2, 0.5, 0.7, frontier=True, war_bound=0.3
            ),
            svg.ScatterPoint("always_deny", 0.0, 0.2, baseline=True, frontier=True),
            svg.ScatterPoint("oracle", 0.0, 0.9, baseline=True, suffix=" (baseline)", ceiling=True),
            svg.ScatterPoint("always_approve", 1.0, 0.3, baseline=True),
        ],
        key="scatter",
        title="WAR vs correct automation",
    )


def _bars() -> str:
    return svg.nrc_bars_svg(
        [
            svg.BarRow("oracle", (0.25,), 0.1, 0.4, baseline=True),
            svg.BarRow("model", (1.4,), 1.1, 1.9),
            svg.BarRow("undefined", (None,)),
        ],
        key="nrc",
        title="NRC",
    )


def _heatmap() -> str:
    return svg.accuracy_heatmap_svg(
        {
            ("sod", "L1"): svg.HeatCell(0.9, 40, items=120),
            ("sod", "L2"): svg.HeatCell(0.25, 4, small_n=True, items=12),
            ("life", "L1"): svg.HeatCell(None, 0),
        },
        ["sod", "life"],
        ["L1", "L2"],
        key="heat",
        title="Accuracy",
        unit="campaigns",
        small_n=30,
    )


_EMPIRICAL = operating_point(certification(0, 5, 0.451))
OPERATING = svg.OperatingMark(
    coverage=0.2,
    war=0.0,
    label=operating_label(_EMPIRICAL, Task.access_request),
    status=operating_status(_EMPIRICAL, Task.access_request, has_dev=True),
    bound=0.451,
)


def _selective() -> str:
    return svg.selective_risk_svg(
        [svg.CurvePoint(0.9, 0.2), svg.CurvePoint(0.5, 0.05), svg.CurvePoint(0.2, 0.0)],
        key="sel",
        title="Selective risk",
        target=0.02,
        operating=OPERATING,
    )


def _calibration() -> str:
    return svg.calibration_svg(
        [svg.ReliabilityBin(0.55, 0.5, 4), svg.ReliabilityBin(0.92, 0.8, 10)],
        key="cal",
        title="Reliability",
    )


CHARTS = {
    "scatter": _scatter,
    "nrc": _bars,
    "heat": _heatmap,
    "sel": _selective,
    "cal": _calibration,
}


@pytest.mark.parametrize("key", sorted(CHARTS))
def test_every_chart_is_well_formed_and_accessible(key: str) -> None:
    text = CHARTS[key]()
    root = _parse(text)
    assert root.get("role") == "img"
    title = root.find(f"{NS}title")
    desc = root.find(f"{NS}desc")
    assert title is not None and title.text
    assert desc is not None and desc.text and len(desc.text) > 20
    assert root.get("aria-labelledby") == f"{title.get('id')} {desc.get('id')}"
    assert title.get("id") == f"{key}-title"


@pytest.mark.parametrize("key", sorted(CHARTS))
def test_every_chart_is_self_contained_and_deterministic(key: str) -> None:
    text = CHARTS[key]()
    assert text == CHARTS[key]()
    without_namespace = text.replace('xmlns="http://www.w3.org/2000/svg"', "")
    for forbidden in ("http", "@import", "url(", "<script", "@font-face"):
        assert forbidden not in without_namespace
    assert float(_parse(text).get("width") or 0) <= svg.WIDTH


def test_names_are_escaped_as_text() -> None:
    root = _parse(_scatter())
    assert any(t.startswith('model <a> & "b"') for t in _texts(root))


def _classed(root: ET.Element, tag: str, cls: str) -> list[ET.Element]:
    return [e for e in root.iter(f"{NS}{tag}") if cls in (e.get("class") or "").split()]


def test_scatter_draws_whiskers_bounds_the_frontier_and_the_ceiling() -> None:
    root = _parse(_scatter())
    assert len(_classed(root, "path", "ibc-s2-line")) == 1
    assert len(_classed(root, "circle", "ibc-frontier")) == 2 + 1  # two points + the legend
    assert len(_classed(root, "path", "ibc-ceiling")) == 2  # the oracle + the legend
    assert len(_classed(root, "line", "ibc-whisker")) == 2
    assert len(_classed(root, "line", "ibc-bound")) == 1 + 1  # the bound + the legend
    texts = _texts(root)
    assert "oracle (baseline) · ceiling" in texts
    assert {"Pareto frontier", "Oracle ceiling", "Exact WAR upper bound"} <= set(texts)
    desc = root.find(f"{NS}desc")
    assert desc is not None
    assert 'Pareto frontier (the oracle ceiling excluded): always_deny, model <a> & "b".' in (
        desc.text or ""
    )
    assert "exact upper bound 30%" in (desc.text or "")


def test_a_frontier_of_one_point_is_ringed_but_not_joined() -> None:
    text = svg.war_automation_svg(
        [svg.ScatterPoint("only", 0.0, 0.5, frontier=True), svg.ScatterPoint("other", 0.5, 0.2)],
        key="one",
        title="t",
    )
    root = _parse(text)
    assert not _classed(root, "path", "ibc-s2-line")
    assert len(_classed(root, "circle", "ibc-frontier")) == 1 + 1  # the point + the legend
    assert "Oracle ceiling" not in _texts(root)


def test_labels_stay_clear_of_the_axis_band() -> None:
    points = [svg.ScatterPoint(f"always_escalate · run_{i}", 0.0, 0.0) for i in range(4)]
    points.append(svg.ScatterPoint("near", 0.0, 0.05))
    root = _parse(svg.war_automation_svg(points, key="axis", title="t"))
    bottom = 420.0 - 52
    small = [t for t in root.iter(f"{NS}text") if "ibc-small" in (t.get("class") or "")]
    ys = [float(t.get("y") or 0) for t in small]
    assert len(ys) == 5 and max(ys) <= bottom - 2
    assert len(set(ys)) == len(ys)


def test_long_names_are_shortened_before_their_markers() -> None:
    name = "a-configuration-with-a-very-long-name-indeed · run_partial"
    root = _parse(
        svg.nrc_bars_svg(
            [svg.BarRow(name, (1.0,), suffix=" (baseline) ‡", baseline=True)], key="k", title="t"
        )
    )
    label = next(t for t in _texts(root) if t.startswith("a-configuration"))
    assert label.endswith("… (baseline) ‡")
    desc = root.find(f"{NS}desc")
    assert desc is not None and f"{name} (baseline) ‡" in (desc.text or "")


def test_undefined_bars_are_drawn_as_text_not_as_zero() -> None:
    root = _parse(_bars())
    bars = [p for p in root.iter(f"{NS}path") if "ibc-line" not in (p.get("class") or "")]
    assert len(bars) == 2
    assert "n/a" in _texts(root)
    assert "status quo = 1" in _texts(root)


def test_bar_chart_series_rules() -> None:
    two = svg.bar_chart_svg(
        [svg.BarRow("a", (1.0, 2.0)), svg.BarRow("b", (None, 0.5))],
        key="lat",
        title="Latency",
        axis_label="s",
        fmt=lambda v: f"{v:.1f}",
        series=("p50", "p95"),
    )
    texts = _texts(_parse(two))
    assert "p50" in texts and "p95" in texts and "n/a" in texts
    with pytest.raises(ValueError, match="at most two series"):
        svg.bar_chart_svg(
            [svg.BarRow("a", (1.0, 2.0, 3.0))],
            key="x",
            title="t",
            axis_label="s",
            fmt=str,
            series=("a", "b", "c"),
        )
    with pytest.raises(ValueError, match="one value per series"):
        svg.bar_chart_svg(
            [svg.BarRow("a", (1.0,))],
            key="x",
            title="t",
            axis_label="s",
            fmt=str,
            series=("a", "b"),
        )


def test_heatmap_widens_to_fit_its_title() -> None:
    title = "Access requests: accuracy by family × tier · model_a · run_main"
    root = _parse(
        svg.accuracy_heatmap_svg(
            {("sod", "L1"): svg.HeatCell(0.5, 2)}, ["sod"], ["L1"], key="h", title=title
        )
    )
    assert title in _texts(root)
    assert float(root.get("width") or 0) <= svg.WIDTH


def test_heatmap_marks_missing_cells_and_small_n() -> None:
    root = _parse(_heatmap())
    texts = _texts(root)
    assert texts.count("n/a") == 2
    assert "n=4†" in texts and "n=40" in texts
    assert "90%" in texts
    assert "n = campaigns per cell · † fewer than 30" in texts
    desc = root.find(f"{NS}desc")
    assert desc is not None
    assert "sod L2: 25% over 4 campaigns, 12 items, small n" in (desc.text or "")
    assert "sod L1: 90% over 40 campaigns, 120 items;" in (desc.text or "")


def test_selective_curve_labels_its_operating_point_certified_or_empirical() -> None:
    root = _parse(_selective())
    texts = _texts(root)
    assert "WAR target 2%" in texts
    assert OPERATING.label == "operating point: empirical (CP95 ≤ 45.1%, 0/5)"
    assert OPERATING.label in texts
    assert len(_classed(root, "line", "ibc-bound")) == 1
    desc = root.find(f"{NS}desc")
    assert desc is not None
    assert OPERATING.status in (desc.text or "")
    assert "exact upper bound 45.1%" in (desc.text or "")


def test_a_two_decimal_target_reads_exactly_on_the_chart() -> None:
    """A 1.25% target is labeled 1.25%, not 1.2%, and the described bound has the status's
    precision, so neither the line nor the description contradicts a certified point."""
    certified = operating_point(certification(0, 240, 0.0124, target_war=0.0125), target_war=0.0125)
    mark = svg.OperatingMark(
        coverage=0.2,
        war=0.0,
        label=operating_label(certified, Task.access_request),
        status=operating_status(certified, Task.access_request, has_dev=True),
        bound=0.0124,
    )
    root = _parse(
        svg.selective_risk_svg(
            [svg.CurvePoint(0.9, 0.2), svg.CurvePoint(0.2, 0.0)],
            key="sel-exact",
            title="Selective risk",
            target=0.0125,
            operating=mark,
        )
    )
    assert "WAR target 1.25%" in _texts(root)
    desc = root.find(f"{NS}desc")
    assert desc is not None
    text = desc.text or ""
    assert "; WAR target 1.25%;" in text
    assert "exact upper bound 1.24%, certified: meets WAR ≤ 1.25% (CP95 ≤ 1.24%, 0/240)" in text
    assert svg.war_target_label(0.02) == "WAR target 2%"


def test_dense_curves_drop_their_markers() -> None:
    points = [svg.CurvePoint(i / 40, i / 400) for i in range(40)]
    root = _parse(svg.selective_risk_svg(points, key="dense", title="t"))
    assert not list(root.iter(f"{NS}circle"))


def test_calibration_has_the_diagonal() -> None:
    assert "perfect calibration" in _texts(_parse(_calibration()))


def test_charts_inlined_together_never_share_ids() -> None:
    ids = [
        m
        for text in (_scatter(), _bars(), _heatmap(), _selective(), _calibration())
        for m in re.findall(r'id="([^"]+)"', text)
    ]
    assert len(ids) == len(set(ids))


def test_pareto_frontier() -> None:
    points = {
        "best_war": (0.0, 0.4),
        "best_automation": (0.2, 0.9),
        "dominated": (0.3, 0.5),
        "tie_a": (0.1, 0.6),
        "tie_b": (0.1, 0.6),
        "weakly_dominated": (0.1, 0.5),
    }
    assert pareto_frontier(points) == {"best_war", "best_automation", "tie_a", "tie_b"}
    assert pareto_frontier({"only": (0.5, 0.5)}) == {"only"}
    assert pareto_frontier({}) == set()
