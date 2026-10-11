"""SPEC §12.8 comparisons: which configurations are compared, the paired cluster bootstrap over
shared units, and Holm's correction across the (configuration pair × primary metric) family."""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from identitybench.schemas.case import Case
from identitybench.schemas.enums import RequestDecision, Task
from identitybench.schemas.gold import RequestGold
from identitybench.schemas.predictions import Prediction
from identitybench.scoring.loss import LossMatrix
from identitybench.scoring.settings import ScoreSettings
from identitybench.scoring.stats import DEFAULT_ALPHA, DEFAULT_N_BOOT, sign_test_p
from identitybench.scoring.summary import (
    HOLM_RESOLUTION,
    METRIC_DIRECTION,
    PRIMARY_METRICS,
    REFERENCE_BASELINE,
    Comparison,
    ScoredConfig,
    compare_configs,
    comparison_pairs,
    comparison_resamples,
    score_config,
    score_run,
)
from identitybench.world.entities import RiskTier
from tests.scoring.builders import (
    baseline_predictions,
    conformance_cases,
    make_run,
    oracle_output,
    oracle_predictions,
    predict,
    request_case,
    request_output,
)

SETTINGS = ScoreSettings(n_boot=200)


def _decision_cases() -> list[Case]:
    return [c for c in conformance_cases() if c.task is not Task.jml]


def _by_key(comparisons: Sequence[Comparison]) -> dict[tuple[str, str, Task, str], Comparison]:
    return {(c.config_a, c.config_b, c.task, c.metric): c for c in comparisons}


# --------------------------------------------------------------------------------------------
# The family
# --------------------------------------------------------------------------------------------


def test_comparison_pairs_compare_models_and_the_reference_only() -> None:
    configs = ["always_deny", "oracle", "m2", "always_escalate", "m1"]
    baselines = {"always_deny", "always_escalate", "oracle"}
    assert comparison_pairs(configs, baselines=baselines, reference="always_escalate") == [
        ("m1", "m2"),
        ("m1", "always_escalate"),  # oriented "model − reference"
        ("m2", "always_escalate"),
    ]
    # Without a reference, models are compared only with each other.
    assert comparison_pairs(configs, baselines=baselines) == [("m1", "m2")]
    # Nothing named a baseline: every pair, in sorted order.
    assert comparison_pairs(["b", "c", "a"]) == [("a", "b"), ("a", "c"), ("b", "c")]
    # The reference is never compared as a model, even when it isn't listed among baselines.
    assert comparison_pairs(["a", "ref", "b"], reference="ref") == [
        ("a", "b"),
        ("a", "ref"),
        ("b", "ref"),
    ]
    assert comparison_pairs(["always_deny", "oracle"], baselines=["always_deny", "oracle"]) == []
    with pytest.raises(ValueError, match="unknown configurations"):
        comparison_pairs(["a"], reference="always_escalate")
    with pytest.raises(ValueError, match="unknown configurations"):
        comparison_pairs(["a"], baselines=["nope"])


def test_comparison_resamples_resolve_the_holm_threshold() -> None:
    """A bootstrap p-value can't fall below 1/(B+1); Holm's first step needs at most alpha/m."""
    assert comparison_resamples(8, alpha=DEFAULT_ALPHA, n_boot=DEFAULT_N_BOOT) == DEFAULT_N_BOOT
    assert comparison_resamples(0, alpha=DEFAULT_ALPHA, n_boot=50) == 50
    for m in (101, 291, 324):
        b = comparison_resamples(m, alpha=DEFAULT_ALPHA, n_boot=DEFAULT_N_BOOT)
        assert b > DEFAULT_N_BOOT
        # The smallest reportable p-value sits an order of magnitude below alpha/m.
        assert 1 / (b + 1) <= DEFAULT_ALPHA / (HOLM_RESOLUTION * m)
    assert comparison_resamples(100, alpha=0.01, n_boot=50) == 100_000


# --------------------------------------------------------------------------------------------
# compare_configs
# --------------------------------------------------------------------------------------------


