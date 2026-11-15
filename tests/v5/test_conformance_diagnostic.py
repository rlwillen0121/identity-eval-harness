from __future__ import annotations

import base64
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from identitybench.v5 import conformance_process as process_module
from identitybench.v5 import conformance_shared as shared_module
from identitybench.v5.conformance_process import (
    DiagnosticComparison,
    DiagnosticProductionEvaluator,
    FrozenEvaluator,
    SharedRuntimeError,
    _resolve_source_profile,
    compare_full_spec_to_production_diagnostic,
    freeze_production_diagnostic,
    freeze_shared_evaluator,
    prepare_full_spec_diagnostic,
    run_diagnostic_production_evaluator,
    run_frozen_evaluator,
    verify_blind_freeze_receipt,
)
from identitybench.v5.conformance_shared import (
    EVALUATOR_WIRE_ID_BY_SLOT,
    PINNED_PUBLIC_SCHEMA_SHA256,
    PROTOCOL,
    ConformanceProtocolError,
    FrozenCase,
    canonical_json_bytes,
    compare_production_result,
    freeze_case,
    serialize_recipe_case,
    sha256_bytes,
    sha256_json,
    validate_evaluator_result,
)
from identitybench.v5.validation import (
    SHARED_FREEZE_SCHEMA_V5_CURRENT,
    _verify_shared_freeze,
)


def _public_case(case_id: str = "base-case") -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "case_id": case_id,
        "operation": "derive_phase",
        "suite_id": "v5_dev",
        "phase_index": 1,
        "public_episode": {
            "episode_id": "episode-1",
            "phase1_manifest": {},
            "phase1_slices": [],
            "policy_card": {},
            "q1_slice_ids_p1": [],
            "q2_slice_ids_p1": [],
        },
        "rendered_card": "synthetic public card\n",
        "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
        "stimulus": {},
    }


def _recipe_case(case_id: str = "v5_dev/episode-1/p1/validate") -> FrozenCase:
    base = freeze_case(_public_case())
    return serialize_recipe_case(
        base.public_case,
        {
            "recipe_id": case_id,
            "input_ref": base.case_id,
            "operation": "validate_submission",
            "stimulus": {
                "recipe": {
                    "candidate_source": "independent_base",
                    "base_scope": "phase_plan",
                    "patches": [],
                }
            },
        },
    )


