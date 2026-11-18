"""Render an existing offline audit inventory without overwriting prior evidence."""

import argparse
import json
import pathlib


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=pathlib.Path)
    args = parser.parse_args()
    out = args.out.resolve()
    target = out / "AUDIT.md"
    if target.exists():
        parser.error("AUDIT.md already exists; refusing to overwrite completed evidence")
    inventory = out / "inventory.json"
    if not inventory.is_file():
        parser.error("--out must contain an existing completed inventory.json")
    data = json.loads(inventory.read_text())
    if "totals" not in data:
        parser.error("inventory is incomplete; collector must finish before rendering")
    rows = data["runs"]
    totals = data["totals"]
    verified = all(
        all(row["manifest_binding_checks"].values())
        and all(row["canonical_dataset_match"].values())
        and row["original_run_unchanged"]
        and row["original_python_reports_unchanged"]
        for row in rows
    )
    json_equal = sum(
        row["python_report_file_sha256"].get("summary.json")
        == row.get("native_report_file_sha256", {}).get("summary.json")
        for row in rows
    )
    markdown_equal = sum(row.get("python_markdown_byte_equal", False) for row in rows)
    lines = [
        "# Draft5 core historical-run Rust offline audit",
        "",
        f"Audited {len(rows)} historical configurations ({totals['records']} recorded slots, "
        f"{totals['planned_slots']} planned slots) using the native Rust release binary. "
        "These are historical Python-executed model runs, regraded offline by Rust; "
        "no live model calls were made.",
        "",
        f"Audit outcomes: {totals['audit_outcomes']}. "
        f"Protocol completion: {totals['run_completion']}. Corrected: 0.",
        "",
        f"Semantic summary delta paths: {totals['summary_delta_paths']}; "
        f"stored episode-score delta paths: {totals['stored_score_delta_paths']}. "
        f"Native summary JSON is byte-identical for {json_equal}/{len(rows)} configurations. "
        f"Native Markdown is byte-identical for {markdown_equal}/{len(rows)} configurations.",
        "",
        f"Provider failures: {totals['provider_errors']} slots. "
        f"Parse failures: {sum(row['parse_failure_count'] for row in rows)} slots. "
        "These remain distinct from turn-limit terminations. "
        "Provider-incomplete runs are not publication-complete results.",
        "",
        "| Configuration | Completion | Execution-valid rate | Provider failures | "
        "Parse failures | Turn limit | Audit |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        turn = row["termination_reason_counts"].get("MAX_TURNS_EXCEEDED", 0)
        lines.append(
            f"| {row['run']} | {row.get('run_completion', 'blocked')} | "
            f"{row.get('execution_valid_rate')} | {row['provider_error_count']} | "
            f"{row['parse_failure_count']} | {turn} | {row['audit_outcome']} |"
        )
    lines += [
        "",
        "## Proof and scope",
        "",
        f"Binary SHA-256: `{data['binary_sha256']}`.",
        "",
        "The native `v5 report` path loads sealed episodes and oracles, validates raw "
        "execution context, recomputes scoring inputs and scores from raw trajectories, "
        "and rejects disagreement with attached scoring inputs or scores. The inventory "
        "records command status and raw-regrading verification per configuration. "
        "Comparisons cover the complete parsed summary, stored scores, and report bytes.",
        "",
        f"All per-run input immutability and dataset binding checks passed: {verified}. "
        "The inventory binds original run files, Python reports, canonical sealed dataset, "
        "and native outputs by SHA-256.",
        "",
        f"Final all-originals hash readback: {data.get('final_all_originals_unchanged')}. "
        f"Binary snapshot unchanged: {data.get('binary_snapshot_unchanged', 'not recorded')}.",
        "",
        "Inventory source_path and python_report_path resolve relative to explicit --source-root. "
        "Native report and local-only binary snapshot locators resolve relative to --out. "
        "Binary identity is its name and SHA-256; snapshots are excluded from publication. "
        "The output directory is separate. Historical execution provenance remains bound "
        "to the original manifest and trajectories; the Rust executable hash identifies "
        "this offline audit. The inventory records any semantic and artifact differences.",
        "",
        "This is offline migration parity evidence, not held-out tuning, a live evaluation, "
        "dataset freeze sign-off, independent normative conformance, or publication approval. "
        "The binary hash makes no source-HEAD provenance claim.",
        "",
        "Excluded unmanifested directories: "
        + ", ".join(data.get("excluded_unmanifested_directories", []))
        + ".",
        "",
        "Machine-readable evidence: [inventory.json](inventory.json). "
        "Native report bundles: [native/](native/).",
        "",
    ]
    with target.open("x") as stream:
        stream.write("\n".join(lines))


if __name__ == "__main__":
    main()