def test_compare_configs_pairs_and_holm() -> None:
    cases = _decision_cases()
    oracle = score_config(cases, oracle_predictions(cases), settings=SETTINGS)
    deny = score_config(cases, baseline_predictions(cases, "always_deny"), settings=SETTINGS)
    twin = score_config(cases, oracle_predictions(cases, config_id="oracle2"), settings=SETTINGS)
    comparisons = compare_configs({"oracle": oracle, "deny": deny, "oracle2": twin})
    assert len(comparisons) == 3 * 8  # 3 pairs × 4 primary metrics × 2 tasks
    resamples = comparison_resamples(24, alpha=DEFAULT_ALPHA, n_boot=DEFAULT_N_BOOT)
    tested = sum(c.p_value is not None for c in comparisons)
    for c in comparisons:
        assert c.n_boot == resamples and c.family_size == tested
        if c.p_value is not None:
            assert c.adjusted_p is not None and c.adjusted_p >= c.p_value
            assert c.reject == (c.adjusted_p <= c.alpha)
        else:
            assert c.adjusted_p is None and not c.reject
    by_key = _by_key(comparisons)
    same = by_key[("oracle", "oracle2", Task.access_request, "nrc")]
    assert same.diff == 0.0 and same.p_value == 1.0 and not same.reject
    better = by_key[("deny", "oracle", Task.access_request, "correct_automation")]
    assert better.diff is not None and better.diff < 0
    assert better.low is not None and better.high is not None and better.high < 0
    assert better.reject
    only = compare_configs({"oracle": oracle, "deny": deny}, pairs=[("oracle", "deny")], n_boot=50)
    assert {c.config_a for c in only} == {"oracle"}
    assert {c.n_boot for c in only} == {comparison_resamples(8, alpha=DEFAULT_ALPHA, n_boot=50)}
    with pytest.raises(ValueError, match="unknown configuration"):
        compare_configs({"oracle": oracle}, pairs=[("oracle", "nope")])
    with pytest.raises(ValueError, match="not both"):
        compare_configs({"oracle": oracle, "deny": deny}, pairs=[], baselines=["deny"])
    other_matrix = score_config(
        cases,
        oracle_predictions(cases, config_id="o3"),
        settings=ScoreSettings(n_boot=50, loss=LossMatrix(routing=2.0)),
    )
    with pytest.raises(ValueError, match="different loss matrices"):
        compare_configs({"oracle": oracle, "o3": other_matrix})


def test_no_comparison_means_an_empty_family() -> None:
    cases = _decision_cases()
    oracle = score_config(cases, oracle_predictions(cases), settings=SETTINGS)
    assert compare_configs({"oracle": oracle}) == []
    assert compare_configs({"oracle": oracle}, baselines=["oracle"]) == []


def _request_mix(n_each: int = 10) -> list[Case]:
    """Hand-built requests: ``n_each`` each of gold DENY, ESCALATE and (standing) APPROVE."""
    return [
        request_case(f"{decision[0].lower()}{i:02d}", decision)
        for decision in ("DENY", "ESCALATE", "APPROVE")
        for i in range(n_each)
    ]


Output = Callable[[int, Case], dict[str, Any]]


def _constant(decision: str) -> Output:
    return lambda _i, _case: request_output(decision)


def _sloppy(every: int) -> Output:
    """The oracle, except that every ``every``-th case is approved outright."""
    return lambda i, case: request_output("APPROVE") if i % every == 0 else oracle_output(case)


def _predictions(cases: Sequence[Case], outputs: dict[str, Output]) -> list[Prediction]:
    return [
        predict(case, output(i, case), config_id=name)
        for name, output in outputs.items()
        for i, case in enumerate(cases)
    ]


NINE: dict[str, Output] = {
    "approver": _constant("APPROVE"),
    "denier": _constant("DENY"),
    "escalator": _constant("ESCALATE"),
    "oracle": lambda _i, case: oracle_output(case),
    "sloppy_2": _sloppy(2),
    "sloppy_3": _sloppy(3),
    "sloppy_4": _sloppy(4),
    "sloppy_5": _sloppy(5),
    "sloppy_7": _sloppy(7),
}