def _closure_manifest_fixture(
    evaluator_id: str,
    cases: tuple[FrozenCase, ...],
    tmp_path: Path,
) -> FrozenEvaluator:
    profile_name = "approved_full_spec_v1" if evaluator_id == "full_spec" else "production_v4"
    profile = _resolve_source_profile(evaluator_id, profile_name)
    contract = process_module._profile_normative_contract(profile)
    source_bytes: dict[str, bytes] = {}
    for relative in profile.source_files:
        source_bytes[relative] = f"synthetic-source:{profile_name}:{relative}".encode()
    runtime_paths = process_module._runtime_inventory_for_profile(profile)
    for relative in runtime_paths:
        source_bytes.setdefault(relative, f"synthetic-runtime:{relative}".encode())
    source_records = [
        {
            "path": relative,
            "sha256": sha256_bytes(source_bytes[relative]),
            "size_bytes": len(source_bytes[relative]),
        }
        for relative in profile.source_files
    ]
    runtime_records = [
        {
            "path": relative,
            "sha256": sha256_bytes(source_bytes[relative]),
            "size_bytes": len(source_bytes[relative]),
        }
        for relative in runtime_paths
    ]
    dependency_files: list[dict[str, Any]] = []
    closure_payload: dict[str, Any] = {
        "format": "identitybench.v5.evaluator-closure.v2"
        if contract
        else "identitybench.v5.evaluator-closure.v1",
        "evaluator_id": evaluator_id,
        "source_profile": profile.profile_id,
        "author_origin": profile.author_origin,
        "entrypoint": profile.entrypoint,
        "entrypoint_module": profile.entrypoint_module,
    }
    if contract is not None:
        closure_payload["normative_contract"] = contract
    closure_payload.update(
        {
            "approved_source_files": source_records,
            "runtime_inventory": runtime_records,
            "dependency_files": dependency_files,
            "python_runtime": {},
        }
    )
    closure_sha256 = sha256_bytes(canonical_json_bytes(closure_payload))
    case_bytes = {case.case_id: case.canonical_bytes for case in cases}
    case_hashes = {case.case_id: case.input_sha256 for case in cases}
    case_manifest = {
        "protocol": PROTOCOL,
        "canonicalization": "canonical-json-v1",
        "cases": [{"case_id": case.case_id, "input_sha256": case.input_sha256} for case in cases],
    }
    temp = tempfile.TemporaryDirectory(dir=tmp_path)
    stage_root = Path(temp.name) / "source"
    dependency_root = Path(temp.name) / "dependencies"
    stage_root.mkdir()
    dependency_root.mkdir()
    manifest = {
        **closure_payload,
        "source_closure_sha256": closure_sha256,
        "case_count": len(cases),
        "dependency_snapshot": {
            "file_count": 0,
            "files_sha256": sha256_json(dependency_files),
            "packages": [],
            "staged_readonly": True,
        },
    }
    if evaluator_id == "production":
        manifest["namespace_packages"] = ["identitybench.v3", "identitybench.v4"]
    return FrozenEvaluator(
        evaluator_id=evaluator_id,
        source_profile=profile.profile_id,
        author_origin=profile.author_origin,
        entrypoint=profile.entrypoint,
        entrypoint_module=profile.entrypoint_module,
        source_root=tmp_path,
        stage_root=stage_root,
        dependency_root=dependency_root,
        source_bytes=source_bytes,
        staged_file_hashes={
            f"source/{relative}": sha256_bytes(source_bytes[relative])
            for relative in profile.source_files
        },
        runtime_file_hashes={},
        case_bytes=case_bytes,
        case_sha256=case_hashes,
        case_manifest_sha256=sha256_bytes(canonical_json_bytes(case_manifest)),
        source_closure_sha256=closure_sha256,
        manifest=manifest,
        blind_freeze=None,
        _temporary_directory=temp,
    )


def _fake_freeze_evaluator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def freeze(
        evaluator_id: str,
        profile: Any,
        root: Path,
        cases_by_id: dict[str, bytes],
        hashes: dict[str, str],
        _case_manifest_sha: str,
        _runtime: dict[str, Any],
        _temporary_root: Path | None,
        _source_snapshot_root: Path | None,
        blind_freeze: object | None,
        _canonical_json_bytes: Any,
        _sha256_bytes: Any,
    ) -> FrozenEvaluator:
        del root
        assert blind_freeze is None
        cases = tuple(freeze_case(json.loads(raw)) for raw in cases_by_id.values())
        evaluator = _closure_manifest_fixture(evaluator_id, cases, tmp_path)
        assert evaluator.source_profile == profile.profile_id
        assert evaluator.case_sha256 == hashes
        return evaluator

    monkeypatch.setattr(process_module, "_runtime_manifest", lambda *_args: {})
    monkeypatch.setattr(process_module, "_freeze_evaluator", freeze)
    monkeypatch.setattr(process_module, "_verify_source_snapshot", lambda *_args: None)


def _valid_result(
    case: FrozenCase, slot: str, *, base_plan: list[dict[str, object]] | None = None
) -> dict[str, object]:
    return {
        "protocol": PROTOCOL,
        "evaluator_id": EVALUATOR_WIRE_ID_BY_SLOT[slot],
        "case_id": case.case_id,
        "input_sha256": case.input_sha256,
        "status": "ok",
        "payload": {"first_failing_stage": None, "diagnostics": []},
        "materialization": {"base_plan": base_plan or [], "submitted_plan": []},
        "coverage": {
            "phase_targets": [],
            "diagnostics": [],
            "rules": [],
            "safety_cases": [],
            "snapshots": [],
        },
        "gaps": [],
    }


