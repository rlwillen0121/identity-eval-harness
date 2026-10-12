"""Cost labels end to end: a run whose manifest records a subscription CLI, an OpenRouter
model, a metered API and a self-hosted model is scored from its files and reported, and each
configuration's headline API cost and its label come out right (SPEC §11.4, §15)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from identitybench.report import build_document, load_report_run
from identitybench.report.common import (
    API_COST_TAG_NOTES,
    api_cost_cell,
    cost_basis_title,
    cost_source_title,
    headline_api_usd,
)
from identitybench.report.document import Section, Table, Text
from identitybench.report.leaderboard import leaderboard_table
from identitybench.schemas.enums import Split
from identitybench.schemas.predictions import CostSource, Prediction
from identitybench.scoring.summary import RunScores, score_run
from identitybench.scoring.usage import UsageScores
from tests.report.builders import (
    N_BOOT,
    RunSpec,
    baseline_predictions,
    main_cases,
    model_predictions,
    write_dataset,
    write_run,
)

OPENROUTER = {
    "provider": "openai_compatible",
    "base_url_env": "OPENROUTER_BASE_URL",
    "cost_mode": "reported",
}
ENTRIES = {
    "model_a_cli": {"provider": "claude_cli", "base_url_env": None, "cost_mode": "reported"},
    "model_a_router": OPENROUTER,
    "model_a_gap": OPENROUTER,
    "model_a_paid": {"provider": "openai_compatible", "base_url_env": None, "cost_mode": "metered"},
    "model_a_local": {
        "provider": "openai_compatible",
        "base_url_env": "VLLM_BASE_URL",
        "cost_mode": "unmetered",
    },
}
SOURCES: dict[str, CostSource] = {
    "model_a_cli": "reported",
    "model_a_router": "reported",
    "model_a_gap": "reported",
    "model_a_paid": "metered",
    "model_a_local": "unmetered",
}


def _costed(predictions: list[Prediction], source: CostSource) -> list[Prediction]:
    """``predictions`` with their cost recorded under ``source`` (a real $0 when unmetered);
    a ``model_a_gap`` call to ``r02`` came back without a cost, as an OpenRouter call that
    omits ``usage.cost`` is recorded."""
    out: list[Prediction] = []
    for p in predictions:
        cost = 0.0 if source == "unmetered" else p.usage.cost_usd
        src: CostSource = source
        if p.config_id == "model_a_gap" and p.case_id == "r02":
            cost, src = None, "unknown"
        out.append(
            p.model_copy(
                update={"usage": p.usage.model_copy(update={"cost_usd": cost, "cost_source": src})}
            )
        )
    return out


@pytest.fixture(scope="module")
def cost_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, RunScores]:
    root = tmp_path_factory.mktemp("cost-run")
    cases = main_cases()
    dataset = write_dataset(root, cases)
    test = [c for c in cases if c.split is Split.test]
    predictions = [
        prediction
        for config_id, source in SOURCES.items()
        for prediction in _costed(model_predictions(test, config_id, "run_cost"), source)
    ]
    baselines = [
        p for p in baseline_predictions(test, "run_cost") if p.config_id == "always_escalate"
    ]
    run_dir = write_run(
        root,
        dataset,
        cases,
        RunSpec(
            "run_cost",
            Split.test,
            [*predictions, *baselines],
            models=dict.fromkeys(ENTRIES, "context"),
            entries=ENTRIES,
        ),
    )
    return run_dir, score_run(run_dir, n_boot=N_BOOT)


def test_each_configuration_scores_its_own_cost_basis(cost_run: tuple[Path, RunScores]) -> None:
    _, scores = cost_run
    usage = {name: config.usage for name, config in scores.configs.items()}
    basis = {name: (u.reported_cost, u.cost_basis) for name, u in usage.items()}
    assert basis == {
        "always_escalate": ("unverified", "unmetered"),
        "model_a_cli": ("estimate", "estimate"),
        "model_a_gap": ("billed", "incomplete"),
        "model_a_local": ("unverified", "unmetered"),
        "model_a_paid": ("unverified", "metered"),
        "model_a_router": ("billed", "billed"),
    }
    cli, router = usage["model_a_cli"], usage["model_a_router"]
    # The same recorded costs: an API figure for the gateway, only an estimate for the CLI.
    assert router.api_usd_per_1k_decisions is not None and router.api_usd_per_1k_decisions > 0
    assert cli.api_usd_per_1k_decisions is None
    assert cli.cli_estimate_usd_per_1k_decisions == pytest.approx(router.api_usd_per_1k_decisions)
    assert usage["model_a_paid"].api_usd_per_1k_decisions == pytest.approx(
        router.api_usd_per_1k_decisions
    )
    assert usage["model_a_local"].api_usd_per_1k_decisions == 0.0
    gap = usage["model_a_gap"]
    assert gap.api_usd_per_1k_decisions is None and gap.n_cost_unknown == 1


def _usage_section(run_dir: Path) -> Section:
    document = build_document([load_report_run(run_dir)])
    return next(s for s in document.sections if s.key == "usage")


def test_the_usage_section_labels_billed_and_estimated_costs(
    cost_run: tuple[Path, RunScores],
) -> None:
    section = _usage_section(cost_run[0])
    main, sources = (b for b in section.blocks if isinstance(b, Table))
    column = {title: i for i, title in enumerate(main.columns)}
    rows = {row[0]: row for row in main.rows}
    basis = {name: row[column["Cost basis"]] for name, row in rows.items()}
    assert basis["model_a_router"] == "billed (gateway)"
    assert basis["model_a_cli"] == "CLI estimate"
    assert basis["model_a_paid"] == "metered (pricing table)"
    assert basis["model_a_local"] == "unmetered ($0)"
    assert basis["model_a_gap"].startswith("incomplete (1 of ")
    api = column["API $/1k decisions"]
    estimate = column["CLI estimate $/1k"]
    assert rows["model_a_router"][api].startswith("$") and rows["model_a_cli"][api] == "n/a"
    assert rows["model_a_cli"][estimate] == rows["model_a_router"][api]
    assert rows["model_a_gap"][api] == "n/a"
    assert rows["model_a_local"][api] == "$0.00"
    labels = {(row[0], row[1]) for row in sources.rows}
    assert ("model_a_router", "reported: billed (gateway)") in labels
    assert ("model_a_cli", "reported: CLI estimate") in labels
    note = next(b.text for b in section.blocks if isinstance(b, Text))
    assert "billed (the charge a billing gateway" in note
    assert "never shown as cheaper than it is" in note
    assert "Subscription-CLI estimates (reported)" not in note


def _cells(table: str) -> dict[str, str]:
    """The README table's API $/1k cell by Model label."""
    lines = [line for line in table.splitlines() if line.startswith("|")][2:]
    rows = [[c.strip() for c in re.split(r"(?<!\\)\|", line[1:-1])] for line in lines]
    return {row[0].split(" ")[0]: row[-1] for row in rows}


