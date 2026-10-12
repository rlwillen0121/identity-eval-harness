"""API-equivalent cost: a subscription CLI's recorded tokens at the model's dated list price,
kept apart from API cost on every surface (SPEC §11.4, §15)."""

from __future__ import annotations

import dataclasses
import re
from datetime import date
from pathlib import Path

import pytest

from identitybench.report import build_document, load_report_run, readme_leaderboard
from identitybench.report.api_equivalent import (
    ApiEquivalent,
    api_equivalent,
    api_equivalents,
    prompt_gap,
    read_keyed_usages,
    read_usages,
)
from identitybench.report.common import config_refs, decision_cost, equivalent_notes
from identitybench.report.document import Table
from identitybench.report.safety_bar import SafetyBar
from identitybench.runner.cost import ModelPrice, PricingTable
from identitybench.schemas.enums import Split
from identitybench.schemas.predictions import PredictionUsage
from identitybench.scoring.summary import score_run
from tests.report.builders import (
    N_BOOT,
    RunSpec,
    baseline_predictions,
    main_cases,
    model_predictions,
    write_dataset,
    write_run,
)

M = 1_000_000

PRICE = ModelPrice.model_validate(
    {
        "input": 4.0,
        "output": 20.0,
        "cache_read": 0.2,
        "cache_write_5m": 5.0,
        "cache_write_1h": 8.0,
        "long_context": {"threshold_tokens": 100_000, "input": 8.0, "output": 40.0},
        "as_of": "2026-10-12",
        "source": "test catalog, vendor/model",
    }
)


def _usage(**counts: int | float | str | None) -> PredictionUsage:
    return PredictionUsage.model_validate(counts)


def _priced(usages: list[PredictionUsage], provider: str = "claude_cli") -> ApiEquivalent:
    return api_equivalent(
        usages, PRICE, model="m", provider=provider, n_decisions=2000, table_sha256="ab" * 32
    )


# --------------------------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------------------------


def test_every_token_kind_is_priced_at_the_row_and_the_clis_cache_lifetime() -> None:
    usage = _usage(
        input_tokens=1_000,
        cache_read_tokens=10_000,
        cache_write_tokens=20_000,
        output_tokens=3_000,
        reasoning_tokens=2_000,  # inside output_tokens: never priced twice
    )
    claude = _priced([usage, usage])
    per_call_1h = (1_000 * 4 + 10_000 * 0.2 + 20_000 * 8 + 3_000 * 20) / M
    assert claude.cache_ttl == "1h" and claude.cache_write_rate == 8.0
    assert claude.total_usd == pytest.approx(2 * per_call_1h)
    assert claude.cache_write_usd == pytest.approx(2 * 20_000 * 8 / M)
    assert claude.usd_per_1k_decisions == pytest.approx(1000 * 2 * per_call_1h / 2000)
    assert claude.cache_write_share == pytest.approx(claude.cache_write_usd / claude.total_usd)
    # Other CLIs write at the 5-minute rate.
    codex = _priced([usage], provider="codex_cli")
    assert codex.cache_ttl == "5m"
    assert codex.total_usd == pytest.approx(
        (1_000 * 4 + 10_000 * 0.2 + 20_000 * 5 + 3_000 * 20) / M
    )
    assert (codex.as_of, codex.source) == (date(2026, 10, 12), "test catalog, vendor/model")


def test_a_summed_prompt_past_the_threshold_is_priced_at_base_rates_and_bounded() -> None:
    """A CLI prediction can sum several model requests, so a long-context tier can't be told
    from the sum: base rates, a count, and the tier's upper bound."""
    short = _usage(input_tokens=100_000, output_tokens=1_000)  # at the threshold: exact
    long = _usage(input_tokens=60_000, cache_read_tokens=50_000, output_tokens=1_000)
    priced = _priced([short, long], provider="codex_cli")
    base = (100_000 * 4 + 1_000 * 20 + 60_000 * 4 + 50_000 * 0.2 + 1_000 * 20) / M
    assert priced.total_usd == pytest.approx(base)
    assert priced.n_over_threshold == 1
    tier_long = (60_000 * 8 + 50_000 * 0.8 + 1_000 * 40) / M  # tier cache read: 0.1 x 8
    assert priced.tier_usd == pytest.approx((100_000 * 4 + 1_000 * 20) / M + tier_long)
    tier, base_per_1k = priced.tier_usd_per_1k_decisions, priced.usd_per_1k_decisions
    assert tier is not None and base_per_1k is not None and tier > base_per_1k
    assert _priced([short]).tier_usd is None


