"""The README leaderboard: SPEC §15 columns, baselines always shown, NRC order, and an
idempotent update that touches nothing outside its markers."""

from __future__ import annotations

import dataclasses
import os
import re
import stat
from pathlib import Path

import pytest

from identitybench.report import (
    README_END,
    README_START,
    IncomparableRunsError,
    MarkerError,
    leaderboard_rows,
    leaderboard_table,
    load_report_runs,
    update_readme,
)
from identitybench.report.inputs import ReportRun
from identitybench.schemas.enums import Task
from identitybench.scoring.loss import LossMatrix
from identitybench.scoring.robustness import TaskRobustness
from tests.report.builders import BASELINES, Fixture, with_request_robustness

SPEC_COLUMNS = (
    "Model",
    "Prompt",
    "Mode",
    "NRC",
    "Accuracy",
    "WAR",
    "High-risk WAR",
    "Correct automation",
    "Grounded accuracy",
    "Harmful flip rate",
    "API $/1k decisions",
)


def _rows(table: str) -> list[list[str]]:
    """Cells of every table line, split on unescaped pipes."""
    lines = [line for line in table.splitlines() if line.startswith("|")]
    return [[cell.strip() for cell in re.split(r"(?<!\\)\|", line[1:-1])] for line in lines]


def test_the_table_has_the_spec_columns_in_order(runs: Fixture) -> None:
    header, align, *body = _rows(leaderboard_table([runs.main]))
    assert tuple(h.rstrip(" ↑↓★") for h in header) == SPEC_COLUMNS
    assert align[3:] == ["---:"] * 8
    assert len(body) == 5
    assert all(len(row) == len(SPEC_COLUMNS) for row in body)


def test_the_primary_columns_are_marked(runs: Fixture) -> None:
    """SPEC §12.8: the README, the most-read leaderboard, says which columns are the
    predeclared primary metrics and that the rest are exploratory."""
    table = leaderboard_table([runs.main])
    header = _rows(table)[0]
    marked = {h.rstrip(" ↑↓★") for h in header if "★" in h}
    assert marked == {"NRC", "WAR", "Correct automation", "Grounded accuracy"}
    assert "★ = SPEC §12.8 primary metric; other columns are exploratory" in table.splitlines()[0]


def test_a_harmful_flip_rate_on_few_families_is_flagged(runs: Fixture) -> None:
    """Pairs are resampled by family, so the flip rate carries its own small-n flag whatever
    the row's base-case count is."""
    (run,) = load_report_runs([runs.main], context=False)
    flips = SPEC_COLUMNS.index("Harmful flip rate")
    body = _rows(leaderboard_table([run]))[2:]
    assert all(row[flips] == "0.0% (no interval: 1 request twin pair) †" for row in body)
    rows = leaderboard_rows([run], Task.access_request)
    assert all(row.harmful_flip_small_n for row in rows)
    caption = leaderboard_table([run]).splitlines()[0]
    assert (
        "after a harmful flip rate, the twin families with an adversarial pair where the "
        "oracle's decision isn't APPROVE)"
    ) in caption
    assert "gold" not in caption
    many = run.scores.settings.model_copy(update={"small_n": 1})
    relaxed = dataclasses.replace(run, scores=run.scores.model_copy(update={"settings": many}))
    body = _rows(leaderboard_table([relaxed]))[2:]
    assert all(not row[flips].endswith("†") for row in body)
    reviews = _rows(leaderboard_table([run], task=Task.access_review))[2:]
    assert all(row[flips] == "n/a" for row in reviews)


