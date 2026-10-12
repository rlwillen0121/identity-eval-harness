"""API cost per 1,000 decisions by cost source and basis (metered, gateway-billed, unmetered,
CLI estimate, unverified, unknown), free failed calls, and latency percentiles."""

from __future__ import annotations

from typing import Any

import pytest

from identitybench.runner.manifest import ConfigManifest, CostSummary, ParamsRecord, RunManifest
from identitybench.schemas.case import Case
from identitybench.schemas.predictions import CostSource, Prediction, PredictionUsage
from identitybench.scoring.settings import ScoreSettings
from identitybench.scoring.summary import score_cases
from identitybench.scoring.usage import (
    ReportedCost,
    UsageScores,
    config_reported_cost,
    reported_cost_of,
)
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
    # One unknown cost blocks the figure: the known three would price it off a subset and
    # could show it cheaper than it is. The CLI estimate is the reason for n/a (it would be n/a
    # even if complete), and the unknown cost is still counted.
    assert usage.api_usd_per_1k_decisions is None
    assert (usage.cost_basis, usage.n_cost_unknown) == ("partial", 1)
    billed = score_cases(cases, predictions, settings=SETTINGS, reported_cost="billed").usage
    assert (billed.cost_basis, billed.n_cost_unknown) == ("incomplete", 1)
    assert usage.by_cost_source["reported"].usd_per_1k_decisions == pytest.approx(500.0)
    assert usage.cli_estimate_usd_per_1k_decisions == pytest.approx(500.0)
    unknown = usage.by_cost_source["unknown"]
    assert unknown.total_usd is None and unknown.usd_per_1k_decisions is None
    assert unknown.n_cost_unknown == 1
    missing = predict(cases[0], request_output("DENY"), usage=_usage(None, "metered"))
    blocked = score_cases(cases[:1], [missing], settings=SETTINGS).usage
    assert blocked.api_usd_per_1k_decisions is None  # an unpriced API call: cost unknown
    assert blocked.cost_basis == "unknown"


def _config_usage(
    sources: list[tuple[float | None, CostSource]], reported: ReportedCost = "estimate"
) -> UsageScores:
    """One configuration answering a request per entry of ``sources`` at that cost."""
    cases = [request_case(f"r{i}", "DENY") for i in range(len(sources))]
    predictions = [
        predict(case, request_output("DENY"), usage=_usage(cost, source))
        for case, (cost, source) in zip(cases, sources, strict=True)
    ]
    return score_cases(cases, predictions, settings=SETTINGS, reported_cost=reported).usage


@pytest.mark.parametrize(
    ("sources", "reported", "headline", "basis", "estimate"),
    [
        # A subscription CLI: its own estimate, listed apart, never an API cost.
        ([(0.4, "reported"), (0.6, "reported")], "estimate", None, "estimate", 500.0),
        # OpenRouter: the USD it charged per call, real API cost.
        ([(0.4, "reported"), (0.6, "reported")], "billed", 500.0, "billed", None),
        # A metered API priced from pricing.yaml.
        ([(0.01, "metered"), (0.03, "metered")], "estimate", 20.0, "metered", None),
        # A self-hosted model: a real $0.
        ([(0.0, "unmetered"), (0.0, "unmetered")], "estimate", 0.0, "unmetered", None),
        # A replay mixing API sources: still API cost, one figure over all of it.
        ([(0.02, "metered"), (0.0, "unmetered")], "estimate", 10.0, "api", None),
        ([(0.02, "metered"), (0.04, "reported")], "billed", 30.0, "api", None),
        # Some calls have only a CLI estimate: no API figure for them, so none at all.
        ([(0.02, "metered"), (0.5, "reported")], "estimate", None, "partial", 500.0),
        # A codex-style CLI that reports nothing.
        ([(None, "unknown"), (None, "unknown")], "estimate", None, "unknown", None),
        # A CLI with one unknown cost: still labelled by its estimate (unknowns counted), never
        # "incomplete", which would imply an API figure once complete.
        ([(0.4, "reported"), (None, "unknown")], "estimate", None, "estimate", 400.0),
        (
            [(0.02, "metered"), (0.5, "reported"), (None, "unknown")],
            "estimate",
            None,
            "partial",
            500.0,
        ),
        # Reported by a source that is neither a known gateway nor a CLI (a replay, another
        # endpoint): its own label, not an API cost, and not a CLI estimate either.
        ([(0.4, "reported"), (0.6, "reported")], "unverified", None, "unverified", None),
        ([(0.4, "reported"), (None, "unknown")], "unverified", None, "unverified", None),
        ([(0.02, "metered"), (0.5, "reported")], "unverified", None, "partial", None),
    ],
)
def test_each_kind_of_configuration_gets_its_headline_cost_and_basis(
    sources: list[tuple[float | None, CostSource]],
    reported: ReportedCost,
    headline: float | None,
    basis: str,
    estimate: float | None,
) -> None:
    usage = _config_usage(sources, reported)
    assert usage.reported_cost == reported and usage.cost_basis == basis
    if headline is None:
        assert usage.api_usd_per_1k_decisions is None
    else:
        assert usage.api_usd_per_1k_decisions == pytest.approx(headline)
    if estimate is None:
        assert usage.cli_estimate_usd_per_1k_decisions is None
    else:
        assert usage.cli_estimate_usd_per_1k_decisions == pytest.approx(estimate)


