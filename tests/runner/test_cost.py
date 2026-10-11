from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from identitybench.providers.base import Usage
from identitybench.runner.config import ExperimentConfig, load_config
from identitybench.runner.cost import (
    PRICING_ENV,
    SOURCE_PRICING_PATH,
    BudgetGuard,
    CostTracker,
    PricingTable,
    cost_usd,
    default_cost_mode,
    default_pricing_path,
    load_pricing,
    reports_cost,
)
from identitybench.runner.manifest import CostSummary
from tests.runner._spec import spec_example

M = 1_000_000


def test_repo_pricing_file_rows() -> None:
    table = load_pricing(SOURCE_PRICING_PATH)
    expected = {
        "claude-fable-5-1": (10.0, 50.0, 0.25),
        "claude-opus-5-5": (4.0, 20.0, 0.20),
        "claude-sonnet-5-5": (2.0, 10.0, 0.20),
        "claude-haiku-5-5": (0.10, 0.50, 0.01),
        "claude-opus-5": (5.0, 25.0, 0.50),
        "claude-sonnet-5": (2.0, 10.0, 0.20),
    }
    assert set(table.models) == set(expected)
    for model, (inp, out, read) in expected.items():
        price = table.models[model]
        assert (price.input, price.output) == (inp, out)
        assert price.cache_read_rate == pytest.approx(read)
        assert price.cache_write_rate("5m") == pytest.approx(1.25 * inp)
        assert price.cache_write_rate("1h") == pytest.approx(2.0 * inp)
        assert price.as_of == date(2026, 10, 6)
        assert "Anthropic" in price.source


def test_plain_input_output_cost() -> None:
    usage = Usage(input_tokens=M, output_tokens=M)
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(24.0)
    assert cost_usd("claude-sonnet-5-5", Usage(input_tokens=1000, output_tokens=500)) == (
        pytest.approx((1000 * 2 + 500 * 10) / M)
    )


def test_cache_tokens_cost() -> None:
    usage = Usage(
        input_tokens=1000,
        output_tokens=2000,
        cache_read_tokens=50_000,
        cache_write_tokens=4000,
        reasoning_tokens=1500,
    )
    expected = (1000 * 4 + 2000 * 20 + 50_000 * 0.20 + 4000 * 5.0) / M
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(expected)
    expected_1h = (1000 * 4 + 2000 * 20 + 50_000 * 0.20 + 4000 * 8.0) / M
    assert cost_usd("claude-opus-5-5", usage, cache_ttl="1h") == pytest.approx(expected_1h)


def test_reasoning_tokens_not_double_counted() -> None:
    a = Usage(output_tokens=1000)
    b = Usage(output_tokens=1000, reasoning_tokens=800)
    assert cost_usd("claude-opus-5-5", a) == cost_usd("claude-opus-5-5", b)


def test_haiku_long_context_tier() -> None:
    short = Usage(input_tokens=100_000, output_tokens=1000)
    assert cost_usd("claude-haiku-5-5", short) == pytest.approx((100_000 * 0.10 + 1000 * 0.50) / M)
    long = Usage(input_tokens=100_001, output_tokens=1000)
    assert cost_usd("claude-haiku-5-5", long) == pytest.approx((100_001 * 0.50 + 1000 * 2.50) / M)


def test_long_context_threshold_counts_cached_prompt_tokens() -> None:
    usage = Usage(
        input_tokens=10_000, cache_read_tokens=80_000, cache_write_tokens=20_000, output_tokens=100
    )
    # 110K prompt tokens -> long tier for everything; cache rates default off the tier input.
    expected = (10_000 * 0.50 + 80_000 * 0.05 + 20_000 * 0.625 + 100 * 2.50) / M
    assert cost_usd("claude-haiku-5-5", usage) == pytest.approx(expected)


def test_unknown_model_has_no_price() -> None:
    assert cost_usd("qwen3-32b", Usage(input_tokens=1000)) is None


