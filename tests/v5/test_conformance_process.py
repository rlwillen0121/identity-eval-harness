from __future__ import annotations

import base64
import importlib.metadata
import importlib.util
import json
import os
import py_compile
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

import pytest

from identitybench.v5 import conformance_process as process_module
from identitybench.v5.conformance_process import (
    FrozenEvaluator,
    SharedRuntimeError,
    SourceProfile,
    _case_index_sha256,
    _child_runner_source,
    _freeze_cases,
    _hash_stdlib_tree,
    _namespace_packages_for_profile,
    _parse_utc_timestamp_ns,
    _resolve_source_profile,
    _stdlib_roots,
    _validate_audit_receipt,
    _validate_case_index_binding,
    _validate_diagnostic_phase_batch_scope,
    _validate_relative_source,
    _validate_v1_proof_scope_count,
    _verify_artifact_chronology,
    _verify_capture_manifest,
    _verify_evaluator_audits,
    _verify_evaluator_receipts,
    _verify_full_shared_v3_inputs,
    _verify_full_shared_v3_protocol_sidecar,
    _verify_full_shared_v3_run_manifest,
    _verify_source_snapshot,
    capture_source_closure,
    run_frozen_evaluator,
    verify_blind_freeze_receipt,
)
from identitybench.v5.conformance_shared import (
    FROZEN_SPEC_SHA256,
    MATERIALIZATION_CONTRACT_ID,
    PINNED_PUBLIC_SCHEMA_SHA256,
    PROTOCOL,
    SCORE_METRIC_NULLABILITY_CONTRACT_ID,
    canonical_json_bytes,
    freeze_case,
    serialize_recipe_case,
    sha256_bytes,
    strict_json_loads,
    strict_jsonl_loads,
    validate_public_case,
)


def _public_case(case_id: str = "case-1") -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "case_id": case_id,
        "operation": "derive_phase",
        "suite_id": "v5_dev",
        "phase_index": 1,
        "public_episode": {
            "episode_id": "episode-1",
            "phase1_manifest": {},
            "phase1_slices": {},
            "policy_card": {},
            "q1_slice_ids_p1": [],
            "q2_slice_ids_p1": [],
        },
        "rendered_card": "",
        "schema_sha256": "9f7e60a00e36c72620fce06a60ce3968f03cdbd1144af7e253781c72d4f8d7d7",
        "stimulus": {},
    }


def _write_readonly_artifact(root: Path, relative: str, raw: bytes) -> dict[str, object]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o444)
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    return {"path": relative, "sha256": sha256_bytes(raw)}


def _small_v3_corpus(root: Path) -> dict[str, object]:
    """Build a hash-bound, complete miniature corpus-v3 fixture."""
    phase1 = _public_case("episode-1-p1")
    phase1["public_episode"] = {
        "episode_id": "episode-1",
        "phase1_manifest": {},
        "phase1_slices": {},
        "policy_card": {},
        "q1_slice_ids_p1": [],
        "q2_slice_ids_p1": [],
    }
    phase2 = _public_case("episode-1-p2")
    phase2["phase_index"] = 2
    phase2["public_episode"] = {
        "episode_id": "episode-1",
        "phase1_manifest": {},
        "phase1_slices": {},
        "phase2_base_manifest": {},
        "phase2_slices": {},
        "policy_card": {},
        "q1_slice_ids_p1": [],
        "q2_slice_ids_p1": [],
        "q1_slice_ids_p2": [],
        "q2_slice_ids_p2": [],
    }
    phase_rows = [validate_public_case(phase1), validate_public_case(phase2)]
    recipe_rows = [
        {
            "recipe_id": "recipe-safety",
            "input_ref": "episode-1-p1",
            "operation": "safety",
            "stimulus": {
                "submitted_plan": [],
                "target_selector": {"mutation_kind": "KIND", "target_entity_id": "entity-1"},
            },
        },
        {
            "recipe_id": "recipe-validate",
            "input_ref": "episode-1-p1",
            "operation": "validate_submission",
            "stimulus": {"submitted_plan": []},
        },
    ]
    trace_rows = [
        {
            "recipe_id": "trace-1",
            "input_ref": "episode-1-p2",
            "operation": "score_trace",
            "stimulus": {"mode": "COUPLED", "events": []},
        }
    ]
    recipe_cases = [serialize_recipe_case(phase_rows[0], row) for row in recipe_rows]
    trace_cases = [serialize_recipe_case(phase_rows[1], row) for row in trace_rows]
    frozen_cases = [freeze_case(case) for case in phase_rows] + recipe_cases + trace_cases

    input_root = "reports/v5/conformance/shared_inputs/v3/synthetic-release"
    phase_relative = "phase_inputs.jsonl"
    checkpoint_relative = "phase_checkpoint.json"
    recipes_relative = "stimuli/recipes.jsonl"
    traces_relative = "stimuli/traces.jsonl"
    family_relative = "family_map.json"
    phase_raw = b"".join(canonical_json_bytes(row) + b"\n" for row in phase_rows)
    recipes_raw = b"".join(canonical_json_bytes(row) + b"\n" for row in recipe_rows)
    traces_raw = b"".join(canonical_json_bytes(row) + b"\n" for row in trace_rows)
    family_raw = canonical_json_bytes({"version": "synthetic", "families": []}) + b"\n"
    phase_ref = _write_readonly_artifact(root, f"{input_root}/{phase_relative}", phase_raw)
    recipes_ref = _write_readonly_artifact(root, f"{input_root}/{recipes_relative}", recipes_raw)
    traces_ref = _write_readonly_artifact(root, f"{input_root}/{traces_relative}", traces_raw)
    family_ref = _write_readonly_artifact(root, f"{input_root}/{family_relative}", family_raw)

    def case_index_row(
        case: object, *, source: str, stimulus: dict[str, object] | None = None
    ) -> dict[str, object]:
        public = case.public_case  # type: ignore[attr-defined]
        frozen = case
        row: dict[str, object] = {
            "canonical_case_bytes": len(frozen.canonical_bytes),  # type: ignore[attr-defined]
            "canonical_case_sha256": frozen.input_sha256,  # type: ignore[attr-defined]
            "case_id": frozen.case_id,  # type: ignore[attr-defined]
            "episode_id": public["public_episode"]["episode_id"],
            "operation": public["operation"],
            "phase_index": public["phase_index"],
            "suite_id": public["suite_id"],
        }
        if stimulus is None:
            row["source_file"] = source
        else:
            row.update(
                {
                    "input_ref": stimulus["input_ref"],
                    "stimulus_record_id": stimulus["recipe_id"],
                    "stimulus_record_index": 0,
                    "stimulus_source_file": source,
                }
            )
        return row

    phase_index_rows = [
        case_index_row(case, source=phase_relative) for case in frozen_cases[: len(phase_rows)]
    ]
    recipe_index_rows = [
        case_index_row(case, source=recipes_relative, stimulus=record)
        for case, record in zip(recipe_cases, recipe_rows, strict=True)
    ]
    for index, row in enumerate(recipe_index_rows):
        row["stimulus_record_index"] = index
    trace_index_rows = [
        case_index_row(case, source=traces_relative, stimulus=record)
        for case, record in zip(trace_cases, trace_rows, strict=True)
    ]
    all_index_rows = phase_index_rows + recipe_index_rows + trace_index_rows
    normative = {
        "schemas_path": "src/identitybench/v5/schemas.py",
        "schemas_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
        "spec_path": "SPEC-v5.md",
        "spec_sha256": FROZEN_SPEC_SHA256,
    }
    phase_case_index = [
        {
            key: row[key]
            for key in (
                "case_id",
                "canonical_case_sha256",
                "canonical_case_bytes",
                "episode_id",
                "phase_index",
                "suite_id",
            )
        }
        for row in phase_index_rows
    ]
    phase_checkpoint = {
        "protocol": PROTOCOL,
        "corpus_version": "v3",
        "normative_inputs": normative,
        "phase_input_artifact": {
            "byte_length": len(phase_raw),
            "canonicalization": (
                "UTF-8 JSON; sorted keys; compact separators; trailing LF per record"
            ),
            "case_digest": "SHA-256 over each full canonical envelope without trailing LF",
            "path": phase_relative,
            "row_count": len(phase_rows),
            "sha256": phase_ref["sha256"],
        },
        "phase_case_index": phase_case_index,
        "phase_target_index": [],
    }
    checkpoint_raw = canonical_json_bytes(phase_checkpoint) + b"\n"
    checkpoint_ref = _write_readonly_artifact(
        root, f"{input_root}/{checkpoint_relative}", checkpoint_raw
    )
    counts_by_operation = {
        "derive_phase": len(phase_rows),
        "safety": 1,
        "score_trace": len(trace_rows),
        "validate_submission": len(recipe_rows) - 1,
    }
    manifest = {
        "artifact_status": "synthetic-test-only",
        "case_index": all_index_rows,
        "corpus_version": "v3",
        "counts": {
            "episodes": 1,
            "operations": counts_by_operation,
            "phase_cases": len(phase_rows),
            "phase_target_tuples": 0,
            "recipe_cases": len(recipe_rows),
            "total_cases": len(frozen_cases),
            "trace_cases": len(trace_rows),
        },
        "coverage_intent_only": {},
        "envelope_contract": {},
        "family_metadata": {"path": family_relative, "sha256": family_ref["sha256"]},
        "normative_inputs": normative,
        "observed_coverage": {},
        "packet_sources": [],
        "phase_checkpoint": {
            "path": checkpoint_relative,
            "phase_case_count": len(phase_rows),
            "phase_inputs_path": phase_relative,
            "phase_inputs_sha256": phase_ref["sha256"],
            "preserved_checkpoint_bytes": True,
            "sha256": checkpoint_ref["sha256"],
        },
        "protocol": PROTOCOL,
        "stimulus_sources": [
            {
                "copied_byte_exact_from": "synthetic test fixture",
                "path": recipes_relative,
                "record_count": len(recipe_rows),
                "sha256": recipes_ref["sha256"],
            },
            {
                "copied_byte_exact_from": "synthetic test fixture",
                "path": traces_relative,
                "record_count": len(trace_rows),
                "sha256": traces_ref["sha256"],
            },
        ],
    }
    manifest_raw = canonical_json_bytes(manifest) + b"\n"
    manifest_ref = _write_readonly_artifact(root, f"{input_root}/manifest.json", manifest_raw)
    public_raw = b"".join(case.canonical_bytes + b"\n" for case in frozen_cases)
    public_ref = _write_readonly_artifact(
        root,
        "reports/v5/conformance/shared_results/v1/synthetic-v3-public.jsonl",
        public_raw,
    )
    inventory = tuple((case.case_id, case.input_sha256) for case in frozen_cases)
    index_sha = _case_index_sha256(inventory, canonical_json_bytes, sha256_bytes)
    case_manifest_sha = sha256_bytes(
        canonical_json_bytes(
            {
                "protocol": PROTOCOL,
                "canonicalization": "canonical-json-v1",
                "cases": [
                    {"case_id": case_id, "input_sha256": digest}
                    for case_id, digest in inventory
                ],
            }
        )
    )
    artifacts = {
        "manifest": manifest_ref,
        "phase_inputs": phase_ref,
        "phase_checkpoint": checkpoint_ref,
        "recipes": recipes_ref,
        "traces": traces_ref,
        "family_map": family_ref,
    }
    return {
        "artifacts": artifacts,
        "case_index_sha256": index_sha,
        "case_inventory": inventory,
        "case_manifest_sha256": case_manifest_sha,
        "frozen_cases": frozen_cases,
        "input_root": input_root,
        "manifest": manifest,
        "public_ref": public_ref,
        "public_raw": public_raw,
        "scope": {
            "scope_id": "synthetic-small-corpus",
            "case_count": len(inventory),
            "case_index_sha256": index_sha,
            "operations": ["derive_phase", "safety", "score_trace", "validate_submission"],
            "phase_indices": [1, 2],
            "turn_context": "T0",
            "steering": "UNSTEERED",
        },
    }


