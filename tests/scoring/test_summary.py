"""Integration: the oracle scores perfectly, baselines behave as SPEC §14 says, outputs are
deterministic, and score_run works end to end on a run directory (comparisons between
configurations are in ``test_compare``)."""

from __future__ import annotations

import json
import os
import random
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest

from identitybench.providers.baselines import make_baseline
from identitybench.runner.dataset import (
    stratified_sample,
    suite_file,
    write_jsonl,
    write_sha256sums,
)
from identitybench.runner.manifest import Counts, DatasetMismatchError
from identitybench.runner.parse import parse_response
from identitybench.schemas.case import Case
from identitybench.schemas.enums import Effect, Label, RequestDecision, Split, Suite, Task
from identitybench.schemas.gold import Certificate, Claim, Finding, RequestGold
from identitybench.schemas.predictions import Prediction, PredictionStatus, ToolTrace
from identitybench.scoring.decisions import DecisionScores
from identitybench.scoring.loss import LossMatrix
from identitybench.scoring.metrics import Metric
from identitybench.scoring.settings import ScoreSettings
from identitybench.scoring.summary import (
    COMPARISON_METHOD,
    PER_CASE_FILE,
    SCORES_DIR,
    SUMMARY_FILE,
    CaseRecord,
    ConfigScores,
    IncompleteRunError,
    RunScores,
    load_run,
    read_predictions,
    run_completeness,
    score_cases,
    score_run,
)
from identitybench.scoring.units import longer
from tests.scoring.builders import (
    baseline_predictions,
    conformance_cases,
    entitlement,
    make_run,
    oracle_output,
    oracle_predictions,
    predict,
    request_case,
    request_output,
    request_view,
    review_case,
    review_output,
    rewrite_manifest,
)

SETTINGS = ScoreSettings(n_boot=200)


def _decision_cases() -> list[Case]:
    return [c for c in conformance_cases() if c.task is not Task.jml]


def _task(scores: DecisionScores | None) -> DecisionScores:
    assert scores is not None
    return scores


def _point(metric: Metric | None) -> float | None:
    """The point of a metric the task reports (the duration rates exist for requests only)."""
    assert metric is not None
    return metric.point


# --------------------------------------------------------------------------------------------
# Oracle and baselines (SPEC §14)
# --------------------------------------------------------------------------------------------


def _assert_perfect(scores: ConfigScores) -> None:
    """Every SPEC §12 and §13.1 number at its best (SPEC §14: the oracle scores perfectly)."""
    assert scores.n_no_prediction == 0
    for decisions in (_task(scores.requests), _task(scores.reviews)):
        m, g = decisions.metrics, decisions.grounding
        for perfect in (m.accuracy, m.label_accuracy, m.automatable_coverage):
            assert perfect.point == 1.0
        for zero in (m.war, m.wdr, m.under_escalation, m.over_escalation, m.invalid_rate):
            assert zero.point == 0.0
        assert m.unsafe_approval_share.point in (0.0, None)
        for perfect in (
            g.rule_recall,
            g.rule_precision,
            g.citation_valid_accuracy,
            g.grounded_accuracy,
            g.exhaustive_coverage,
        ):
            assert perfect.point == 1.0
        assert g.exhaustive_coverage_multi.point in (1.0, None)  # n/a without such units
        assert g.right_for_wrong_reason.point == 0.0
        assert g.false_claim_rate.point == 0.0
        assert g.default_outcome_grounding.point == 1.0
        # The oracle escalates exactly the gold-ESCALATE cases, each costing one routing.
        gold_e = sum(decisions.confusion[Label.E].values())
        assert decisions.confusion[Label.E][Label.E] == gold_e
        assert decisions.metrics.correct_automation.point == pytest.approx(
            1 - gold_e / decisions.n_units
        )
        assert decisions.cost.nrc.point == pytest.approx(gold_e / decisions.n_units)
    requests = _task(scores.requests)
    assert _point(requests.metrics.over_duration_rate) == 0.0
    assert _point(requests.metrics.over_restriction_rate) == 0.0
    assert scores.jml is not None
    assert scores.jml.unsafe_plan_rate.point == 0.0
    assert scores.jml.exact_timeline_match.point == 1.0
    assert scores.jml.grounding.grounded_accuracy.point == 1.0
    assert scores.jml.grounding.rule_precision.point == 1.0


def test_oracle_scores_perfectly() -> None:
    cases = list(conformance_cases())
    scores = score_cases(cases, oracle_predictions(cases), settings=SETTINGS)
    _assert_perfect(scores)
    assert _task(scores.requests).grounding.exhaustive_coverage_multi.point == 1.0


def _shipped_baseline_predictions(cases: Sequence[Case], name: str) -> list[Prediction]:
    """What the runner records for a shipped baseline: ``providers.baselines`` answers, and
    ``runner.parse`` decides validity, exactly as for a model."""
    baseline = make_baseline(name)
    predictions: list[Prediction] = []
    for case in cases:
        if not baseline.supports(case.task):
            continue
        result = parse_response(case, baseline.respond(case))
        predictions.append(
            Prediction(
                run_id="run_1",
                config_id=name,
                case_id=case.case_id,
                task=case.task,
                status=result.status,
                output=result.output,
                invalid_reason=result.invalid_reason,
                item_errors=dict(result.item_errors),
            )
        )
    return predictions