def _audit(evaluator_id: str) -> dict[str, object]:
    return {
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


def _outer_receipt(
    evaluator: FrozenEvaluator,
    case: FrozenCase,
    result: dict[str, object],
) -> dict[str, Any]:
    stdout = canonical_json_bytes(result)
    audit = _audit(evaluator.evaluator_id)
    stderr = canonical_json_bytes(audit)
    return {
        "protocol": PROTOCOL,
        "evaluator_id": evaluator.evaluator_id,
        "source_profile": evaluator.source_profile,
        "author_origin": evaluator.author_origin,
        "case_id": case.case_id,
        "input_sha256": case.input_sha256,
        "source_closure_sha256": evaluator.source_closure_sha256,
        "case_manifest_sha256": evaluator.case_manifest_sha256,
        "execution_status": "ok",
        "result": result,
        "audit": audit,
        "result_sha256": sha256_bytes(stdout),
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
        "raw_stdout_b64": base64.b64encode(stdout).decode("ascii"),
        "raw_stderr_b64": base64.b64encode(stderr).decode("ascii"),
        "exit_code": 0,
    }


def _current_fixtures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: FrozenCase
) -> tuple[process_module.DiagnosticFullSpecEvaluator, DiagnosticProductionEvaluator]:
    _fake_freeze_evaluator(monkeypatch, tmp_path)
    production = freeze_production_diagnostic(
        [case], source_profile="production_v4", repository_root=Path.cwd()
    )
    full_spec = prepare_full_spec_diagnostic(
        _closure_manifest_fixture("full_spec", (case,), tmp_path)
    )
    monkeypatch.setattr(process_module, "_verify_source_snapshot", lambda *_args: None)
    return full_spec, production


def test_diagnostic_freeze_requires_production_v4_and_exact_frozen_case_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _recipe_case()
    _fake_freeze_evaluator(monkeypatch, tmp_path)
    with pytest.raises(SharedRuntimeError, match="production_v4"):
        freeze_production_diagnostic(
            [case], source_profile="production_v3", repository_root=Path.cwd()
        )

    stale_case = replace(case, input_sha256="a" * 64)
    with pytest.raises(SharedRuntimeError, match="FrozenCase bytes or digest"):
        freeze_production_diagnostic(
            [stale_case], source_profile="production_v4", repository_root=Path.cwd()
        )

    frozen = freeze_production_diagnostic(
        [case], source_profile="production_v4", repository_root=Path.cwd()
    )
    assert frozen.manifest["diagnostic_only"] is True
    assert frozen.manifest["g_spec_01"] == "not_evaluated"
    assert frozen.manifest["agent_view"] == "outstanding"
    assert frozen.manifest["author_independence"] == "not_asserted"
    assert frozen.manifest["case_manifest_sha256"] == frozen.case_manifest_sha256
    assert frozen.evaluator.blind_freeze is None


def test_diagnostic_runner_is_separate_from_qualified_production_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _recipe_case()
    _full_spec, production = _current_fixtures(monkeypatch, tmp_path, case)
    execution = _outer_receipt(production.evaluator, case, _valid_result(case, "production"))
    monkeypatch.setattr(process_module, "_run_frozen_evaluator_process", lambda *_args: execution)

    receipt = run_diagnostic_production_evaluator(production, case.case_id)
    assert receipt["diagnostic_only"] is True
    assert receipt["g_spec_01"] == "not_evaluated"
    assert receipt["agent_view"] == "outstanding"
    assert "blind_freeze_receipt_sha256" not in receipt["execution"]
    with pytest.raises(SharedRuntimeError, match="diagnostic evaluator freezes"):
        run_frozen_evaluator(production, "production", case.case_id)
    with pytest.raises(SharedRuntimeError, match="verified blind-freeze token"):
        run_frozen_evaluator(production.evaluator, "production", case.case_id)
    with pytest.raises(SharedRuntimeError, match="diagnostic evaluator freezes"):
        run_frozen_evaluator(_full_spec, "full_spec", case.case_id)