def _run_child(
    tmp_path: Path,
    worker_body: str,
    input_bytes: bytes,
    *,
    dependency_body: str | None = None,
    dependency_files: dict[str, bytes] | None = None,
    stdlib_roots: tuple[Path, ...] | None = None,
    inherited_sys_path: tuple[Path, ...] = (),
    runtime_files: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    source_root = tmp_path / "source"
    dependency_root = tmp_path / "dependencies"
    worker_path = source_root / "tests/conformance/v5/worker.py"
    worker_path.parent.mkdir(parents=True)
    dependency_root.mkdir()
    worker_path.write_text(worker_body, encoding="utf-8")
    if dependency_body is not None:
        (dependency_root / "frozen_probe.py").write_text(dependency_body, encoding="utf-8")
    for relative, raw in (dependency_files or {}).items():
        target = dependency_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    script = _child_runner_source(
        source_root,
        dependency_root,
        worker_path,
        "test_worker",
        runtime_files=runtime_files,
        stdlib_roots=stdlib_roots,
        inherited_sys_path=inherited_sys_path,
    )
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        input=input_bytes,
        capture_output=True,
        check=False,
        cwd=source_root,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
        timeout=10,
    )


def _pydantic_dependency_files() -> dict[str, bytes]:
    purelib_value = sysconfig.get_path("purelib")
    assert purelib_value is not None
    purelib = Path(purelib_value).resolve(strict=True)
    files: dict[str, bytes] = {}
    for package in (
        "annotated-types",
        "pydantic",
        "pydantic-core",
        "typing-extensions",
        "typing-inspection",
    ):
        distribution = importlib.metadata.distribution(package)
        for entry in distribution.files or ():
            source = Path(str(distribution.locate_file(entry)))
            try:
                resolved = source.resolve(strict=True)
                relative = resolved.relative_to(purelib)
            except (OSError, ValueError):
                continue
            if source.is_symlink() or not resolved.is_file() or relative.suffix == ".pyc":
                continue
            files[relative.as_posix()] = resolved.read_bytes()
    return files


def _frozen_worker(
    tmp_path: Path,
    worker_body: str,
    *,
    dependency_body: str | None = None,
) -> FrozenEvaluator:
    temp_manager = tempfile.TemporaryDirectory(dir=tmp_path)
    base = Path(temp_manager.name)
    stage_root = base / "source"
    dependency_root = base / "dependencies"
    worker = stage_root / "tests/conformance/v5/worker.py"
    worker.parent.mkdir(parents=True)
    dependency_root.mkdir()
    worker_bytes = worker_body.encode("utf-8")
    worker.write_bytes(worker_bytes)
    hashes = {f"source/{worker.relative_to(stage_root).as_posix()}": sha256_bytes(worker_bytes)}
    if dependency_body is not None:
        dependency = dependency_root / "frozen_probe.py"
        dependency_bytes = dependency_body.encode("utf-8")
        dependency.write_bytes(dependency_bytes)
        hashes[f"dependencies/{dependency.name}"] = sha256_bytes(dependency_bytes)
    return FrozenEvaluator(
        evaluator_id="test_worker",
        source_profile="test_synthetic",
        author_origin="test_fixture",
        entrypoint="tests/conformance/v5/worker.py",
        entrypoint_module=None,
        source_root=tmp_path,
        stage_root=stage_root,
        dependency_root=dependency_root,
        source_bytes={},
        staged_file_hashes=hashes,
        runtime_file_hashes={},
        case_bytes={"case-1": b"{}"},
        case_sha256={"case-1": sha256_bytes(b"{}")},
        case_manifest_sha256="0" * 64,
        source_closure_sha256="1" * 64,
        manifest={},
        blind_freeze=None,
        _temporary_directory=temp_manager,
    )


def test_freeze_cases_binds_canonical_bytes_and_rejects_duplicate_ids() -> None:
    cases, hashes, manifest_sha = _freeze_cases(
        [_public_case()], canonical_json_bytes, sha256_bytes, validate_public_case
    )

    expected = canonical_json_bytes(_public_case())
    assert cases == {"case-1": expected}
    assert hashes == {"case-1": sha256_bytes(expected)}
    assert manifest_sha == sha256_bytes(
        canonical_json_bytes(
            {
                "protocol": PROTOCOL,
                "canonicalization": "canonical-json-v1",
                "cases": [{"case_id": "case-1", "input_sha256": sha256_bytes(expected)}],
            }
        )
    )
    with pytest.raises(SharedRuntimeError, match="duplicate public case id"):
        _freeze_cases(
            [_public_case(), _public_case()],
            canonical_json_bytes,
            sha256_bytes,
            validate_public_case,
        )


@pytest.mark.parametrize(
    "path",
    [
        "../datasets/v5_dev/oracles.jsonl",
        "src/identitybench/v5/oracle.py",
        "src/identitybench/v5/generator.py",
        "reports/v5/private.json",
    ],
)
def test_source_closure_rejects_forbidden_or_traversing_paths(path: str) -> None:
    with pytest.raises(SharedRuntimeError):
        _validate_relative_source(path)


def test_capture_rejects_paths_outside_the_evaluator_allowlist(tmp_path: Path) -> None:
    with pytest.raises(SharedRuntimeError, match="exact evaluator allowlist"):
        capture_source_closure(
            "full_spec",
            approved_paths=("../outside.py",),
            repository_root=tmp_path,
            result_root=tmp_path / "capture",
        )


def test_fresh_full_spec_profile_is_pinned_and_excludes_retired_sources() -> None:
    fresh = _resolve_source_profile("full_spec")
    historical = _resolve_source_profile("full_spec", "fresh_full_spec_v2")
    legacy = _resolve_source_profile("full_spec", "legacy_diagnostic")

    assert fresh.profile_id == "fresh_full_spec_v3"
    assert fresh.author_origin == "fresh_full_spec_v2"
    assert fresh.entrypoint == "tests/conformance/v5/fresh_full_spec_v2/worker.py"
    assert fresh.entrypoint_module == "tests.conformance.v5.fresh_full_spec_v2.worker"
    assert fresh.source_files == (
        "src/identitybench/__init__.py",
        "src/identitybench/v5/__init__.py",
        "src/identitybench/v5/schemas.py",
        "SPEC-v5.md",
        "tests/__init__.py",
        "tests/conformance/__init__.py",
        "tests/conformance/v5/fresh_full_spec_v2/__init__.py",
        "tests/conformance/v5/fresh_full_spec_v2/common.py",
        "tests/conformance/v5/fresh_full_spec_v2/policy.py",
        "tests/conformance/v5/fresh_full_spec_v2/phase1.py",
        "tests/conformance/v5/fresh_full_spec_v2/phase2.py",
        "tests/conformance/v5/fresh_full_spec_v2/submission.py",
        "tests/conformance/v5/fresh_full_spec_v2/apply.py",
        "tests/conformance/v5/fresh_full_spec_v2/trace.py",
        "tests/conformance/v5/fresh_full_spec_v2/worker.py",
    )
    assert historical.profile_id == "fresh_full_spec_v2"
    assert historical.author_origin == "fresh_independent_full_spec_author"
    assert historical.entrypoint == "tests/conformance/v5/fresh_full_spec_v2/worker.py"
    assert historical.source_files != fresh.source_files
    assert "tests/conformance/v5/test_conformance_v5.py" not in fresh.source_files
    assert "tests/conformance/v5/shared_full_spec_worker.py" not in fresh.source_files
    assert "tests/conformance/v5/fresh_full_spec_v2/apply.py" in fresh.source_files
    assert "tests/conformance/v5/fresh_full_spec_v2/trace.py" in fresh.source_files
    assert "tests/conformance/v5/fresh_full_spec_v2/apply.py" not in historical.source_files
    assert legacy.profile_id == "legacy_diagnostic"
    assert legacy.author_origin.startswith("retired_diagnostic")
    assert legacy.source_files != fresh.source_files
    with pytest.raises(SharedRuntimeError, match="source profile is not allowlisted"):
        _resolve_source_profile("full_spec", "caller_supplied_profile")


def test_fresh_card_only_profile_is_exact_and_excludes_spec_and_old_references() -> None:
    fresh = _resolve_source_profile("agent_view")
    historical = _resolve_source_profile("agent_view", "agent_view_v1")

    assert fresh.profile_id == "fresh_card_only_v2"
    assert fresh.author_origin == "fresh_independent_card_only_author_v2"
    assert fresh.entrypoint == "tests/conformance/v5/fresh_card_only_v2/worker.py"
    assert fresh.entrypoint_module is None
    assert fresh.source_files == (
        "src/identitybench/v5/schemas.py",
        "tests/conformance/v5/fresh_card_only_v2/worker.py",
    )
    assert "SPEC-v5.md" not in fresh.source_files
    assert not any("reference" in path or "old" in path for path in fresh.source_files)
    assert historical.profile_id == "agent_view_v1"
    assert historical.source_files != fresh.source_files


def test_fresh_full_spec_v3_profile_reuses_the_authors_v2_runtime_directory() -> None:
    profile = _resolve_source_profile("full_spec", "fresh_full_spec_v3")

    assert profile.profile_id == "fresh_full_spec_v3"
    assert profile.entrypoint == "tests/conformance/v5/fresh_full_spec_v2/worker.py"
    assert profile.entrypoint_module == "tests.conformance.v5.fresh_full_spec_v2.worker"
    versioned_paths = [
        path for path in profile.source_files if path.startswith("tests/conformance/v5/")
    ]
    assert versioned_paths
    assert all("tests/conformance/v5/fresh_full_spec_v2/" in path for path in versioned_paths)


def test_bounded_full_spec_v1_profile_is_exact_and_preflights_regular_sources() -> None:
    profile = _resolve_source_profile("full_spec", "bounded_full_spec_v1")

    assert profile.profile_id == "bounded_full_spec_v1"
    assert profile.author_origin == "fresh_bounded_full_spec_author_v1"
    assert profile.entrypoint == "tests/conformance/v5/bounded_full_spec_reference/worker.py"
    assert (
        profile.entrypoint_module
        == "tests.conformance.v5.bounded_full_spec_reference.worker"
    )
    assert profile.source_files == (
        "src/identitybench/__init__.py",
        "src/identitybench/v5/__init__.py",
        "src/identitybench/v5/schemas.py",
        "src/identitybench/v5/scope_algebra.py",
        "tests/__init__.py",
        "tests/conformance/__init__.py",
        "tests/conformance/v5/bounded_full_spec_reference/__init__.py",
        "tests/conformance/v5/bounded_full_spec_reference/common.py",
        "tests/conformance/v5/bounded_full_spec_reference/phase1.py",
        "tests/conformance/v5/bounded_full_spec_reference/phase2.py",
        "tests/conformance/v5/bounded_full_spec_reference/submission.py",
        "tests/conformance/v5/bounded_full_spec_reference/safety.py",
        "tests/conformance/v5/bounded_full_spec_reference/trace.py",
        "tests/conformance/v5/bounded_full_spec_reference/worker.py",
    )
    assert "SPEC-v5.md" not in profile.source_files
    assert not any("test_reference" in path for path in profile.source_files)
    assert not any("audit" in path for path in profile.source_files)

    repository_root = Path(__file__).resolve().parents[2]
    for relative in profile.source_files:
        source = repository_root / relative
        assert source.is_file() and not source.is_symlink()
        assert source.stat().st_size >= 0


