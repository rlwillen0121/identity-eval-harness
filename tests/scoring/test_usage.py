"""API cost per 1,000 decisions by cost source, and latency percentiles."""

from __future__ import annotations

import pytest

from identitybench.schemas.predictions import CostSource, PredictionUsage
from identitybench.scoring.settings import ScoreSettings
from identitybench.scoring.summary import score_cases
from tests.scoring.builders import (
    predict,
    request_case,
    request_output,
    review_case,
    review_output,
)

SETTINGS = ScoreSettings(n_boot=50)


def _usage(cost: float | None, source: CostSource) -> PredictionUsage:
    return PredictionUsage(cost_usd=cost, cost_source=source, input_tokens=100, output_tokens=10)


def test_cost_per_thousand_decisions_counts_review_items() -> None:
    r1, r2 = request_case("r1", "DENY"), request_case("r2", "DENY")
    campaign = review_case("c", [("i1", "CERTIFY"), ("i2", "CERTIFY"), ("i3", "REVOKE")])
    predictions = [
        predict(r1, request_output("DENY"), latency_s=1.0, usage=_usage(0.01, "metered")),
        predict(r2, request_output("DENY"), latency_s=2.0, usage=_usage(0.01, "metered")),
        predict(
            campaign,
            review_output([("i1", "CERTIFY"), ("i2", "CERTIFY"), ("i3", "REVOKE")]),
            latency_s=4.0,
            usage=_usage(0.03, "metered"),
        ),
    ]
    usage = score_cases([r1, r2, campaign], predictions, settings=SETTINGS).usage
    assert (usage.n_predictions, usage.n_decisions) == (3, 5)
    assert usage.api_usd_per_1k_decisions == pytest.approx(1000 * 0.05 / 5)
    metered = usage.by_cost_source["metered"]
    assert metered.total_usd == pytest.approx(0.05) and metered.n_cost_unknown == 0
    assert usage.input_tokens == 300 and usage.output_tokens == 30
    assert usage.latency_p50_s == pytest.approx(2.0)
    assert usage.latency_p95_s == pytest.approx(3.8)  # linear interpolation over 1, 2, 4


def test_sources_stay_apart_and_unknown_cost_blocks_the_api_figure() -> None:
    cases = [request_case(f"r{i}", "DENY") for i in range(4)]
    predictions = [
        predict(cases[0], request_output("DENY"), usage=_usage(0.02, "metered")),
        predict(cases[1], request_output("DENY"), usage=_usage(0.0, "unmetered")),
        predict(cases[2], request_output("DENY"), usage=_usage(0.5, "reported")),
        predict(cases[3], request_output("DENY"), usage=_usage(None, "unknown")),
    ]
    usage = score_cases(cases, predictions, settings=SETTINGS).usage
    assert sorted(usage.by_cost_source) == ["metered", "reported", "unknown", "unmetered"]
    assert usage.api_usd_per_1k_decisions == pytest.approx(1000 * 0.02 / 2)  # metered + unmetered
    assert usage.by_cost_source["reported"].usd_per_1k_decisions == pytest.approx(500.0)
    unknown = usage.by_cost_source["unknown"]
    assert unknown.total_usd is None and unknown.usd_per_1k_decisions is None
    assert unknown.n_cost_unknown == 1
    missing = predict(cases[0], request_output("DENY"), usage=_usage(None, "metered"))
    blocked = score_cases(cases[:1], [missing], settings=SETTINGS).usage
    assert blocked.api_usd_per_1k_decisions is None  # an unpriced API call: cost unknown


def test_latency_skips_cache_hits_and_transport_errors() -> None:
    cases = [request_case(f"r{i}", "DENY") for i in range(3)]
    predictions = [
        predict(cases[0], request_output("DENY"), latency_s=3.0),
        predict(cases[1], request_output("DENY"), latency_s=0.0, cache_hit=True),
        predict(cases[2], status="error", latency_s=60.0),
    ]
    usage = score_cases(cases, predictions, settings=SETTINGS).usage
    assert usage.n_latency == 1 and usage.n_cache_hits == 1
    assert usage.latency_p50_s == usage.latency_p95_s == 3.0
    empty = score_cases(cases[:1], [], settings=SETTINGS, config_id="cfg").usage
    assert empty.latency_p50_s is None and empty.api_usd_per_1k_decisions is None