def test_the_shipped_oracle_scores_perfectly_through_the_parser() -> None:
    """The integration SPEC §14 asks for: ``tasks.ideal`` via the oracle baseline and the
    parser, not the test's own oracle output, so a change to either can't break it unseen."""
    cases = list(conformance_cases())
    predictions = _shipped_baseline_predictions(cases, "oracle")
    assert len(predictions) == len(cases)
    assert {p.status for p in predictions} == {PredictionStatus.ok}
    _assert_perfect(score_cases(cases, predictions, settings=SETTINGS))


def test_the_shipped_always_escalate_has_nrc_one_under_every_setting() -> None:
    cases = list(conformance_cases())
    predictions = _shipped_baseline_predictions(cases, "always_escalate")
    assert {p.task for p in predictions} == {Task.access_request, Task.access_review}
    scores = score_cases(_decision_cases(), predictions, settings=SETTINGS)
    for decisions in (_task(scores.requests), _task(scores.reviews)):
        assert decisions.cost.nrc.point == 1.0
        assert {point.nrc for point in decisions.cost.sensitivity} == {1.0}


def test_always_escalate_nrc_is_exactly_one() -> None:
    cases = _decision_cases()
    scores = score_cases(cases, baseline_predictions(cases, "always_escalate"), settings=SETTINGS)
    for decisions in (_task(scores.requests), _task(scores.reviews)):
        assert decisions.cost.nrc.point == 1.0
        assert decisions.cost.mean_cost.point == 1.0
        for point in decisions.cost.sensitivity:
            assert point.nrc == 1.0, point.setting
            assert point.mean_cost == point.routing
        m = decisions.metrics
        assert m.correct_automation.point == 0.0 and m.war.point == 0.0
        assert m.over_escalation.point == 1.0 and m.under_escalation.point == 0.0
    requests = _task(scores.requests)
    assert {p.mix for p in requests.cost.sensitivity} == {"dataset", "production"}


def test_always_escalate_nrc_is_one_under_a_custom_matrix() -> None:
    cases = _decision_cases()
    settings = ScoreSettings(n_boot=100, loss=LossMatrix(routing=1.7, invalid_overhead=2.0))
    scores = score_cases(cases, baseline_predictions(cases, "always_escalate"), settings=settings)
    assert _task(scores.requests).cost.nrc.point == 1.0
    assert _task(scores.requests).cost.always_escalate_mean_cost == pytest.approx(1.7)


def test_always_approve_properties() -> None:
    cases = _decision_cases()
    scores = score_cases(cases, baseline_predictions(cases, "always_approve"), settings=SETTINGS)
    golds: list[RequestGold] = []
    requested: list[int | None] = []
    for case in cases:
        if isinstance(case.gold, RequestGold) and case.view.request is not None:
            golds.append(case.gold)
            requested.append(case.view.request.requested_duration_days)
    approve = [g.decision is RequestDecision.APPROVE for g in golds]
    durations = [g.effective_duration_days for g in golds]
    n = len(golds)
    assert n == sum(c.task is Task.access_request for c in cases)
    exact = sum(a and r == d for a, r, d in zip(approve, requested, durations, strict=True))
    too_long = sum(
        a and longer(r, d) for a, r, d in zip(approve, requested, durations, strict=True)
    )
    m = _task(scores.requests).metrics
    assert m.war.point == 1.0 and m.high_risk_war.point == 1.0
    assert m.wdr.point == 0.0 and m.over_escalation.point == 0.0
    assert m.under_escalation.point == 1.0
    assert m.correct_automation.point == pytest.approx(exact / n)
    assert _point(m.over_duration_rate) == pytest.approx(too_long / sum(approve))
    assert _point(m.over_restriction_rate) == 0.0  # the requested duration is never too short
    assert m.unsafe_approval_share.point == pytest.approx((n - sum(approve) + too_long) / n)
    reviews = _task(scores.reviews)
    assert reviews.metrics.war.point == 1.0
    assert reviews.review is not None
    assert reviews.review.must_revoke_recall.point == 0.0
    assert reviews.review.certification_rate.point == 1.0


def test_always_deny_properties() -> None:
    cases = _decision_cases()
    scores = score_cases(cases, baseline_predictions(cases, "always_deny"), settings=SETTINGS)
    for decisions in (_task(scores.requests), _task(scores.reviews)):
        m = decisions.metrics
        assert m.war.point == 0.0 and m.unsafe_approval_share.point is None
        assert m.wdr.point == 1.0 and m.over_escalation.point == 0.0
        gold_n = sum(decisions.confusion[Label.N].values())
        assert m.correct_automation.point == pytest.approx(gold_n / decisions.n_units)
    reviews = _task(scores.reviews)
    assert reviews.review is not None and reviews.review.must_revoke_recall.point == 1.0


# --------------------------------------------------------------------------------------------
# Determinism and input validation
# --------------------------------------------------------------------------------------------


def _mixed_predictions(cases: Sequence[Case]) -> list[Prediction]:
    """A config that is right on some cases and wrong on others: the oracle, with every third
    request approved outright and every seventh other case INVALID."""
    predictions: list[Prediction] = []
    for i, case in enumerate(cases):
        if case.task is Task.access_request and i % 3 == 0:
            predictions.append(predict(case, request_output("APPROVE"), config_id="mixed"))
        elif i % 7 == 0:
            predictions.append(predict(case, status="invalid", config_id="mixed"))
        else:
            predictions.append(predict(case, oracle_output(case), config_id="mixed"))
    return predictions