def test_bounded_full_spec_capture_roundtrips_synthetic_no_spec_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _resolve_source_profile("full_spec", "bounded_full_spec_v1")
    synthetic_schema = b"# synthetic public schema\n"
    monkeypatch.setattr(process_module, "_SCHEMA_SHA256", sha256_bytes(synthetic_schema))
    for relative in profile.source_files:
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(
            synthetic_schema
            if relative == "src/identitybench/v5/schemas.py"
            else f"# synthetic bounded source: {relative}\n".encode()
        )

    capture = capture_source_closure(
        "full_spec",
        source_profile=profile.profile_id,
        repository_root=tmp_path,
        result_root=tmp_path / "capture",
    )
    snapshot_root = tmp_path / "capture"
    _verify_capture_manifest(
        "full_spec",
        snapshot_root,
        profile.source_files,
        sha256_bytes,
        profile,
    )

    assert capture["source_profile"] == "bounded_full_spec_v1"
    assert capture["author_origin"] == "fresh_bounded_full_spec_author_v1"
    assert capture["schema_sha256"] == sha256_bytes(synthetic_schema)
    assert "SPEC-v5.md" not in [row["path"] for row in capture["source_files"]]


def test_spec_bearing_full_spec_capture_keeps_the_pinned_spec_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _resolve_source_profile("full_spec", "fresh_full_spec_v2")
    synthetic_schema = b"# synthetic public schema\n"
    monkeypatch.setattr(process_module, "_SCHEMA_SHA256", sha256_bytes(synthetic_schema))
    for relative in profile.source_files:
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(
            synthetic_schema
            if relative == "src/identitybench/v5/schemas.py"
            else b"# synthetic source\n"
        )

    with pytest.raises(SharedRuntimeError, match=r"SPEC-v5\.md changed"):
        capture_source_closure(
            "full_spec",
            source_profile=profile.profile_id,
            repository_root=tmp_path,
            result_root=tmp_path / "capture",
        )


def test_bounded_full_spec_v1_package_imports_only_synthetic_pinned_modules(
    tmp_path: Path,
) -> None:
    profile = _resolve_source_profile("full_spec", "bounded_full_spec_v1")
    source_root = tmp_path / "source"
    dependency_root = tmp_path / "dependencies"
    dependency_root.mkdir()
    package_root = source_root / "tests/conformance/v5/bounded_full_spec_reference"
    package_root.mkdir(parents=True)
    for relative in (
        "tests/__init__.py",
        "tests/conformance/__init__.py",
        "tests/conformance/v5/bounded_full_spec_reference/__init__.py",
    ):
        target = source_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# inert synthetic package initializer\n", encoding="utf-8")
    module_names = ("common", "phase1", "phase2", "submission", "safety", "trace")
    for module_name in module_names:
        (package_root / f"{module_name}.py").write_text(
            f"MARKER = {module_name!r}\n", encoding="utf-8"
        )
    (package_root / "worker.py").write_text(
        "from .common import MARKER as COMMON\n"
        "from .phase1 import MARKER as PHASE1\n"
        "from .phase2 import MARKER as PHASE2\n"
        "from .submission import MARKER as SUBMISSION\n"
        "from .safety import MARKER as SAFETY\n"
        "from .trace import MARKER as TRACE\n"
        "def evaluate_shared_input(case):\n"
        "    return {'case_id': case['case_id'], 'markers': "
        "[COMMON, PHASE1, PHASE2, SUBMISSION, SAFETY, TRACE]}\n",
        encoding="utf-8",
    )
    worker = source_root / profile.entrypoint
    script = _child_runner_source(
        source_root,
        dependency_root,
        worker,
        "full_spec",
        profile.entrypoint_module,
        source_profile=profile.profile_id,
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        input=canonical_json_bytes({"case_id": "synthetic-package-case"}),
        capture_output=True,
        check=False,
        cwd=source_root,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
        timeout=10,
    )

    assert completed.returncode == 0
    assert strict_json_loads(completed.stdout) == {
        "case_id": "synthetic-package-case",
        "markers": ["common", "phase1", "phase2", "submission", "safety", "trace"],
    }
    audit = strict_json_loads(completed.stderr)
    assert isinstance(audit, dict)
    for module_name in module_names:
        assert (
            f"source:tests/conformance/v5/bounded_full_spec_reference/{module_name}.py"
            in audit["read_paths"]
        )
    _validate_audit_receipt(audit)


def test_fresh_full_spec_v3_entrypoint_loads_only_pinned_relative_modules(
    tmp_path: Path,
) -> None:
    profile = _resolve_source_profile("full_spec", "fresh_full_spec_v3")
    source_root = tmp_path / "source"
    dependency_root = tmp_path / "dependencies"
    dependency_root.mkdir()
    package_root = source_root / "tests/conformance/v5/fresh_full_spec_v2"
    package_root.mkdir(parents=True)
    for relative in (
        "tests/__init__.py",
        "tests/conformance/__init__.py",
        "tests/conformance/v5/fresh_full_spec_v2/__init__.py",
    ):
        target = source_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# inert pinned package initializer\n", encoding="utf-8")
    modules = {
        "common.py": "COMMON = 'common'\n",
        "policy.py": "POLICY = 'policy'\n",
        "phase1.py": "PHASE1 = 'phase1'\n",
        "phase2.py": "PHASE2 = 'phase2'\n",
        "submission.py": "SUBMISSION = 'submission'\n",
        "apply.py": "APPLY = 'apply'\n",
        "trace.py": "TRACE = 'trace'\n",
    }
    for filename, body in modules.items():
        (package_root / filename).write_text(body, encoding="utf-8")
    worker = package_root / "worker.py"
    worker.write_text(
        "from .apply import APPLY\n"
        "from .common import COMMON\n"
        "from .trace import TRACE\n"
        "def evaluate_shared_input(case):\n"
        "    return {'case_id': case['case_id'], 'markers': [COMMON, APPLY, TRACE]}\n",
        encoding="utf-8",
    )
    script = _child_runner_source(
        source_root,
        dependency_root,
        worker,
        "full_spec",
        profile.entrypoint_module,
        source_profile=profile.profile_id,
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        input=canonical_json_bytes({"case_id": "v3-loader-case"}),
        capture_output=True,
        check=False,
        cwd=source_root,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
        timeout=10,
    )

    assert completed.returncode == 0
    assert strict_json_loads(completed.stdout) == {
        "case_id": "v3-loader-case",
        "markers": ["common", "apply", "trace"],
    }
    audit = strict_json_loads(completed.stderr)
    assert isinstance(audit, dict)
    assert "source:tests/conformance/v5/fresh_full_spec_v2/common.py" in audit["read_paths"]
    assert "source:tests/conformance/v5/fresh_full_spec_v2/apply.py" in audit["read_paths"]
    assert "source:tests/conformance/v5/fresh_full_spec_v2/trace.py" in audit["read_paths"]
    assert all("fresh_full_spec_v3" not in path for path in audit["read_paths"])
    _validate_audit_receipt(audit)


def test_fresh_card_only_v2_worker_uses_bare_loader_and_only_direct_schema(
    tmp_path: Path,
) -> None:
    profile = _resolve_source_profile("agent_view", "fresh_card_only_v2")
    source_root = tmp_path / "source"
    dependency_root = tmp_path / "dependencies"
    dependency_root.mkdir()
    worker = source_root / profile.entrypoint
    worker.parent.mkdir(parents=True)
    worker.write_text(
        "import importlib.util, sys\n"
        "from pathlib import Path\n"
        "schema_path = Path(__file__).resolve().parents[4] / 'src/identitybench/v5/schemas.py'\n"
        "schema_name = '_fresh_card_only_test_schema'\n"
        "schema_spec = importlib.util.spec_from_file_location(schema_name, schema_path)\n"
        "assert schema_spec is not None and schema_spec.loader is not None\n"
        "schema_module = importlib.util.module_from_spec(schema_spec)\n"
        "sys.modules[schema_name] = schema_module\n"
        "schema_spec.loader.exec_module(schema_module)\n"
        "def evaluate_shared_input(case):\n"
        "    return {'case_id': case['case_id'], 'schema_marker': schema_module.MARKER}\n",
        encoding="utf-8",
    )
    schema_path = source_root / "src/identitybench/v5/schemas.py"
    schema_path.parent.mkdir(parents=True)
    schema_path.write_text("MARKER = 'pinned-public-schema'\n", encoding="utf-8")
    script = _child_runner_source(
        source_root,
        dependency_root,
        worker,
        "agent_view",
        profile.entrypoint_module,
        source_profile=profile.profile_id,
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        input=canonical_json_bytes({"case_id": "card-only-loader-case"}),
        capture_output=True,
        check=False,
        cwd=source_root,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
        timeout=10,
    )

    assert completed.returncode == 0
    assert strict_json_loads(completed.stdout) == {
        "case_id": "card-only-loader-case",
        "schema_marker": "pinned-public-schema",
    }
    audit = strict_json_loads(completed.stderr)
    assert isinstance(audit, dict)
    assert "source:src/identitybench/v5/schemas.py" in audit["read_paths"]
    assert not any("__init__.py" in path for path in audit["read_paths"])
    _validate_audit_receipt(audit)


def test_production_v2_profile_adds_only_the_missing_transitive_source() -> None:
    production_v1 = _resolve_source_profile("production", "production_v1")
    production_v2 = _resolve_source_profile("production", "production_v2")

    assert len(production_v1.source_files) == 15
    assert "src/identitybench/v5/oracle_requests.py" not in production_v1.source_files
    assert production_v2.profile_id == "production_v2"
    assert production_v2.author_origin == production_v1.author_origin
    assert production_v2.source_files == (
        *production_v1.source_files,
        "src/identitybench/v5/oracle_requests.py",
    )
    assert production_v2.entrypoint == production_v1.entrypoint
    assert production_v2.entrypoint_module is None


