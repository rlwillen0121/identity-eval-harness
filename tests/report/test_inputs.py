"""Loading scored runs for a report, and gating judge dimensions on their validation."""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import pytest

from identitybench.judge.validate import GateStatus
from identitybench.report import (
    JudgeSummary,
    ValidationSummary,
    judge_gate,
    load_report_run,
    load_report_runs,
)
from identitybench.report.common import config_refs
from identitybench.report.inputs import ReportRun
from identitybench.runner.manifest import RunManifest
from tests.report.builders import JUDGES, RUBRIC, Fixture, build_fixture, validation_report


def test_a_run_loads_with_its_context(runs: Fixture) -> None:
    run = load_report_run(runs.main)
    assert run.run_id == "run_main"
    assert run.manifest is not None and run.manifest.run_id == "run_main"
    assert run.context_note is None
    assert {"r01", "c01", "j01"} <= set(run.cases)
    assert run.prediction("model_a", "r02") is not None
    assert run.prediction("model_a", "nope") is None
    assert len(run.records_for("model_a")) == len(run.cases)
    assert run.judge is not None and run.judge.rubric_sha256 == RUBRIC
    assert run.is_baseline("oracle") and not run.is_baseline("model_a")


def test_without_context_only_scores_are_read(runs: Fixture) -> None:
    run = load_report_run(runs.main, context=False)
    assert not run.cases and not run.predictions and run.judge is None
    assert run.records


def test_tool_metrics_are_read_from_the_scores_without_context(runs: Fixture) -> None:
    """Tool-use metrics travel in ``scores/summary.json`` (``ConfigScores.tools``), so a run read
    without its cases or predictions, as published results are, still has them."""
    assert runs.tools is not None
    run = load_report_run(runs.tools, context=False)
    assert not run.cases and not run.predictions
    tools = {k: v.tools for k, v in run.scores.configs.items()}
    assert tools["always_escalate"] is None
    scores = tools["model_a_tools"]
    assert scores is not None and scores.n_cases == 12 and scores.n_untraced == 0
    assert scores.requests is not None and scores.requests.retrieval.n_certificate_units == 6


def test_a_missing_dataset_leaves_a_note_instead_of_failing(runs: Fixture, tmp_path: Path) -> None:
    moved = tmp_path / "elsewhere" / "runs" / "run_main"
    shutil.copytree(runs.main, moved)
    run = load_report_run(moved)
    assert run.context_note is not None and "unavailable" in run.context_note
    assert not run.cases
    assert run.records


def test_a_dataset_that_changed_is_not_shown(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    case_file = next(fixture.dataset.glob("requests.test.jsonl"))
    case_file.write_text(case_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    run = load_report_run(fixture.main)
    assert run.context_note is not None and not run.cases


def test_unscored_and_mismatched_runs_are_refused(runs: Fixture, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="score the run first"):
        load_report_run(runs.dev)
    copy = tmp_path / "copy"
    shutil.copytree(runs.main, copy)
    manifest = RunManifest.load(copy / "manifest.json")
    manifest.model_copy(update={"run_id": "other"}).save(copy / "manifest.json")
    with pytest.raises(ValueError, match="scores run run_main"):
        load_report_run(copy)


def test_a_report_needs_distinct_runs(runs: Fixture) -> None:
    with pytest.raises(ValueError, match="at least one"):
        load_report_runs([])
    with pytest.raises(ValueError, match="more than once"):
        load_report_runs([runs.main, runs.main], context=False)


def test_the_validation_report_is_passed_explicitly(runs: Fixture, tmp_path: Path) -> None:
    assert load_report_run(runs.main).validation is None
    path = tmp_path / "report.json"
    path.write_text(json.dumps(validation_report().to_dict()), encoding="utf-8")
    for given in (validation_report(), ValidationSummary.of(validation_report()), path, str(path)):
        run = load_report_run(runs.main, judge_validation=given)
        assert run.validation is not None
        assert run.validation.gate["faithfulness"] is GateStatus.leaderboard


def test_loaded_runs_take_a_validation_report_or_refuse_it(runs: Fixture) -> None:
    loaded = load_report_run(runs.main)
    (gated,) = load_report_runs([loaded], judge_validation=validation_report())
    assert gated.validation == ValidationSummary.of(validation_report())
    assert load_report_runs([gated], judge_validation=validation_report()) == [gated]
    other = validation_report(leaderboard=("faithfulness", "actionability"))
    with pytest.raises(ValueError, match="already carries a different judge validation"):
        load_report_runs([gated], judge_validation=other)
    with pytest.raises(ValueError, match="dataset_dir applies only to run directories"):
        load_report_runs([loaded], dataset_dir=runs.dataset)


def _judge() -> JudgeSummary:
    return JudgeSummary(rubric_sha256=RUBRIC, judges=dict(JUDGES))


def test_the_gate_admits_only_validated_dimensions() -> None:
    gate = judge_gate(_judge(), ValidationSummary.of(validation_report()))
    assert gate.validated
    assert gate.leaderboard == ("faithfulness",)
    assert gate.experimental == ("decisive_coverage", "actionability", "auditability")


@pytest.mark.parametrize(
    ("validation", "reason"),
    [
        (None, "no judge validation report"),
        (ValidationSummary.of(validation_report(rubric="0" * 64)), "rubric"),
        (ValidationSummary.of(validation_report(judges={"judge_x": "family_x"})), "panel"),
        (
            ValidationSummary.of(
                dataclasses.replace(validation_report(), split="dev", n_labels=50)
            ),
            "not the held-out ones",
        ),
    ],
)
def test_the_gate_refuses_a_validation_of_another_instrument(
    validation: ValidationSummary | None, reason: str
) -> None:
    gate = judge_gate(_judge(), validation)
    assert not gate.validated
    assert gate.leaderboard == ()
    assert reason in gate.reason


def test_validation_summaries_read_from_report_objects_and_files(tmp_path: Path) -> None:
    report = validation_report()
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report.to_dict()), encoding="utf-8")
    assert ValidationSummary.read(path) == ValidationSummary.of(report)


def test_configuration_keys_stay_unique_when_slugs_collide(runs: Fixture) -> None:
    base = load_report_run(runs.main, context=False)

    def renamed(run_id: str, config_id: str) -> ReportRun:
        scores = base.scores.model_copy(
            update={
                "run_id": run_id,
                "configs": {config_id: base.scores.configs["model_a"]},
                "config_info": {config_id: base.scores.config_info["model_a"]},
            }
        )
        return dataclasses.replace(base, scores=scores, records=())

    first, second = config_refs([renamed("x", "a_b"), renamed("x_a", "b")])
    assert first.key != second.key
    assert first.key.startswith("x-a-b-") and second.key.startswith("x-a-b-")
    assert {first.name, second.name} == {"a_b", "b"}