def test_an_overwhelming_difference_is_rejected_among_nine_configurations() -> None:
    """36 pairs × 3 testable metrics = 108 comparisons: past the ~100 a fixed B = 2,000 could
    ever reject, since its smallest p-value 1/2001 times 108 exceeds alpha."""
    cases = _request_mix()
    predictions = _predictions(cases, NINE)
    scored = {
        name: score_config(
            cases, [p for p in predictions if p.config_id == name], settings=SETTINGS
        )
        for name in NINE
    }
    comparisons = compare_configs(scored)
    planned = len(list(itertools.combinations(NINE, 2))) * 4
    resamples = comparison_resamples(planned, alpha=DEFAULT_ALPHA, n_boot=DEFAULT_N_BOOT)
    assert len(comparisons) == planned
    war = _by_key(comparisons)[("approver", "oracle", Task.access_request, "war")]
    assert (war.diff, war.low, war.high) == (1.0, 1.0, 1.0)
    assert war.n_boot == resamples
    assert war.p_value == pytest.approx(1 / (resamples + 1))  # no resample crosses zero
    # Grounded accuracy has no certificate units here, so no p-value: 3 metrics are tested.
    assert war.family_size == 36 * 3
    assert war.family_size / (DEFAULT_N_BOOT + 1) > DEFAULT_ALPHA
    assert war.adjusted_p is not None and war.adjusted_p <= DEFAULT_ALPHA
    assert war.reject
    routing = _by_key(comparisons)[("escalator", "oracle", Task.access_request, "nrc")]
    assert routing.reject  # escalating costs routing on every decidable case


def test_score_run_compares_models_with_each_other_and_the_status_quo(tmp_path: Path) -> None:
    """Nine configurations, five of them models: the family is the ten model pairs plus each
    model against always_escalate, never two baselines."""
    cases = _request_mix()
    models = {
        "m_approve": _constant("APPROVE"),
        "m_oracle": NINE["oracle"],
        "m_sloppy2": _sloppy(2),
        "m_sloppy3": _sloppy(3),
        "m_sloppy5": _sloppy(5),
    }
    predictions = [
        *_predictions(cases, models),
        *baseline_predictions(cases, "always_approve"),
        *baseline_predictions(cases, "always_deny"),
        *baseline_predictions(cases, REFERENCE_BASELINE),
        *oracle_predictions(cases),
    ]
    run = make_run(tmp_path, cases, predictions, models=models)
    result = score_run(run, n_boot=50)
    assert len(result.configs) == 9
    pairs = {(c.config_a, c.config_b) for c in result.comparisons}
    expected = set(itertools.combinations(sorted(models), 2))
    expected |= {(model, REFERENCE_BASELINE) for model in models}
    assert pairs == expected
    resamples = comparison_resamples(len(expected) * 4, alpha=DEFAULT_ALPHA, n_boot=50)
    assert {c.n_boot for c in result.comparisons} == {resamples}
    by_key = _by_key(result.comparisons)
    war = by_key[("m_approve", "m_oracle", Task.access_request, "war")]
    assert war.diff == 1.0 and war.reject
    status_quo = by_key[("m_oracle", REFERENCE_BASELINE, Task.access_request, "nrc")]
    assert status_quo.point_b == 1.0 and status_quo.diff is not None and status_quo.diff < 0
    assert status_quo.reject  # the oracle beats the all-human status quo


def test_without_always_escalate_models_are_compared_only_with_each_other(
    tmp_path: Path,
) -> None:
    cases = _request_mix(4)
    models = {"m_oracle": NINE["oracle"], "m_sloppy2": _sloppy(2)}
    predictions = [*_predictions(cases, models), *baseline_predictions(cases, "always_deny")]
    result = score_run(make_run(tmp_path, cases, predictions, models=models), n_boot=50)
    assert {(c.config_a, c.config_b) for c in result.comparisons} == {("m_oracle", "m_sloppy2")}


def test_compare_configs_uses_shared_units_only() -> None:
    cases = [c for c in _decision_cases() if c.task is Task.access_request]
    full = score_config(cases, oracle_predictions(cases), settings=SETTINGS)
    half = score_config(
        cases[::2], oracle_predictions(cases[::2], config_id="half"), settings=SETTINGS
    )
    (war,) = [
        c for c in compare_configs({"full": full, "half": half}, n_boot=50) if c.metric == "war"
    ]
    assert war.n_units == len(cases[::2])


