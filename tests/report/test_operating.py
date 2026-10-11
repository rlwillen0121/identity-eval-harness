"""SPEC §12.7 operating-point labels: *certified* appears only when the scores say the exact
bound meets the target, and every other case says why it is empirical."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from identitybench.report import build_document, load_report_runs
from identitybench.report.charts import operating_label
from identitybench.report.common import operating_bound, operating_bound_note, operating_status
from identitybench.report.document import Figure, Group, Table
from identitybench.schemas.enums import Task
from identitybench.scoring.operating import OperatingPoint
from tests.report.builders import (
    CERTIFIED_NEGATIVES,
    build_certified_fixture,
    certification,
    operating_point,
)

LABELED: list[tuple[str, OperatingPoint, Task, str, str]] = [
    (
        "meets",
        operating_point(certification(0, 299, 0.00997)),
        Task.access_request,
        "certified: meets WAR ≤ 2.0% (CP95 ≤ 1.0%, 0/299)",
        "operating point: certified (CP95 ≤ 1.0%, 0/299)",
    ),
    (
        "bound above the target",
        operating_point(certification(0, 5, 0.451)),
        Task.access_request,
        "empirical: bound above the 2.0% target (CP95 ≤ 45.1%, 0/5)",
        "operating point: empirical (CP95 ≤ 45.1%, 0/5)",
    ),
    (
        "bound just above the target",
        operating_point(certification(0, 148, 0.02004)),
        Task.access_request,
        "empirical: bound above the 2.0% target (CP95 ≤ 2.004%, 0/148)",
        "operating point: empirical (CP95 ≤ 2.004%, 0/148)",
    ),
    (
        "bound just under the target",
        operating_point(certification(0, 149, 0.01990)),
        Task.access_request,
        "certified: meets WAR ≤ 2.0% (CP95 ≤ 1.99%, 0/149)",
        "operating point: certified (CP95 ≤ 1.99%, 0/149)",
    ),
    (
        "bound at the target",
        operating_point(certification(1, 237, 0.02)),
        Task.access_request,
        "certified: meets WAR ≤ 2.0% (CP95 ≤ 2.0%, 1/237)",
        "operating point: certified (CP95 ≤ 2.0%, 1/237)",
    ),
    (
        # ScoreSettings.war_target is a free float: a target needing two decimals prints with
        # them, so a bound under it never reads as above it (1.25 rounds half-even to 1.2).
        "bound under a two-decimal target",
        operating_point(certification(0, 240, 0.0124, target_war=0.0125), target_war=0.0125),
        Task.access_request,
        "certified: meets WAR ≤ 1.25% (CP95 ≤ 1.24%, 0/240)",
        "operating point: certified (CP95 ≤ 1.24%, 0/240)",
    ),
    (
        "bound over a two-decimal target",
        operating_point(certification(0, 236, 0.0126, target_war=0.0125), target_war=0.0125),
        Task.access_request,
        "empirical: bound above the 1.25% target (CP95 ≤ 1.26%, 0/236)",
        "operating point: empirical (CP95 ≤ 1.26%, 0/236)",
    ),
    (
        "bound just over a two-decimal target",
        operating_point(certification(0, 238, 0.01251, target_war=0.0125), target_war=0.0125),
        Task.access_request,
        "empirical: bound above the 1.25% target (CP95 ≤ 1.251%, 0/238)",
        "operating point: empirical (CP95 ≤ 1.251%, 0/238)",
    ),
    (
        "no gold non-APPROVE case",
        operating_point(certification(0, 0, None)),
        Task.access_request,
        "empirical: no gold non-APPROVE case was scored, so WAR at the threshold has no bound "
        "and nothing can be certified",
        "operating point: empirical (no gold non-APPROVE case, no bound)",
    ),
    (
        "requests without a certification",
        operating_point(None),
        Task.access_request,
        "empirical (not certifiable)",
        "operating point: empirical (not certifiable)",
    ),
    (
        "reviews",
        operating_point(None),
        Task.access_review,
        "empirical (not certifiable: review items are clustered in campaigns)",
        "operating point: empirical (not certifiable: clustered items)",
    ),
]


@pytest.mark.parametrize(
    ("op", "task", "status", "label"),
    [case[1:] for case in LABELED],
    ids=[case[0] for case in LABELED],
)
def test_operating_points_say_whether_they_are_certified(
    op: OperatingPoint, task: Task, status: str, label: str
) -> None:
    assert operating_status(op, task, has_dev=True) == status
    assert operating_label(op, task) == label
    certified = op.certification is not None and op.certification.status == "meets"
    assert status.startswith("certified") is certified
    assert label.startswith("operating point: certified") is certified


def test_without_a_threshold_nothing_is_certified() -> None:
    no_threshold = operating_point(None, threshold=None)
    assert operating_status(no_threshold, Task.access_request, has_dev=True) == (
        "no dev threshold reaches WAR ≤ 2.0%"
    )
    quarter = operating_point(None, threshold=None, target_war=0.0025)
    assert operating_status(quarter, Task.access_request, has_dev=True) == (
        "no dev threshold reaches WAR ≤ 0.25%"
    )
    assert operating_status(None, Task.access_request, has_dev=True) == (
        "no threshold: the dev run has no answers from this configuration on this task"
    )
    assert operating_status(None, Task.access_review, has_dev=False) == (
        "no threshold: no dev run supplied one"
    )


def test_a_certified_operating_point_reads_certified_end_to_end(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A real certification from ``score_run``, not a hand-built record: the table, the chart and
    its Vega-Lite twin all say certified, with the scorer's bound and counts."""
    run_dir, _ = build_certified_fixture(tmp_path_factory.mktemp("certified"))
    (run,) = load_report_runs([run_dir], context=False)
    requests = run.scores.configs["model_c"].requests
    assert requests is not None and requests.calibration is not None
    op = requests.calibration.operating_point
    assert op is not None and op.certification is not None
    assert op.certification.status == "meets"
    assert (op.certification.k, op.certification.n) == (0, CERTIFIED_NEGATIVES)

    document = build_document([run])
    section = next(s for s in document.sections if s.key == "calibration")
    group = next(b for b in section.blocks if isinstance(b, Group) and b.title == "Access requests")
    table = [b for b in group.blocks if isinstance(b, Table)][1]
    status = {row[0]: row[-1] for row in table.rows}
    # 1 − 0.05^(1/150) = 1.977%: one decimal would print 2.0%, beside a 2.0% target.
    expected = f"certified: meets WAR ≤ 2.0% (CP95 ≤ 1.98%, 0/{CERTIFIED_NEGATIVES})"
    assert status["model_c"] == expected

    figure = next(
        b for b in group.blocks if isinstance(b, Figure) and b.key.startswith("selective-")
    )
    texts = [t.text for t in ET.fromstring(figure.svg).iter("{http://www.w3.org/2000/svg}text")]
    label = f"operating point: certified (CP95 ≤ 1.98%, 0/{CERTIFIED_NEGATIVES})"
    assert label in texts
    assert figure.vega is not None
    point = next(layer for layer in figure.vega["layer"] if layer["mark"]["type"] == "point")
    (datum,) = point["data"]["values"]
    assert datum["label"] == label and datum["status"] == expected


def test_a_certifications_bound_note_names_its_estimand() -> None:
    """Only operating points with a bound are described, once per confidence level."""
    bounded = operating_point(certification(0, 5, 0.451))
    unbounded = [operating_point(certification(0, 0, None)), operating_point(None), None]
    assert operating_bound_note(unbounded) is None
    note = operating_bound_note([bounded, *unbounded, bounded])
    assert note is not None and note.count("CP95 bound on WAR at the operating threshold") == 1
    assert note.endswith(
        "failure event: a gold non-APPROVE case predicted APPROVE with confidence ≥ t"
    )


def test_operating_bound_refuses_a_certification_without_one() -> None:
    with pytest.raises(ValueError, match="no bound"):
        operating_bound(certification(0, 0, None), 0.02)
