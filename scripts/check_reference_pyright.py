"""Enforce the reviewed Pyright baseline for the frozen Python v5 reference."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = Path(__file__).with_name("reference_pyright_baseline.json")
REFERENCE_SOURCE_ROOT = Path("src/identitybench/v5")
EXPECTED_DIAGNOSTIC_COUNT = 25
BASELINE_SCHEMA = "identitybench-reference-pyright-baseline-v1"


def source_hashes(root: Path) -> dict[str, str]:
    """Return byte hashes for Python source files in the frozen reference tree."""
    source_root = root / REFERENCE_SOURCE_ROOT
    if not source_root.is_dir():
        return {}

    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source_root.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".pyi"}
    }


def pyright_config(root: Path) -> dict[str, Any]:
    """Read the effective project-local Pyright settings from pyproject.toml."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    tool = project.get("tool", {})
    config = tool.get("pyright", {}) if isinstance(tool, dict) else {}
    if not isinstance(config, dict):
        raise ValueError("[tool.pyright] must be a TOML table")
    return config


def canonical_diagnostics(payload: dict[str, Any], root: Path) -> list[dict[str, Any]]:
    """Keep stable, reviewable diagnostic fields and make paths repository-relative."""
    diagnostics = payload.get("generalDiagnostics")
    if not isinstance(diagnostics, list):
        raise ValueError("Pyright JSON has no generalDiagnostics array")

    records: list[dict[str, Any]] = []
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            raise ValueError("Pyright emitted a malformed diagnostic")
        filename = diagnostic.get("file")
        if not isinstance(filename, str):
            raise ValueError("Pyright diagnostic has no file path")
        path = Path(filename)
        if path.is_absolute():
            try:
                normalized_path = path.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                normalized_path = path.resolve().as_posix()
        else:
            normalized_path = path.as_posix()

        record = {
            "path": normalized_path,
            "severity": diagnostic.get("severity"),
            "rule": diagnostic.get("rule"),
            "range": diagnostic.get("range"),
            "message": diagnostic.get("message"),
        }
        if not isinstance(record["range"], dict) or not isinstance(record["message"], str):
            raise ValueError("Pyright emitted a diagnostic without a range or message")
        records.append(record)

    return sorted(records, key=diagnostic_key)