def test_the_flip_flag_counts_only_families_in_its_denominator(runs: Fixture) -> None:
    """Gold-APPROVE adversarial pairs are resampled but carry nothing of the harmful flip rate:
    40 families of which 28 bear on it are flagged at the default threshold of 30, on the row
    and in the README cell; 30 such families are not."""
    (run,) = load_report_runs([runs.main], context=False)
    flips = SPEC_COLUMNS.index("Harmful flip rate")

    def widened(denominator: float) -> ReportRun:
        def edit(t: TaskRobustness) -> TaskRobustness:
            m = t.harmful_flip_rate.model_copy(
                update={"n_clusters": 40, "denominator": denominator, "low": 0.0, "high": 0.1}
            )
            return t.model_copy(update={"harmful_flip_rate": m})

        return with_request_robustness(run, "model_a", edit)

    for denominator, flagged in ((28.0, True), (30.0, False)):
        edited = widened(denominator)
        (row,) = [
            r
            for r in leaderboard_rows([edited], Task.access_request)
            if r.ref.config_id == "model_a"
        ]
        assert row.harmful_flip_small_n is flagged
        (cells,) = [c for c in _rows(leaderboard_table([edited]))[2:] if c[0].startswith("model-a")]
        assert cells[flips].endswith("†") is flagged


def test_baselines_are_always_shown_and_rows_follow_nrc(runs: Fixture) -> None:
    table = leaderboard_table([runs.main])
    body = _rows(table)[2:]
    models = [row[0] for row in body]
    for baseline in BASELINES:
        assert any(m.startswith(baseline.replace("_", "-")) and "(baseline)" in m for m in models)
    nrcs = [float(row[3].split()[0]) for row in body]
    assert nrcs[:-1] == sorted(nrcs[:-1])
    assert "always-escalate (baseline)" in models[nrcs.index(1.0)]


def test_the_oracle_is_listed_last_as_the_unranked_ceiling(runs: Fixture) -> None:
    body = _rows(leaderboard_table([runs.main]))[2:]
    nrcs = [float(row[3].split()[0]) for row in body]
    # The oracle has the lowest NRC of all, yet it is a pipeline check, not a rank.
    assert nrcs[-1] == min(nrcs)
    assert body[-1][0] == "oracle (baseline) · ceiling (not ranked) †"
    assert all("ceiling" not in row[0] for row in body[:-1])
    caption = leaderboard_table([runs.main]).splitlines()[0]
    assert "ceiling (not ranked) = the oracle baseline, which answers with the" in caption


def test_the_caption_says_what_the_numbers_are(runs: Fixture) -> None:
    caption = leaderboard_table([runs.main]).splitlines()[0]
    assert caption.startswith("Access requests · toy v1 (sha256 ")
    assert "test split · n = 8 cases per row, the base cases scored" in caption
    assert "WAR counts cases where the oracle's decision isn't APPROVE" in caption
    assert "harmful flip rate adversarial pairs where the oracle's decision isn't" in caption
    assert "API $/1k decisions = API cost per 1,000" in caption
    assert "never a CLI's estimate" in caption
    assert "over every task and repeat, not this task alone" in caption
    assert "gold" not in caption
    assert "no interval = fewer than two resampled units" in caption
    reviews = leaderboard_table([runs.main], task=Task.access_review).splitlines()[0]
    assert "n = 2 campaigns (7 items) per row" in reviews
    assert "WAR counts items where the oracle's decision isn't CERTIFY" in reviews
    # The † clause speaks the review table's words too, and never says "gold".
    assert "with an adversarial pair where the oracle's decision isn't CERTIFY)" in reviews
    assert "approve" not in reviews.lower() and "gold" not in reviews
    assert "n/a = undefined" in caption
    assert "fewer than 30 resampled units" in caption
    assert "CP95 ≤ x, k/n = exact one-sided 95% bound" in caption
    assert caption.endswith(".")


def test_the_caption_explains_collapsed_intervals(runs: Fixture) -> None:
    table = leaderboard_table([runs.main], task=Task.access_review)
    war = SPEC_COLUMNS.index("WAR")
    escalate = next(row for row in _rows(table)[2:] if row[0].startswith("always-escalate"))
    assert escalate[war] == "0.0% (0.0–0.0)"
    assert "a collapsed bootstrap interval (no resample saw an event" in table.splitlines()[0]