def test_the_clis_own_estimate_is_kept_only_when_every_prediction_has_one() -> None:
    usage = _usage(input_tokens=1_000, output_tokens=100)
    cost = (1_000 * 4 + 100 * 20) / M
    reported = _usage(input_tokens=1_000, output_tokens=100, cost_usd=cost, cost_source="reported")
    agreed = _priced([reported, reported])
    assert agreed.cli_estimate_usd == pytest.approx(2 * cost)
    assert agreed.estimate_gap == pytest.approx(0.0, abs=1e-12)
    assert _priced([reported, usage]).cli_estimate_usd is None
    assert _priced([]).cli_estimate_usd is None and _priced([]).usd_per_1k_decisions == 0.0


def test_read_usages_skips_a_torn_tail_and_refuses_a_corrupt_line(tmp_path: Path) -> None:
    path = tmp_path / "predictions.jsonl"
    path.write_text(
        '{"config_id": "a", "usage": {"input_tokens": 5}, "status": "ok"}\n'
        "\n"
        '{"config_id": "b", "usage": {"output_tokens": 7}}\n'
        '{"config_id": "a", "usage": {"input_t',
        encoding="utf-8",
    )
    usages = read_usages(path, ["a", "c"])
    assert usages == {"a": [_usage(input_tokens=5)], "c": []}
    keyed = read_keyed_usages(path, ["b"])
    assert keyed == {"b": [(("", 0), _usage(output_tokens=7))]}
    path.write_text('{"config_id": "a"\n{"config_id": "a"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"predictions\.jsonl:1"):
        read_usages(path, ["a"])


# --------------------------------------------------------------------------------------------
# A run read back from disk
# --------------------------------------------------------------------------------------------

ENTRIES = {
    "model_a_cli": {"provider": "claude_cli", "base_url_env": None, "cost_mode": "reported"},
    "model_a_paid": {"provider": "openai_compatible", "base_url_env": None, "cost_mode": "metered"},
}


@pytest.fixture(scope="module")
def cli_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A run with a subscription-CLI configuration (its costs recorded as the CLI's estimate)
    and a metered one, scored from its files."""
    root = tmp_path_factory.mktemp("cli-run")
    cases = main_cases()
    dataset = write_dataset(root, cases)
    test = [c for c in cases if c.split is Split.test]
    predictions = []
    for config_id in ENTRIES:
        source = "reported" if config_id == "model_a_cli" else "metered"
        for p in model_predictions(test, config_id, "run_cli"):
            usage = p.usage.model_copy(update={"cost_source": source, "cache_write_tokens": 500})
            predictions.append(p.model_copy(update={"usage": usage}))
    baselines = [
        p for p in baseline_predictions(test, "run_cli") if p.config_id == "always_escalate"
    ]
    run_dir = write_run(
        root,
        dataset,
        cases,
        RunSpec(
            "run_cli",
            Split.test,
            [*predictions, *baselines],
            models=dict.fromkeys(ENTRIES, "context"),
            entries=ENTRIES,
        ),
    )
    score_run(run_dir, n_boot=N_BOOT)
    return run_dir


def _table(*models: str) -> PricingTable:
    row = PRICE.model_dump(mode="json")
    return PricingTable.model_validate({"models": dict.fromkeys(models, row)})


def test_a_loaded_run_prices_only_its_cli_configurations(cli_run: Path) -> None:
    table = _table("model_a_cli-weights", "model_a_paid-weights")
    run = load_report_run(cli_run, context=False, pricing=table)
    assert set(run.api_equivalent) == {"model_a_cli"}  # the metered model keeps its charge
    equivalent = run.api_equivalent["model_a_cli"]
    usage = run.scores.configs["model_a_cli"].usage
    assert equivalent.n_predictions == usage.n_predictions
    assert equivalent.n_decisions == usage.n_decisions
    assert equivalent.cache_ttl == "1h" and equivalent.table_sha256 == table.sha256
    # Priced from the predictions themselves when they are loaded: the same figure.
    full = load_report_run(cli_run, pricing=table)
    assert full.predictions and full.api_equivalent == run.api_equivalent
    tokens = [p.usage for p in full.predictions["model_a_cli"]]
    expected = sum(
        (u.input_tokens * 4 + u.output_tokens * 20 + u.cache_read_tokens * 0.2)
        + u.cache_write_tokens * 8
        for u in tokens
    )
    assert equivalent.total_usd == pytest.approx(expected / M)
    # No row for the model, or a scorer count the predictions don't match: no figure.
    assert load_report_run(cli_run, context=False, pricing=_table("other")).api_equivalent == {}
    shorter = {"model_a_cli": tokens[:-1]}
    assert api_equivalents(run.scores, shorter, table) == {}
    # The CLI's prompt is compared with the run's API configuration on the same predictions:
    # here both recorded the same tokens, so no overhead.
    assert equivalent.prompt_tokens == sum(
        u.input_tokens + u.cache_read_tokens + u.cache_write_tokens for u in tokens
    )
    assert equivalent.prompt_gap == (0.0, 0.0) and equivalent.n_peers == 1


def test_the_clis_own_prompt_is_estimated_against_the_api_models() -> None:
    cli = {("c1", 0): 9_000, ("c2", 0): 10_000, ("c3", 0): 12_000, ("c4", 0): 0}
    near = {("c1", 0): 3_000, ("c2", 0): 3_500, ("c3", 0): 6_000, ("c4", 0): 4_000}
    far = {("c1", 0): 1_000, ("c2", 0): 2_000, ("c9", 0): 5_000}
    # Per peer, the median paired difference over predictions both recorded a prompt for.
    assert prompt_gap(cli, [near, far]) == ((6_000.0, 8_000.0), 2)
    assert prompt_gap(cli, [{("c9", 0): 1}]) is None
    assert prompt_gap(cli, []) is None


def test_the_equivalent_is_labelled_and_never_bolded_or_mixed_into_api_cost(
    cli_run: Path,
) -> None:
    table = _table("model_a_cli-weights")
    run = load_report_run(cli_run, context=False, pricing=table)
    refs = {ref.config_id: ref for ref in config_refs([run])}
    cost = decision_cost(refs["model_a_cli"])
    assert cost.kind == "equivalent" and cost.priced and cost.label == "API-equivalent"
    assert cost.cell().startswith("◇$")
    assert decision_cost(refs["model_a_paid"]).kind == "charge"
    assert decision_cost(refs["always_escalate"]).kind == "none"
    # The scorer's headline is untouched: the CLI's API figure stays n/a.
    assert run.scores.configs["model_a_cli"].usage.api_usd_per_1k_decisions is None

    block = readme_leaderboard([run])
    md = block.markdown
    # The full table marks the equivalent with a footnote letter, never a glyph, and never in
    # bold.
    text = re.search(r"<summary>Full leaderboard table</summary>.*?</details>", md, flags=re.S)
    assert text is not None
    rows = [line for line in text.group(0).splitlines() if line.startswith("| model")]
    marked = [r for r in rows if "<sup>a</sup>" in r]
    assert len(marked) == 1 and "<b>" not in marked[0] and "◇" not in text.group(0)
    assert (
        "<sup>a</sup> API-equivalent: the model's list price × the tokens a subscription CLI "
        "recorded; a price, not a charge." in text.group(0)
    )
    # The thesis graphic gives a cost only to the models that clear the safety bar: with a bar
    # both clear, the equivalent is marked as in the tables, beside the charge, and explained.
    assert "API-equivalent" not in block.charts["readme-leaderboard-light.svg"]
    loose = SafetyBar(war_bound=1.0, unsafe_plans=1.0, harmful_flips=1.0, invalid=1.0)
    figure = readme_leaderboard([run], bar=loose).charts["readme-leaderboard-light.svg"]
    for config_id in ("model_a_cli", "model_a_paid"):
        assert f">{decision_cost(refs[config_id]).cell()}</text>" in figure
    assert "◇ API-equivalent: list price × the tokens used, not a charge." in figure
    assert "◇ marks an API-equivalent: the tokens a subscription CLI recorded" in md

    usage = next(s for s in build_document([run]).sections if s.key == "usage")
    first = next(b for b in usage.blocks if isinstance(b, Table))
    column = first.columns.index("API-equivalent $/1k")
    cells = {row[0]: row[column] for row in first.rows}
    assert cells["model_a_cli"].startswith("◇$") and cells["model_a_paid"] == "n/a"


def test_the_notes_say_where_prices_come_from_and_what_the_figure_cannot_see() -> None:
    usage = _usage(input_tokens=1_000, cache_write_tokens=9_000, output_tokens=100)
    reported = usage.model_copy(
        update={"cost_usd": (1_000 * 4 + 9_000 * 8 + 100 * 20) / M, "cost_source": "reported"}
    )
    claude = _priced([reported])
    codex = _priced([usage], provider="codex_cli")
    over = _priced([_usage(input_tokens=200_000)], provider="codex_cli")
    notes = equivalent_notes([("Claude", claude), ("GPT", codex), ("Big", over)])
    text = " ".join(notes)
    assert notes[0].startswith("◇ API-equivalent: the model's public list price × the tokens")
    assert "in-memory table sha256 abababababab" in text and "source: test catalog" in text
    assert "Claude CLI rows price cache writes at the 1-hour rate the CLI uses (2× the input" in (
        text
    )
    assert "the API. Each matches the CLI's own cost estimate to the cent." in text
    assert "The Codex CLI reports no cost of its own to check its rows' figures against" in text
    assert "1 prediction of Big sum the CLI's internal turns past the model's long-context" in text
    assert notes[-1].startswith("A CLI's figure can only count the tokens the CLI reports")
    assert equivalent_notes([]) == []
    drifted = dataclasses.replace(claude, cli_estimate_usd=claude.total_usd * 1.1)
    assert "They differ from the CLI's own estimates by up to 9.1%" in " ".join(
        equivalent_notes([("Claude", drifted)])
    )
    # The CLI's own prompt overhead, estimated against the run's API models, is quantified.
    assert "fixed instructions sent with every call" in text  # no estimate: said in words
    luna = dataclasses.replace(codex, prompt_gap=(5_642.0, 8_236.0), n_peers=10)
    sol = dataclasses.replace(
        codex, prompt_gap=(6_330.0, 8_930.0), n_peers=10, prompt_tokens=25_000
    )
    luna = dataclasses.replace(luna, prompt_tokens=25_000)
    quantified = equivalent_notes([("Luna", luna), ("Sol", sol)])
    assert "The Codex CLI reports no cost of its own to check its rows' figures against." in (
        quantified
    )
    assert (
        "Codex CLI rows record a median 5.6k–8.9k more prompt tokens per prediction than the "
        "run's 10 API models on the same predictions, 23–36% of their prompt. That is the CLI's "
        "own instructions and any internal turns, which a direct API call wouldn't send, so "
        "these figures run high by up to about that share of their prompt cost. Each model "
        "counts tokens with its own tokenizer, so the gaps are estimates." in quantified
    )
    assert "fixed instructions sent with every call" not in " ".join(quantified)
    # Two CLIs' overheads in one note, what the overhead is said once.
    opus = dataclasses.replace(
        claude, prompt_gap=(4_300.0, 7_100.0), n_peers=10, prompt_tokens=25_000
    )
    both = " ".join(equivalent_notes([("Opus", opus), ("Luna", luna), ("Sol", sol)]))
    assert (
        "Per prediction, the CLI rows record more prompt tokens than the run's 10 API models on "
        "the same predictions, by a median of 4.3k–7.1k for the Claude CLI rows (" in both
    )
    assert "and 5.6k–8.9k for the Codex CLI rows (23–36% of their prompt)." in both
    assert both.count("the CLI's own instructions and any internal turns") == 1
    big = dataclasses.replace(sol, prompt_gap=(22_662.0, 24_963.0), prompt_tokens=45_000)
    assert "(most for Big, 22.7k–25.0k)" in " ".join(
        equivalent_notes([("Luna", luna), ("Big", big)])
    )
    assert "more prompt tokens" not in " ".join(
        equivalent_notes([("Luna", dataclasses.replace(luna, prompt_gap=(-10.0, 5.0)))])
    )