def test_production_v3_profile_closes_cold_scoring_import_with_v3_schema(
    tmp_path: Path,
) -> None:
    production_v1 = _resolve_source_profile("production", "production_v1")
    production_v2 = _resolve_source_profile("production", "production_v2")
    production_v3 = _resolve_source_profile("production", "production_v3")

    assert len(production_v1.source_files) == 15
    assert production_v2.source_files == (
        *production_v1.source_files,
        "src/identitybench/v5/oracle_requests.py",
    )
    assert len(production_v2.source_files) == 16
    assert production_v3.source_files == (
        *production_v2.source_files,
        "src/identitybench/v3/schemas.py",
    )
    assert len(production_v3.source_files) == 17
    assert _resolve_source_profile("production").profile_id == "production_v1"
    assert production_v3.author_origin == production_v2.author_origin
    assert production_v3.entrypoint == production_v2.entrypoint
    assert production_v3.entrypoint_module is None
    assert _namespace_packages_for_profile(production_v1) == ["identitybench.v4"]
    assert _namespace_packages_for_profile(production_v2) == ["identitybench.v4"]
    assert _namespace_packages_for_profile(production_v3) == [
        "identitybench.v3",
        "identitybench.v4",
    ]

    def cold_import(
        profile: SourceProfile, profile_name: str
    ) -> subprocess.CompletedProcess[bytes]:
        source_root = tmp_path / profile_name / "source"
        dependency_root = tmp_path / profile_name / "dependencies"
        dependency_root.mkdir(parents=True)
        for relative in profile.source_files:
            target = source_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# synthetic inert production closure\n", encoding="utf-8")
        (source_root / "src/identitybench/v5/conformance_production.py").write_text(
            "from identitybench.v5 import scoring\n"
            "def evaluate_shared_input(case):\n"
            "    return {'case_id': case['case_id'], 'marker': scoring.V3_SCHEMA_MARKER}\n",
            encoding="utf-8",
        )
        (source_root / "src/identitybench/v5/scoring.py").write_text(
            "from identitybench.v4.jcs import V3_SCHEMA_MARKER\n",
            encoding="utf-8",
        )
        (source_root / "src/identitybench/v4/jcs.py").write_text(
            "from identitybench.v4.schemas import V3_SCHEMA_MARKER\n",
            encoding="utf-8",
        )
        (source_root / "src/identitybench/v4/schemas.py").write_text(
            "from identitybench.v3.schemas import V3_SCHEMA_MARKER\n",
            encoding="utf-8",
        )
        if profile_name == "production_v3":
            (source_root / "src/identitybench/v3/schemas.py").write_text(
                "V3_SCHEMA_MARKER = 'pinned-v3-schema'\n",
                encoding="utf-8",
            )
        entrypoint = source_root / profile.entrypoint
        runner = _child_runner_source(
            source_root,
            dependency_root,
            entrypoint,
            "production",
            profile.entrypoint_module,
            source_profile=profile.profile_id,
        )
        return subprocess.run(
            [sys.executable, "-I", "-S", "-c", runner],
            input=canonical_json_bytes({"case_id": "synthetic-closure-probe"}),
            capture_output=True,
            check=False,
            cwd=source_root,
            env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
            timeout=10,
        )

    red = cold_import(production_v2, "production_v2")
    assert red.returncode == 70
    assert strict_json_loads(red.stdout) == {"worker_error": "ModuleNotFoundError"}
    red_audit = strict_json_loads(red.stderr)
    assert isinstance(red_audit, dict)
    assert {
        "identitybench.v5.scoring",
        "identitybench.v4.jcs",
        "identitybench.v4.schemas",
        "identitybench.v3.schemas",
    } <= set(red_audit["import_names"])
    _validate_audit_receipt(red_audit)

    green = cold_import(production_v3, "production_v3")
    assert green.returncode == 0
    assert strict_json_loads(green.stdout) == {
        "case_id": "synthetic-closure-probe",
        "marker": "pinned-v3-schema",
    }
    green_audit = strict_json_loads(green.stderr)
    assert isinstance(green_audit, dict)
    assert "source:src/identitybench/v3/schemas.py" in green_audit["read_paths"]
    assert "identitybench.v3.schemas" in green_audit["import_names"]
    _validate_audit_receipt(green_audit)


def test_fresh_full_spec_entrypoint_loads_in_pinned_package_context(tmp_path: Path) -> None:
    profile = _resolve_source_profile("full_spec", "fresh_full_spec_v2")
    source_root = tmp_path / "source"
    dependency_root = tmp_path / "dependencies"
    dependency_root.mkdir()
    package_root = source_root / "tests/conformance/v5/fresh_full_spec_v2"
    package_root.mkdir(parents=True)
    (source_root / "tests/__init__.py").parent.mkdir(parents=True, exist_ok=True)
    (source_root / "tests/__init__.py").write_text("", encoding="utf-8")
    (source_root / "tests/conformance/__init__.py").parent.mkdir(parents=True, exist_ok=True)
    (source_root / "tests/conformance/__init__.py").write_text("", encoding="utf-8")
    (package_root / "__init__.py").write_text("", encoding="utf-8")
    (package_root / "common.py").write_text("MARKER = 'loaded-relative-module'\n", encoding="utf-8")
    worker = package_root / "worker.py"
    worker.write_text(
        "from .common import MARKER\n"
        "def evaluate_shared_input(case):\n"
        "    return {'marker': MARKER, 'case_id': case['case_id']}\n",
        encoding="utf-8",
    )
    script = _child_runner_source(
        source_root,
        dependency_root,
        worker,
        "full_spec",
        profile.entrypoint_module,
        source_profile=profile.profile_id,
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script],
        input=canonical_json_bytes({"case_id": "loader-case"}),
        capture_output=True,
        check=False,
        cwd=source_root,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
        timeout=10,
    )

    assert completed.returncode == 0
    assert strict_json_loads(completed.stdout) == {
        "case_id": "loader-case",
        "marker": "loaded-relative-module",
    }
    audit = strict_json_loads(completed.stderr)
    assert isinstance(audit, dict)
    assert "source:tests/conformance/v5/fresh_full_spec_v2/common.py" in audit["read_paths"]
    _validate_audit_receipt(audit)

    with pytest.raises(SharedRuntimeError, match="pinned profile"):
        _child_runner_source(
            source_root,
            dependency_root,
            worker,
            "full_spec",
            "caller.supplied.module",
            source_profile=profile.profile_id,
        )


def test_child_returns_only_strict_json_and_records_read_paths(tmp_path: Path) -> None:
    public_case = {"array": ["first", "second"]}
    completed = _run_child(
        tmp_path,
        "def evaluate_shared_input(case):\n    return {'array': case['array'], 'status': 'ok'}\n",
        json.dumps(public_case).encode("utf-8"),
    )

    assert completed.returncode == 0
    assert strict_json_loads(completed.stdout) == {
        "array": ["first", "second"],
        "status": "ok",
    }
    audit = strict_json_loads(completed.stderr)
    assert isinstance(audit, dict)
    assert "source:tests/conformance/v5/worker.py" in audit["read_paths"]
    _validate_audit_receipt(audit)


def test_bare_worker_loader_registers_future_pydantic_annotations(tmp_path: Path) -> None:
    worker = (
        "from __future__ import annotations\n"
        "from collections.abc import Mapping\n"
        "from pydantic import BaseModel, ConfigDict, StrictStr\n"
        "class PublicProjection(BaseModel):\n"
        "    model_config = ConfigDict(strict=True)\n"
        "    evidence: Mapping[str, StrictStr]\n"
        "def evaluate_shared_input(case):\n"
        "    projection = PublicProjection(**case)\n"
        "    return {'evidence': dict(projection.evidence), 'status': 'ok'}\n"
    )
    completed = _run_child(
        tmp_path,
        worker,
        b'{"evidence":{"public":"bound"}}',
        dependency_files=_pydantic_dependency_files(),
        runtime_files=("/System/Library/CoreServices/SystemVersion.plist",),
    )

    assert completed.returncode == 0
    assert strict_json_loads(completed.stdout) == {
        "evidence": {"public": "bound"},
        "status": "ok",
    }
    audit = strict_json_loads(completed.stderr)
    assert isinstance(audit, dict)
    _validate_audit_receipt(audit)


def test_bare_worker_loader_registers_future_dataclass_annotations(tmp_path: Path) -> None:
    worker = (
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "from typing import ClassVar\n"
        "@dataclass\n"
        "class PublicRecord:\n"
        "    marker: ClassVar[str] = 'loaded'\n"
        "    value: str\n"
        "def evaluate_shared_input(case):\n"
        "    record = PublicRecord(value=case['value'])\n"
        "    return {'marker': record.marker, 'value': record.value}\n"
    )
    completed = _run_child(tmp_path, worker, b'{"value":"public"}')

    assert completed.returncode == 0
    assert strict_json_loads(completed.stdout) == {
        "marker": "loaded",
        "value": "public",
    }
    audit = strict_json_loads(completed.stderr)
    assert isinstance(audit, dict)
    _validate_audit_receipt(audit)