def _variant(base: Case, variant: str) -> Case:
    """A copy of ``base`` as one of its variants: same view, gold and family."""
    suffix = variant.replace(":", "-")
    return base.model_copy(
        update={"case_id": f"{base.case_id}--{suffix}", "variant": variant, "twin_of": base.case_id}
    )


def test_scores_do_not_depend_on_prediction_order() -> None:
    """Shuffled predictions and cases give identical scores, robustness pairs and repeat
    disagreement included."""
    cases = list(conformance_cases())
    request = next(c for c in cases if c.task is Task.access_request)
    campaign = next(c for c in cases if c.task is Task.access_review)
    event = next(c for c in cases if c.task is Task.jml)
    variants = [
        _variant(request, "adversarial:urgency"),
        _variant(request, "invariance:name"),
        _variant(campaign, "adversarial:reviewer_note"),
    ]
    assert campaign.view.campaign is not None
    approve = request_output("APPROVE")
    certify_all = review_output((item.item_id, "CERTIFY") for item in campaign.view.campaign.items)
    repeats = [
        predict(request, approve, config_id="mixed", sample_idx=1),
        predict(request, oracle_output(request), config_id="mixed", sample_idx=2),
        predict(campaign, certify_all, config_id="mixed", sample_idx=1),
        predict(event, status="invalid", config_id="mixed", sample_idx=1),
    ]
    variant_predictions = [
        predict(variants[0], approve, config_id="mixed"),
        predict(variants[1], oracle_output(variants[1]), config_id="mixed"),
        predict(variants[2], certify_all, config_id="mixed"),
    ]
    all_cases = [*cases, *variants]
    predictions = [*_mixed_predictions(cases), *variant_predictions, *repeats]
    shuffled = list(predictions)
    random.Random(7).shuffle(shuffled)
    first = score_cases(all_cases, predictions, settings=SETTINGS)
    second = score_cases(list(reversed(all_cases)), shuffled, settings=SETTINGS)
    assert first.model_dump_json() == second.model_dump_json()
    assert _task(first.requests).metrics.war.point not in (0.0, None)
    assert first.robustness is not None
    assert first.robustness.requests is not None and first.robustness.reviews is not None
    assert first.robustness.requests.n_adversarial_pairs == 1
    assert first.robustness.requests.n_invariance_pairs == 1
    assert first.repeats is not None and first.repeats.n_repeated_cases == 3


def test_predictions_must_belong_to_the_cases() -> None:
    a, b = request_case("a", "DENY"), request_case("b", "DENY")
    with pytest.raises(ValueError, match="unknown case"):
        score_cases([a], [predict(b, request_output("DENY"))], settings=SETTINGS)
    with pytest.raises(ValueError, match="duplicate case"):
        score_cases([a, a], [], settings=SETTINGS, config_id="cfg")
    empty = score_cases([a, b], [], settings=SETTINGS, config_id="cfg")
    assert empty.n_no_prediction == 2
    assert _task(empty.requests).metrics.invalid_rate.point == 1.0


# --------------------------------------------------------------------------------------------
# Run directories
# --------------------------------------------------------------------------------------------


def _run_predictions(cases: Sequence[Case]) -> list[Prediction]:
    decisions = [c for c in cases if c.task is not Task.jml]
    return [
        *oracle_predictions(cases),
        *baseline_predictions(decisions, "always_escalate"),
        *baseline_predictions(decisions, "always_deny"),
    ]


def test_score_run_writes_deterministic_outputs(tmp_path: Path) -> None:
    cases = list(conformance_cases())
    predictions = _run_predictions(cases)
    shuffled = list(predictions)
    random.Random(3).shuffle(shuffled)
    first = make_run(
        tmp_path / "a", cases, predictions, extra_configs=["never_ran"], models=["oracle"]
    )
    second = make_run(
        tmp_path / "b", cases, shuffled, extra_configs=["never_ran"], models=["oracle"]
    )
    result = score_run(first, n_boot=100)
    score_run(second, n_boot=100)
    for name in (SUMMARY_FILE, PER_CASE_FILE):
        a = (first / SCORES_DIR / name).read_bytes()
        assert a == (second / SCORES_DIR / name).read_bytes(), name
    summary_text = (first / SCORES_DIR / SUMMARY_FILE).read_text(encoding="utf-8")
    assert RunScores.model_validate_json(summary_text) == result
    assert json.loads(summary_text)["run_id"] == "run_1"
    assert result.comparisons  # the oracle, as a model, against always_escalate
    assert sorted(result.configs) == ["always_deny", "always_escalate", "oracle"]
    assert result.unscored_configs == ["never_ran"]
    assert result.completeness.complete and result.completeness.n_expected_cases == len(cases)
    assert set(result.completeness.missing_predictions.values()) == {0}
    assert result.dev_run_id is None and result.dev_completeness is None
    assert result.config_info["oracle"].temperature == 0.0
    # Baselines without JML predictions aren't scored on JML at all.
    assert result.configs["always_deny"].jml is None
    assert result.configs["oracle"].jml is not None
    records = [
        CaseRecord.model_validate_json(line)
        for line in (first / SCORES_DIR / PER_CASE_FILE).read_text(encoding="utf-8").splitlines()
    ]
    n_decision = sum(c.task is not Task.jml for c in cases)
    assert len(records) == len(cases) + 2 * n_decision
    keys = [(r.config_id, r.case_id) for r in records]
    assert keys == sorted(keys)