def test_a_billed_configuration_with_an_unknown_cost_stays_incomplete() -> None:
    """An OpenRouter call that came back without ``usage.cost`` is recorded as unknown; the
    configuration must not be priced off its other calls."""
    usage = _config_usage([(0.4, "reported"), (None, "unknown"), (0.6, "reported")], "billed")
    assert usage.api_usd_per_1k_decisions is None
    assert (usage.cost_basis, usage.n_cost_unknown, usage.n_predictions) == ("incomplete", 1, 3)
    assert usage.by_cost_source["reported"].total_usd == pytest.approx(1.0)


def test_a_reported_cost_without_a_figure_counts_as_unknown() -> None:
    usage = _config_usage([(0.4, "reported"), (None, "reported")], "billed")
    assert usage.api_usd_per_1k_decisions is None and usage.cost_basis == "incomplete"


OPENROUTER = "OPENROUTER_BASE_URL"


@pytest.mark.parametrize(
    ("provider", "base_url_env", "cost_mode", "expected"),
    [
        ("claude_cli", None, "reported", "estimate"),
        ("grok_cli", None, "reported", "estimate"),
        ("codex_cli", None, "reported", "estimate"),
        ("cursor_cli", None, "reported", "estimate"),
        ("openai_compatible", OPENROUTER, "reported", "billed"),
        # Priced from pricing.yaml instead: no reported cost is a gateway charge.
        ("openai_compatible", OPENROUTER, "metered", "unverified"),
        # Another endpoint isn't known to report per-call charges, and isn't a CLI.
        ("openai_compatible", "VLLM_BASE_URL", "reported", "unverified"),
        ("openai_compatible", None, "reported", "unverified"),
        ("replay", OPENROUTER, "reported", "unverified"),
        ("openai_compatible", OPENROUTER, None, "unverified"),
        ("baseline", None, None, "unverified"),
    ],
)
def test_reported_costs_are_billed_only_for_a_reporting_gateway(
    provider: str, base_url_env: str | None, cost_mode: str | None, expected: ReportedCost
) -> None:
    assert reported_cost_of(provider, base_url_env=base_url_env, cost_mode=cost_mode) == expected


def _manifest(entries: list[dict[str, Any]], cost: dict[str, CostSummary]) -> RunManifest:
    return RunManifest.model_validate(
        {
            "run_id": "run_1",
            "created_at": "2026-10-11T00:00:00Z",
            "dataset": {"name": "toy", "version": "v1", "sha256": "0" * 64, "path": "toy-v1"},
            "split": "test",
            "cost": cost,
            "experiment": {"models": entries},
        }
    )


def _config(config_id: str, label: str, provider: str) -> ConfigManifest:
    return ConfigManifest(
        config_id=config_id,
        label=label,
        provider=provider,
        model="m",
        endpoint_identity="e",
        params=ParamsRecord(max_output_tokens=1024),
        prompt_variant="baseline",
        prompt_sha256="a" * 64,
        mode="context",
    )