def test_only_copied_dependencies_import_not_live_site_packages(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime" / "python3.12"
    global_site_packages = runtime_root / "site-packages"
    global_site_packages.mkdir(parents=True)
    (global_site_packages / "live_only.py").write_text("SOURCE = 'LIVE'\n", encoding="utf-8")
    completed = _run_child(
        tmp_path / "worker",
        (
            "from frozen_probe import SOURCE as FROZEN_SOURCE\n"
            "try:\n"
            "    import live_only\n"
            "    LIVE_AVAILABLE = True\n"
            "except ModuleNotFoundError:\n"
            "    LIVE_AVAILABLE = False\n"
            "def evaluate_shared_input(case):\n"
            "    return {'frozen': FROZEN_SOURCE, 'live_available': LIVE_AVAILABLE}\n"
        ),
        b"{}",
        dependency_body="SOURCE = 'FROZEN'\n",
        stdlib_roots=(runtime_root,),
        inherited_sys_path=(global_site_packages,),
    )

    assert completed.returncode == 0
    assert strict_json_loads(completed.stdout) == {
        "frozen": "FROZEN",
        "live_available": False,
    }
    audit = strict_json_loads(completed.stderr)
    assert isinstance(audit, dict)
    assert "dependency:frozen_probe.py" in audit["read_paths"]
    assert all("live_only.py" not in path for path in audit["read_paths"])
    _validate_audit_receipt(audit)


def test_live_native_extension_is_denied_before_loader_and_frozen_extension_loads(
    tmp_path: Path,
) -> None:
    live_root = tmp_path / "runtime" / "python3.12"
    live_site_packages = live_root / "site-packages"
    live_site_packages.mkdir(parents=True)
    fake_native = live_site_packages / "native_probe.cpython-312-darwin.so"
    fake_native.write_bytes(b"not executed")
    native_spec = importlib.util.find_spec("pydantic_core._pydantic_core")
    assert native_spec is not None and native_spec.origin is not None
    native_name = Path(native_spec.origin).name
    native_bytes = Path(native_spec.origin).read_bytes()
    frozen_native = tmp_path / "frozen" / "dependencies" / "pydantic_core" / native_name
    blocked = _run_child(
        tmp_path / "live",
        (
            "import importlib.util\n"
            f"spec = importlib.util.spec_from_file_location('native_probe', {str(fake_native)!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "def evaluate_shared_input(case):\n"
            "    return {'status': 'unreachable'}\n"
        ),
        b"{}",
        stdlib_roots=(live_root,),
        inherited_sys_path=(live_site_packages,),
    )
    assert blocked.returncode == 70
    blocked_audit = strict_json_loads(blocked.stderr)
    assert isinstance(blocked_audit, dict)
    assert "import-site-packages:native_probe" in blocked_audit["blocked_events"]

    completed = _run_child(
        tmp_path / "frozen",
        (
            "import importlib.util\n"
            f"native_path = {str(frozen_native)!r}\n"
            "spec = importlib.util.spec_from_file_location('_pydantic_core', native_path)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "def evaluate_shared_input(case):\n"
            "    return {'version': module.__version__}\n"
        ),
        b"{}",
        dependency_files={f"pydantic_core/{native_name}": native_bytes},
    )
    assert completed.returncode == 0
    assert strict_json_loads(completed.stdout) == {"version": "2.50.0"}
    frozen_audit = strict_json_loads(completed.stderr)
    assert isinstance(frozen_audit, dict)
    assert any(
        path.startswith("_pydantic_core=dependency:pydantic_core/")
        for path in frozen_audit["import_paths"]
    )
    _validate_audit_receipt(frozen_audit)


def test_success_and_failure_receipts_preserve_raw_output_and_digests(
    tmp_path: Path,
) -> None:
    successful = _frozen_worker(
        tmp_path,
        "def evaluate_shared_input(case):\n    return {'status': 'ok'}\n",
    )
    try:
        success_receipt = run_frozen_evaluator(successful, "test_worker", "case-1")
    finally:
        successful.close()
    success_stdout = base64.b64decode(success_receipt["raw_stdout_b64"])
    success_stderr = base64.b64decode(success_receipt["raw_stderr_b64"])
    assert success_receipt["execution_status"] == "ok"
    assert sha256_bytes(success_stdout) == success_receipt["result_sha256"]
    assert sha256_bytes(success_stdout) == success_receipt["stdout_sha256"]
    assert sha256_bytes(success_stderr) == success_receipt["stderr_sha256"]
    assert strict_json_loads(success_stdout) == {"status": "ok"}
    assert strict_json_loads(success_stderr) == success_receipt["audit"]

    failed = _frozen_worker(
        tmp_path,
        (
            "def evaluate_shared_input(case):\n"
            "    open('/etc/passwd', encoding='utf-8').read()\n"
            "    return {'status': 'ok'}\n"
        ),
    )
    try:
        failure_receipt = run_frozen_evaluator(failed, "test_worker", "case-1")
    finally:
        failed.close()
    failure_stdout = base64.b64decode(failure_receipt["raw_stdout_b64"])
    failure_stderr = base64.b64decode(failure_receipt["raw_stderr_b64"])
    assert failure_receipt["execution_status"] == "failed"
    assert sha256_bytes(failure_stdout) == failure_receipt["stdout_sha256"]
    assert sha256_bytes(failure_stderr) == failure_receipt["stderr_sha256"]
    assert strict_json_loads(failure_stdout) == {"worker_error": "PermissionError"}
    assert failure_receipt["audit"]["blocked_events"]


def test_full_shared_v3_integrity_binds_exact_declared_four_operation_inventory(
    tmp_path: Path,
) -> None:
    fixture = _small_v3_corpus(tmp_path)
    public_path = tmp_path / fixture["public_ref"]["path"]  # type: ignore[index]
    public_raw = public_path.read_bytes()
    input_rows = strict_jsonl_loads(public_raw)
    inventory = fixture["case_inventory"]
    assert isinstance(inventory, tuple)
    scope_id, bound = _verify_full_shared_v3_inputs(
        tmp_path,
        fixture["scope"],
        fixture["artifacts"],
        public_path,
        fixture["public_ref"],
        input_rows,
        inventory,
        case_manifest_sha256=fixture["case_manifest_sha256"],
        case_index_sha256=fixture["case_index_sha256"],
        canonical_json_bytes=canonical_json_bytes,
        sha256_bytes=sha256_bytes,
        strict_json_loads=strict_json_loads,
        strict_jsonl_loads=strict_jsonl_loads,
        validate_public_case=validate_public_case,
    )

    assert scope_id == "synthetic-small-corpus"
    assert len(inventory) == 5
    assert {case["operation"] for case in input_rows} == {
        "derive_phase",
        "safety",
        "score_trace",
        "validate_submission",
    }
    assert {path for path, _ in bound} >= {
        tmp_path / reference["path"] for reference in fixture["artifacts"].values()
    }


def test_full_shared_v3_integrity_rejects_reordered_and_incomplete_inventory(
    tmp_path: Path,
) -> None:
    fixture = _small_v3_corpus(tmp_path)
    public_path = tmp_path / fixture["public_ref"]["path"]  # type: ignore[index]
    input_rows = strict_jsonl_loads(public_path.read_bytes())
    inventory = fixture["case_inventory"]
    assert isinstance(inventory, tuple)
    kwargs = {
        "case_manifest_sha256": fixture["case_manifest_sha256"],
        "canonical_json_bytes": canonical_json_bytes,
        "sha256_bytes": sha256_bytes,
        "strict_json_loads": strict_json_loads,
        "strict_jsonl_loads": strict_jsonl_loads,
        "validate_public_case": validate_public_case,
    }
    reversed_inventory = tuple(reversed(inventory))
    reversed_scope = {
        **fixture["scope"],
        "case_index_sha256": _case_index_sha256(
            reversed_inventory, canonical_json_bytes, sha256_bytes
        ),
    }
    with pytest.raises(SharedRuntimeError, match="inventories differ"):
        _verify_full_shared_v3_inputs(
            tmp_path,
            reversed_scope,
            fixture["artifacts"],
            public_path,
            fixture["public_ref"],
            input_rows,
            reversed_inventory,
            case_index_sha256=reversed_scope["case_index_sha256"],
            **kwargs,
        )

    short_inventory = inventory[:-1]
    short_scope = {**fixture["scope"], "case_count": len(short_inventory)}
    short_scope["case_index_sha256"] = _case_index_sha256(
        short_inventory, canonical_json_bytes, sha256_bytes
    )
    with pytest.raises(SharedRuntimeError, match="inventories differ"):
        _verify_full_shared_v3_inputs(
            tmp_path,
            short_scope,
            fixture["artifacts"],
            public_path,
            fixture["public_ref"],
            input_rows,
            short_inventory,
            case_index_sha256=short_scope["case_index_sha256"],
            **kwargs,
        )


def test_full_shared_v3_integrity_rejects_public_bytes_drift(tmp_path: Path) -> None:
    fixture = _small_v3_corpus(tmp_path)
    public_path = tmp_path / fixture["public_ref"]["path"]  # type: ignore[index]
    public_path.chmod(0o644)
    public_path.write_bytes(b" " + fixture["public_raw"])
    with pytest.raises(SharedRuntimeError, match="public input bytes differ"):
        _verify_full_shared_v3_inputs(
            tmp_path,
            fixture["scope"],
            fixture["artifacts"],
            public_path,
            fixture["public_ref"],
            strict_jsonl_loads(public_path.read_bytes()),
            fixture["case_inventory"],
            case_manifest_sha256=fixture["case_manifest_sha256"],
            case_index_sha256=fixture["case_index_sha256"],
            canonical_json_bytes=canonical_json_bytes,
            sha256_bytes=sha256_bytes,
            strict_json_loads=strict_json_loads,
            strict_jsonl_loads=strict_jsonl_loads,
            validate_public_case=validate_public_case,
        )


@pytest.mark.parametrize(
    "execution_classification",
    [
        "DIAGNOSTIC_NOT_STRICT_ELIGIBLE",
        "blind_full_shared_conformance_observation",
    ],
)
def test_full_shared_v3_freeze_binds_run_qualification_and_raw_statuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution_classification: str,
) -> None:
    fixture = _small_v3_corpus(tmp_path)
    result_root = "reports/v5/conformance/shared_results/v1"

    def write_result(name: str, raw: bytes) -> dict[str, object]:
        return _write_readonly_artifact(tmp_path, f"{result_root}/{name}", raw)

    sidecar_ref = write_result(
        "synthetic-v3-sidecar.json",
        canonical_json_bytes(
            {
                "protocol": PROTOCOL,
                "materialization_contract_id": MATERIALIZATION_CONTRACT_ID,
                "score_metric_nullability_contract_id": SCORE_METRIC_NULLABILITY_CONTRACT_ID,
                "scope_id": fixture["scope"]["scope_id"],  # type: ignore[index]
            }
        )
        + b"\n",
    )
    evaluators: dict[str, object] = {}

    def verification_stub(
        root: Path, reference: object, evaluator_id: str, digest_fn: object, *args: object
    ) -> tuple[tuple[Path, str], ...]:
        del evaluator_id, args
        assert isinstance(reference, dict)
        path = root / reference["path"]
        raw = path.read_bytes()
        assert digest_fn(raw) == reference["sha256"]  # type: ignore[operator]
        return ((path, str(reference["sha256"])),)

    globals_ = verify_blind_freeze_receipt.__globals__
    monkeypatch.setitem(globals_, "_verify_capture_reference", verification_stub)
    monkeypatch.setitem(globals_, "_verify_runtime_archive_reference", lambda *_a, **_k: ())
    monkeypatch.setitem(globals_, "_verify_dependency_archive_reference", lambda *_a, **_k: ())

    for evaluator_id, wire_id, profile_id in (
        ("full_spec", "full_spec_blind", "fresh_full_spec_v3"),
        ("agent_view", "agent_view_only", "fresh_card_only_v2"),
    ):
        profile = _resolve_source_profile(evaluator_id, profile_id)
        source_rows = [
            {"path": source, "sha256": "a" * 64, "size_bytes": 1}
            for source in profile.source_files
        ]
        capture_ref = write_result(
            f"{evaluator_id}-capture-manifest.json",
            canonical_json_bytes({"source_files": source_rows}) + b"\n",
        )
        dependency_files = [{"path": "typing_extensions.py", "sha256": "d" * 64, "size_bytes": 1}]
        runtime_inventory = [
            {
                "path": path,
                "sha256": "e" * 64,
                "size_bytes": 1,
            }
            for path in (
                "src/identitybench/v5/conformance_shared.py",
                "src/identitybench/v5/conformance_process.py",
                "uv.lock",
            )
        ]
        closure_payload = {
            "format": "identitybench.v5.evaluator-closure.v1",
            "evaluator_id": evaluator_id,
            "entrypoint": profile.entrypoint,
            "approved_source_files": source_rows,
            "runtime_inventory": runtime_inventory,
            "dependency_files": dependency_files,
            "python_runtime": {},
            "source_profile": profile.profile_id,
            "author_origin": profile.author_origin,
            "entrypoint_module": profile.entrypoint_module,
        }
        actual_closure_sha = sha256_bytes(canonical_json_bytes(closure_payload))
        runtime_manifest = {
            **closure_payload,
            "source_closure_sha256": actual_closure_sha,
            "case_count": len(fixture["case_inventory"]),  # type: ignore[arg-type]
            "dependency_snapshot": {
                "file_count": len(dependency_files),
                "files_sha256": sha256_bytes(canonical_json_bytes(dependency_files)),
                "packages": ["typing-extensions"],
                "staged_readonly": True,
            },
        }
        runtime_ref = write_result(
            f"{evaluator_id}-runtime-manifest.json",
            canonical_json_bytes(runtime_manifest) + b"\n",
        )

        def runtime_verifier(
            root: Path,
            reference: object,
            _evaluator_id: str = evaluator_id,
            *_args: object,
            **_kwargs: object,
        ) -> tuple[tuple[Path, str], ...]:
            return verification_stub(root, reference, _evaluator_id, sha256_bytes)

        monkeypatch.setitem(
            globals_,
            "_verify_runtime_reference",
            runtime_verifier,
        )
        runtime_archive_ref = write_result(
            f"{evaluator_id}-runtime-archive.json", b"{}\n"
        )
        dependency_ref = write_result(f"{evaluator_id}-dependency-manifest.json", b"{}\n")
        receipts: list[dict[str, object]] = []
        audits: list[dict[str, object]] = []
        for case_id, input_sha in fixture["case_inventory"]:  # type: ignore[assignment]
            result = {
                "protocol": PROTOCOL,
                "evaluator_id": wire_id,
                "case_id": case_id,
                "input_sha256": input_sha,
                "status": "underdefined",
                "payload": {},
                "materialization": None,
                "coverage": {
                    "phase_targets": [],
                    "diagnostics": [],
                    "rules": [],
                    "safety_cases": [],
                    "snapshots": [],
                },
                "gaps": [
                    {
                        "code": "OTHER",
                        "operation": "derive_phase",
                        "case_id": case_id,
                        "path": "public_episode.phase1_manifest",
                        "detail": "synthetic retained gap",
                    }
                ],
            }
            audit = {
                "audit_protocol": "shared-audit-v1",
                "evaluator_id": evaluator_id,
                "read_paths": [],
                "import_names": [],
                "import_paths": [],
                "write_attempts": [],
                "blocked_events": [],
                "network_attempts": [],
                "author_stdout_bytes": 0,
                "author_stderr_bytes": 0,
            }
            stdout = canonical_json_bytes(result)
            stderr = canonical_json_bytes(audit)
            row = {
                "protocol": PROTOCOL,
                "evaluator_id": evaluator_id,
                "source_profile": profile.profile_id,
                "author_origin": profile.author_origin,
                "case_id": case_id,
                "input_sha256": input_sha,
                "source_closure_sha256": actual_closure_sha,
                "case_manifest_sha256": fixture["case_manifest_sha256"],
                "execution_status": "ok",
                "result": result,
                "audit": audit,
                "result_sha256": sha256_bytes(stdout),
                "stdout_sha256": sha256_bytes(stdout),
                "stderr_sha256": sha256_bytes(stderr),
                "raw_stdout_b64": base64.b64encode(stdout).decode("ascii"),
                "raw_stderr_b64": base64.b64encode(stderr).decode("ascii"),
            }
            receipts.append(row)
            audits.append(
                {
                    key: row[key]
                    for key in (
                        "audit",
                        "case_id",
                        "execution_status",
                        "input_sha256",
                        "raw_stderr_b64",
                        "raw_stdout_b64",
                        "source_closure_sha256",
                        "stderr_sha256",
                        "stdout_sha256",
                    )
                }
            )
        receipt_raw = b"".join(canonical_json_bytes(row) + b"\n" for row in receipts)
        audit_raw = b"".join(canonical_json_bytes(row) + b"\n" for row in audits)
        raw_ref = write_result(f"{evaluator_id}-receipts.jsonl", receipt_raw)
        audit_ref = write_result(f"{evaluator_id}-audits.jsonl", audit_raw)
        raw_ref["row_count"] = len(receipts)
        audit_ref["row_count"] = len(audits)
        qualification_ref = _write_readonly_artifact(
            tmp_path,
            f"reports/v5/conformance/review/{evaluator_id}-qualification.json",
            canonical_json_bytes({"status": "INELIGIBLE"}) + b"\n",
        )
        run_manifest = {
            "source_closure_sha256": actual_closure_sha,
            "case_manifest_sha256": fixture["case_manifest_sha256"],
            "raw_receipts": raw_ref,
            "raw_audits": audit_ref,
            "execution_summary": {"status": "failed"},
        }
        run_ref = write_result(
            f"{evaluator_id}-run-manifest.json",
            canonical_json_bytes(run_manifest) + b"\n",
        )
        evaluators[evaluator_id] = {
            "wire_evaluator_id": wire_id,
            "source_profile": profile.profile_id,
            "author_origin": profile.author_origin,
            "source_closure_sha256": actual_closure_sha,
            "source_capture_manifest": capture_ref,
            "runtime_manifest": runtime_ref,
            "runtime_archive": runtime_archive_ref,
            "dependency_manifest": dependency_ref,
            "raw_receipts": raw_ref,
            "raw_audits": audit_ref,
            "run_manifest": run_ref,
            "qualification": qualification_ref,
        }

    receipt = {
        "schema_version": "identitybench.v5.blind-output-freeze.v3",
        "proof_scope": "full_shared_conformance",
        "scope": fixture["scope"],
        "case_inventory": [
            {"case_id": case_id, "input_sha256": input_sha}
            for case_id, input_sha in fixture["case_inventory"]  # type: ignore[assignment]
        ],
        "public_inputs": fixture["public_ref"],
        "input_artifacts": fixture["artifacts"],
        "protocol_sidecar": sidecar_ref,
        "evaluators": evaluators,
        "runtime_attestation": "diagnostic_unaccepted",
        "author_independence": "pending_qualified_adjudication",
        "execution_classification": execution_classification,
        "frozen_at_utc": "2099-01-01T00:00:00Z",
    }
    receipt_raw = canonical_json_bytes(receipt) + b"\n"
    receipt_ref = _write_readonly_artifact(
        tmp_path,
        f"{result_root}/synthetic-v3-freeze.json",
        receipt_raw,
    )
    token = verify_blind_freeze_receipt(
        Path(receipt_ref["path"]), receipt_ref["sha256"], repository_root=tmp_path
    )
    assert token.proof_scope == "full_shared_conformance"
    assert token.case_inventory == fixture["case_inventory"]
    assert len(token.case_inventory) == 5
    for item in evaluators.values():
        assert isinstance(item, dict)
        raw_path = tmp_path / item["raw_receipts"]["path"]
        raw_rows = strict_jsonl_loads(raw_path.read_bytes())
        assert len(raw_rows) == 5
        assert all(row["result"]["status"] == "underdefined" for row in raw_rows)
        assert all(
            row["result"]["gaps"][0]["detail"] == "synthetic retained gap"
            for row in raw_rows
        )
    token.assert_current()

    public_path = tmp_path / fixture["public_ref"]["path"]  # type: ignore[index]
    after_freeze_ns = _parse_utc_timestamp_ns("2100-01-01T00:00:00Z")
    os.utime(public_path, ns=(after_freeze_ns, after_freeze_ns))
    with pytest.raises(SharedRuntimeError, match="predates a bound artifact"):
        verify_blind_freeze_receipt(
            Path(receipt_ref["path"]), receipt_ref["sha256"], repository_root=tmp_path
        )

    public_path.chmod(0o644)
    public_path.write_bytes(fixture["public_raw"] + b" ")
    with pytest.raises(SharedRuntimeError, match="not immutable/read-only"):
        token.assert_current()