def test_the_readme_column_marks_billed_costs_and_says_why_others_are_na(
    cost_run: tuple[Path, RunScores],
) -> None:
    table = leaderboard_table([cost_run[0]])
    cells = _cells(table)
    assert cells["model-a-router"].endswith(" (billed)")
    assert cells["model-a-paid"].startswith("$") and "(" not in cells["model-a-paid"]
    assert cells["model-a-local"] == "$0.00"
    assert cells["model-a-cli"] == "n/a (CLI estimate)"
    assert cells["model-a-gap"] == "n/a (incomplete)"
    caption = table.splitlines()[0]
    assert "metered (usage × the dated pricing table) or billed by a gateway" in caption
    assert "never a CLI's estimate" in caption
    assert "(billed) = the per-call charges a billing gateway reported" in caption
    assert "n/a (CLI estimate) = the recorded costs are only a subscription CLI's own" in caption
    assert "n/a (incomplete) = some calls' cost is unknown" in caption
    assert "n/a (unknown)" not in caption  # only the tags the table uses are explained
    assert "rows come from more than one run" not in caption  # one run: nothing to say


def _scores(**update: object) -> UsageScores:
    """A configuration's usage with ``update`` applied."""
    base = UsageScores(
        n_predictions=19,
        n_decisions=19,
        n_cache_hits=0,
        input_tokens=0,
        output_tokens=0,
        reasoning_tokens=0,
        by_cost_source={},
        api_usd_per_1k_decisions=None,
        n_latency=0,
        latency_p50_s=None,
        latency_p95_s=None,
        cost_basis="estimate",
    )
    return base.model_copy(update=update)


def test_a_cli_with_unknown_costs_is_labelled_by_its_estimate() -> None:
    usage = _scores(cost_basis="estimate", n_cost_unknown=6)
    assert api_cost_cell(usage) == "n/a (CLI estimate)"
    assert cost_basis_title(usage) == "CLI estimate (6 of 19 costs unknown)"


def test_an_unverified_reported_cost_gets_its_own_label() -> None:
    """A replay of a gateway recording, or another endpoint that reports a cost, is neither a
    known charge nor a CLI's estimate, and says so."""
    usage = _scores(cost_basis="unverified", reported_cost="unverified")
    assert api_cost_cell(usage) == "n/a (unverified source)"
    assert cost_basis_title(usage) == "reported by an unverified source"
    assert cost_source_title("reported", usage) == "reported: unverified source"
    partial = _scores(cost_basis="partial", reported_cost="unverified")
    assert api_cost_cell(partial) == "n/a (partly unverified)"
    assert "CLI" not in cost_basis_title(partial)
    assert "not known to be a billing gateway or a CLI" in API_COST_TAG_NOTES["unverified source"]


def test_a_summary_scored_before_cost_bases_reads_rescore() -> None:
    """Its stored figure was computed over a subset of its predictions and could be too low."""
    stale = _scores(cost_basis=None, api_usd_per_1k_decisions=1.25)
    assert api_cost_cell(stale) == "n/a (re-score)"
    assert headline_api_usd(stale) is None
    assert cost_basis_title(stale) == "not recorded (re-score the run)"
    assert "re-score the run" in API_COST_TAG_NOTES["re-score"]


def test_free_failed_calls_are_named_in_the_cost_basis() -> None:
    usage = _scores(cost_basis="billed", api_usd_per_1k_decisions=2.0, n_free_errors=2)
    assert api_cost_cell(usage) == "$2.00 (billed)"
    assert cost_basis_title(usage) == "billed (gateway); 2 failed calls with no usage at $0"