def test_pairwise_diagnostic_compares_only_full_spec_and_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _recipe_case()
    full_spec, production = _current_fixtures(monkeypatch, tmp_path, case)
    full_result = _valid_result(case, "full_spec")
    production_result = _valid_result(case, "production")
    full_receipt = _outer_receipt(full_spec.evaluator, case, full_result)
    production_execution = _outer_receipt(production.evaluator, case, production_result)
    monkeypatch.setattr(
        process_module, "_run_frozen_evaluator_process", lambda *_args: production_execution
    )
    diagnostic_receipt = run_diagnostic_production_evaluator(production, case.case_id)

    comparison = compare_full_spec_to_production_diagnostic(
        case, full_spec, production, full_receipt, diagnostic_receipt
    )
    assert isinstance(comparison, DiagnosticComparison)
    assert comparison.status == "MATCH"
    assert comparison.diagnostic_only is True
    assert comparison.g_spec_01 == "not_evaluated"
    assert comparison.agent_view == "outstanding"
    assert set(comparison.source_closure_sha256_by_evaluator) == {"full_spec", "production"}
    assert comparison.case_manifest_sha256 == production.case_manifest_sha256
    assert comparison.diagnostic_freeze_manifest_sha256 == production.manifest_sha256
    record = comparison.to_record()
    assert record["diagnostic_only"] is True
    assert record["g_spec_01"] == "not_evaluated"
    assert record["agent_view"] == "outstanding"
    assert record["case_manifest_sha256"] == production.case_manifest_sha256
    assert record["diagnostic_freeze_manifest_sha256"] == production.manifest_sha256


def test_comparison_retains_payload_and_materialization_mismatches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _recipe_case()
    full_spec, production = _current_fixtures(monkeypatch, tmp_path, case)
    full_result = _valid_result(case, "full_spec")
    changed_production = _valid_result(
        case,
        "production",
        base_plan=[
            {
                "mutation_kind": "CONNECTOR_SYNC_MUTATION",
                "target_entity_id": "conn_01",
                "if_match": ["conn_01@0123456789abcdef:1"],
                "cite": ["CONN-V5-01:ev_00001"],
                "halted_fail_closed": False,
                "halt_reason": "NONE",
            }
        ],
    )
    changed_production["payload"] = {"first_failing_stage": 1, "diagnostics": []}
    changed_production["coverage"] = {
        "phase_targets": [],
        "diagnostics": [],
        "rules": ["CONN-V5-01"],
        "safety_cases": [],
        "snapshots": [],
    }
    full_receipt = _outer_receipt(full_spec.evaluator, case, full_result)
    production_execution = _outer_receipt(production.evaluator, case, changed_production)
    monkeypatch.setattr(
        process_module, "_run_frozen_evaluator_process", lambda *_args: production_execution
    )
    production_receipt = run_diagnostic_production_evaluator(production, case.case_id)

    comparison = compare_full_spec_to_production_diagnostic(
        case, full_spec, production, full_receipt, production_receipt
    )
    assert comparison.status == "MISMATCH"
    assert "$.materialization" in comparison.mismatch_paths
    assert "$.payload/first_failing_stage" in comparison.mismatch_paths
    assert "$.coverage/rules/0" in comparison.mismatch_paths

    malformed_result = _valid_result(case, "production")
    malformed_result["unexpected"] = "shape"
    malformed_execution = _outer_receipt(production.evaluator, case, malformed_result)
    monkeypatch.setattr(
        process_module,
        "_run_frozen_evaluator_process",
        lambda *_args: malformed_execution,
    )
    malformed_receipt = run_diagnostic_production_evaluator(production, case.case_id)
    with pytest.raises(SharedRuntimeError, match="failed the shared protocol contract"):
        compare_full_spec_to_production_diagnostic(
            case, full_spec, production, full_receipt, malformed_receipt
        )


