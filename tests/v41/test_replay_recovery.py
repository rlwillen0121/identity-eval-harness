"""Recovery must prove recorded scores without guessing authoritative execution state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from identitybench.v41 import runner, verify
from identitybench.v41.schemas import RunConfigV41, WaveExecutionMode

from .helpers import probe
from .test_runner_integrity import ROOT


def write_config(path: Path, **settings: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "models": [
                    {"provider": "baseline", "model": "oracle_team_replay", "label": "gold"}
                ],
                **settings,
            }
        )
    )


def test_candidate_sources_and_experiment_settings(tmp_path: Path) -> None:
    run = tmp_path / "run"
    experiments = tmp_path / "experiments"
    write_config(run / "configs/prior.yaml", max_turns=20, max_calls_per_turn=8)
    write_config(experiments / "v41-prior.yaml", max_turns=20, max_calls_per_turn=8)
    candidates = verify.load_run_config_candidates(run, experiments_dir=experiments)["gold"]
    assert len(candidates) == 1
    assert len(candidates[0].sources) == 2
    assert candidates[0].config.max_turns == 20
    assert candidates[0].config.max_calls_per_turn == 8
    assert not candidates[0].authoritative


def test_candidate_replay_and_authority_boundary(tmp_path: Path) -> None:
    ep, oracle = probe()
    rec = runner._baseline_record("oracle_team_replay", "gold", ep, oracle, RunConfigV41())
    run = tmp_path / "run"
    experiments = tmp_path / "experiments"
    write_config(experiments / "v41-gold.yaml")
    candidates = verify.load_run_config_candidates(run, experiments_dir=experiments)["gold"]
    result, restored = verify.verify_with_candidates(rec, ep, oracle, candidates)
    assert result.status == "verified" and restored == rec
    assert result.matched_candidate is not None
    assert result.matched_candidate.sources == ("experiments/v41-gold.yaml",)
    forged = rec.model_copy(update={"scorecard": rec.scorecard.model_copy(update={"g_srr": 0})})
    result, _ = verify.verify_with_candidates(forged, ep, oracle, candidates)
    assert result.status == "unverifiable"
    write_config(run / "manifest.json")
    candidates = verify.load_run_config_candidates(run, experiments_dir=experiments)["gold"]
    result, _ = verify.verify_with_candidates(forged, ep, oracle, candidates)
    assert result.status == "mismatch"


def test_candidates_pruned_by_wave_mode(tmp_path: Path) -> None:
    ep, oracle = probe()
    rec = runner._baseline_record("oracle_team_replay", "gold", ep, oracle, RunConfigV41())
    candidate = verify.RunConfigCandidate(
        RunConfigV41(wave_execution_mode=WaveExecutionMode.DIAGNOSTIC_RESET),
        ("experiments/v41-reset.yaml",),
    )
    result, _ = verify.verify_with_candidates(rec, ep, oracle, [candidate])
    assert result.status == "unverifiable" and "wave_execution_mode" in result.reason


def test_legacy_recovery_requires_every_current_score_field() -> None:
    ep, oracle = probe()
    cfg = RunConfigV41()
    rec = runner._baseline_record("oracle_team_replay", "gold", ep, oracle, cfg)
    raw = rec.model_dump(mode="json")
    raw["scorecard"]["waves_commit"][0]["g_k"] = 123
    raw["scorecard"]["waves_commit"][0]["stage34"] = 1.0
    candidate = verify.RunConfigCandidate(cfg, ("manifest.json",), authoritative=True)
    result, restored = verify.verify_with_candidates(raw, ep, oracle, [candidate])
    assert result.status == "recovered" and restored == rec
    raw["scorecard"]["waves_commit"][0]["stage34"] = 0.0
    result, restored = verify.verify_with_candidates(raw, ep, oracle, [candidate])
    assert result.status == "mismatch" and restored is None
    del raw["scorecard"]["g_min"]
    result, restored = verify.verify_with_candidates(raw, ep, oracle, [candidate])
    assert result.status == "mismatch" and restored is None


def test_legacy_recovery_refuses_inconsistent_aggregate() -> None:
    ep, oracle = probe()
    cfg = RunConfigV41()
    rec = runner._baseline_record("oracle_team_replay", "gold", ep, oracle, cfg)
    raw = rec.model_dump(mode="json")
    raw["scorecard"]["waves_commit"][0]["g_k"] = 0.85
    raw["scorecard"]["u_exec"] = 0
    raw["scorecard"]["u_overgrant"] = 2
    candidate = verify.RunConfigCandidate(cfg, ("experiments/v41-gold.yaml",))
    result, restored = verify.verify_with_candidates(raw, ep, oracle, [candidate])
    assert result.status == "unverifiable" and restored is None


@pytest.mark.parametrize("value", ["/Users/someone/bin/muse", "/home/someone/muse", "~/muse"])
@pytest.mark.parametrize(("provider", "key"), [("muse_cli", "binary"), ("replay", "path")])
def test_v41_home_paths_require_local_config(
    tmp_path: Path,
    provider: str,
    key: str,
    value: str,
) -> None:
    path = tmp_path / "published.yaml"
    payload = {"models": [{"provider": provider, "model": "test", "extra": {key: value}}]}
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="experiments/local/"):
        runner.load_v41_experiment_config(path)
    local = tmp_path / "experiments/local/experiment.yaml"
    local.parent.mkdir(parents=True)
    local.write_text(path.read_text())
    assert runner.load_v41_experiment_config(local).models[0].extra[key] == value
    link = local.parent / "linked.yaml"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="experiments/local/"):
        runner.load_v41_experiment_config(link)


@pytest.mark.parametrize("value", ["muse", "/usr/local/bin/muse", "/opt/bin/muse"])
def test_v41_portable_binary_paths_load(tmp_path: Path, value: str) -> None:
    path = tmp_path / "experiment.yaml"
    path.write_text(
        json.dumps(
            {
                "models": [{"provider": "muse_cli", "model": "test", "extra": {"binary": value}}],
            }
        )
    )
    assert runner.load_v41_experiment_config(path).models[0].extra["binary"] == value


def test_all_committed_v41_experiments_load() -> None:
    paths = sorted((ROOT / "experiments").glob("v41-*.yaml"))
    assert paths
    for path in paths:
        runner.load_v41_experiment_config(path)