def test_comparisons_report_discordance_and_detectable_difference() -> None:
    cases = _decision_cases()
    oracle = score_config(cases, oracle_predictions(cases), settings=SETTINGS)
    deny = score_config(cases, baseline_predictions(cases, "always_deny"), settings=SETTINGS)
    comparisons = compare_configs({"o": oracle, "d": deny}, n_boot=50)
    assert {(c.config_a, c.config_b) for c in comparisons} == {("d", "o")}  # sorted pair
    by_key = {(c.task, c.metric): c for c in comparisons}
    automation = by_key[(Task.access_request, "correct_automation")]
    golds = [c.gold for c in cases if isinstance(c.gold, RequestGold)]
    # The deny baseline is right only on gold-DENY cases, which the oracle also gets right; the
    # oracle alone automates the gold-APPROVE cases correctly.
    gold_a = sum(g.decision is RequestDecision.APPROVE for g in golds)
    assert automation.binary
    assert automation.n_discordant == gold_a
    assert (automation.n_favoring_a, automation.n_favoring_b) == (0, gold_a)
    assert automation.discordance_rate == pytest.approx(gold_a / len(golds))
    mdd, rate = automation.minimum_detectable_difference, automation.discordance_rate
    assert mdd is not None and rate is not None
    assert 0 < mdd <= rate
    war = by_key[(Task.access_request, "war")]
    assert war.n_discordant == 0 and war.minimum_detectable_difference is None  # no discordance
    assert war.p_signflip == 1.0 and war.signflip_exact
    # Review units are resampled by campaign, so discordance counts campaigns, not items, and
    # the independent-units approximation isn't applied.
    review = by_key[(Task.access_review, "correct_automation")]
    campaigns = sum(c.task is Task.access_review for c in cases)
    assert review.cluster == "campaign" and 0 < review.n_discordant <= campaigns
    assert review.n_discordant == review.n_favoring_a + review.n_favoring_b
    assert review.minimum_detectable_difference is None
    # NRC counts discordant cases too (a per-case cost, so no detectable-difference formula):
    # always_deny costs 2 on every gold-APPROVE and gold-ESCALATE case, where the oracle costs
    # 0 and 1, and both cost 0 on gold-DENY cases.
    nrc = by_key[(Task.access_request, "nrc")]
    gold_not_n = sum(g.decision is not RequestDecision.DENY for g in golds)
    assert not nrc.binary
    assert (nrc.n_discordant, nrc.n_favoring_a, nrc.n_favoring_b) == (gold_not_n, 0, gold_not_n)
    assert nrc.discordance_rate == pytest.approx(gold_not_n / len(golds))
    assert nrc.minimum_detectable_difference is None


def test_every_primary_metric_declares_its_direction() -> None:
    primary = {metric for metrics in PRIMARY_METRICS.values() for metric in metrics}
    assert set(METRIC_DIRECTION) == primary


def test_units_favoring_each_side_follow_the_metric_direction() -> None:
    """Lower is better for NRC and WAR, higher for correct automation: one unit going each way
    is counted on the side it favors, whichever orientation the pair has."""
    cases = _request_mix(2)  # d00 d01 e00 e01 a00 a01
    oracle_outputs = {c.case_id: oracle_output(c) for c in cases}
    a_outputs = dict(oracle_outputs, d00=request_output("APPROVE"))  # a wrongful approval
    b_outputs = dict(oracle_outputs, a00=request_output("ESCALATE"))  # a needless escalation
    scored = {
        name: score_config(
            cases,
            [predict(c, outputs[c.case_id], config_id=name) for c in cases],
            settings=SETTINGS,
        )
        for name, outputs in (("a", a_outputs), ("b", b_outputs))
    }
    by_metric = {
        c.metric: c
        for c in compare_configs(scored, pairs=[("a", "b")])
        if c.task is Task.access_request
    }
    war = by_metric["war"]  # a wrongfully approved d00; b did nothing wrong on gold non-A
    assert (war.n_discordant, war.n_favoring_a, war.n_favoring_b) == (1, 0, 1)
    nrc = by_metric["nrc"]  # d00 costs a 10, a00 costs b 1
    assert (nrc.n_discordant, nrc.n_favoring_a, nrc.n_favoring_b) == (2, 1, 1)
    automation = by_metric["correct_automation"]  # a automates a00, b automates d00 correctly
    assert (automation.n_discordant, automation.n_favoring_a, automation.n_favoring_b) == (2, 1, 1)
    assert automation.p_signflip == 1.0  # one each way is no evidence at all
    # On NRC the two units differ by +10 and −1: every flip of their signs lands at least 9 from
    # 0, so two units are no evidence on a cost either.
    assert nrc.p_signflip == 1.0


# --------------------------------------------------------------------------------------------
# The sign-flip test keeps p-values honest when few units carry the difference
# --------------------------------------------------------------------------------------------