def test_failed_execution_and_non_ok_results_never_produce_a_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _recipe_case()
    full_spec, production = _current_fixtures(monkeypatch, tmp_path, case)
    full_receipt = _outer_receipt(
        full_spec.evaluator, case, _valid_result(case, "full_spec")
    )
    failed = {
        "protocol": PROTOCOL,
        "evaluator_id": "production",
        "source_profile": "production_v4",
        "author_origin": "production_adapter",
        "case_id": case.case_id,
        "input_sha256": case.input_sha256,
        "source_closure_sha256": production.source_closure_sha256,
        "case_manifest_sha256": production.case_manifest_sha256,
        "execution_status": "failed",
        "failure": "synthetic timeout",
        "audit": None,
        "stdout_sha256": sha256_bytes(b""),
        "stderr_sha256": sha256_bytes(b""),
        "raw_stdout_b64": "",
        "raw_stderr_b64": "",
    }
    monkeypatch.setattr(process_module, "_run_frozen_evaluator_process", lambda *_args: failed)
    failed_receipt = run_diagnostic_production_evaluator(production, case.case_id)
    comparison = compare_full_spec_to_production_diagnostic(
        case, full_spec, production, full_receipt, failed_receipt
    )
    assert comparison.status == "INCOMPLETE"
    assert comparison.execution_status_by_evaluator["production"] == "failed"

    unsupported = _valid_result(case, "production")
    unsupported.update(
        {
            "status": "underdefined",
            "gaps": [
                {
                    "code": "UNDERDEFINED_PUBLIC_INPUT",
                    "operation": "validate_submission",
                    "case_id": case.case_id,
                    "path": "/stimulus",
                    "detail": "synthetic gap",
                }
            ],
        }
    )
    monkeypatch.setattr(
        process_module,
        "_run_frozen_evaluator_process",
        lambda *_args: _outer_receipt(production.evaluator, case, unsupported),
    )
    unsupported_receipt = run_diagnostic_production_evaluator(production, case.case_id)
    incomplete = compare_full_spec_to_production_diagnostic(
        case, full_spec, production, full_receipt, unsupported_receipt
    )
    assert incomplete.status == "MISMATCH"
    assert "$.status" in incomplete.mismatch_paths


def test_case_source_and_contract_mismatches_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _recipe_case()
    full_spec, production = _current_fixtures(monkeypatch, tmp_path, case)
    full_receipt = _outer_receipt(
        full_spec.evaluator, case, _valid_result(case, "full_spec")
    )
    production_execution = _outer_receipt(
        production.evaluator, case, _valid_result(case, "production")
    )
    monkeypatch.setattr(
        process_module, "_run_frozen_evaluator_process", lambda *_args: production_execution
    )
    production_receipt = run_diagnostic_production_evaluator(production, case.case_id)

    with pytest.raises(SharedRuntimeError, match="case inventories differ"):
        other_case = _recipe_case("v5_dev/episode-1/p1/other")
        other_full_spec = prepare_full_spec_diagnostic(
            _closure_manifest_fixture("full_spec", (other_case,), tmp_path)
        )
        compare_full_spec_to_production_diagnostic(
            case, other_full_spec, production, full_receipt, production_receipt
        )

    tampered_case_bytes = dict(production.evaluator.case_bytes)
    tampered_case_bytes[case.case_id] = b"{}"
    tampered_evaluator = replace(production.evaluator, case_bytes=tampered_case_bytes)
    tampered_production = replace(production, evaluator=tampered_evaluator)
    with pytest.raises(SharedRuntimeError, match="sealed inventory"):
        run_diagnostic_production_evaluator(tampered_production, case.case_id)

    tampered_full_spec_bytes = dict(full_spec.evaluator.case_bytes)
    tampered_full_spec_bytes[case.case_id] = b"{}"
    changed_full_spec = replace(
        full_spec, evaluator=replace(full_spec.evaluator, case_bytes=tampered_full_spec_bytes)
    )
    with pytest.raises(SharedRuntimeError, match="sealed inventory"):
        compare_full_spec_to_production_diagnostic(
            case, changed_full_spec, production, full_receipt, production_receipt
        )

    with pytest.raises(SharedRuntimeError, match="source closure identity is stale"):
        changed_evaluator = replace(production.evaluator, source_closure_sha256="e" * 64)
        changed_production = replace(production, evaluator=changed_evaluator)
        compare_full_spec_to_production_diagnostic(
            case, full_spec, changed_production, full_receipt, production_receipt
        )

    changed_contract = dict(production.normative_contract)
    changed_contract["contract_id"] = "stale-contract"
    stale_production = replace(production, normative_contract=changed_contract)
    with pytest.raises(SharedRuntimeError, match="normative contract"):
        compare_full_spec_to_production_diagnostic(
            case, full_spec, stale_production, full_receipt, production_receipt
        )