def test_blind_receipt_binds_raw_case_manifest_digest() -> None:
    stdout = b"unparsed diagnostic output"
    stderr = b"unparsed audit output"
    row = {
        "protocol": PROTOCOL,
        "evaluator_id": "full_spec",
        "case_id": "case-1",
        "input_sha256": "a" * 64,
        "source_closure_sha256": "b" * 64,
        "case_manifest_sha256": "c" * 64,
        "execution_status": "failed",
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
        "raw_stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "raw_stderr_b64": base64.b64encode(stderr).decode("ascii"),
    }
    args = (
        [row],
        "full_spec",
        "b" * 64,
        (("case-1", "a" * 64),),
        "c" * 64,
        "full_spec_blind",
        "diagnostic_phase_checkpoint",
        _resolve_source_profile("full_spec", "legacy_diagnostic"),
        sha256_bytes,
        strict_json_loads,
    )

    _verify_evaluator_receipts(*args)
    row["case_manifest_sha256"] = "d" * 64
    with pytest.raises(SharedRuntimeError, match="case manifest hash differs"):
        _verify_evaluator_receipts(*args)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("missing", "raw receipt count differs"),
        ("extra", "raw receipt count differs"),
        ("duplicate", "raw receipt case binding differs"),
        ("reordered", "raw receipt order differs"),
        ("misattributed", "raw receipt case binding differs"),
        ("digest", "raw stdout digest differs"),
        ("profile", "outer source-profile identity differs"),
    ],
)
def test_full_shared_v3_receipts_require_exact_ordered_identity_bijection(
    mutation: str, error: str
) -> None:
    profile = _resolve_source_profile("full_spec", "fresh_full_spec_v3")
    inventory = (("case-1", "a" * 64), ("case-2", "b" * 64))

    def row(case_id: str, input_sha: str) -> dict[str, object]:
        stdout = f"stdout:{case_id}".encode()
        stderr = f"stderr:{case_id}".encode()
        return {
            "protocol": PROTOCOL,
            "evaluator_id": "full_spec",
            "source_profile": profile.profile_id,
            "author_origin": profile.author_origin,
            "case_id": case_id,
            "input_sha256": input_sha,
            "source_closure_sha256": "c" * 64,
            "case_manifest_sha256": "d" * 64,
            "execution_status": "failed",
            "stdout_sha256": sha256_bytes(stdout),
            "stderr_sha256": sha256_bytes(stderr),
            "raw_stdout_b64": base64.b64encode(stdout).decode("ascii"),
            "raw_stderr_b64": base64.b64encode(stderr).decode("ascii"),
        }

    rows = [row(*item) for item in inventory]
    if mutation == "missing":
        rows.pop()
    elif mutation == "extra":
        rows.append(row("case-3", "e" * 64))
    elif mutation == "duplicate":
        rows[1] = row(*inventory[0])
    elif mutation == "reordered":
        rows.reverse()
    elif mutation == "misattributed":
        rows[1]["input_sha256"] = "f" * 64
    elif mutation == "digest":
        rows[0]["stdout_sha256"] = "f" * 64
    elif mutation == "profile":
        rows[0].pop("source_profile")
        rows[0]["author_origin"] = "misattributed-author"

    with pytest.raises(SharedRuntimeError, match=error):
        _verify_evaluator_receipts(
            rows,
            "full_spec",
            "c" * 64,
            inventory,
            "d" * 64,
            "full_spec_blind",
            "full_shared_conformance",
            profile,
            sha256_bytes,
            strict_json_loads,
            require_ordered_inventory=True,
        )


def test_v3_receipt_schema_rejects_unknown_fields_and_versions(tmp_path: Path) -> None:
    result_root = tmp_path / "reports/v5/conformance/shared_results/v1"
    result_root.mkdir(parents=True)
    for name, receipt, expected_error in (
        (
            "unknown-field.json",
            {
                "schema_version": "identitybench.v5.blind-output-freeze.v3",
                "unexpected": True,
            },
            "v3 contract",
        ),
        (
            "unknown-version.json",
            {"schema_version": "identitybench.v5.blind-output-freeze.v9"},
            "unsupported blind freeze receipt schema",
        ),
    ):
        raw = canonical_json_bytes(receipt) + b"\n"
        path = result_root / name
        path.write_bytes(raw)
        path.chmod(0o444)
        with pytest.raises(SharedRuntimeError, match=expected_error):
            verify_blind_freeze_receipt(
                Path(f"reports/v5/conformance/shared_results/v1/{name}"),
                sha256_bytes(raw),
                repository_root=tmp_path,
            )


def test_v3_sidecar_binds_exact_scope_and_contract_fields(tmp_path: Path) -> None:
    sidecar = {
        "protocol": PROTOCOL,
        "materialization_contract_id": MATERIALIZATION_CONTRACT_ID,
        "score_metric_nullability_contract_id": SCORE_METRIC_NULLABILITY_CONTRACT_ID,
        "scope_id": "synthetic-scope",
    }
    raw = canonical_json_bytes(sidecar) + b"\n"
    reference = _write_readonly_artifact(
        tmp_path,
        "reports/v5/conformance/shared_results/v1/v3-sidecar.json",
        raw,
    )
    verified, artifacts = _verify_full_shared_v3_protocol_sidecar(
        tmp_path,
        reference,
        scope_id="synthetic-scope",
        sha256_bytes=sha256_bytes,
        strict_json_loads=strict_json_loads,
        materialization_contract_id=MATERIALIZATION_CONTRACT_ID,
        score_metric_nullability_contract_id=SCORE_METRIC_NULLABILITY_CONTRACT_ID,
    )
    assert verified == sidecar
    assert artifacts == ((tmp_path / reference["path"], reference["sha256"]),)

    with pytest.raises(SharedRuntimeError, match="sidecar identity differs"):
        _verify_full_shared_v3_protocol_sidecar(
            tmp_path,
            reference,
            scope_id="different-scope",
            sha256_bytes=sha256_bytes,
            strict_json_loads=strict_json_loads,
            materialization_contract_id=MATERIALIZATION_CONTRACT_ID,
            score_metric_nullability_contract_id=SCORE_METRIC_NULLABILITY_CONTRACT_ID,
        )


