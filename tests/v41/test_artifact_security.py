"""Offline regressions for replay integrity, oracle resume pins and token budgets."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from identitybench.providers.base import ModelResponse, ToolCall, Usage
from identitybench.v41 import runner
from identitybench.v41.schemas import RunConfigV41

from .helpers import probe
from .test_runner_integrity import ROOT, config


def test_retired_integration_removed() -> None:
    assert not list((ROOT / "src/identitybench/v41").glob("orgai*"))


def test_optional_budget_serialization_and_fingerprint() -> None:
    from .test_runner_fingerprint import _fp

    assert _fp() == "21b25c36a5fd0853f4eaa4bbf64963eec9c34579293502946a6498e5602c752e"
    entry = runner.V41ModelEntryConfig(provider="baseline", model="noop_team")
    cfg = runner.V41ExperimentConfig(models=(entry,))
    assert "max_total_tokens" not in cfg.model_dump_json()
    assert "max_total_tokens" not in RunConfigV41().model_dump_json()
    budget = RunConfigV41(max_total_tokens=10)
    assert budget.model_dump()["max_total_tokens"] == 10


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("error", [None, "temporary failure", "malformed function call"])
def test_cumulative_budget_stops_before_more_calls(native: bool, error: str | None) -> None:
    episode, oracle = probe()
    calls = 0

    async def complete(request: Any) -> ModelResponse:
        nonlocal calls
        calls += 1
        tc = ToolCall("read", "read_wave_evidence", {"wave_index": 1})
        return ModelResponse(
            tool_calls=[tc] if native else [],
            parsed={"actions": [{"action": tc.name, "arguments": tc.arguments}]},
            usage=Usage(input_tokens=4, output_tokens=2),
            error=error if calls == 2 else None,
        )

    provider = SimpleNamespace(
        kind="stub",
        model="stub",
        complete=complete,
        capabilities=lambda: SimpleNamespace(native_tools=native),
    )
    record = asyncio.run(
        runner.run_v41_agent_episode(
            episode,
            oracle,
            provider,
            run_config=RunConfigV41(max_total_tokens=6),
        )
    )
    assert calls == 2  # Equality is permitted; the next response exceeds the budget.
    assert record.termination_reason == "TOKEN_BUDGET_EXCEEDED"
    assert record.input_tokens + record.output_tokens == 12
    assert len(record.tool_trace) == 1
    assert record.execution_valid and not record.provider_error


def test_oracle_pin_resume_rejection_and_legacy_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = runner.run_v41_experiment(config("gold"), tmp_path, repo_root=ROOT)
    assert artifact.seal is not None
    assert all(r.oracles_sha256 == artifact.seal.oracles_sha256 for r in artifact.records)
    path = tmp_path / "records/gold.jsonl"
    lines = [json.loads(s) for s in path.read_text().splitlines()]
    lines[0]["oracles_sha256"] = "0" * 64
    lines[1].pop("oracles_sha256")
    bad = json.dumps(lines[0]) + "\n"
    path.write_text(bad + json.dumps(lines[1]) + "\n")
    original = runner._baseline_record
    seen = []

    def execute(*args: Any, **kwargs: Any) -> runner.EpisodeTrialRecordV41:
        seen.append(args[2].episode_id)
        return original(*args, **kwargs)

    monkeypatch.setattr(runner, "_baseline_record", execute)
    resumed = runner.run_v41_experiment(config("gold"), tmp_path, repo_root=ROOT, resume=True)
    assert seen == [lines[0]["episode_id"]]
    assert (tmp_path / "records/gold.rejected.jsonl").read_text() == bad
    assert len(resumed.records) == 2


def test_trace_replay_detects_score_and_episode_tampering() -> None:
    from identitybench.v41.verify import verify_record

    ep, oracle = probe()
    cfg = RunConfigV41()
    rec = runner._baseline_record("oracle_team_replay", "gold", ep, oracle, cfg)
    assert verify_record(rec, ep, oracle, cfg).status == "verified"
    forged = rec.model_copy(update={"scorecard": rec.scorecard.model_copy(update={"g_srr": 0})})
    assert verify_record(forged, ep, oracle, cfg).status == "mismatch"
    forged = rec.model_copy(update={"episode_sha256": "0" * 64})
    assert verify_record(forged, ep, oracle, cfg).status == "mismatch"


def test_replay_caps_and_bounded_rejections() -> None:
    from identitybench.v41.verify import verify_record

    ep, oracle = probe()
    cfg = RunConfigV41(max_turns=1)
    rec = runner._baseline_record("noop_team", "noop", ep, oracle, cfg)
    rejection = dict(
        turn=0,
        name="stage_wave_mutations",
        arguments={"_rejected_arguments": "depth_limit", "max_depth": 32, "max_bytes": 262144},
        result_status="INVALID_ARGUMENTS",
    )
    capped = dict(rejection, result_status="CALL_CAP_EXCEEDED")
    rec = rec.model_copy(
        update={
            "tool_trace": (*rec.tool_trace, rejection, capped),
            "tool_calls_count": rec.tool_calls_count + 1,
        }
    )
    assert verify_record(rec, ep, oracle, cfg).status == "verified"
    bad = dict(rejection, result_status="STAGED")
    forged = rec.model_copy(update={"tool_trace": (*rec.tool_trace[:-2], bad, capped)})
    assert verify_record(forged, ep, oracle, cfg).status == "mismatch"


def test_legacy_trace_context_is_explicitly_unverifiable() -> None:
    from identitybench.v41.verify import verify_record

    ep, oracle = probe()
    cfg = RunConfigV41()
    rec = runner._baseline_record("noop_team", "noop", ep, oracle, cfg)
    assert verify_record(rec, ep, oracle).status == "unverifiable"
    legacy = rec.model_copy(update={"tool_trace": ({"name": "read_wave_evidence"},)})
    result = verify_record(legacy, ep, oracle, cfg)
    assert result.status == "unverifiable" and "lacks replay fields" in result.reason
    corrupt = rec.model_copy(
        update={"tool_trace": ({**rec.tool_trace[0], "result_status": "FORGED"},)}
    )
    assert verify_record(corrupt, ep, oracle, cfg).status == "mismatch"


def test_budget_overrides_are_fingerprinted_and_persisted(tmp_path: Path) -> None:
    entry = runner.V41ModelEntryConfig(provider="baseline", model="noop_team", max_total_tokens=20)
    cfg = config("gold").model_copy(update={"max_total_tokens": 10, "models": (entry,)})
    run_cfg = entry.to_run_config(max_total_tokens=cfg.max_total_tokens)
    assert run_cfg.max_total_tokens == 20
    args = dict(seed=1, dataset_sha256="0" * 64, endpoint_identity="baseline:noop_team")
    assert runner.compute_v41_config_fingerprint(entry, run_cfg, **args) != (
        runner.compute_v41_config_fingerprint(entry, RunConfigV41(), **args)
    )
    runner.run_v41_experiment(cfg, tmp_path, repo_root=ROOT)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["max_total_tokens"] == 10
    assert manifest["models"][0]["max_total_tokens"] == 20


def test_verify_cli_detects_journal_tampering_and_reports_missing_context(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from identitybench.cli import app

    artifact = runner.run_v41_experiment(config("gold"), tmp_path, repo_root=ROOT)
    args = ["--root", str(ROOT), "--no-env-file", "v41", "verify", "--run", str(tmp_path), "--json"]
    cli = CliRunner()
    result = cli.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["models"]["gold"] == {
        "verified": 2,
        "recovered": 0,
        "mismatch": 0,
        "unverifiable": 0,
    }
    journal = tmp_path / "records/gold.jsonl"
    raw = artifact.records[0].model_dump(mode="json")
    raw["scorecard"]["g_srr"] = 0
    journal.write_text(json.dumps(raw) + "\n")
    result = cli.invoke(app, args)
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["models"]["gold"]["mismatch"] == 1
    journal.unlink()
    (tmp_path / "manifest.json").unlink()
    result = cli.invoke(app, args)
    assert result.exit_code == 2, result.output
    assert json.loads(result.output)["models"]["gold"]["unverifiable"] == 2
    allowed = cli.invoke(app, [*args, "--allow-unverifiable"])
    assert allowed.exit_code == 0, allowed.output
    allowed_json = json.loads(allowed.output)
    assert allowed_json["models"]["gold"]["unverifiable"] == 2
    assert all(detail["reason"] for detail in allowed_json["details"])


def test_verify_cli_checks_summaries_and_seals(tmp_path: Path) -> None:
    from identitybench.v41.verify import verify_run

    artifact = runner.run_v41_experiment(config("gold"), tmp_path, repo_root=ROOT)
    path = tmp_path / "v41_summary.json"
    raw = artifact.model_dump(mode="json")
    raw["summaries"]["gold"]["g_srr"] = 0
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="summaries"):
        verify_run(tmp_path)
    raw = artifact.model_dump(mode="json")
    raw["seal"]["oracles_sha256"] = "0" * 64
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="seal"):
        verify_run(tmp_path)


def test_legacy_scorecard_is_not_silently_normalized(tmp_path: Path) -> None:
    from identitybench.v41.verify import verify_run

    artifact = runner.run_v41_experiment(config("gold"), tmp_path, repo_root=ROOT)
    raw = artifact.records[0].model_dump(mode="json")
    raw["scorecard"]["waves_commit"][0]["g_k"] = 0
    (tmp_path / "records/gold.jsonl").write_text(json.dumps(raw) + "\n")
    _, result = verify_run(tmp_path)
    assert result["models"]["gold"] == {
        "verified": 2,
        "recovered": 1,
        "mismatch": 0,
        "unverifiable": 0,
    }
    assert result["details"][0]["reason"] == "legacy record re-scored by replay"


@pytest.mark.parametrize("reset", [False, True])
def test_replay_restores_steering_and_wave_state(reset: bool) -> None:
    from identitybench.v41.schemas import SteeringMode, WaveExecutionMode
    from identitybench.v41.verify import verify_record

    ep, oracle = probe()
    cfg = RunConfigV41(
        steering_mode=SteeringMode.FULL_LIVE_STEERED,
        wave_execution_mode=(
            WaveExecutionMode.DIAGNOSTIC_RESET if reset else WaveExecutionMode.COUPLED
        ),
    )
    rec = runner._baseline_record("oracle_team_replay", "gold", ep, oracle, cfg)
    assert rec.scorecard.steer_calls == 3
    assert verify_record(rec, ep, oracle, cfg).status == "verified"


@pytest.mark.parametrize("error", ["temporary failure", "malformed function call"])
def test_retry_usage_also_exhausts_budget(error: str) -> None:
    ep, oracle = probe()
    calls = 0

    async def complete(request: Any) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(error=error, usage=Usage(input_tokens=4, output_tokens=2))

    provider = SimpleNamespace(
        kind="stub",
        model="stub",
        complete=complete,
        capabilities=lambda: SimpleNamespace(native_tools=False),
    )
    rec = asyncio.run(
        runner.run_v41_agent_episode(
            ep,
            oracle,
            provider,
            run_config=RunConfigV41(max_total_tokens=6),
        )
    )
    assert calls == 2 and rec.turns_used == 1
    assert rec.termination_reason == "TOKEN_BUDGET_EXCEEDED"
    assert rec.input_tokens + rec.output_tokens == 12
    assert rec.tool_trace == ()