def test_undefined_cells_read_na(runs: Fixture) -> None:
    body = _rows(leaderboard_table([runs.main], task=Task.access_review))[2:]
    grounded = SPEC_COLUMNS.index("Grounded accuracy")
    flips = SPEC_COLUMNS.index("Harmful flip rate")
    assert all(row[grounded] == "n/a" and row[flips] == "n/a" for row in body)


def test_request_war_carries_its_exact_bound(runs: Fixture) -> None:
    body = _rows(leaderboard_table([runs.main]))[2:]
    war = SPEC_COLUMNS.index("WAR")
    model_a = next(row for row in body if row[0].startswith("model-a"))
    assert "(CP95 ≤ " in model_a[war] and ", 1/5)" in model_a[war]


def test_jml_has_no_leaderboard(runs: Fixture) -> None:
    with pytest.raises(ValueError, match="JML"):
        leaderboard_table([runs.main], task=Task.jml)


def test_several_runs_are_disambiguated_and_incomplete_runs_marked(runs: Fixture) -> None:
    assert runs.incomplete is not None and runs.tools is not None
    table = leaderboard_table([runs.main, runs.incomplete])
    models = [row[0] for row in _rows(table)[2:]]
    assert "model-a · run_main †" in models
    assert "model-a · run_partial ‡ †" in models
    assert "‡ = incomplete run" in table.splitlines()[0]


def test_a_table_combining_runs_says_their_order_is_untested(runs: Fixture) -> None:
    """SPEC §12.8 compares configurations only within one run: a caption over rows from
    several runs says so, and a single run's caption says nothing about it."""
    assert runs.incomplete is not None
    main, partial = load_report_runs([runs.main, runs.incomplete], context=False)
    caption = leaderboard_table([main, partial]).splitlines()[0]
    assert (
        "rows come from more than one run: configurations from different runs were not "
        "compared (SPEC §12.8), so their relative order is untested"
    ) in caption
    assert "more than one run" not in leaderboard_table([main]).splitlines()[0]


def test_runs_on_different_cases_are_refused_unless_mixing_is_allowed(runs: Fixture) -> None:
    (main,) = load_report_runs([runs.main], context=False)
    assert main.manifest is not None
    smoke = dataclasses.replace(
        main,
        scores=main.scores.model_copy(update={"run_id": "run_smoke"}),
        manifest=main.manifest.model_copy(update={"run_id": "run_smoke", "sample": 0.1}),
    )
    with pytest.raises(IncomparableRunsError, match="samples differ"):
        leaderboard_table([main, smoke])
    caption = leaderboard_table([main, smoke], allow_mixed=True).splitlines()[0]
    assert "Warning: rows from different runs were not measured on the same cases" in caption
    fewer = dataclasses.replace(
        main,
        scores=main.scores.model_copy(update={"run_id": "run_fewer"}),
        manifest=None,
        records=tuple(r for r in main.records if r.case_id != "r01"),
    )
    with pytest.raises(IncomparableRunsError, match="different case IDs"):
        leaderboard_table([main, fewer])


def test_runs_under_different_settings_are_refused_unless_mixing_is_allowed(
    runs: Fixture,
) -> None:
    assert runs.incomplete is not None
    main, partial = load_report_runs([runs.main, runs.incomplete], context=False)
    assert leaderboard_table([main, partial])
    settings = partial.scores.settings.model_copy(
        update={"loss": LossMatrix(routing=2.0), "alpha": 0.1}
    )
    other = dataclasses.replace(
        partial, scores=partial.scores.model_copy(update={"settings": settings})
    )
    with pytest.raises(IncomparableRunsError, match="loss matrices differ") as error:
        leaderboard_table([main, other])
    assert "default except routing 2" in str(error.value)
    assert "interval levels differ (run_main: 95%; run_partial: 90%)" in str(error.value)
    caption = leaderboard_table([main, other], allow_mixed=True).splitlines()[0]
    assert "Warning: rows from different runs were scored under different settings" in caption
    assert "loss matrices differ (run_main: SPEC §12.3 default; run_partial: default" in caption
    assert "not measured on the same cases" not in caption
    # A grid or a WAR target that differs changes nothing a README row shows.
    grid = partial.scores.settings.model_copy(update={"routing_costs": (1.0,), "war_target": 0.05})
    regridded = dataclasses.replace(
        partial, scores=partial.scores.model_copy(update={"settings": grid})
    )
    assert "Warning" not in leaderboard_table([main, regridded]).splitlines()[0]