def _wrongful_approvals(n: int, approved: int) -> dict[str, ScoredConfig]:
    """``n`` gold-DENY requests: the oracle denies them all, ``sloppy`` approves the first
    ``approved``. Every discordant unit favors the oracle, on every primary metric."""
    cases = [request_case(f"n{i:02d}", "DENY") for i in range(n)]
    return {
        "oracle": score_config(cases, oracle_predictions(cases), settings=SETTINGS),
        "sloppy": score_config(
            cases,
            [
                predict(
                    c, request_output("APPROVE" if i < approved else "DENY"), config_id="sloppy"
                )
                for i, c in enumerate(cases)
            ],
            settings=SETTINGS,
        ),
    }


@pytest.mark.parametrize(("discordant", "exact"), [(4, 0.125), (5, 0.0625), (7, 0.015625)])
def test_few_discordant_units_get_the_exact_sign_test_p_value(
    discordant: int, exact: float
) -> None:
    """The measured cases at n = 40 independent units, all discordance one way: the bootstrap's
    tail shrinks like e^-d (d = 4 gives about 0.03), far below the exact 2/2^d. Every unit
    differs by one, so the sign-flip test is the exact two-sided sign test."""
    comparisons = compare_configs(_wrongful_approvals(40, discordant), pairs=[("sloppy", "oracle")])
    war = _by_key(comparisons)[("sloppy", "oracle", Task.access_request, "war")]
    assert war.diff == pytest.approx(discordant / 40)
    assert war.n_discordant == discordant and war.n_favoring_b == discordant
    assert war.p_bootstrap is not None and war.p_bootstrap < exact  # anti-conservative
    assert war.p_signflip == exact and war.signflip_exact
    assert war.p_value == exact  # the larger of the two
    assert war.method == "paired_bootstrap+sign_flip"
    # NRC and correct automation differ on the same cases, so they get the same exact bound.
    by_metric = {c.metric: c for c in comparisons}
    assert by_metric["nrc"].p_value == exact and by_metric["correct_automation"].p_value == exact
    for c in comparisons:
        if c.p_value is not None:
            assert c.adjusted_p is not None and c.adjusted_p >= c.p_value


def test_four_discordant_units_of_forty_are_not_a_significant_difference() -> None:
    comparisons = compare_configs(_wrongful_approvals(40, 4), pairs=[("sloppy", "oracle")])
    assert comparisons and not any(c.reject for c in comparisons)
    # On its own, the bootstrap would have called the WAR difference significant.
    war = _by_key(comparisons)[("sloppy", "oracle", Task.access_request, "war")]
    assert war.p_bootstrap is not None and war.p_bootstrap < DEFAULT_ALPHA
    assert war.p_value is not None and war.p_value > DEFAULT_ALPHA


def test_a_large_clear_difference_is_still_rejected() -> None:
    """With 20 of 40 units discordant the sign-flip p (2/2^20) is far below the bootstrap's
    1/(B+1) floor, so the bootstrap's p-value binds and the difference is rejected."""
    comparisons = compare_configs(_wrongful_approvals(40, 20), pairs=[("sloppy", "oracle")])
    war = _by_key(comparisons)[("sloppy", "oracle", Task.access_request, "war")]
    assert war.p_signflip == pytest.approx(2 / 2**20)
    assert war.p_bootstrap is not None and war.p_value == war.p_bootstrap
    assert war.reject
    assert all(c.reject for c in comparisons if c.p_value is not None)


def _cost_trade_off(catastrophic: int, escalations: int, n: int) -> dict[str, ScoredConfig]:
    """``n`` gold-DENY requests, the first ``catastrophic`` of them critical. ``careful``
    escalates the next ``escalations`` (1 each) and denies the rest; ``reckless`` wrongfully
    approves the critical ones (10·w = 40 each) and denies the rest."""
    cases = [
        request_case(
            f"n{i:03d}", "DENY", risk=RiskTier.critical if i < catastrophic else RiskTier.medium
        )
        for i in range(n)
    ]

    def careful(i: int) -> str:
        return "ESCALATE" if catastrophic <= i < catastrophic + escalations else "DENY"

    def reckless(i: int) -> str:
        return "APPROVE" if i < catastrophic else "DENY"

    return {
        name: score_config(
            cases,
            [predict(c, request_output(decide(i)), config_id=name) for i, c in enumerate(cases)],
            settings=SETTINGS,
        )
        for name, decide in (("careful", careful), ("reckless", reckless))
    }