def test_score_run_ranks_and_compares(tmp_path: Path) -> None:
    """Baselines are ranked like models, but only models are compared: here the one model
    (the oracle, run as a model) against always_escalate, the status quo."""
    cases = list(conformance_cases())
    run = make_run(tmp_path, cases, _run_predictions(cases), models=["oracle"])
    result = score_run(run, n_boot=100)
    ranking = result.rankings[Task.access_request]
    assert ranking.ranks["oracle"] == 1
    assert ranking.nrc["always_escalate"] == 1.0
    pareto = result.pareto[Task.access_request]
    assert pareto["oracle"].on_frontier is True
    assert pareto["always_escalate"].on_frontier is False  # oracle beats it on both
    comparisons = {(c.config_a, c.config_b, c.task, c.metric): c for c in result.comparisons}
    assert {(a, b) for a, b, _, _ in comparisons} == {("oracle", "always_escalate")}
    nrc = comparisons[("oracle", "always_escalate", Task.access_request, "nrc")]
    assert nrc.diff is not None and nrc.diff < 0 and nrc.point_b == 1.0
    assert nrc.units == "base request case" and nrc.cluster is None
    assert nrc.method == COMPARISON_METHOD
    review = comparisons[("oracle", "always_escalate", Task.access_review, "war")]
    assert review.cluster == "campaign" and review.units == "review item"
    # always_escalate makes no plans, so JML has no comparison to make.
    assert {task for _, _, task, _ in comparisons} == {Task.access_request, Task.access_review}
    for c in result.comparisons:
        if c.p_value is not None:
            assert c.adjusted_p is not None and c.adjusted_p >= c.p_value
            assert c.reject == (c.adjusted_p <= c.alpha)


def test_score_run_uses_the_declared_loss_matrix(tmp_path: Path) -> None:
    cases = _decision_cases()
    predictions = baseline_predictions(cases, "always_escalate")
    run = make_run(tmp_path, cases, predictions, loss_matrix={"routing": 2, "over_duration": 4})
    result = score_run(run, n_boot=50)
    assert result.settings.loss == LossMatrix(routing=2.0, over_duration=4.0)
    requests = result.configs["always_escalate"].requests
    assert requests is not None
    assert requests.cost.mean_cost.point == 2.0 and requests.cost.nrc.point == 1.0


def test_score_run_refuses_mismatched_inputs(tmp_path: Path) -> None:
    cases = _decision_cases()[:5]
    predictions = baseline_predictions(cases, "always_deny")
    run = make_run(tmp_path, cases, predictions)
    other = tmp_path / "other"
    write_jsonl(suite_file(other, cases[0].suite, Split.test), cases[:1])
    with pytest.raises(DatasetMismatchError):
        score_run(run, other, n_boot=50)
    foreign = make_run(
        tmp_path / "foreign", cases, [p.model_copy(update={"run_id": "x"}) for p in predictions]
    )
    with pytest.raises(ValueError, match="other runs"):
        score_run(foreign, n_boot=50)
    # Moved away from the repo root that holds the dataset, the manifest's relative path breaks.
    moved = tmp_path / "elsewhere" / "deeper" / "run_1"
    moved.parent.mkdir(parents=True)
    run.rename(moved)
    with pytest.raises(FileNotFoundError, match="pass dataset_dir"):
        score_run(moved, n_boot=50)
    assert score_run(moved, tmp_path / "datasets" / "toy-v1", n_boot=50).run_id == "run_1"


def test_score_run_with_a_dev_run_certifies_an_operating_point(tmp_path: Path) -> None:
    def confident(cases: Sequence[Case], run_id: str) -> list[Prediction]:
        output = request_output("DENY", confidence=0.9)
        return [predict(c, output, config_id="model", run_id=run_id) for c in cases]

    test_cases = [request_case(f"t{i}", "DENY") for i in range(4)]
    dev_cases = [request_case(f"d{i}", "DENY", split=Split.dev) for i in range(4)]
    test_run = make_run(
        tmp_path / "test", test_cases, confident(test_cases, "run_1"), models=["model"]
    )
    dev_run = make_run(
        tmp_path / "dev",
        dev_cases,
        confident(dev_cases, "dev_1"),
        run_id="dev_1",
        split=Split.dev,
        models=["model"],
    )
    result = score_run(test_run, dev_run_dir=dev_run, n_boot=50)
    assert result.dev_run_id == "dev_1"
    assert result.dev_completeness is not None and result.dev_completeness.complete
    requests = result.configs["model"].requests
    assert requests is not None and requests.calibration is not None
    point = requests.calibration.operating_point
    assert point is not None and point.threshold == 0.9
    assert point.certification is not None and point.certification.n == 4
    with pytest.raises(ValueError, match="not a dev run"):
        score_run(test_run, dev_run_dir=test_run, n_boot=50)
    # An unfinished dev run is refused like an unfinished run, naming the dev run's own
    # setting: a complete run can be scored with a partial dev run.
    rewrite_manifest(dev_run, finished_at=None)
    with pytest.raises(IncompleteRunError, match="run dev_1 is incomplete") as refused:
        score_run(test_run, dev_run_dir=dev_run, n_boot=50)
    assert "pass allow_incomplete_dev=True to score it" in str(refused.value)
    allowed = score_run(test_run, dev_run_dir=dev_run, n_boot=50, allow_incomplete=True)
    assert allowed.completeness.complete
    assert allowed.dev_completeness is not None and not allowed.dev_completeness.finished
    # The two runs can be accepted incomplete separately. Here allow_incomplete=True is already
    # passed, so the hint must name the setting that refused the run.
    with pytest.raises(IncompleteRunError, match="run dev_1 is incomplete") as refused:
        score_run(
            test_run,
            dev_run_dir=dev_run,
            n_boot=50,
            allow_incomplete=True,
            allow_incomplete_dev=False,
        )
    assert "allow_incomplete_dev=True" in str(refused.value)
    assert "allow_incomplete=True" not in str(refused.value)
    dev_only = score_run(test_run, dev_run_dir=dev_run, n_boot=50, allow_incomplete_dev=True)
    assert dev_only.dev_completeness is not None and not dev_only.dev_completeness.complete
    rewrite_manifest(dev_run, finished_at=datetime(2026, 10, 10, 13, 0))
    rewrite_manifest(test_run, finished_at=None)
    with pytest.raises(IncompleteRunError, match="run run_1 is incomplete") as refused:
        score_run(test_run, dev_run_dir=dev_run, n_boot=50, allow_incomplete_dev=True)
    assert "pass allow_incomplete=True to score it" in str(refused.value)
    run_only = score_run(
        test_run,
        dev_run_dir=dev_run,
        n_boot=50,
        allow_incomplete=True,
        allow_incomplete_dev=False,
    )
    assert not run_only.completeness.complete
    assert run_only.dev_completeness is not None and run_only.dev_completeness.complete