def test_the_manifest_decides_what_a_configurations_reported_costs_are() -> None:
    entries = [
        {"provider": "claude_cli", "label": "cli", "base_url_env": None, "cost_mode": "reported"},
        {
            "provider": "openai_compatible",
            "label": "router",
            "base_url_env": OPENROUTER,
            "cost_mode": "reported",
        },
        # An older manifest without the resolved cost mode: the CostSummary supplies it.
        {"provider": "openai_compatible", "label": "router-old", "base_url_env": OPENROUTER},
    ]
    cost = {"router_old": CostSummary(cost_mode="reported", reported_usd=0.5)}
    manifest = _manifest(entries, cost)
    assert config_reported_cost(manifest, _config("cli", "cli", "claude_cli")) == "estimate"
    router = _config("router", "router", "openai_compatible")
    assert config_reported_cost(manifest, router) == "billed"
    old = _config("router_old", "router-old", "openai_compatible")
    assert config_reported_cost(manifest, old) == "billed"
    # No entry (a baseline), or an entry for another provider under the same label: no
    # evidence of a gateway, so unverified; a CLI kind is a CLI estimate either way.
    assert config_reported_cost(manifest, _config("b", "always_deny", "baseline")) == "unverified"
    assert config_reported_cost(manifest, _config("x", "router", "replay")) == "unverified"
    assert config_reported_cost(_manifest([], {}), router) == "unverified"
    assert config_reported_cost(_manifest([], {}), _config("c", "c", "grok_cli")) == "estimate"


def _free_error(case: Case) -> Prediction:
    """A transport error that recorded no cost and used no tokens, as a reported-mode run
    records it."""
    return predict(case, status="error", usage=PredictionUsage(cost_source="unknown"))


def test_a_free_failed_call_costs_nothing_under_the_configurations_basis() -> None:
    """A leftover timeout under a gateway-billed configuration is $0, as the runner books it
    and as a metered configuration records it, not an unknown cost that blocks the figure."""
    cases = [request_case(f"r{i}", "DENY") for i in range(2)]
    billed = [predict(cases[0], request_output("DENY"), usage=_usage(0.4, "reported"))]
    usage = score_cases(
        cases, [*billed, _free_error(cases[1])], settings=SETTINGS, reported_cost="billed"
    ).usage
    assert (usage.cost_basis, usage.n_cost_unknown, usage.n_free_errors) == ("billed", 0, 1)
    assert usage.api_usd_per_1k_decisions == pytest.approx(1000 * 0.4 / 2)
    # A CLI's: the estimate figure takes it at $0 too.
    cli = score_cases(cases, [*billed, _free_error(cases[1])], settings=SETTINGS).usage
    assert (cli.cost_basis, cli.n_cost_unknown) == ("estimate", 0)
    assert cli.cli_estimate_usd_per_1k_decisions == pytest.approx(1000 * 0.4 / 2)


def test_a_failed_call_that_used_tokens_or_proves_nothing_stays_unknown() -> None:
    cases = [request_case(f"r{i}", "DENY") for i in range(2)]
    billed = predict(cases[0], request_output("DENY"), usage=_usage(0.4, "reported"))
    # Used tokens: it may have been billed, so its cost is unknown.
    used = predict(cases[1], status="error", usage=_usage(None, "unknown"))
    usage = score_cases(cases, [billed, used], settings=SETTINGS, reported_cost="billed").usage
    assert (usage.cost_basis, usage.n_cost_unknown, usage.n_free_errors) == ("incomplete", 1, 0)
    # A CLI that reports no cost at all: a free error is no evidence of an estimate.
    silent = predict(cases[0], request_output("DENY"), usage=_usage(None, "unknown"))
    codex = score_cases(cases, [silent, _free_error(cases[1])], settings=SETTINGS).usage
    assert (codex.cost_basis, codex.n_cost_unknown, codex.n_free_errors) == ("unknown", 1, 1)
    assert codex.cli_estimate_usd_per_1k_decisions is None
    only = score_cases(cases[:1], [_free_error(cases[0])], settings=SETTINGS).usage
    assert only.cost_basis == "unknown" and only.api_usd_per_1k_decisions is None


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