def test_a_cost_difference_resting_on_few_units_is_not_significant() -> None:
    """Four catastrophic approvals against twenty cheap escalations the other way: the NRC
    difference rests on four units. The bootstrap calls it significant and a sign test would
    too (twenty units against four); the sign-flip test gives the four units' 2/2^4."""
    comparisons = compare_configs(_cost_trade_off(4, 20, 40), pairs=[("careful", "reckless")])
    nrc = _by_key(comparisons)[("careful", "reckless", Task.access_request, "nrc")]
    assert nrc.diff == pytest.approx((20 - 4 * 40) / 40)
    assert (nrc.n_discordant, nrc.n_favoring_a, nrc.n_favoring_b) == (24, 4, 20)
    assert nrc.p_bootstrap is not None and nrc.p_bootstrap < DEFAULT_ALPHA
    assert sign_test_p(4, 20) < DEFAULT_ALPHA
    assert nrc.p_signflip == pytest.approx(0.125) and nrc.signflip_exact
    assert nrc.p_value is not None and nrc.p_value >= 0.125
    assert not nrc.reject


def test_a_cost_trade_off_carried_by_many_units_is_still_rejected() -> None:
    """Thirty catastrophic approvals against a hundred escalations is a real difference: the
    sign-flip p-value is about 6e-8, so the bootstrap's floor binds and the comparison rejects
    (a directional sign test, thirty against a hundred, would have called it no evidence)."""
    comparisons = compare_configs(_cost_trade_off(30, 100, 160), pairs=[("careful", "reckless")])
    nrc = _by_key(comparisons)[("careful", "reckless", Task.access_request, "nrc")]
    assert (nrc.n_favoring_a, nrc.n_favoring_b) == (30, 100)
    assert nrc.p_signflip == pytest.approx(5.6759e-8, rel=1e-4)
    assert nrc.p_value == nrc.p_bootstrap
    assert nrc.reject


def test_tied_total_costs_give_p_one_instead_of_a_crash() -> None:
    """Total costs that tie over many mixed-size units: the observed sum is the smallest one
    reachable, so the exact sign-flip test sums its whole null distribution, which float
    rounding can push a hair past 1. Holm refuses p-values above 1, so the comparison must cap
    it and come back with p = 1 rather than raise."""
    deny = [request_case(f"n{i:03d}", "DENY", risk=RiskTier.low) for i in range(60)]
    approve = [request_case(f"a{i:03d}", "APPROVE") for i in range(60)]
    cases = deny + approve

    def escalate_and_deny(i: int, case: Case) -> dict[str, Any]:
        # Escalates 9 gold-DENY cases and wrongly denies 33 gold-APPROVE ones.
        if case.case_id.startswith("n") and 15 <= i < 24:
            return request_output("ESCALATE")
        if case.case_id.startswith("a") and i - 60 < 33:
            return request_output("DENY")
        return oracle_output(case)

    def approve_low_risk(i: int, case: Case) -> dict[str, Any]:
        # Wrongly approves 15 low-risk gold-DENY cases.
        if case.case_id.startswith("n") and i < 15:
            return request_output("APPROVE")
        return oracle_output(case)

    scored = {
        name: score_config(
            cases,
            [predict(c, decide(i, c), config_id=name) for i, c in enumerate(cases)],
            settings=SETTINGS,
        )
        for name, decide in (("a", escalate_and_deny), ("b", approve_low_risk))
    }
    nrc = _by_key(compare_configs(scored, pairs=[("a", "b")]))[
        ("a", "b", Task.access_request, "nrc")
    ]
    assert nrc.diff == pytest.approx(0.0)
    assert nrc.p_signflip == 1.0 and nrc.signflip_exact
    assert nrc.p_value == 1.0
    assert not nrc.reject