# --------------------------------------------------------------------------------------------
# Incomplete runs, the case selection and the dataset
# --------------------------------------------------------------------------------------------


def test_an_incomplete_run_is_refused_unless_allowed(tmp_path: Path) -> None:
    """A budget stop leaves the tail of the schedule (here every JML event) unanswered by every
    configuration; scoring only what was answered would quietly drop a whole suite."""
    cases = list(conformance_cases())
    decisions = [c for c in cases if c.task is not Task.jml]
    events = [c for c in cases if c.task is Task.jml]
    predictions = [
        *oracle_predictions(decisions, config_id="model"),
        *baseline_predictions(decisions, "always_deny"),
    ]
    run = make_run(
        tmp_path,
        cases,
        predictions,
        models=["model", "late_model"],
        extra_configs=["late_model"],
        finished=False,
        skipped=[("model", c.case_id, 0) for c in events],
    )
    with pytest.raises(IncompleteRunError) as refused:
        score_run(run, n_boot=50)
    message = str(refused.value)
    assert "no finished_at" in message
    assert f"{len(events)} cells are in skipped.jsonl" in message
    assert f"late_model {len(cases)}, model {len(events)}" in message
    assert not (run / SCORES_DIR).exists()  # nothing is written for a refused run
    result = score_run(run, n_boot=50, allow_incomplete=True)
    completeness = result.completeness
    assert not completeness.complete and not completeness.finished
    assert completeness.n_expected_cases == len(cases)
    assert completeness.n_skipped_cells == len(events)
    assert completeness.missing_predictions == {
        "always_deny": 0,
        "late_model": len(cases),
        "model": len(events),
    }
    model = result.configs["model"]
    assert model.n_cases == len(cases) and model.n_no_prediction == len(events)
    plans = model.jml
    assert plans is not None  # the unanswered suite is scored, every event INVALID
    assert plans.n_events == len(events) and plans.n_no_prediction == len(events)
    assert plans.invalid_rate.point == 1.0 and plans.exact_timeline_match.point == 0.0
    # A baseline is scored on the tasks it does: always_deny makes no JML plans.
    assert result.configs["always_deny"].jml is None
    assert result.unscored_configs == ["late_model"]  # it never answered anything


def test_a_finished_run_with_missing_predictions_is_incomplete(tmp_path: Path) -> None:
    cases = [request_case("a", "DENY"), request_case("b", "DENY")]
    predictions = [predict(cases[0], request_output("DENY"), config_id="model")]
    run = make_run(tmp_path, cases, predictions, models=["model"])
    with pytest.raises(IncompleteRunError, match=r"lack a repeat-0 prediction \(model 1\)"):
        score_run(run, n_boot=50)
    result = score_run(run, n_boot=50, allow_incomplete=True)
    assert result.completeness.finished and not result.completeness.complete
    requests = result.configs["model"].requests
    assert requests is not None and requests.n_no_prediction == 1
    assert requests.metrics.invalid_rate.point == 0.5


def test_baseline_tasks_are_read_off_answered_and_skipped_cells(tmp_path: Path) -> None:
    request = request_case("r", "DENY")
    campaign = review_case("c", [("i1", "CERTIFY")])
    predictions = [predict(request, request_output("DENY"), config_id="always_deny")]
    run = make_run(
        tmp_path,
        [request, campaign],
        predictions,
        finished=False,
        skipped=[("always_deny", "c", 0)],
    )
    loaded = load_run(run)
    assert [s.case_id for s in loaded.skipped] == ["c"]
    # The skipped campaign shows the baseline does reviews, so the review is expected of it.
    assert run_completeness(loaded).missing_predictions == {"always_deny": 1}
    result = score_run(run, n_boot=50, allow_incomplete=True)
    reviews = result.configs["always_deny"].reviews
    assert reviews is not None and reviews.n_no_prediction == 1