def test_full_v3_execution_classification_domain_is_narrow_and_v2_unchanged() -> None:
    validate_classification = getattr(
        process_module, "_validate_execution_classification", None
    )
    assert callable(validate_classification)
    validate_classification("full_shared_conformance", "DIAGNOSTIC_NOT_STRICT_ELIGIBLE")
    validate_classification(
        "full_shared_conformance", "blind_full_shared_conformance_observation"
    )
    with pytest.raises(SharedRuntimeError, match="full conformance freeze"):
        validate_classification("full_shared_conformance", "unknown_observation")
    with pytest.raises(SharedRuntimeError, match="full conformance freeze"):
        validate_classification("full_shared_conformance", [])
    with pytest.raises(SharedRuntimeError, match="diagnostic batch"):
        validate_classification(
            "diagnostic_phase_batch", "blind_full_shared_conformance_observation"
        )


def test_v3_run_manifest_cross_binds_source_inventory_and_streams(tmp_path: Path) -> None:
    result_root = "reports/v5/conformance/shared_results/v1"
    raw_receipts = _write_readonly_artifact(tmp_path, f"{result_root}/receipts.jsonl", b"r\n")
    raw_audits = _write_readonly_artifact(tmp_path, f"{result_root}/audits.jsonl", b"a\n")
    receipt_ref = {**raw_receipts, "row_count": 5}
    audit_ref = {**raw_audits, "row_count": 5}
    run = {
        "source_closure_sha256": "b" * 64,
        "case_manifest_sha256": "c" * 64,
        "raw_receipts": receipt_ref,
        "raw_audits": audit_ref,
        "execution_summary": {"status": "failed"},
    }
    run_raw = canonical_json_bytes(run) + b"\n"
    run_ref = _write_readonly_artifact(tmp_path, f"{result_root}/run.json", run_raw)
    run_artifacts = _verify_full_shared_v3_run_manifest(
        tmp_path,
        run_ref,
        evaluator_id="full_spec",
        source_closure_sha256="b" * 64,
        case_manifest_sha256="c" * 64,
        raw_receipts_ref=receipt_ref,
        raw_audits_ref=audit_ref,
        case_count=5,
        sha256_bytes=sha256_bytes,
        strict_json_loads=strict_json_loads,
    )
    assert run_artifacts == ((tmp_path / run_ref["path"], run_ref["sha256"]),)

    with pytest.raises(SharedRuntimeError, match="inventory or stream binding differs"):
        _verify_full_shared_v3_run_manifest(
            tmp_path,
            run_ref,
            evaluator_id="full_spec",
            source_closure_sha256="d" * 64,
            case_manifest_sha256="c" * 64,
            raw_receipts_ref=receipt_ref,
            raw_audits_ref=audit_ref,
            case_count=5,
            sha256_bytes=sha256_bytes,
            strict_json_loads=strict_json_loads,
        )


def test_v1_diagnostic_scope_keeps_two_case_compatibility() -> None:
    _validate_v1_proof_scope_count("diagnostic_phase_checkpoint", 2)

    with pytest.raises(SharedRuntimeError, match="exactly two frozen cases"):
        _validate_v1_proof_scope_count("diagnostic_phase_checkpoint", 3)


def test_diagnostic_freeze_timestamp_must_follow_every_bound_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "frozen-output.jsonl"
    artifact.write_bytes(b"frozen")
    os.utime(artifact, ns=(5_000_000_000, 5_000_000_000))
    artifacts = ((artifact, sha256_bytes(b"frozen")),)

    frozen_at = _parse_utc_timestamp_ns("1970-01-01T00:00:06Z")
    _verify_artifact_chronology(frozen_at, artifacts)

    before_artifact = _parse_utc_timestamp_ns("1970-01-01T00:00:04Z")
    with pytest.raises(SharedRuntimeError, match="predates a bound artifact"):
        _verify_artifact_chronology(before_artifact, artifacts)
    with pytest.raises(SharedRuntimeError, match="must be UTC"):
        _parse_utc_timestamp_ns("1970-01-01T00:00:06+01:00")