def test_custom_table_with_explicit_rates(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(
        "models:\n"
        "  m:\n"
        "    input: 1\n    output: 2\n    cache_read: 0.5\n"
        "    cache_write_5m: 1.5\n    cache_write_1h: 3\n"
        "    long_context: {threshold_tokens: 10, input: 10, output: 20, cache_read: 1}\n"
        "    as_of: 2026-01-01\n    source: test\n",
        encoding="utf-8",
    )
    table = load_pricing(path)
    usage = Usage(input_tokens=2, output_tokens=1, cache_read_tokens=2, cache_write_tokens=2)
    assert cost_usd("m", usage, table=table) == pytest.approx((2 + 2 + 1 + 3) / M)
    assert cost_usd("m", usage, table=table, cache_ttl="1h") == pytest.approx((2 + 2 + 1 + 6) / M)
    big = Usage(input_tokens=11, output_tokens=1)
    assert cost_usd("m", big, table=table) == pytest.approx((110 + 20) / M)


def test_pricing_rejects_bad_rows() -> None:
    with pytest.raises(ValueError):
        PricingTable.model_validate({"models": {"m": {"input": 1, "output": 2}}})
    with pytest.raises(ValueError):
        PricingTable.model_validate(
            {"models": {"m": {"input": -1, "output": 2, "as_of": "2026-01-01", "source": "s"}}}
        )


def test_pricing_path_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PRICING_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    # No ./pricing.yaml: fall back to the source checkout's file.
    assert default_pricing_path() == SOURCE_PRICING_PATH
    local = tmp_path / "pricing.yaml"
    local.write_text(
        "models:\n  m: {input: 1, output: 2, as_of: 2026-01-01, source: t}\n", encoding="utf-8"
    )
    assert default_pricing_path() == local
    assert set(load_pricing().models) == {"m"}
    override = tmp_path / "other.yaml"
    monkeypatch.setenv(PRICING_ENV, str(override))
    assert default_pricing_path() == override


def test_pricing_path_missing_everywhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PRICING_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("identitybench.runner.cost.SOURCE_PRICING_PATH", tmp_path / "nope.yaml")
    with pytest.raises(FileNotFoundError, match=PRICING_ENV):
        default_pricing_path()


def test_tracker_metered_priced() -> None:
    tracker = CostTracker("claude-opus-5-5")
    assert tracker.priced and tracker.budget_enforceable
    spent = tracker.add(Usage(input_tokens=M, output_tokens=0))
    assert spent == pytest.approx(4.0)
    tracker.add(Usage(input_tokens=0, output_tokens=M), reported_cost_usd=99.0)
    assert tracker.calls == 2
    assert tracker.usage.input_tokens == M and tracker.usage.output_tokens == M
    assert tracker.metered_api_usd == pytest.approx(24.0)
    assert tracker.reported_usd == pytest.approx(99.0)
    # Metered cost wins over a reported cost for the budget when the model is priced.
    assert tracker.spend_usd == pytest.approx(24.0)


def test_tracker_long_context_applied_per_call() -> None:
    tracker = CostTracker("claude-haiku-5-5")
    for _ in range(3):
        tracker.add(Usage(input_tokens=60_000))
    # Summed usage would be 180K (long tier); per call each is 60K (base tier).
    assert tracker.metered_usd == pytest.approx(3 * 60_000 * 0.10 / M)


def test_tracker_metered_unpriced_is_unknown_not_free() -> None:
    api = CostTracker("gpt-5")
    assert not api.priced
    assert not api.budget_enforceable
    for _ in range(3):
        api.add(Usage(input_tokens=100_000, output_tokens=10_000))
    assert api.metered_api_usd is None
    assert api.spend_usd == 0.0
    # A gateway that reports a cost still has it counted.
    api.add(Usage(input_tokens=10), reported_cost_usd=0.5)
    assert api.spend_usd == pytest.approx(0.5)
    assert api.metered_api_usd is None


def test_tracker_unmetered_is_real_zero() -> None:
    local = CostTracker("qwen3-32b", mode="unmetered")
    assert local.budget_enforceable and not local.priced
    local.add(Usage(input_tokens=10_000, output_tokens=10_000))
    assert local.metered_api_usd == 0.0
    assert local.spend_usd == 0.0
    # Even a model name that has a price row is not billed when it is self-hosted.
    named = CostTracker("claude-opus-5-5", mode="unmetered")
    named.add(Usage(input_tokens=M))
    assert (named.metered_api_usd, named.spend_usd) == (0.0, 0.0)


def test_tracker_reported_cli_never_uses_api_rates() -> None:
    cli = CostTracker("claude-opus-5-5", mode="reported")
    assert not cli.priced and cli.budget_enforceable
    assert cli.add(Usage(input_tokens=M, output_tokens=M), reported_cost_usd=0.0) == 0.0
    cli.add(Usage(input_tokens=10), reported_cost_usd=0.5)
    cli.add(Usage(input_tokens=10))
    assert cli.metered_api_usd is None
    assert cli.reported_usd == pytest.approx(0.5)
    assert cli.spend_usd == pytest.approx(0.5)


def test_budget_guard_sums_across_configs() -> None:
    guard = BudgetGuard(10.0)
    a = guard.track("a", "claude-opus-5-5", "metered")
    b = guard.track("b", "claude-sonnet-5-5", "metered")
    a.add(Usage(input_tokens=M))  # $4
    b.add(Usage(input_tokens=M))  # $2
    assert guard.spend_usd == pytest.approx(6.0)
    assert not guard.would_exceed()
    assert not guard.would_exceed(pending_usd=4.0)
    assert guard.would_exceed(pending_usd=4.01)
    a.add(Usage(output_tokens=M // 4))  # $5 -> $11 total, though each config is under $10
    assert a.spend_usd < 10 and b.spend_usd < 10
    assert guard.would_exceed()
    assert [cid for cid, _ in guard] == ["a", "b"]
    with pytest.raises(ValueError, match="already tracked"):
        guard.track("a", "m", "metered")


def test_budget_guard_without_budget() -> None:
    guard = BudgetGuard(None)
    guard.track("x", "gpt-5", "metered").add(Usage(input_tokens=M))
    assert not guard.would_exceed(pending_usd=1e9)
    assert guard.unenforceable() == []


def test_spec_example_costs(tmp_path: Path) -> None:
    """SPEC §16's example, verbatim: the self-hosted model is a real $0, and only the
    unpriced OpenAI model leaves the $50 budget unenforceable."""
    path = tmp_path / "exp.yaml"
    path.write_text(spec_example(), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.budget_usd == 50
    local = cfg.models[3]
    assert local.cache_namespace == "deploy-1"
    assert local.resolved_cost_mode == "unmetered"
    guard = BudgetGuard(cfg.budget_usd)
    for j, cid in enumerate(cfg.config_ids()):  # matrix order: model-major, then prompt
        entry = cfg.models[j // len(cfg.prompts)]
        tracker = guard.track(cid, entry.model, entry.resolved_cost_mode)
        tracker.add(Usage(input_tokens=1000, output_tokens=100))
    local_ids = [cid for cid in cfg.config_ids() if "served-model-name" in cid]
    assert len(local_ids) == 2
    for cid in local_ids:
        assert guard.trackers[cid].metered_api_usd == 0.0
    assert guard.unenforceable() == [
        cid for cid in cfg.config_ids() if cid.startswith("openai_compatible_openai-model-id")
    ]
    assert cfg.warnings() == []


def test_budget_guard_flags_unpriced_metered_and_silent_clis() -> None:
    cfg = ExperimentConfig.model_validate(
        {
            "name": "x",
            "dataset": "d",
            "tasks": ["access_request"],
            "budget_usd": 50,
            "models": [
                {"provider": "anthropic", "model": "claude-opus-5-5"},
                {"provider": "openai_compatible", "model": "gpt-5", "label": "openai"},
                {"provider": "openai_compatible", "model": "qwen", "cost_mode": "unmetered"},
                {"provider": "claude_cli", "model": "claude-opus-5-5"},
                {"provider": "codex_cli", "model": "gpt-5-codex", "label": "codex"},
            ],
        }
    )
    guard = BudgetGuard(cfg.budget_usd)
    for entry, cid in zip(cfg.models, cfg.config_ids(), strict=True):
        guard.track(cid, entry.model, entry.resolved_cost_mode, reports_cost=entry.reports_cost)
    assert guard.unenforceable() == ["openai__baseline__context", "codex__baseline__context"]
    assert any("codex" in w and "reports no cost" in w for w in cfg.warnings())


def test_reported_tracker_without_costs_becomes_unenforceable() -> None:
    cli = CostTracker("grok-4", mode="reported")
    assert cli.budget_enforceable
    cli.add(Usage(input_tokens=10), reported_cost_usd=0.1)
    assert cli.budget_enforceable
    cli.add(Usage(input_tokens=10))
    assert cli.unreported_calls == 1
    assert not cli.budget_enforceable
    silent = CostTracker("gpt-5-codex", mode="reported", reports_cost=False)
    assert not silent.budget_enforceable


@pytest.mark.parametrize(
    ("kind", "reports"),
    [("claude_cli", True), ("grok_cli", True), ("codex_cli", False), ("cursor_cli", False)],
)
def test_which_clis_report_cost(kind: str, reports: bool) -> None:
    assert reports_cost(kind) is reports


def test_default_cost_mode_cache_namespace_means_self_hosted() -> None:
    assert default_cost_mode("openai_compatible") == "metered"
    assert default_cost_mode("openai_compatible", cache_namespace="d1") == "unmetered"
    assert default_cost_mode("anthropic", cache_namespace="d1") == "metered"


def test_tracker_would_exceed_per_config() -> None:
    tracker = CostTracker("claude-opus-5-5")
    tracker.add(Usage(input_tokens=M))  # $4
    assert not tracker.would_exceed(None, pending_usd=1e9)
    assert not tracker.would_exceed(5.0)
    assert not tracker.would_exceed(5.0, pending_usd=1.0)
    assert tracker.would_exceed(5.0, pending_usd=1.01)
    assert tracker.would_exceed(3.99)


def test_budget_guard_reservations_cover_in_flight_calls() -> None:
    guard = BudgetGuard(10.0)
    tracker = guard.track("a", "claude-opus-5-5", "metered")
    tracker.add(Usage(input_tokens=M))  # $4 spent
    assert guard.reserve(3.0)
    assert guard.reserve(3.0)  # 4 + 6 = 10, still within budget
    assert not guard.reserve(0.01)  # a third concurrent call would overshoot
    assert guard.reserved_usd == pytest.approx(6.0)
    assert guard.would_exceed(0.01)
    # The first call finishes, cheaper than estimated: $1 spent, its $3 released.
    tracker.add(Usage(output_tokens=M // 20))
    guard.release(3.0)
    assert guard.spend_usd == pytest.approx(5.0)
    assert guard.reserve(2.0)
    guard.release(3.0)
    guard.release(2.0)
    assert guard.reserved_usd == 0.0
    with pytest.raises(ValueError):
        guard.reserve(-1.0)
    unlimited = BudgetGuard(None)
    assert unlimited.reserve(1e9)


def test_cost_summary_from_tracker() -> None:
    local = CostTracker("qwen", mode="unmetered")
    assert CostSummary.from_tracker(local) == CostSummary(
        cost_mode="unmetered", metered_api_usd=0.0, reported_usd=None
    )
    cli = CostTracker("m", mode="reported")
    cli.add(Usage(), reported_cost_usd=0.25)
    assert CostSummary.from_tracker(cli).model_dump() == {
        "cost_mode": "reported",
        "metered_api_usd": None,
        "reported_usd": 0.25,
    }