def diagnostic_key(diagnostic: dict[str, Any]) -> str:
    return json.dumps(diagnostic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def diagnostic_difference(
    expected: list[dict[str, Any]], observed: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return missing expected and unexpected observed diagnostics, preserving duplicates."""
    expected_counts = Counter(diagnostic_key(item) for item in expected)
    observed_counts = Counter(diagnostic_key(item) for item in observed)
    expected_by_key = {diagnostic_key(item): item for item in expected}
    observed_by_key = {diagnostic_key(item): item for item in observed}

    missing = [
        expected_by_key[key]
        for key in sorted(expected_counts.keys() | observed_counts.keys())
        for _ in range(max(0, expected_counts[key] - observed_counts[key]))
    ]
    unexpected = [
        observed_by_key[key]
        for key in sorted(expected_counts.keys() | observed_counts.keys())
        for _ in range(max(0, observed_counts[key] - expected_counts[key]))
    ]
    return missing, unexpected


def source_hash_difference(
    expected: dict[str, str], observed: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """Return missing, added, and changed reference source paths."""
    expected_paths = set(expected)
    observed_paths = set(observed)
    missing = sorted(expected_paths - observed_paths)
    added = sorted(observed_paths - expected_paths)
    changed = sorted(
        path for path in expected_paths & observed_paths if expected[path] != observed[path]
    )
    return missing, added, changed


def _fail(message: str) -> int:
    print(f"Pyright reference baseline rejected: {message}", file=sys.stderr)
    return 1


def main() -> int:
    try:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(f"cannot read {BASELINE_PATH.relative_to(ROOT)}: {exc}")

    if not isinstance(baseline, dict) or baseline.get("schema") != BASELINE_SCHEMA:
        return _fail("unsupported or malformed baseline schema")

    expected_version = baseline.get("pyright_version")
    expected_sources = baseline.get("source_hashes")
    expected_config = baseline.get("pyright_config")
    expected_diagnostics = baseline.get("diagnostics")
    if (
        not isinstance(expected_version, str)
        or not isinstance(expected_sources, dict)
        or not isinstance(expected_config, dict)
        or not isinstance(expected_diagnostics, list)
        or len(expected_diagnostics) != EXPECTED_DIAGNOSTIC_COUNT
    ):
        return _fail("baseline must pin the analyzer, config, 27 source hashes, and 25 diagnostics")
    if any(
        not isinstance(path, str)
        or not path.startswith(f"{REFERENCE_SOURCE_ROOT.as_posix()}/")
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        for path, digest in expected_sources.items()
    ):
        return _fail("baseline contains an invalid frozen-reference source hash")
    if len(expected_sources) != 27:
        return _fail("baseline must pin exactly 27 frozen-reference Python source files")
    if any(
        not isinstance(item, dict)
        or item.get("severity") != "error"
        or not isinstance(item.get("path"), str)
        or not item["path"].startswith(f"{REFERENCE_SOURCE_ROOT.as_posix()}/")
        for item in expected_diagnostics
    ):
        return _fail("baseline diagnostics must be errors confined to the frozen v5 reference")

    current_config = pyright_config(ROOT)
    if current_config != expected_config:
        return _fail(
            "[tool.pyright] changed; review the analyzer configuration and baseline together"
        )

    current_sources = source_hashes(ROOT)
    missing_sources, added_sources, changed_sources = source_hash_difference(
        expected_sources, current_sources
    )
    if missing_sources or added_sources or changed_sources:
        details = {
            "missing": missing_sources,
            "added": added_sources,
            "changed": changed_sources,
        }
        return _fail("frozen-reference source hashes changed:\n" + json.dumps(details, indent=2))

    pyright = shutil.which("pyright")
    if pyright is None:
        return _fail("the locked `pyright` executable is not on PATH")
    version_result = subprocess.run(
        [pyright, "--version"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    version_output = version_result.stdout.strip()
    version_match = re.fullmatch(r"pyright\s+(\S+)", version_output)
    if version_result.returncode != 0 or version_match is None:
        return _fail(
            f"cannot determine Pyright version: {version_output or version_result.stderr.strip()}"
        )
    current_version = version_match.group(1)
    if current_version != expected_version:
        return _fail(
            f"Pyright version changed: expected {expected_version}, observed {current_version}"
        )

    result = subprocess.run(
        [
            pyright,
            "--project",
            str(ROOT / "pyproject.toml"),
            "--pythonpath",
            sys.executable,
            "--outputjson",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
        observed_diagnostics = canonical_diagnostics(payload, ROOT)
    except (json.JSONDecodeError, ValueError) as exc:
        return _fail(f"Pyright did not return usable JSON: {exc}\n{result.stderr.strip()}")

    missing, unexpected = diagnostic_difference(expected_diagnostics, observed_diagnostics)
    summary = payload.get("summary", {})
    if (
        result.returncode != 1
        or not isinstance(summary, dict)
        or summary.get("errorCount") != EXPECTED_DIAGNOSTIC_COUNT
        or summary.get("warningCount") != 0
        or missing
        or unexpected
    ):
        print("Pyright diagnostic difference (full messages are compared):", file=sys.stderr)
        if missing:
            print("Missing expected diagnostics:", file=sys.stderr)
            for diagnostic in missing:
                print(json.dumps(diagnostic, ensure_ascii=False, indent=2), file=sys.stderr)
        if unexpected:
            print("Unexpected diagnostics:", file=sys.stderr)
            for diagnostic in unexpected:
                print(json.dumps(diagnostic, ensure_ascii=False, indent=2), file=sys.stderr)
        return _fail(
            f"expected 25 exact frozen-v5 diagnostics and no others; observed "
            f"{summary.get('errorCount', 'unknown')} errors and "
            f"{summary.get('warningCount', 'unknown')} warnings (exit {result.returncode})"
        )

    print(
        "Pyright reference baseline accepted: 25 exact known diagnostics from the frozen v5 "
        f"reference, no diagnostics elsewhere, {len(current_sources)} source hashes verified, "
        f"Pyright {current_version}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
