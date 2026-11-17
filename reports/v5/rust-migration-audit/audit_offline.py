"""Offline evidence collection; never mutate historical inputs or call providers."""

import argparse
import collections
import datetime
import hashlib
import json
import pathlib
import shutil
import subprocess


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(path):
    return {
        str(file.relative_to(path)): sha(file) for file in sorted(path.rglob("*")) if file.is_file()
    }


def diffs(left, right, path=""):
    result = []
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(left.keys() | right.keys()):
            result.extend(diffs(left.get(key), right.get(key), path + "/" + key))
        return result
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [path + "/length"]
        for index, (x, y) in enumerate(zip(left, right, strict=True)):
            result.extend(diffs(x, y, path + "/" + str(index)))
        return result
    return [] if left == right else [path]


def overlaps(left, right):
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=pathlib.Path)
    parser.add_argument("--binary", required=True, type=pathlib.Path)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()
    root, binary, out = (p.resolve() for p in (args.source_root, args.binary, args.out))
    if not binary.is_file():
        parser.error("--binary must identify an existing executable")
    if out.exists():
        parser.error("--out must be a fresh, nonexistent directory; resume is unsupported")
    canonical = root / "datasets/v5_draft5/v5_core"
    runs = sorted(p for p in (root / "runs").glob("v5_core_*") if p.is_dir())
    manifested = [p for p in runs if (p / "manifest.json").is_file()]
    if not canonical.is_dir() or not manifested:
        parser.error("--source-root must contain the Draft5 core dataset and manifested core runs")
    # Resolve symlinks before checking every input boundary, including individual
    # symlinked files. Permit a separate directory beneath source-root/reports.
    inputs = [canonical, binary, root / "runs", root / "reports/scratch"]
    inputs += [p for run in manifested for p in (run, root / "reports/scratch" / run.name)]
    for directory in [canonical, *manifested, root / "reports/scratch"]:
        inputs.extend(p for p in directory.rglob("*") if p.is_file())
    if any(overlaps(out, path.resolve()) for path in inputs):
        parser.error("--out must be disjoint from all source inputs and scratch reports")
    for run in manifested:
        if not (root / "reports/scratch" / run.name / "summary.json").is_file():
            parser.error("every manifested core run must have an existing Python summary")
    # Snapshot once so concurrent rebuilds cannot change the executable mid-audit.
    out.mkdir(parents=True, exist_ok=False)
    snapshot = out / "binary" / "identitybench"
    snapshot.parent.mkdir()
    original_binary_sha = sha(binary)
    shutil.copy2(binary, snapshot)
    if sha(snapshot) != original_binary_sha:
        raise RuntimeError("binary changed while snapshotting; use a fresh output directory")
    res = {
        "schema_version": "offline-rust-migration-audit-v2",
        "created_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "execution_origin": "historical Python runs; Rust offline raw-trajectory regrading only",
        "live_calls": 0,
        "binary_sha256": original_binary_sha,
        "binary_name": binary.name,
        "binary_snapshot_path": str(snapshot.relative_to(out)),
        "locator_resolution": {
            "source_path": "relative to explicit --source-root",
            "python_report_path": "relative to explicit --source-root",
            "native_report_path": "relative to audit output directory",
            "binary_snapshot_path": "relative to audit output directory; local-only artifact",
            "binary_identity": "binary_name and binary_sha256",
        },
        "canonical_dataset_sha256": tree(canonical),
        "excluded_unmanifested_directories": [p.name for p in runs if p not in manifested],
        "runs": [],
    }
    for run in manifested:
        before = tree(run)
        olddir = root / "reports/scratch" / run.name
        oldhash = tree(olddir)
        dest = out / "native" / run.name
        proc = subprocess.run(
            [
                str(snapshot),
                "v5",
                "report",
                "--no-env-file",
                "--root",
                str(root),
                "--run",
                str(run),
                "--out",
                str(dest),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        # Never persist unfiltered provider-related errors or raw records.
        row = {
            "run": run.name,
            "source_path": str(run.relative_to(root)),
            "source_file_sha256": before,
            "python_report_path": str(olddir.relative_to(root)),
            "python_report_file_sha256": oldhash,
            "native_report_path": str(dest.relative_to(out)),
            "native_exit_code": proc.returncode,
        }
        records = [
            json.loads(line)
            for line in (run / "episodes.jsonl").read_text().splitlines()
            if line.strip()
        ]
        row["record_count"] = len(records)
        row["provider_error_count"] = sum(bool(r.get("provider_error")) for r in records)
        row["parse_failure_count"] = sum(bool(r.get("parse_failure")) for r in records)
        row["termination_reason_counts"] = dict(
            collections.Counter(r.get("termination_reason") for r in records)
        )
        row["attempt_status_counts"] = dict(
            collections.Counter(r.get("attempt_status") for r in records)
        )
        manifest = json.loads((run / "manifest.json").read_text())
        row["manifest_binding_checks"] = {
            key: manifest.get(key) == before.get(value)
            for key, value in [
                ("run_episodes_sha256", "episodes.jsonl"),
                ("dataset_episodes_sha256", "dataset/episodes.jsonl"),
                ("dataset_oracles_sha256", "dataset/oracles.jsonl"),
                ("dataset_seal_sha256", "dataset/SEAL.json"),
            ]
        }
        row["canonical_dataset_match"] = {
            name: before.get("dataset/" + name) == res["canonical_dataset_sha256"].get(name)
            for name in ["SEAL.json", "episodes.jsonl", "oracles.jsonl"]
        }
        if proc.returncode == 0:
            fresh = json.loads((dest / "summary.json").read_text())
            old = json.loads((olddir / "summary.json").read_text())
            row.update(
                {
                    "audit_outcome": "complete",
                    "run_completion": "protocol_complete"
                    if fresh["is_complete"]
                    else "provider_incomplete",
                    "planned_slots": fresh["planned_slots"],
                    "observed_slots": fresh["observed_slots"],
                    "execution_valid_rate": fresh["metrics"].get("execution_valid_rate"),
                    "summary_delta_paths": diffs(old, fresh),
                    "native_report_file_sha256": tree(dest),
                    "raw_trajectory_regrading_verified": fresh["provenance"].get(
                        "scoring_evidence_status"
                    )
                    == "VERIFIED",
                    "provenance": fresh["provenance"],
                }
            )
            stored = {
                str((r["score"]["episode_id"], r["score"].get("repeat_index", 0))): r["score"]
                for r in records
            }
            native = {
                str((r["episode_id"], r.get("repeat_index", 0))): r for r in fresh["episode_scores"]
            }
            row["stored_score_delta_paths"] = diffs(stored, native)
            row["python_markdown_byte_equal"] = (olddir / "report.md").is_file() and (
                olddir / "report.md"
            ).read_bytes() == (dest / "report.md").read_bytes()
            if row["summary_delta_paths"] or row["stored_score_delta_paths"]:
                row["audit_outcome"] = "semantic_delta_requires_review"
        else:
            row.update(
                {
                    "audit_outcome": "blocked",
                    "failure_class": "native_raw_regrade_rejected_input",
                    "error_output_sha256": hashlib.sha256(proc.stderr.encode()).hexdigest(),
                }
            )
        row["original_run_unchanged"] = before == tree(run)
        row["original_python_reports_unchanged"] = oldhash == tree(olddir)
        if (
            not all(row["manifest_binding_checks"].values())
            or not all(row["canonical_dataset_match"].values())
            or not row["original_run_unchanged"]
            or not row["original_python_reports_unchanged"]
        ):
            row["audit_outcome"] = "blocked"
            row["failure_class"] = "input_integrity_failure"
        res["runs"].append(row)
        (out / "inventory.json").write_text(json.dumps(res, indent=2, sort_keys=True) + "\n")
        print(run.name, row["audit_outcome"], flush=True)
    rows = res["runs"]
    res["final_all_originals_unchanged"] = (
        all(
            tree(root / r["source_path"]) == r["source_file_sha256"]
            and tree(root / r["python_report_path"]) == r["python_report_file_sha256"]
            for r in rows
        )
        and tree(canonical) == res["canonical_dataset_sha256"]
    )
    res["binary_snapshot_unchanged"] = sha(snapshot) == original_binary_sha
    res["finished_utc"] = datetime.datetime.now(datetime.UTC).isoformat()
    res["totals"] = {
        "runs": len(rows),
        "audit_outcomes": dict(collections.Counter(r["audit_outcome"] for r in rows)),
        "run_completion": dict(
            collections.Counter(r.get("run_completion", "blocked") for r in rows)
        ),
        "records": sum(r["record_count"] for r in rows),
        "planned_slots": sum(r.get("planned_slots", 144) for r in rows),
        "provider_errors": sum(r["provider_error_count"] for r in rows),
        "parse_failures": sum(r["parse_failure_count"] for r in rows),
        "max_turns_exceeded": sum(
            r["termination_reason_counts"].get("MAX_TURNS_EXCEEDED", 0) for r in rows
        ),
        "summary_delta_paths": sum(len(r.get("summary_delta_paths", [])) for r in rows),
        "stored_score_delta_paths": sum(len(r.get("stored_score_delta_paths", [])) for r in rows),
        "summary_json_byte_identical": sum(
            r["python_report_file_sha256"].get("summary.json")
            == r.get("native_report_file_sha256", {}).get("summary.json")
            for r in rows
        ),
        "report_markdown_byte_identical": sum(
            r.get("python_markdown_byte_equal", False) for r in rows
        ),
        "corrected": 0,
        "blocked": sum(r["audit_outcome"] == "blocked" for r in rows),
    }
    (out / "inventory.json").write_text(json.dumps(res, indent=2, sort_keys=True) + "\n")
    print(json.dumps(res["totals"]), flush=True)
    if (
        not res["final_all_originals_unchanged"]
        or not res["binary_snapshot_unchanged"]
        or any(r["audit_outcome"] != "complete" for r in rows)
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