def test_mixed_sizes_give_each_row_its_n(runs: Fixture) -> None:
    (main,) = load_report_runs([runs.main], context=False)
    configs = {
        config_id: scores.model_copy(
            update={"requests": scores.requests.model_copy(update={"n_cases": 30})}
        )
        for config_id, scores in main.scores.configs.items()
        if scores.requests is not None
    }
    larger = dataclasses.replace(
        main,
        scores=main.scores.model_copy(update={"run_id": "run_larger", "configs": configs}),
        manifest=None,
    )
    with pytest.raises(IncomparableRunsError, match="base access requests scored differ"):
        leaderboard_table([main, larger])
    table = leaderboard_table([main, larger], allow_mixed=True)
    models = [row[0] for row in _rows(table)[2:]]
    assert "model-a · run_main (n = 8 cases) †" in models
    assert "model-a · run_larger (n = 30 cases) †" in models
    assert "differs between rows and follows each Model name" in table.splitlines()[0]


def test_loaded_runs_and_directories_give_the_same_table(runs: Fixture) -> None:
    loaded = load_report_runs([runs.main], context=False)
    assert leaderboard_table(loaded) == leaderboard_table([runs.main])
    rows = leaderboard_rows(loaded, Task.access_request)
    assert rows[0].ref.config_id == "model_b"
    assert rows[-1].ref.config_id == "oracle"


README = f"""# Project

Intro text that must survive.

{README_START}
old table
{README_END}

## Next section
Trailing text.
"""


def test_update_readme_replaces_only_the_block_and_is_idempotent(
    runs: Fixture, tmp_path: Path
) -> None:
    path = tmp_path / "README.md"
    path.write_text(README, encoding="utf-8")
    table = leaderboard_table([runs.main])
    assert update_readme(path, table) is True
    first = path.read_bytes()
    text = first.decode("utf-8")
    before, rest = text.split(README_START)
    block, after = rest.split(README_END)
    assert before == README.split(README_START)[0]
    assert after == README.split(README_END)[1]
    assert block == "\n" + table
    assert "old table" not in text
    assert update_readme(path, table) is False
    assert path.read_bytes() == first


def test_update_readme_keeps_line_endings_and_permissions(runs: Fixture, tmp_path: Path) -> None:
    path = tmp_path / "README.md"
    path.write_bytes(README.replace("\n", "\r\n").encode("utf-8"))
    os.chmod(path, 0o644)
    update_readme(path, leaderboard_table([runs.main]))
    data = path.read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("# No markers here\n", "exactly one"),
        (f"{README_START}\n{README_END}\n{README_START}\n{README_END}\n", "exactly one"),
        (f"{README_END}\nx\n{README_START}\n", "comes before"),
        (f"{README_START}\nonly a start\n", "exactly one"),
    ],
)
def test_update_readme_refuses_bad_markers_and_leaves_the_file(
    tmp_path: Path, content: str, message: str
) -> None:
    path = tmp_path / "README.md"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(MarkerError, match=message):
        update_readme(path, "| table |\n")
    assert path.read_text(encoding="utf-8") == content
    assert sorted(p.name for p in tmp_path.iterdir()) == ["README.md"]


def test_a_table_without_baselines_says_so(runs: Fixture) -> None:
    (run,) = load_report_runs([runs.main], context=False)
    models = {k: v for k, v in run.scores.configs.items() if k.startswith("model")}
    info = {k: v for k, v in run.scores.config_info.items() if k in models}
    scores = run.scores.model_copy(update={"configs": models, "config_info": info})
    table = leaderboard_table([dataclasses.replace(run, scores=scores)])
    assert "no SPEC §14 baseline was scored" in table.splitlines()[0]
    assert "no SPEC §14 baseline" not in leaderboard_table([run])
