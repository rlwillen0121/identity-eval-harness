"""Chart inputs must pass the same seal, aggregate and replay checks as run artifacts."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from identitybench.v41 import runner
from identitybench.v41.schemas import SplitRingV41

from .test_runner_integrity import ROOT, config

CHART_SOURCE = ROOT / "reports/v41-full/build_charts.py"


@pytest.fixture
def charts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT / "reports/v41-full"))
    spec = importlib.util.spec_from_file_location("security_charts", CHART_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    module.REPO_ROOT = tmp_path
    module.DATASET_DIR = ROOT / "datasets/v41_full"
    return module


def make_run(tmp_path: Path, name: str = "one", label: str = "gold") -> Path:
    out = tmp_path / "runs" / f"v41-full-{name}"
    cfg = config(label).model_copy(update={"suite": SplitRingV41.v41_full, "max_episodes": 1})
    runner.run_v41_experiment(cfg, out, repo_root=ROOT)
    return out


def test_chart_loader_rejects_forged_score_with_consistent_summary(
    tmp_path: Path,
    charts: ModuleType,
) -> None:
    out = make_run(tmp_path)
    artifact = runner.load_v41_run_artifact(out)
    rec = artifact.records[0]
    rec = rec.model_copy(update={"scorecard": rec.scorecard.model_copy(update={"g_srr": 0})})
    summaries, rates = runner._summaries([rec], ["gold"], artifact.summary_n_boot or 10)
    altered = artifact.model_copy(
        update={
            "records": (rec,),
            "summaries": summaries,
            "execution_valid_rates": rates,
        }
    )
    (out / "v41_summary.json").write_text(altered.model_dump_json())
    (out / "records/gold.jsonl").write_text(rec.model_dump_json() + "\n")
    with pytest.raises(ValueError, match=r"v41-full-one.*gold.*scorecard"):
        charts._load_records_by_model()


def test_chart_loader_rejects_wrong_pin(tmp_path: Path, charts: ModuleType) -> None:
    out = make_run(tmp_path)
    raw = json.loads((out / "v41_summary.json").read_text())
    raw["seal"]["oracles_sha256"] = "0" * 64
    (out / "v41_summary.json").write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="seal"):
        charts._load_records_by_model()


def test_chart_loader_rejects_unlisted_duplicates(tmp_path: Path, charts: ModuleType) -> None:
    make_run(tmp_path, "one")
    make_run(tmp_path, "two")
    with pytest.raises(ValueError, match="duplicate model id"):
        charts._load_records_by_model()


def test_chart_loader_keeps_and_logs_published_precedence(
    tmp_path: Path,
    charts: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    label = "composer-2.5-t0-medium"
    make_run(tmp_path, "composer-cpa", label)
    later = make_run(tmp_path, "cursor", label)
    rec = runner.load_v41_run_artifact(later).records[0]
    # A valid record beats a later invalid record, preserving historical selection.
    invalid = rec.model_copy(update={"execution_valid": False, "provider_error": True})
    (later / f"records/{label}.jsonl").write_text(invalid.model_dump_json() + "\n")
    rows = charts._load_records_by_model()
    assert rows[label][0]["execution_valid"]
    assert "published precedence" in capsys.readouterr().err


def test_chart_loader_recovers_legacy_scorecard_with_log(
    tmp_path: Path,
    charts: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = make_run(tmp_path)
    raw = runner.load_v41_run_artifact(out).records[0].model_dump(mode="json")
    raw["scorecard"]["waves_commit"][0]["g_k"] = 0.85
    raw["scorecard"]["waves_commit"][0]["stage34"] = 1.0
    (out / "records/gold.jsonl").write_text(json.dumps(raw) + "\n")
    rows = charts._load_records_by_model()
    assert "g_k" not in rows["gold"][0]["scorecard"]["waves_commit"][0]
    assert "records/gold.jsonl:1: legacy record re-scored by replay" in capsys.readouterr().err
    raw["scorecard"]["u_overgrant"] = 2
    (out / "records/gold.jsonl").write_text(json.dumps(raw) + "\n")
    with pytest.raises(ValueError, match="scorecard disagrees"):
        charts._load_records_by_model()


def test_chart_main_redirects_every_output(
    tmp_path: Path,
    charts: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "report-source"
    source.mkdir()
    (source / "DRAFT_REPORT.md").write_text("source report")
    output = tmp_path / "generated"
    calls = []

    def render(arms, directory, **kwargs):
        calls.append(directory)

    monkeypatch.setattr(charts, "ROOT", source)
    monkeypatch.setattr(charts, "build_summary_data", lambda runs: {"v41_full": {"live_arms": {}}})
    monkeypatch.setattr(charts, "render_grantt_v41_full_suite", render)
    monkeypatch.setattr(charts, "render_v41_full_b_style_suite", render)
    monkeypatch.setattr(charts, "write_v41_full_html_files", render)
    monkeypatch.setattr(sys, "argv", ["build_charts.py", "--out-dir", str(output)])
    charts.main()
    assert calls == [output / "charts", output / "charts", output]
    assert (output / "report.md").read_text() == "source report"
    assert (output / "summary.json").exists() and (output / "CHARTS.md").exists()
    assert sorted(p.name for p in source.iterdir()) == ["DRAFT_REPORT.md"]
