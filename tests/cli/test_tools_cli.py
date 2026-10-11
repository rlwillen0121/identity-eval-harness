"""The CLI on a tool-use run (``tests.cli.tools_run``): every command that scores or shows a
tool-use answer serves the records its tools returned outside the case view
(``scoring.served.WorldServedIndex`` over the run's dataset), as ``score`` does. Without that,
``compare``, ``judge``, ``judge --validate`` and the judge-label export each fail with
``ServedIndexError`` on such a run. Judges are fakes; nothing reaches a model."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from identitybench.cli import ExitCode
from identitybench.judge.labels import KEY_FILE, SHEET_CSV_COLUMNS, ProbeKind
from identitybench.judge.rubric import DIMENSIONS
from identitybench.judge.run import Judge
from identitybench.judge.targets import build_targets
from identitybench.render.tools import WorldStore, WorldUnavailableError
from identitybench.runner.manifest import dataset_fingerprint
from identitybench.runner.run import PREDICTIONS_FILE, load_predictions
from identitybench.schemas.case import Case
from identitybench.scoring.served import WorldServedIndex
from identitybench.scoring.summary import load_run, score_run
from tests.cli.conftest import Invoke, make_invoker
from tests.cli.sheets import read_csv, write_csv
from tests.cli.tools_run import (
    AGENT_CONFIG,
    AGENT_FAMILY,
    BASELINE,
    SECOND_SEED,
    TOOLS_DATASET,
    make_tools_repo,
    outsider,
    tools_cases,
)
from tests.judge.helpers import FakeJudgeProvider, user_text

FAMILY = ("--family", f"{AGENT_CONFIG}={AGENT_FAMILY}")
LABELS = ("--n", "8", "--n-dev", "4", "--probes", "1", "--allow-reused-sources", *FAMILY)
SERVED_OPEN = "<served_records>"


@pytest.fixture(scope="module")
def tools_template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, tuple[str, ...]]:
    """The repo with both runs, and the run IDs (the first dataset's first)."""
    root = tmp_path_factory.mktemp("tools") / "repo"
    runs = make_tools_repo(root, second=True)
    return root, tuple(run.name for run in runs)


@pytest.fixture
def tools_repo(
    tools_template: tuple[Path, tuple[str, ...]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(tools_template[0], root)
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def run_dirs(tools_template: tuple[Path, tuple[str, ...]], tools_repo: Path) -> tuple[Path, ...]:
    return tuple(tools_repo / "runs" / name for name in tools_template[1])


@pytest.fixture
def run_dir(run_dirs: tuple[Path, ...]) -> Path:
    """The run on the first dataset, which most tests use alone."""
    return run_dirs[0]


@pytest.fixture
def cli(tools_repo: Path) -> Invoke:
    del tools_repo  # requested for its working directory
    return make_invoker()


@pytest.fixture
def panel(monkeypatch: pytest.MonkeyPatch) -> list[FakeJudgeProvider]:
    """Two fake judges of two families, standing in for the judges file."""
    providers = [
        FakeJudgeProvider("anthropic", "claude-judge"),
        FakeJudgeProvider("openai_compatible", "gpt-judge"),
    ]
    monkeypatch.setattr(
        "identitybench.cli.load_judges", lambda _path: [Judge(p) for p in providers]
    )
    return providers


def served_messages(providers: list[FakeJudgeProvider]) -> list[str]:
    return [user_text(r) for p in providers for r in p.requests if SERVED_OPEN in user_text(r)]


def test_the_scripted_run_rests_on_records_only_its_tools_returned(run_dir: Path) -> None:
    """The premise of every test here: each agent answer cites a record outside its view."""
    by_id = {case.case_id: case for case in tools_cases()}
    agent = [p for p in load_predictions(run_dir / PREDICTIONS_FILE) if p.config_id == AGENT_CONFIG]
    assert len(agent) == len(by_id)
    for prediction in agent:
        case = by_id[prediction.case_id]
        assert prediction.tool_trace is not None, prediction.invalid_reason
        assert outsider(case) in prediction.tool_trace.fetched_ids
        assert outsider(case) not in case.view.identities


def test_compare_scores_a_tools_run_as_score_does(cli: Invoke, run_dir: Path) -> None:
    scored = cli("score", run_dir, "--n-boot", "50")
    assert scored.exit_code == 0, scored.output
    out = run_dir.parent / "comparison.json"
    args = ("compare", AGENT_CONFIG, BASELINE, "--run", run_dir, "--n-boot", "50", "--json", out)
    result = cli(*args)
    assert result.exit_code == 0, result.output
    summary = json.loads((run_dir / "scores" / "summary.json").read_text())
    grounding = summary["configs"][AGENT_CONFIG]["requests"]["grounding"]
    [grounded] = [
        c
        for c in json.loads(out.read_text())
        if c["task"] == "access_request" and c["metric"] == "grounded_accuracy"
    ]
    # The served claims are true (no false claim among them), so the agent's ideal answers stay
    # grounded: the figure ``score`` wrote, which a view-only reading of those claims can't give.
    assert grounding["false_claim_rate"]["point"] == 0.0
    assert grounded["point_a"] == grounding["grounded_accuracy"]["point"] == 1.0


def test_judge_shows_the_judges_the_records_the_tools_served(
    cli: Invoke, run_dir: Path, panel: list[FakeJudgeProvider]
) -> None:
    result = cli("judge", run_dir, *FAMILY, "--all-items")
    assert result.exit_code == 0, result.output
    shown = served_messages(panel)
    # Every agent rationale is judged by both judges with the served block; baselines are left
    # out by default and never have one.
    assert shown and len(shown) == sum(len(p.requests) for p in panel)
    names = {outsider(case) for case in tools_cases()}
    assert all(any(name in text for name in names) for text in shown)


def fill_labels(sheet: Path) -> None:
    """Score every row of a label sheet (1-4 per dimension, varied by row)."""
    rows = read_csv(sheet)
    for n, row in enumerate(rows):
        row.update({dim: str((n + i) % 4 + 1) for i, dim in enumerate(DIMENSIONS)})
    write_csv(sheet, rows, SHEET_CSV_COLUMNS)


def test_label_export_writes_the_served_records_each_row_shows(
    cli: Invoke, tools_repo: Path, run_dir: Path
) -> None:
    result = cli("audit", "export", "--judge-labels", "--run", run_dir, *LABELS)
    assert result.exit_code == 0, result.output
    sheets = tools_repo / "audit" / "judge"
    rows = [json.loads(line) for line in (sheets / "dev.jsonl").read_text().splitlines()]
    assert rows and all(row["served_ref"] for row in rows)
    for row in rows:
        served = json.loads((sheets / row["served_ref"]).read_text())
        assert outsider(next(c for c in tools_cases() if c.case_id == row["case_id"])) in (
            json.dumps(served)
        )


def test_a_label_export_of_runs_on_two_worlds_serves_each_row_from_its_own_run(
    cli: Invoke, tools_repo: Path, run_dirs: tuple[Path, ...]
) -> None:
    """Each run's dataset fingerprints only its own world, so a case must be served by the
    index of the run it came from: one index for all would refuse the other run's world."""
    first, second = run_dirs
    args = ("audit", "export", "--judge-labels", "--run", first, "--run", second)
    result = cli(*args, *LABELS, "--n", "12", "--n-dev", "6")
    assert result.exit_code == 0, result.output
    sheets = tools_repo / "audit" / "judge"
    by_id = {case.case_id: case for case in (*tools_cases(), *tools_cases(SECOND_SEED))}
    rows = [
        json.loads(line)
        for name in ("dev.jsonl", "heldout.jsonl")
        for line in (sheets / name).read_text().splitlines()
    ]
    worlds = {by_id[row["case_id"]].world_id for row in rows}
    assert len(worlds) == 2, "the export drew from one run only, so routing went untested"
    for row in rows:
        served = json.loads((sheets / row["served_ref"]).read_text())
        assert outsider(by_id[row["case_id"]]) in json.dumps(served)


def test_a_label_export_short_of_model_rationales_names_the_flag(
    cli: Invoke, run_dir: Path
) -> None:
    result = cli("audit", "export", "--judge-labels", "--run", run_dir, *FAMILY, "--n", "5000")
    assert result.exit_code == ExitCode.INPUT
    message = " ".join(result.stderr.split())
    assert (
        "baseline rationales left out; pass --include-baselines NAME (or all) to label them)"
        in (message)
    )


def agent_rationales(run_dir: Path) -> int:
    """How many rationales the agent gave in ``run_dir``: the rows a label export can draw
    when baselines are left out (one per request and plan, one per review item)."""
    loaded = load_run(run_dir)
    served = WorldServedIndex(loaded.dataset_dir)
    agent = list(loaded.predictions[AGENT_CONFIG])
    return len(
        build_targets(list(loaded.cases), agent, {AGENT_CONFIG: AGENT_FAMILY}, served=served)
    )


def test_label_export_refusals_name_the_flags_never_the_library_keywords(
    cli: Invoke, run_dir: Path
) -> None:
    # One probe of each kind on top of every agent rationale as a real row: each probe's
    # source would then be a held-out row too.
    real = agent_rationales(run_dir)
    rows = str(real + len(ProbeKind))
    base = ("audit", "export", "--judge-labels", "--run", run_dir, *FAMILY, "--n", rows)
    reused = cli("--quiet", *base, "--n-dev", "4", "--probes", "1")
    assert reused.exit_code == ExitCode.INPUT, reused.output
    [line] = reused.stderr.splitlines()
    assert "would reuse held-out rationales as sources" in line
    assert line.endswith("lower the probe counts, or pass --allow-reused-sources")
    over = str(real + 1)
    dev = cli("--quiet", *base, "--n-dev", over, "--probes", "1", "--allow-reused-sources")
    assert dev.exit_code == ExitCode.INPUT, dev.output
    [line] = dev.stderr.splitlines()
    assert line == f"error: --n-dev {over} must be between 0 and the {real} real rows"


def copy_run(run_dir: Path, run_id: str, *, dataset: Path | None = None) -> Path:
    """A copy of ``run_dir`` under ``run_id``, beside it; with ``dataset``, made on that
    dataset instead (the manifest's path and fingerprint point at it)."""
    target = run_dir.parent / run_id
    shutil.copytree(run_dir, target)
    shutil.rmtree(target / "scores", ignore_errors=True)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    manifest["run_id"] = run_id
    if dataset is not None:
        manifest["dataset"] |= {"path": dataset.as_posix(), "sha256": dataset_fingerprint(dataset)}
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    path = target / PREDICTIONS_FILE
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text(
        "".join(json.dumps(json.loads(line) | {"run_id": run_id}) + "\n" for line in lines),
        encoding="utf-8",
    )
    return target


def test_a_label_export_refuses_runs_on_two_versions_of_a_dataset_sharing_case_ids(
    cli: Invoke, tools_repo: Path, run_dir: Path
) -> None:
    """An erratum release keeps its case IDs: one ID would row the earlier run's answers
    against the later run's case, gold and worlds, so the runs must be exported apart. Runs
    on one dataset share their cases and are exported together."""
    erratum = Path("datasets") / "tools-v1.1"
    shutil.copytree(tools_repo / TOOLS_DATASET, tools_repo / erratum)
    (tools_repo / erratum / "ERRATA.md").write_text("A corrected record.\n", encoding="utf-8")
    later = copy_run(run_dir, "run-on-erratum", dataset=erratum)
    args = ("audit", "export", "--judge-labels", *FAMILY, "--n", "12", "--n-dev", "4")
    refused = cli("--quiet", *args, "--run", run_dir, "--run", later)
    assert refused.exit_code == ExitCode.INPUT, refused.output
    [line] = refused.stderr.splitlines()
    assert line.startswith(f"error: runs {run_dir.name} and run-on-erratum were made on different")
    assert line.endswith("export labels for each dataset's runs separately")
    # Both datasets carry one name and version here; their fingerprints tell them apart.
    for data in (TOOLS_DATASET, erratum):
        assert f"sha256 {dataset_fingerprint(tools_repo / data)[:12]}" in line
    assert not (tools_repo / "audit").exists()

    rerun = copy_run(run_dir, "rerun")
    both = cli("--quiet", *args, "--probes", "0", "--run", run_dir, "--run", rerun)
    assert both.exit_code == 0, both.output


def test_compare_scores_each_side_exactly_as_score_does(cli: Invoke, run_dir: Path) -> None:
    """``compare`` scores a side as ``score`` scores it in its run: here a tool-use model,
    which gets its tool metrics, and a baseline, which is scored in context mode."""
    from identitybench.cli import _score_side  # pyright: ignore[reportPrivateUsage]

    scored = score_run(run_dir, n_boot=50, seed=3)
    loaded = load_run(run_dir)
    served = WorldServedIndex(loaded.dataset_dir)
    assert scored.configs[AGENT_CONFIG].tools is not None
    assert scored.configs[BASELINE].tools is None
    for config_id, expected in scored.configs.items():
        side = _score_side(loaded, config_id, served=served, seed=3, n_boot=50)
        assert side.scores.model_dump() == expected.model_dump(), config_id


def test_a_compare_across_runs_on_one_dataset_rebuilds_its_worlds_once(
    cli: Invoke, run_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Path] = []

    class Counted(WorldServedIndex):
        def __init__(self, dataset_dir: Path) -> None:
            built.append(Path(dataset_dir))
            super().__init__(dataset_dir)

    monkeypatch.setattr("identitybench.scoring.served.WorldServedIndex", Counted)
    rerun = copy_run(run_dir, "rerun")
    a, b = f"{run_dir}:{AGENT_CONFIG}", f"{rerun}:{AGENT_CONFIG}"
    result = cli("compare", a, b, "--n-boot", "50")
    assert result.exit_code == 0, result.output
    assert len(built) == 1


def test_judge_validation_on_a_tools_run_serves_the_records_and_writes_beside_the_key(
    cli: Invoke, tools_repo: Path, run_dir: Path, panel: list[FakeJudgeProvider]
) -> None:
    exported = cli("audit", "export", "--judge-labels", "--run", run_dir, *LABELS)
    assert exported.exit_code == 0, exported.output
    sheets = tools_repo / "audit" / "judge"
    for name in ("dev.csv", "heldout.csv"):
        fill_labels(sheets / name)
        with (sheets / name).open("a", encoding="utf-8") as handle:
            handle.write("," * (len(SHEET_CSV_COLUMNS) - 1) + "\n")  # a spreadsheet's blank row
        ingested = cli("audit", "ingest", sheets / name, "--judge-labels")
        assert ingested.exit_code == 0, ingested.output
    # The key moved elsewhere: the report follows it, never the labels' default key dir.
    secrets = tools_repo / "secrets" / "judge-key"
    secrets.mkdir(parents=True)
    shutil.move(tools_repo / "audit" / "judge-key" / KEY_FILE, secrets / KEY_FILE)
    labels = sheets / "judge_validation.jsonl"
    args = ("judge", "--validate", labels, "--dataset", TOOLS_DATASET, "--split", "dev")
    result = cli(*args, "--key", secrets / KEY_FILE)
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == str(secrets / "validation" / "report.json")
    assert (secrets / "validation" / "scores.jsonl").is_file()
    assert not (tools_repo / "audit" / "judge-key" / "validation").exists()
    assert served_messages(panel), "the labeled tool-use rationales were judged without them"


def test_records_that_cant_be_served_are_an_input_error(
    cli: Invoke,
    tools_repo: Path,
    run_dir: Path,
    panel: list[FakeJudgeProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A world that can't be rebuilt as the dataset recorded it (the generator changed since)
    is a one-line input error naming the case, before any judge is paid or anything written."""

    def changed(self: WorldStore, case: Case, *, base: Case | None = None) -> None:
        raise WorldUnavailableError(f"world {case.world_id} differs from the dataset's")

    monkeypatch.setattr(WorldStore, "environment", changed)
    commands = {
        "judge": ("judge", run_dir, *FAMILY),
        "export": ("audit", "export", "--judge-labels", "--run", run_dir, *LABELS),
        "compare": ("compare", AGENT_CONFIG, BASELINE, "--run", run_dir, "--n-boot", "50"),
    }
    for args in commands.values():
        result = cli("--quiet", *args)
        assert result.exit_code == ExitCode.INPUT, result.output
        [line] = result.stderr.splitlines()
        assert line.startswith("error: the records the tools served for case_11_"), line
        assert line.endswith(
            "can't be rebuilt: world startup-11-2026-10-10 differs from the dataset's"
        )
    assert not any(p.requests for p in panel)
    assert not (run_dir / "judge").exists()
    assert not [path for path in (tools_repo / "audit").rglob("*") if path.is_file()]