def test_the_case_selection_is_rebuilt_from_the_manifest(tmp_path: Path) -> None:
    cases = [request_case(f"r{i:02d}", "DENY") for i in range(20)]
    sampled = stratified_sample(cases, 0.5, seed=11)
    assert 0 < len(sampled) < len(cases)
    predictions = baseline_predictions(sampled, "always_deny")
    run = make_run(tmp_path / "ok", cases, predictions, sample=0.5, seed=11, counted=len(sampled))
    result = score_run(run, n_boot=50)
    assert result.completeness.complete
    assert result.completeness.n_expected_cases == len(sampled)
    assert result.configs["always_deny"].n_cases == len(sampled)
    # A prediction for a case the sample didn't draw doesn't belong to this run.
    drawn = {c.case_id for c in sampled}
    outside = next(c for c in cases if c.case_id not in drawn)
    stray = make_run(
        tmp_path / "stray",
        cases,
        [*predictions, *baseline_predictions([outside], "always_deny")],
        sample=0.5,
        seed=11,
        counted=len(sampled),
    )
    with pytest.raises(ValueError, match="outside the run's selection"):
        score_run(stray, n_boot=50)
    # The manifest's own count must agree with the rebuilt selection.
    rewrite_manifest(run, counts=Counts(cases=len(sampled) + 1))
    with pytest.raises(ValueError, match=f"counts {len(sampled) + 1} cases"):
        score_run(run, n_boot=50)
    rewrite_manifest(run, counts=Counts(cases=len(sampled)), sample_strata="by_owner")
    with pytest.raises(ValueError, match="only 'suite_task_tier' samples can be rebuilt"):
        score_run(run, n_boot=50)
    rewrite_manifest(run, sample_strata="suite_task_tier", seeds={})
    with pytest.raises(ValueError, match="no sample seed"):
        score_run(run, n_boot=50)


def test_suites_outside_the_experiment_are_not_expected(tmp_path: Path) -> None:
    request = request_case("r", "DENY")
    campaign = review_case("c", [("i1", "CERTIFY")])
    predictions = [predict(request, request_output("DENY"), config_id="model")]
    run = make_run(
        tmp_path,
        [request, campaign],
        predictions,
        models=["model"],
        suites=[Suite.requests],
        counted=1,
    )
    result = score_run(run, n_boot=50)
    assert result.completeness.complete and result.completeness.n_expected_cases == 1
    assert result.configs["model"].reviews is None
    # A manifest that doesn't record its suites expects every suite of the split: the safe way.
    rewrite_manifest(run, experiment={"loss_matrix": "default"}, counts=Counts())
    with pytest.raises(IncompleteRunError, match=r"\(model 1\)"):
        score_run(run, n_boot=50)
    for malformed in ("requests", [1]):
        rewrite_manifest(run, experiment={"tasks": malformed})
        with pytest.raises(ValueError, match="must be a list of suites"):
            score_run(run, n_boot=50)


