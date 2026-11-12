"""Focused tests for v4.1 report diagnostics and Markdown safety."""

from __future__ import annotations

import json
from pathlib import Path

from identitybench.report.markdown_text import escape_cell, escape_inline
from identitybench.v41 import runner
from identitybench.v41.report import render_v41_markdown_report, write_v41_report
from identitybench.v41.schemas import DatasetSealV41, SplitRingV41

from .helpers import probe


def _artifact(*, name: str, model_id: str, with_seal: bool = False) -> runner.V41RunArtifact:
    episode, oracle = probe()
    base = runner._baseline_record("noop_team", model_id, episode, oracle, None)
    records = tuple(
        base.model_copy(
            update={
                "episode_id": f"episode-{index}",
                "family_id": f"family-{index}",
                "trial_index": index,
                "termination_reason": reason,
                "provider_error": provider_error,
                "parse_failure": parse_failure,
            }
        )
        for index, (reason, provider_error, parse_failure) in enumerate(
            (
                ("EPISODE_COMPLETED", False, False),
                ("PROVIDER|ERROR", True, False),
                ("PARSE_FAILURE", False, True),
            )
        )
    )
    summaries, rates = runner._summaries(records, [model_id], 20)
    seal = (
        DatasetSealV41(
            suite=SplitRingV41.v41_dev,
            seed=1,
            episode_count=len(records),
            episodes_sha256="0" * 64,
            oracles_sha256="1" * 64,
        )
        if with_seal
        else None
    )
    return runner.V41RunArtifact(
        name=name,
        suite=SplitRingV41.v41_dev,
        seal=seal,
        records=records,
        summaries=summaries,
        execution_valid_rates=rates,
        summary_n_boot=20,
    )


def test_markdown_escapes_untrusted_run_name_and_model_id() -> None:
    run_name = "<img src=x onerror=alert(1)> [run](https://example.invalid)"
    model_id = "`evil|model`\n<script>alert(1)</script>"
    artifact = _artifact(name=run_name, model_id=model_id)

    report = render_v41_markdown_report(artifact, check_dataset=False)

    assert f"# IdentityBench v4.1 report: {escape_inline(run_name)}" in report
    assert escape_cell(model_id) in report
    assert "\\<img src=x" in report
    assert "\\<script\\>alert(1)\\</script\\>" in report
    assert "PROVIDER\\|ERROR: 1" in report


def test_write_report_includes_diagnostics_and_missing_dataset_note(tmp_path: Path) -> None:
    artifact = _artifact(name="run", model_id="provider:model", with_seal=True)
    missing_dataset_root = tmp_path / "datasets-unavailable"

    report_path = write_v41_report(
        artifact, tmp_path / "out", dataset_root=missing_dataset_root, n_boot=20
    )

    report = report_path.read_text(encoding="utf-8")
    assert "dataset not available; seal not checked" in report
    assert "EPISODE_COMPLETED: 1, PARSE_FAILURE: 1, PROVIDER\\|ERROR: 1" in report
    summary = json.loads((report_path.parent / "summary.json").read_text(encoding="utf-8"))
    assert summary["record_diagnostics"]["provider:model"] == {
        "termination_reason_counts": {
            "EPISODE_COMPLETED": 1,
            "PARSE_FAILURE": 1,
            "PROVIDER|ERROR": 1,
        },
        "provider_error_count": 1,
        "parse_failure_count": 1,
    }
    assert summary["name"] == artifact.name
    assert summary["summaries"] == artifact.model_dump(mode="json")["summaries"]
