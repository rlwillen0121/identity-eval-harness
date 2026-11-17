#!/usr/bin/env python3
"""Generate reference report.md and summary.json fixtures from real v5 Draft 5 pilot runs."""

from __future__ import annotations

from pathlib import Path

from identitybench.v5.report import report_from_raw_artifacts, write_v5_report

PILOT_ROOT = Path("/Users/ryanwillen/orca/projects/identity-eval-harness/runs")
FIXTURES_DIR = Path(__file__).resolve().parent


def main() -> None:
    runs = {
        "pilot_t0": PILOT_ROOT / "v5_draft5_pilot_v3_gemini_hydrogen_t0",
        "pilot_t5": PILOT_ROOT / "v5_draft5_pilot_v3_gemini_hydrogen_t5",
    }
    for prefix, run_dir in runs.items():
        if not run_dir.exists():
            raise SystemExit(f"Pilot run directory not found: {run_dir}")
        artifact = report_from_raw_artifacts(run_dir)
        tmp_out = FIXTURES_DIR / f"_tmp_{prefix}"
        report_path, summary_path = write_v5_report(artifact, tmp_out)
        (FIXTURES_DIR / f"{prefix}_report.md").write_bytes(report_path.read_bytes())
        (FIXTURES_DIR / f"{prefix}_summary.json").write_bytes(summary_path.read_bytes())
        report_path.unlink()
        summary_path.unlink()
        tmp_out.rmdir()
        print(f"Wrote {prefix}_report.md and {prefix}_summary.json")


if __name__ == "__main__":
    main()