def test_joint_snapshot_and_evaluator_replacement_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _recipe_case()
    full_spec, production = _current_fixtures(monkeypatch, tmp_path, case)
    changed_public_case = dict(case.public_case)
    changed_public_case["rendered_card"] = "post-freeze tampered card\n"
    changed_case = freeze_case(changed_public_case)
    mutable_full_spec = _closure_manifest_fixture("full_spec", (case,), tmp_path)
    original_case_map = mutable_full_spec.case_bytes
    sealed_full_spec = prepare_full_spec_diagnostic(mutable_full_spec)
    original_case_map[case.case_id] = changed_case.canonical_bytes
    assert sealed_full_spec.evaluator.case_bytes[case.case_id] == case.canonical_bytes

    def forged_snapshot(wrapper: Any) -> tuple[Any, dict[str, bytes], dict[str, str]]:
        case_bytes = {changed_case.case_id: changed_case.canonical_bytes}
        case_hashes = {changed_case.case_id: changed_case.input_sha256}
        with pytest.raises(SharedRuntimeError, match="factory-issued"):
            process_module._DiagnosticCaseInventory(
                inventory=wrapper.case_snapshot.inventory,
                manifest_sha256=wrapper.case_snapshot.manifest_sha256,
                case_bytes=case_bytes,
                case_sha256=case_hashes,
            )

        # Simulate a caller bypassing the constructor and retaining mutable maps
        # behind mapping proxies. Verifiers must reject the missing factory mark.
        snapshot = object.__new__(process_module._DiagnosticCaseInventory)
        object.__setattr__(snapshot, "inventory", wrapper.case_snapshot.inventory)
        object.__setattr__(snapshot, "manifest_sha256", wrapper.case_snapshot.manifest_sha256)
        object.__setattr__(snapshot, "case_bytes", MappingProxyType(case_bytes))
        object.__setattr__(snapshot, "case_sha256", MappingProxyType(case_hashes))
        object.__setattr__(snapshot, "_issuer", process_module._DIAGNOSTIC_CASE_INVENTORY_ISSUER)
        return snapshot, case_bytes, case_hashes

    prod_snapshot, prod_backing_bytes, prod_backing_hashes = forged_snapshot(production)
    full_snapshot, full_backing_bytes, full_backing_hashes = forged_snapshot(full_spec)
    changed_production = replace(
        production,
        evaluator=replace(
            production.evaluator,
            case_bytes=prod_snapshot.case_bytes,
            case_sha256=prod_snapshot.case_sha256,
        ),
        case_snapshot=prod_snapshot,
    )
    changed_full_spec = replace(
        full_spec,
        evaluator=replace(
            full_spec.evaluator,
            case_bytes=full_snapshot.case_bytes,
            case_sha256=full_snapshot.case_sha256,
        ),
        case_snapshot=full_snapshot,
    )
    prod_backing_bytes[changed_case.case_id] = b"mutated after wrapping"
    prod_backing_hashes[changed_case.case_id] = "f" * 64
    full_backing_bytes[changed_case.case_id] = b"mutated after wrapping"
    full_backing_hashes[changed_case.case_id] = "f" * 64

    with pytest.raises(SharedRuntimeError, match="factory-issued provenance"):
        run_diagnostic_production_evaluator(changed_production, changed_case.case_id)
    with pytest.raises(SharedRuntimeError, match="factory-issued provenance"):
        compare_full_spec_to_production_diagnostic(
            changed_case,
            changed_full_spec,
            changed_production,
            {},
            {},
        )

    with pytest.raises(TypeError):
        production.case_snapshot.case_bytes[case.case_id] = changed_case.canonical_bytes