def test_a_comparison_without_a_bootstrap_p_value_stays_out_of_the_family() -> None:
    """One gold non-APPROVE case: WAR has a point but no interval, so no p-value, though the
    sign-flip test still reports on that single unit."""
    cases = [request_case("n0", "DENY"), *(request_case(f"a{i}", "APPROVE") for i in range(3))]
    scored = {
        "oracle": score_config(cases, oracle_predictions(cases), settings=SETTINGS),
        "approver": score_config(
            cases,
            [predict(c, request_output("APPROVE"), config_id="approver") for c in cases],
            settings=SETTINGS,
        ),
    }
    comparisons = compare_configs(scored, pairs=[("approver", "oracle")])
    war = _by_key(comparisons)[("approver", "oracle", Task.access_request, "war")]
    assert war.diff == 1.0 and war.p_bootstrap is None and war.p_value is None
    assert war.p_signflip == 1.0 and war.n_discordant == 1
    assert war.adjusted_p is None and not war.reject
    assert war.family_size == sum(c.p_value is not None for c in comparisons)


# --------------------------------------------------------------------------------------------
# The oracle is a ceiling, not a contender (SPEC §14)
# --------------------------------------------------------------------------------------------


def test_a_ceiling_is_never_compared() -> None:
    cases = _request_mix(4)
    oracle = score_config(cases, oracle_predictions(cases), settings=SETTINGS, ceiling=True)
    assert oracle.scores.ceiling
    models = {
        name: score_config(
            cases, [p for p in _predictions(cases, {name: output})], settings=SETTINGS
        )
        for name, output in (("m1", _sloppy(2)), ("m2", _sloppy(3)))
    }
    assert not models["m1"].scores.ceiling
    comparisons = compare_configs({"oracle": oracle, **models})
    assert {(c.config_a, c.config_b) for c in comparisons} == {("m1", "m2")}
    with pytest.raises(ValueError, match="names a ceiling"):
        compare_configs({"oracle": oracle, **models}, pairs=[("m1", "oracle")])
    assert compare_configs({"oracle": oracle}) == []


def test_comparison_pairs_leave_ceilings_out() -> None:
    configs = ["oracle", "m1", "m2", "always_escalate"]
    assert comparison_pairs(
        configs,
        baselines={"oracle", "always_escalate"},
        reference="always_escalate",
        ceilings={"oracle"},
    ) == [("m1", "m2"), ("m1", "always_escalate"), ("m2", "always_escalate")]
    # A ceiling that isn't also listed as a baseline is still never compared.
    assert comparison_pairs(configs, ceilings={"oracle"}) == [
        ("always_escalate", "m1"),
        ("always_escalate", "m2"),
        ("m1", "m2"),
    ]
    with pytest.raises(ValueError, match="is a ceiling"):
        comparison_pairs(configs, reference="oracle", ceilings={"oracle"})
    with pytest.raises(ValueError, match="unknown configurations"):
        comparison_pairs(configs, ceilings={"nope"})


def test_score_run_keeps_the_oracle_scores_but_never_ranks_or_compares_it(
    tmp_path: Path,
) -> None:
    """The oracle baseline beats everything on both axes: ranked, it would be #1 under every
    setting and alone on the Pareto frontier; compared, it would add comparisons whose answer
    is known to the Holm family."""
    cases = _request_mix(4)
    models = {"m_sloppy2": _sloppy(2), "m_sloppy3": _sloppy(3)}
    predictions = [
        *_predictions(cases, models),
        *oracle_predictions(cases),
        *baseline_predictions(cases, REFERENCE_BASELINE),
    ]
    result = score_run(make_run(tmp_path, cases, predictions, models=models), n_boot=50)
    assert result.configs["oracle"].ceiling
    assert not any(result.configs[name].ceiling for name in (*models, REFERENCE_BASELINE))
    requests = result.configs["oracle"].requests
    assert requests is not None and requests.metrics.war.point == 0.0  # scored in full
    ranking = result.rankings[Task.access_request]
    assert set(ranking.nrc) == set(ranking.ranks) == {*models, REFERENCE_BASELINE}
    for setting in ranking.by_setting:
        assert "oracle" not in setting.ranks and "oracle" not in setting.changed
    assert min(r for r in ranking.ranks.values() if r is not None) == 1  # a contender leads
    pareto = result.pareto[Task.access_request]
    assert set(pareto) == {*models, REFERENCE_BASELINE}
    assert any(point.on_frontier for point in pareto.values())
    assert all("oracle" not in (c.config_a, c.config_b) for c in result.comparisons)
    pairs = {(c.config_a, c.config_b) for c in result.comparisons}
    assert pairs == {
        ("m_sloppy2", "m_sloppy3"),
        ("m_sloppy2", REFERENCE_BASELINE),
        ("m_sloppy3", REFERENCE_BASELINE),
    }