def test_run_files_must_name_the_manifest_configs_and_selected_cases(tmp_path: Path) -> None:
    cases = [request_case("a", "DENY")]
    run = make_run(tmp_path, cases, baseline_predictions(cases, "always_deny"))
    (run / "skipped.jsonl").write_text(
        '{"config_id": "always_deny", "case_id": "zzz", "sample_idx": 1}\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match=r"skipped.jsonl names cases outside"):
        score_run(run, n_boot=50)
    (run / "skipped.jsonl").write_text('\n{"config_id": "always_deny"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"skipped.jsonl:2: invalid skipped cell"):
        score_run(run, n_boot=50)
    (run / "skipped.jsonl").unlink()
    rewrite_manifest(run, configs=[])
    with pytest.raises(ValueError, match="configurations the manifest doesn't list"):
        score_run(run, n_boot=50)


def test_the_dataset_is_recognized_by_its_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = [request_case("a", "DENY"), request_case("b", "DENY")]
    run = make_run(tmp_path / "repo", cases, baseline_predictions(cases, "always_deny"))
    # A same-named dataset with other content under the working directory is passed over.
    decoy = tmp_path / "cwd" / "datasets" / "toy-v1"
    write_jsonl(suite_file(decoy, Suite.requests, Split.test), cases[:1])
    monkeypatch.chdir(tmp_path / "cwd")
    assert score_run(run, n_boot=50).run_id == "run_1"
    # Moved away from its repo, the run still finds its dataset from the working directory.
    moved = tmp_path / "moved" / "runs" / "run_1"
    moved.parent.mkdir(parents=True)
    run.rename(moved)
    monkeypatch.chdir(tmp_path / "repo")
    assert score_run(moved, n_boot=50).run_id == "run_1"
    # When every candidate holds other data, the error says what each one holds.
    monkeypatch.chdir(tmp_path / "cwd")
    with pytest.raises(DatasetMismatchError, match="datasets/toy-v1 has fingerprint"):
        score_run(moved, n_boot=50)
    # A candidate whose files no longer match its own SHA256SUMS is passed over too.
    write_sha256sums(decoy)
    with suite_file(decoy, Suite.requests, Split.test).open("a", encoding="utf-8") as fh:
        fh.write("\n")
    with pytest.raises(DatasetMismatchError, match="checksum mismatch"):
        score_run(moved, n_boot=50)


# --------------------------------------------------------------------------------------------
# Rankings, rank changes and the Pareto frontier on hand-built configurations
# --------------------------------------------------------------------------------------------


def test_rank_changes_across_sensitivity_settings_are_flagged(tmp_path: Path) -> None:
    cases = [request_case("n1", "DENY"), request_case("n2", "DENY")]
    cases += [request_case("a1", "APPROVE"), request_case("a2", "APPROVE")]
    # escalator: mean cost = routing. careless: one wrongful approval (10·w) and nothing else.
    careless = ["APPROVE", "DENY", "APPROVE", "APPROVE"]
    predictions = [
        *[predict(c, request_output("ESCALATE"), config_id="escalator") for c in cases],
        *[
            predict(c, request_output(d), config_id="careless")
            for c, d in zip(cases, careless, strict=True)
        ],
        *[
            predict(c, request_output(d), config_id="denier")
            for c, d in zip(cases, ["DENY"] * 4, strict=True)
        ],
    ]
    result = score_run(make_run(tmp_path, cases, predictions), n_boot=50)
    ranking = result.rankings[Task.access_request]
    # Headline (wa=10, routing 1): escalator 1.0, careless 2.5, denier (2+2)/4 = 1.0.
    assert ranking.nrc == {"careless": 2.5, "denier": 1.0, "escalator": 1.0}
    assert ranking.ranks == {"careless": 3, "denier": 1, "escalator": 1}
    by_setting = {s.setting: s for s in ranking.by_setting}
    # wa=3: careless costs 3/4 = 0.75 and moves to the top.
    lenient = by_setting["wa=3|routing=1|mix=dataset"]
    assert lenient.ranks == {"careless": 1, "denier": 2, "escalator": 2}
    assert lenient.changed == ["careless", "denier", "escalator"]
    # Routing 2 halves the denier's NRC (its cost doesn't route) but not the escalator's.
    routed = by_setting["wa=10|routing=2|mix=dataset"]
    assert routed.nrc["denier"] == pytest.approx(0.5) and routed.nrc["escalator"] == 1.0
    # No gold-ESCALATE cases: the production mix can't be formed, which ranks nothing.
    production = by_setting["wa=10|routing=1|mix=production"]
    assert set(production.nrc.values()) == {None} and production.changed == []
    assert ranking.rank_changes
    pareto = result.pareto[Task.access_request]
    # careless: WAR 1/2, CA 3/4; denier: WAR 0, CA 1/2; escalator: WAR 0, CA 0 (dominated).
    assert pareto["careless"].on_frontier and pareto["denier"].on_frontier
    assert pareto["escalator"].on_frontier is False


def test_predictions_file_parsing(tmp_path: Path) -> None:
    cases = [request_case("a", "DENY"), request_case("b", "DENY")]
    predictions = baseline_predictions(cases, "always_deny")
    run = make_run(tmp_path, cases, predictions)
    path = run / "predictions.jsonl"
    path.write_text("\n" + path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert len(read_predictions(path)) == 2  # blank lines are skipped
    path.write_text(path.read_text(encoding="utf-8") + '{"run_id": "run_1"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"predictions.jsonl:6: invalid prediction"):
        score_run(run, n_boot=50)


def test_a_torn_last_line_is_skipped_and_makes_the_run_incomplete(tmp_path: Path) -> None:
    """A crash mid-append leaves an unterminated last line (SPEC §11.4: runs are resumable).
    The scorer skips it as the runner does, without rewriting the run, and the run reads as
    incomplete, so ``allow_incomplete`` can still score it."""
    cases = [request_case("a", "DENY"), request_case("b", "DENY")]
    predictions = baseline_predictions(cases, "always_deny")
    run = make_run(tmp_path, cases, predictions)
    path = run / "predictions.jsonl"
    complete = path.read_text(encoding="utf-8")
    torn = complete + predictions[0].model_dump_json()[:25]  # no trailing newline
    path.write_text(torn, encoding="utf-8")
    assert read_predictions(path) == predictions
    with pytest.raises(IncompleteRunError, match="ends in a torn line"):
        score_run(run, n_boot=50)
    result = score_run(run, n_boot=50, allow_incomplete=True)
    assert result.completeness.torn_tail and not result.completeness.complete
    assert result.completeness.finished  # finished, nothing missing: the torn line alone
    assert result.configs["always_deny"].n_no_prediction == 0
    assert path.read_text(encoding="utf-8") == torn  # scoring never modifies a run
    # A bad line that does end in a newline is corruption, not an interrupted append.
    path.write_text(torn + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"predictions.jsonl:3: invalid prediction"):
        read_predictions(path)
    # A complete last record without its newline is kept, as the runner keeps it.
    path.write_text(complete.rstrip("\n"), encoding="utf-8")
    assert read_predictions(path) == predictions
    assert score_run(run, n_boot=50).completeness.complete


def test_predictions_for_cases_outside_the_split_are_refused(tmp_path: Path) -> None:
    cases = [request_case("a", "DENY"), request_case("b", "DENY")]
    stray = predict(request_case("not_in_dataset", "DENY"), request_output("DENY"))
    run = make_run(tmp_path, cases[:1], [*baseline_predictions(cases[:1], "always_deny"), stray])
    with pytest.raises(ValueError, match="outside the run's selection"):
        score_run(run, n_boot=50)


def test_a_failed_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = [request_case("a", "DENY")]
    run = make_run(tmp_path, cases, baseline_predictions(cases, "always_deny"))

    def broken_replace(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("identitybench.scoring.summary.os.replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        score_run(run, n_boot=50)
    assert list((run / SCORES_DIR).iterdir()) == []


def test_a_summary_never_sits_beside_another_passs_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """per_case.jsonl is replaced first and summary.json last, with the old summary removed
    before either, so a pass that fails between the two replaces leaves no summary."""
    cases = [request_case("a", "DENY"), request_case("b", "APPROVE")]
    run = make_run(tmp_path, cases, baseline_predictions(cases, "always_deny"))
    score_run(run, n_boot=50)
    scores = run / SCORES_DIR
    assert sorted(p.name for p in scores.iterdir()) == [PER_CASE_FILE, SUMMARY_FILE]
    real_replace = os.replace
    replaced: list[str] = []

    def summary_fails(src: str | Path, dst: str | Path) -> None:
        if Path(dst).name == SUMMARY_FILE:
            raise OSError("disk full")
        replaced.append(Path(dst).name)
        real_replace(src, dst)

    monkeypatch.setattr("identitybench.scoring.summary.os.replace", summary_fails)
    with pytest.raises(OSError, match="disk full"):
        score_run(run, n_boot=60)
    assert replaced == [PER_CASE_FILE]
    # The new records are in place and the old summary is gone with no temporary file left.
    assert sorted(p.name for p in scores.iterdir()) == [PER_CASE_FILE]
    monkeypatch.undo()
    score_run(run, n_boot=50)
    assert sorted(p.name for p in scores.iterdir()) == [PER_CASE_FILE, SUMMARY_FILE]


# --------------------------------------------------------------------------------------------
# Tool-use metrics (SPEC §8.6)
# --------------------------------------------------------------------------------------------


def _traced_requests() -> tuple[list[Case], list[Prediction]]:
    """Two gold-DENY requests whose certificate rests on ``ent_1``; a tool-use model denies
    both, fetching ``ent_1`` (2 calls) on the first and nothing (4 calls) on the second."""
    certificate = Certificate(
        rule="ELIG-004", claim_sets=((Claim(id="ent_1", field="requestable", value=False),),)
    )
    view = request_view(entitlements={"ent_1": entitlement(requestable=False)})
    cases = [
        request_case(
            case_id,
            "DENY",
            view=view,
            findings=(Finding(rule="ELIG-004", effect=Effect.DENY),),
            certificates=(certificate,),
        )
        for case_id in ("r1", "r2")
    ]
    traces = [
        ToolTrace(calls=2, fetched_ids=("ent_1",), tool_names=("get_entitlement",) * 2),
        ToolTrace(calls=4, fetched_ids=(), tool_names=("search_identities",) * 4),
    ]
    predictions = [
        predict(case, request_output("DENY"), config_id="tooly", tool_trace=trace)
        for case, trace in zip(cases, traces, strict=True)
    ]
    return cases, predictions


def test_a_tool_mode_config_carries_its_tool_metrics() -> None:
    cases, predictions = _traced_requests()
    scores = score_cases(cases, predictions, settings=SETTINGS, mode="tools")
    tools = scores.tools
    assert tools is not None
    assert (tools.n_cases, tools.n_untraced) == (2, 0)
    assert tools.calls.mean_calls.point == 3.0
    assert tools.requests is not None
    retrieval = tools.requests.retrieval
    assert retrieval.retrieval_rate.point == 0.5  # r1 fetched its certificate's entity
    assert retrieval.lucky_guess_rate.point == 0.5  # r2 was right without it
    assert tools.reviews is None and tools.jml is None
    # The same predictions scored as a context-mode configuration carry none.
    assert score_cases(cases, predictions, settings=SETTINGS).tools is None


def test_score_run_gives_tool_metrics_to_tool_mode_models_only(tmp_path: Path) -> None:
    # Laid out as runner.run writes a tools-mode run: the experiment's one mode is recorded on
    # every configuration, baselines included, though baselines never call a tool.
    cases, traced = _traced_requests()
    configs = ("tooly", "always_deny", "always_escalate", "oracle")
    predictions = [
        *traced,
        *baseline_predictions(cases, "always_deny"),
        *baseline_predictions(cases, "always_escalate"),
        *oracle_predictions(cases),
    ]
    run = make_run(
        tmp_path, cases, predictions, models=["tooly"], modes=dict.fromkeys(configs, "tools")
    )
    result = score_run(run, n_boot=50)
    assert {name: info.mode for name, info in result.config_info.items()} == dict.fromkeys(
        configs, "tools"
    )
    tools = result.configs["tooly"].tools
    assert tools is not None and (tools.n_cases, tools.n_untraced) == (2, 0)
    # Every baseline, the ceiling included, carries none rather than an all-untraced block.
    assert result.configs["oracle"].ceiling
    assert all(result.configs[name].tools is None for name in configs[1:])
    # Written to summary.json and read back unchanged.
    text = (run / SCORES_DIR / SUMMARY_FILE).read_text(encoding="utf-8")
    loaded = RunScores.model_validate_json(text)
    assert loaded == result and loaded.configs["tooly"].tools == tools
    assert json.loads(text)["configs"]["oracle"]["tools"] is None


def test_score_run_gives_a_context_mode_model_no_tool_metrics(tmp_path: Path) -> None:
    cases, _ = _traced_requests()
    context = [predict(c, request_output("DENY"), config_id="ctx") for c in cases]
    result = score_run(make_run(tmp_path, cases, context, models=["ctx"]), n_boot=50)
    assert result.config_info["ctx"].mode == "context"
    assert result.configs["ctx"].tools is None