def test_diagnostic_receipts_cannot_enter_qualified_receipt_or_pair_validators(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    case = _recipe_case()
    full_spec, production = _current_fixtures(monkeypatch, tmp_path, case)
    production_execution = _outer_receipt(
        production.evaluator, case, _valid_result(case, "production")
    )
    monkeypatch.setattr(
        process_module, "_run_frozen_evaluator_process", lambda *_args: production_execution
    )
    diagnostic_receipt = run_diagnostic_production_evaluator(production, case.case_id)

    with pytest.raises(ConformanceProtocolError, match="result keys differ"):
        validate_evaluator_result(diagnostic_receipt, case, "production")

    comparison = compare_full_spec_to_production_diagnostic(
        case,
        full_spec,
        production,
        _outer_receipt(full_spec.evaluator, case, _valid_result(case, "full_spec")),
        diagnostic_receipt,
    )
    with pytest.raises(ConformanceProtocolError, match="three verified source-closure hashes"):
        compare_production_result(
            case,
            (),  # type: ignore[arg-type]
            _valid_result(case, "production"),
            source_closure_sha256_by_evaluator=comparison.source_closure_sha256_by_evaluator,
        )

    current_root = "reports/v5/conformance/shared_results/approved_addendum_v1"
    relative = f"{current_root}/diagnostic-only/diagnostic-freeze.json"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    raw = canonical_json_bytes(dict(production.manifest))
    path.write_bytes(raw)
    path.chmod(0o444)
    with pytest.raises(SharedRuntimeError, match="unsupported blind freeze receipt schema"):
        verify_blind_freeze_receipt(Path(relative), sha256_bytes(raw), repository_root=tmp_path)

    verified, error = _verify_shared_freeze(
        path,
        {"path": relative, "sha256": sha256_bytes(raw)},
        {"schema_version": SHARED_FREEZE_SCHEMA_V5_CURRENT},
        repository_root=tmp_path,
    )
    assert verified is None
    assert error is not None and "not yet available" in error


def test_qualified_production_defaults_and_token_requirement_remain_unchanged() -> None:
    case = _recipe_case()
    assert _resolve_source_profile("production").profile_id == "production_v1"
    assert _resolve_source_profile("production", "production_v4").profile_id == "production_v4"
    with pytest.raises(SharedRuntimeError, match="requires a verified blind-output receipt"):
        freeze_shared_evaluator(
            "production",
            [case.public_case],
            source_profile="production_v4",
        )


@pytest.mark.parametrize("case_count", [2, 4, 8])
def test_case_validation_growth_is_linear_after_inventory_sealing(
    case_count: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cases = tuple(_recipe_case(f"case-{index}") for index in range(case_count))
    _fake_freeze_evaluator(monkeypatch, tmp_path)
    production = freeze_production_diagnostic(
        cases, source_profile="production_v4", repository_root=Path.cwd()
    )
    full_spec = prepare_full_spec_diagnostic(
        _closure_manifest_fixture("full_spec", cases, tmp_path)
    )
    case_by_id = {case.case_id: case for case in cases}
    validations = 0
    real_freeze_case = shared_module.freeze_case

    def counting_freeze_case(value: Any) -> FrozenCase:
        nonlocal validations
        validations += 1
        return real_freeze_case(value)

    monkeypatch.setattr(shared_module, "freeze_case", counting_freeze_case)

    def execute(evaluator: FrozenEvaluator, case_id: str, *_args: Any) -> dict[str, Any]:
        case = case_by_id[case_id]
        return _outer_receipt(evaluator, case, _valid_result(case, evaluator.evaluator_id))

    monkeypatch.setattr(process_module, "_run_frozen_evaluator_process", execute)
    for case in cases:
        production_receipt = run_diagnostic_production_evaluator(production, case.case_id)
        full_receipt = _outer_receipt(
            full_spec.evaluator, case, _valid_result(case, "full_spec")
        )
        compare_full_spec_to_production_diagnostic(
            case, full_spec, production, full_receipt, production_receipt
        )

    assert validations == case_count


def test_forbidden_parts_blocks_oracle_requests_module() -> None:
    assert "oracle_requests.py" in process_module._FORBIDDEN_PARTS