def test_diagnostic_freeze_timestamp_binds_public_input_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path
    result_root = root / "reports/v5/conformance/shared_results/v1"
    result_root.mkdir(parents=True)

    def write_artifact(
        relative: str, raw: bytes, *, mtime_ns: int = 1_000_000_000
    ) -> dict[str, object]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        path.chmod(0o444)
        os.utime(path, ns=(mtime_ns, mtime_ns))
        return {"path": relative, "sha256": sha256_bytes(raw)}

    public_case = validate_public_case(_public_case())
    public_raw = canonical_json_bytes(public_case) + b"\n"
    public_ref = write_artifact(
        "reports/v5/conformance/shared_results/v1/public-inputs.jsonl",
        public_raw,
        mtime_ns=5_000_000_000,
    )
    case_inventory = [
        {
            "case_id": public_case["case_id"],
            "input_sha256": sha256_bytes(canonical_json_bytes(public_case)),
        }
    ]
    input_manifest_ref = write_artifact(
        "reports/v5/conformance/shared_results/v1/input-manifest.json", b"{}"
    )
    checkpoint_ref = write_artifact(
        "reports/v5/conformance/shared_results/v1/phase-checkpoint.json", b"{}"
    )
    monkeypatch.setitem(
        verify_blind_freeze_receipt.__globals__,
        "_DIAGNOSTIC_PHASE_BATCH_SCOPES",
        {
            "synthetic-test-scope": {
                "input_manifest_path": input_manifest_ref["path"],
                "input_manifest_sha256": input_manifest_ref["sha256"],
                "phase_checkpoint_path": checkpoint_ref["path"],
                "phase_checkpoint_sha256": checkpoint_ref["sha256"],
            }
        },
    )
    globals_ = verify_blind_freeze_receipt.__globals__
    monkeypatch.setitem(
        globals_,
        "_validate_diagnostic_phase_batch_scope",
        lambda *_args, **_kwargs: "synthetic-test-scope",
    )
    monkeypatch.setitem(
        globals_, "_verify_diagnostic_phase_sidecar", lambda *_args, **_kwargs: ({}, ())
    )
    monkeypatch.setitem(globals_, "_verify_capture_reference", lambda *_args, **_kwargs: ())
    monkeypatch.setitem(globals_, "_verify_runtime_reference", lambda *_args, **_kwargs: ())
    monkeypatch.setitem(globals_, "_verify_sidecar_capture_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_verify_sidecar_runtime_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_verify_runtime_archive_reference", lambda *_args, **_kwargs: ())
    monkeypatch.setitem(
        globals_, "_verify_dependency_archive_reference", lambda *_args, **_kwargs: ()
    )
    monkeypatch.setitem(globals_, "_verify_evaluator_receipts", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_verify_evaluator_audits", lambda *_args, **_kwargs: None)

    evaluators: dict[str, object] = {}
    for evaluator_id, wire_id, profile_id in (
        ("full_spec", "full_spec_blind", "fresh_full_spec_v2"),
        ("agent_view", "agent_view_only", "agent_view_v1"),
    ):
        profile = _resolve_source_profile(evaluator_id, profile_id)
        capture_ref = write_artifact(
            f"reports/v5/conformance/shared_results/v1/{evaluator_id}-capture.json", b"{}"
        )
        runtime_ref = write_artifact(
            f"reports/v5/conformance/shared_results/v1/{evaluator_id}-runtime.json", b"{}"
        )
        receipts_ref = write_artifact(
            f"reports/v5/conformance/shared_results/v1/{evaluator_id}-receipts.jsonl", b"{}\n"
        )
        audits_ref = write_artifact(
            f"reports/v5/conformance/shared_results/v1/{evaluator_id}-audits.jsonl", b"{}\n"
        )
        evaluators[evaluator_id] = {
            "wire_evaluator_id": wire_id,
            "source_closure_sha256": "b" * 64,
            "source_capture_manifest": capture_ref,
            "runtime_manifest": runtime_ref,
            "raw_receipts": {**receipts_ref, "row_count": 1},
            "raw_audits": {**audits_ref, "row_count": 1},
            "source_profile": profile.profile_id,
            "author_origin": profile.author_origin,
            "runtime_archive": {"path": "unused-runtime-archive", "sha256": "c" * 64},
            "dependency_manifest": {"path": "unused-dependency-manifest", "sha256": "d" * 64},
        }

    receipt = {
        "schema_version": "identitybench.v5.blind-output-freeze.v2",
        "proof_scope": "diagnostic_phase_batch",
        "scope": {"scope_id": "synthetic-test-scope"},
        "case_inventory": case_inventory,
        "public_inputs": public_ref,
        "input_artifacts": {
            "manifest": input_manifest_ref,
            "phase_checkpoint": checkpoint_ref,
        },
        "protocol_sidecar": {},
        "evaluators": evaluators,
        "runtime_attestation": "diagnostic_unaccepted",
        "author_independence": "pending_qualified_adjudication",
        "execution_classification": "DIAGNOSTIC_NOT_STRICT_ELIGIBLE",
        "frozen_at_utc": "1970-01-01T00:00:04Z",
    }
    receipt_raw = canonical_json_bytes(receipt) + b"\n"
    receipt_path = result_root / "synthetic-freeze-receipt.json"
    receipt_path.write_bytes(receipt_raw)
    receipt_path.chmod(0o444)

    with pytest.raises(SharedRuntimeError, match="predates a bound artifact"):
        verify_blind_freeze_receipt(
            Path("reports/v5/conformance/shared_results/v1/synthetic-freeze-receipt.json"),
            sha256_bytes(receipt_raw),
            repository_root=root,
        )


def test_case_index_requires_complete_unique_ordered_bijection() -> None:
    inventory = (("case-1", "a" * 64), ("case-2", "b" * 64))
    index_sha = _case_index_sha256(inventory, canonical_json_bytes, sha256_bytes)
    assert (
        _validate_case_index_binding(
            2,
            index_sha,
            inventory,
            inventory,
            canonical_json_bytes,
            sha256_bytes,
        )
        == index_sha
    )

    with pytest.raises(SharedRuntimeError, match="declared case count"):
        _validate_case_index_binding(
            1,
            _case_index_sha256(inventory[:1], canonical_json_bytes, sha256_bytes),
            inventory[:1],
            inventory,
            canonical_json_bytes,
            sha256_bytes,
        )
    with pytest.raises(SharedRuntimeError, match="declared case count"):
        _validate_case_index_binding(
            3,
            _case_index_sha256(
                (*inventory, ("extra", "c" * 64)), canonical_json_bytes, sha256_bytes
            ),
            (*inventory, ("extra", "c" * 64)),
            inventory,
            canonical_json_bytes,
            sha256_bytes,
        )
    with pytest.raises(SharedRuntimeError, match="duplicate case IDs"):
        duplicate = (inventory[0], inventory[0])
        _validate_case_index_binding(
            2,
            _case_index_sha256(duplicate, canonical_json_bytes, sha256_bytes),
            duplicate,
            inventory,
            canonical_json_bytes,
            sha256_bytes,
        )
    with pytest.raises(SharedRuntimeError, match="exactly cover"):
        _validate_case_index_binding(
            2,
            index_sha,
            inventory,
            tuple(reversed(inventory)),
            canonical_json_bytes,
            sha256_bytes,
        )
    with pytest.raises(SharedRuntimeError, match="index digest differs"):
        _validate_case_index_binding(
            2,
            "0" * 64,
            inventory,
            inventory,
            canonical_json_bytes,
            sha256_bytes,
        )


def test_diagnostic_batch_scope_rejects_unknown_scope_and_unknown_fields() -> None:
    inventory = (("case-1", "a" * 64),)
    base = {
        "scope_id": "unapproved-future-1702",
        "case_count": 1,
        "case_index_sha256": _case_index_sha256(inventory, canonical_json_bytes, sha256_bytes),
        "operation": "derive_phase",
        "phase_indices": [1, 2],
        "turn_context": "T0",
        "steering": "UNSTEERED",
    }
    with pytest.raises(SharedRuntimeError, match="unknown diagnostic phase batch scope"):
        _validate_diagnostic_phase_batch_scope(
            base,
            inventory,
            inventory,
            {"path": "public.jsonl", "sha256": "d" * 64},
        )
    with pytest.raises(SharedRuntimeError, match="scope fields"):
        _validate_diagnostic_phase_batch_scope(
            {**base, "unexpected": True},
            inventory,
            inventory,
            {"path": "public.jsonl", "sha256": "d" * 64},
        )


def test_v2_freeze_receipt_rejects_unknown_top_level_fields(tmp_path: Path) -> None:
    result_root = tmp_path / "reports/v5/conformance/shared_results/v1"
    result_root.mkdir(parents=True)
    receipt = {
        "schema_version": "identitybench.v5.blind-output-freeze.v2",
        "proof_scope": "diagnostic_phase_batch",
        "scope": {},
        "case_inventory": [],
        "public_inputs": {},
        "input_artifacts": {},
        "protocol_sidecar": {},
        "evaluators": {},
        "runtime_attestation": "diagnostic_unaccepted",
        "author_independence": "pending_qualified_adjudication",
        "execution_classification": "DIAGNOSTIC_NOT_STRICT_ELIGIBLE",
        "frozen_at_utc": "2026-11-12T00:00:00Z",
        "unexpected": True,
    }
    raw = canonical_json_bytes(receipt) + b"\n"
    receipt_path = result_root / "unknown-field-receipt.json"
    receipt_path.write_bytes(raw)
    receipt_path.chmod(0o444)

    with pytest.raises(SharedRuntimeError, match="v2 contract"):
        verify_blind_freeze_receipt(
            Path("reports/v5/conformance/shared_results/v1/unknown-field-receipt.json"),
            sha256_bytes(raw),
            repository_root=tmp_path,
        )


def test_diagnostic_freeze_preserves_non_ok_semantic_status_and_gaps() -> None:
    result = {
        "protocol": PROTOCOL,
        "evaluator_id": "full_spec_blind",
        "case_id": "case-1",
        "input_sha256": "a" * 64,
        "status": "underdefined",
        "gaps": ["phase_context_unavailable"],
        "payload": {"derived": []},
    }
    audit = {
        "audit_protocol": "shared-audit-v1",
        "evaluator_id": "full_spec",
        "read_paths": [],
        "import_names": [],
        "import_paths": [],
        "write_attempts": [],
        "blocked_events": [],
        "network_attempts": [],
        "author_stdout_bytes": 0,
        "author_stderr_bytes": 0,
    }
    stdout = canonical_json_bytes(result)
    stderr = canonical_json_bytes(audit)
    row = {
        "protocol": PROTOCOL,
        "evaluator_id": "full_spec",
        "source_profile": "legacy_diagnostic",
        "author_origin": _resolve_source_profile("full_spec", "legacy_diagnostic").author_origin,
        "case_id": "case-1",
        "input_sha256": "a" * 64,
        "source_closure_sha256": "b" * 64,
        "case_manifest_sha256": "c" * 64,
        "execution_status": "ok",
        "result": result,
        "audit": audit,
        "result_sha256": sha256_bytes(stdout),
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
        "raw_stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "raw_stderr_b64": base64.b64encode(stderr).decode("ascii"),
    }

    _verify_evaluator_receipts(
        [row],
        "full_spec",
        "b" * 64,
        (("case-1", "a" * 64),),
        "c" * 64,
        "full_spec_blind",
        "diagnostic_phase_batch",
        _resolve_source_profile("full_spec", "legacy_diagnostic"),
        sha256_bytes,
        strict_json_loads,
    )
    audit_row = {
        key: row[key]
        for key in (
            "audit",
            "case_id",
            "execution_status",
            "input_sha256",
            "raw_stderr_b64",
            "raw_stdout_b64",
            "source_closure_sha256",
            "stderr_sha256",
            "stdout_sha256",
        )
    }
    _verify_evaluator_audits([audit_row], [row], "full_spec")
    audit_row["input_sha256"] = "d" * 64
    with pytest.raises(SharedRuntimeError, match="raw audit binding differs"):
        _verify_evaluator_audits([audit_row], [row], "full_spec")


@pytest.mark.parametrize(
    ("field", "mutated_value", "error"),
    [
        ("protocol", "different-protocol", "raw result protocol differs"),
        ("case_id", "different-case", "raw result case binding differs"),
        ("input_sha256", "e" * 64, "raw result case binding differs"),
    ],
)
def test_diagnostic_freeze_rejects_inner_result_identity_mismatch(
    field: str, mutated_value: str, error: str
) -> None:
    profile = _resolve_source_profile("full_spec", "legacy_diagnostic")
    result = {
        "protocol": PROTOCOL,
        "evaluator_id": "full_spec_blind",
        "case_id": "case-1",
        "input_sha256": "a" * 64,
        "status": "underdefined",
        "gaps": ["phase_context_unavailable"],
        "payload": {"derived": []},
    }
    result[field] = mutated_value
    audit = {
        "audit_protocol": "shared-audit-v1",
        "evaluator_id": "full_spec",
        "read_paths": [],
        "import_names": [],
        "import_paths": [],
        "write_attempts": [],
        "blocked_events": [],
        "network_attempts": [],
        "author_stdout_bytes": 0,
        "author_stderr_bytes": 0,
    }
    stdout = canonical_json_bytes(result)
    stderr = canonical_json_bytes(audit)
    row = {
        "protocol": PROTOCOL,
        "evaluator_id": "full_spec",
        "source_profile": profile.profile_id,
        "author_origin": profile.author_origin,
        "case_id": "case-1",
        "input_sha256": "a" * 64,
        "source_closure_sha256": "b" * 64,
        "case_manifest_sha256": "c" * 64,
        "execution_status": "ok",
        "result": result,
        "audit": audit,
        "result_sha256": sha256_bytes(stdout),
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
        "raw_stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "raw_stderr_b64": base64.b64encode(stderr).decode("ascii"),
    }

    with pytest.raises(SharedRuntimeError, match=error):
        _verify_evaluator_receipts(
            [row],
            "full_spec",
            "b" * 64,
            (("case-1", "a" * 64),),
            "c" * 64,
            "full_spec_blind",
            "diagnostic_phase_batch",
            profile,
            sha256_bytes,
            strict_json_loads,
        )


def test_diagnostic_freeze_preserves_failed_outer_receipt_bytes() -> None:
    stdout = b"{bad candidate bytes"
    stderr = b"captured worker failure"
    profile = _resolve_source_profile("full_spec", "legacy_diagnostic")
    row = {
        "protocol": PROTOCOL,
        "evaluator_id": "full_spec",
        "source_profile": profile.profile_id,
        "author_origin": profile.author_origin,
        "case_id": "case-1",
        "input_sha256": "a" * 64,
        "source_closure_sha256": "b" * 64,
        "case_manifest_sha256": "c" * 64,
        "execution_status": "failed",
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
        "raw_stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "raw_stderr_b64": base64.b64encode(stderr).decode("ascii"),
    }

    _verify_evaluator_receipts(
        [row],
        "full_spec",
        "b" * 64,
        (("case-1", "a" * 64),),
        "c" * 64,
        "full_spec_blind",
        "diagnostic_phase_batch",
        profile,
        sha256_bytes,
        strict_json_loads,
    )


def test_child_denies_reads_outside_frozen_roots(tmp_path: Path) -> None:
    completed = _run_child(
        tmp_path,
        (
            "def evaluate_shared_input(case):\n"
            "    open('/etc/passwd', encoding='utf-8').read()\n"
            "    return {'status': 'ok'}\n"
        ),
        b"{}",
    )

    assert completed.returncode == 70
    audit = strict_json_loads(completed.stderr)
    assert isinstance(audit, dict)
    assert audit["blocked_events"]
    with pytest.raises(SharedRuntimeError, match="blocked filesystem"):
        _validate_audit_receipt(audit)


def test_cached_module_bytecode_is_rejected_as_unbound_snapshot_content(
    tmp_path: Path,
) -> None:
    temp_manager = tempfile.TemporaryDirectory(dir=tmp_path)
    base = Path(temp_manager.name)
    source_root = base / "source"
    dependency_root = base / "dependencies"
    worker = source_root / "tests/conformance/v5/worker.py"
    worker.parent.mkdir(parents=True)
    dependency_root.mkdir()
    cached_source = "def evaluate_shared_input(case):\n    return {'value': 'CACHED'}\n"
    current_source = "def evaluate_shared_input(case):\n    return {'value': 'SOURCE'}\n"
    worker.write_text(cached_source, encoding="utf-8")
    py_compile.compile(str(worker), doraise=True)
    prior_stat = worker.stat()
    worker.write_text(current_source, encoding="utf-8")
    os.utime(worker, ns=(prior_stat.st_atime_ns, prior_stat.st_mtime_ns))

    spec = importlib.util.spec_from_file_location("cache_reproduction", worker)
    assert spec is not None and spec.loader is not None
    cached_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cached_module)
    assert cached_module.evaluate_shared_input({}) == {"value": "CACHED"}

    current_bytes = worker.read_bytes()
    evaluator = FrozenEvaluator(
        evaluator_id="test_worker",
        source_profile="test_synthetic",
        author_origin="test_fixture",
        entrypoint="tests/conformance/v5/worker.py",
        entrypoint_module=None,
        source_root=tmp_path,
        stage_root=source_root,
        dependency_root=dependency_root,
        source_bytes={},
        staged_file_hashes={"source/tests/conformance/v5/worker.py": sha256_bytes(current_bytes)},
        runtime_file_hashes={},
        case_bytes={"case-1": b"{}"},
        case_sha256={"case-1": sha256_bytes(b"{}")},
        case_manifest_sha256="0" * 64,
        source_closure_sha256="0" * 64,
        manifest={},
        blind_freeze=None,
        _temporary_directory=temp_manager,
    )
    try:
        with pytest.raises(SharedRuntimeError, match="file set changed; unbound"):
            _verify_source_snapshot(evaluator, sha256_bytes)
    finally:
        evaluator.close()


def test_stdlib_manifest_hashes_bytecode_cache_files(tmp_path: Path) -> None:
    cache = tmp_path / "__pycache__" / "stdlib_module.cpython-312.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"frozen cached code")

    records = _hash_stdlib_tree([tmp_path], sha256_bytes)

    assert records == [
        {
            "path": f"{tmp_path.name}/__pycache__/stdlib_module.cpython-312.pyc",
            "sha256": sha256_bytes(cache.read_bytes()),
        }
    ]


def test_stdlib_roots_do_not_include_the_repository() -> None:
    repository = Path(__file__).resolve().parents[2]
    assert all(root != repository and repository not in root.parents for root in _stdlib_roots())
