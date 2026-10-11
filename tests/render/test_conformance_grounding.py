"""Ideal outputs on every conformance scenario: correct, fully grounded under the real scorer, and
answerable from the rendered case message.

The conformance tables (``tests/conformance``, written from the SPEC text alone) are the widest
set of hand-made views in the repo: together they reach every certificate rule and every JML
event type, including shapes the generated fixture worlds rarely produce (break-glass requests,
orphaned and ownerless service accounts, expiring controls, rehires and conversions). Each one
whose view the oracle accepts becomes a dev case here, and its ``ideal_output``:

- validates against the task's output schema;
- is correct and passes the SPEC §13.1 assertions of ``tests.render.grounding``;
- scores as a perfect unit under ``scoring.units`` / ``scoring.plans`` (the run scorer):
  correct, grounded, rule recall, precision and exhaustive coverage 1, no false claims and no
  unsupported rules;
- comes from a case message that ``parse_view`` turns back into the same view, with the same
  oracle gold.

Only the loader is imported from ``tests/conformance``; the scenarios are read as data.
"""

from __future__ import annotations

import re
from functools import cache

import pytest

from identitybench.policy.access import OracleInputError
from identitybench.policy.oracle import decide
from identitybench.render.view import parse_view, render_view
from identitybench.schemas.case import Case
from identitybench.schemas.enums import Task
from identitybench.schemas.gold import ReviewGold
from identitybench.schemas.json_schemas import OUTPUT_SCHEMAS
from identitybench.schemas.predictions import Prediction, PredictionStatus
from identitybench.scoring.grounding import UnitGrounding
from identitybench.scoring.plans import plan_unit
from identitybench.scoring.units import decision_units
from identitybench.tasks.ideal import ideal_output
from identitybench.world.entities import HrEventType
from tests.conformance.loader import Scenario, load_all
from tests.render.fixtures import make_case
from tests.render.grounding import assert_correct, assert_fully_grounded
from tests.render.schema_check import schema_errors

NEWLY_COVERED = frozenset(
    {
        "LIFE-001",
        "LIFE-003",
        "ELIG-001",
        "ELIG-002",
        "ELIG-004",
        "APPR-002",
        "APPR-003",
        "APPR-004",
        "MIT-002",
        "MIT-003",
        "HYG-002",
        "HYG-003",
        "HYG-005",
        "JML-004",
        "JML-005",
        "JML-006",
    }
)
"""Certificate rules the hand-built and generated fixtures don't reach; this module must."""


@cache
def _cases() -> tuple[Case, ...]:
    """A dev case for every scenario whose view the oracle accepts (the incomplete-view
    scenarios exist to be rejected)."""
    cases: list[Case] = []
    for scenario in load_all():
        try:
            cases.append(_case(scenario))
        except OracleInputError:
            continue
    return tuple(cases)


def _case(scenario: Scenario) -> Case:
    name = re.sub(r"[^a-z0-9]+", "_", scenario.id.lower())
    return make_case(scenario.view, scenario.task, name)


def _ids() -> list[str]:
    return [c.case_id for c in _cases()]


def _assert_perfect(grounding: UnitGrounding, where: str) -> None:
    assert grounding.grounded, where
    assert grounding.false_claims == 0, where
    assert grounding.unsupported_rules == (), where
    assert grounding.rule_recall in (None, 1.0), where
    assert grounding.rule_precision in (None, 1.0), where
    assert grounding.exhaustive_coverage in (None, 1.0), where


@pytest.mark.parametrize("case", _cases(), ids=_ids())
def test_ideal_output_is_perfect_under_the_scorer(case: Case) -> None:
    output = ideal_output(case)
    assert schema_errors(output, OUTPUT_SCHEMAS[case.task]) == []
    assert_correct(case, output)
    assert_fully_grounded(case, output)
    prediction = Prediction.model_validate(
        {
            "run_id": "r",
            "config_id": "c",
            "case_id": case.case_id,
            "task": case.task,
            "status": PredictionStatus.ok,
            "output": output,
        }
    )
    if case.task is Task.jml:
        unit = plan_unit(case, prediction)
        assert unit.correct
        _assert_perfect(unit.grounding, case.case_id)
    else:
        for decision in decision_units(case, prediction):
            assert decision.correct, (case.case_id, decision.item_id)
            _assert_perfect(decision.grounding, f"{case.case_id} {decision.item_id}")


@pytest.mark.parametrize("case", _cases(), ids=_ids())
def test_rendered_case_round_trips_to_the_same_gold(case: Case) -> None:
    parsed = parse_view(render_view(case.view, case.task), case.view.policy_pack)
    assert parsed == case.view
    assert decide(parsed, case.task) == case.gold


def test_scenarios_cover_the_rules_and_events_fixtures_miss() -> None:
    """Guards the coverage the tests above rely on, should the conformance tables shrink."""
    cases = _cases()
    certified: set[str] = set()
    for case in cases:
        gold = case.gold
        records = gold.items if isinstance(gold, ReviewGold) else (gold,)
        certified |= {c.rule for record in records for c in record.certificates}
    assert certified >= NEWLY_COVERED, sorted(NEWLY_COVERED - certified)
    events = {c.view.event.type for c in cases if c.view.event is not None}
    assert events == set(HrEventType)
