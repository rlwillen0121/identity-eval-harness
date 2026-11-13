from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_args

import pytest

from identitybench.v5 import validation as validation_module
from identitybench.v5.conformance_process import (
    SharedRuntimeError,
    _resolve_source_profile,
    _verify_full_shared_v3_protocol_sidecar,
)
from identitybench.v5.conformance_shared import (
    EVALUATOR_WIRE_ID_BY_SLOT,
    MATERIALIZATION_CONTRACT_ID,
    PINNED_PUBLIC_SCHEMA_SHA256,
    PROTOCOL,
    SCORE_METRIC_NULLABILITY_CONTRACT_ID,
    ConformanceProtocolError,
    canonical_json_bytes,
    freeze_case,
    sha256_bytes,
    strict_json_loads,
    validate_evaluator_result,
)
from identitybench.v5.generator import write_v5_dataset
from identitybench.v5.schemas import RuleIdV5, StrictV5Model
from identitybench.v5.validation import (
    _V7_BOUNDED_CARD_AUDIT_KIND,
    _V7_BOUNDED_FULL_SPEC_AUDIT_KIND,
    _V7_CANONICAL_CAPTURE_HASH_ALGORITHM,
    _V7_CARD_EXECUTED_AUDIT_NOTE,
    _V7_CARD_EXECUTED_AUDIT_SHA256,
    _V7_CARD_HISTORICAL_SCOPE_NOTE,
    _V7_FULL_SPEC_AUTHORIZED_SPEC_SCOPE,
    _V7_FULL_SPEC_EXECUTED_AUDIT_NOTE,
    _V7_FULL_SPEC_NORMATIVE_USE,
    SHARED_PROOF_RECEIPT_SCHEMA_V5,
    SPEC_V5_SHA256,
    GateResultV5,
    GateStatusV5,
    V5ArtifactError,
    V5ValidationReport,
    _agent_view_input_allowed,
    _blind_gate,
    _blind_source_identity,
    _expected_policy_card_anchor_map,
    _g_base_status,
    _negative_mutation_gate,
    _paired_world_noninterference,
    _read_provenance_input,
    _run_baseline_with_host_diagnostics,
    _shared_bound_bytes,
    _shared_expected_coverage,
    _shared_native_origin_claims,
    _shared_outer_stream_profile_matches,
    _shared_reconstruct_corpus,
    _shared_runtime_archive_inventory_matches,
    _shared_v3_manifest_adapter,
    _user_requested_initial_text_tokens,
    _validate_agent_view_packet_inputs,
    _validate_obs_witness,
    _validate_review_provenance,
    _validate_shared_proof_receipt,
    _validate_shared_qualification,
    _verify_shared_audit_companions,
    bind_v5_reference_receipt,
    load_v5_dataset,
)


@pytest.fixture
def artifacts(tmp_path: Path) -> Any:
    dataset_dir = tmp_path / "sealed"
    write_v5_dataset("v5_dev", dataset_dir)
    return load_v5_dataset(dataset_dir)


def _proof_bundle(artifacts: Any, directory: Path) -> Path:
    directory = directory / "reports" / "v5" / "conformance" / "v5_dev"
    directory.mkdir(parents=True, exist_ok=True)
    episodes_sha = hashlib.sha256(artifacts.episodes_bytes).hexdigest()
    episode = artifacts.episodes[0]
    oracle = artifacts.oracles[0]
    gold = []
    for mutation in oracle.gold_p1_mutations:
        data = mutation.model_dump(mode="json")
        common = {"mutation_kind", "target_entity_id", "if_match", "cite"}
        gold.append(
            {
                "mutation_kind": data["mutation_kind"],
                "target_entity_id": data["target_entity_id"],
                "semantic_fields": {key: value for key, value in data.items() if key not in common},
                "if_match": data["if_match"],
                "cite": data["cite"],
            }
        )
    targets = [
        {"mutation_kind": item["mutation_kind"], "target_entity_id": item["target_entity_id"]}
        for item in gold
    ]
    for name in ("full_spec_blind", "agent_view_only"):
        proof = {
            "schema_version": "identitybench.v5.reference-expectations.v1",
            "proof_scope": "full_spec_blind" if name == "full_spec_blind" else "agent_view_only",
            "suite_id": "v5_dev",
            "episodes_sha256": episodes_sha,
            "phase_expectations": [
                {
                    "episode_id": episode.episode_id,
                    "family_id": episode.family_id,
                    "phase_index": 1,
                    "witness_id": f"{name}-partial-phase-row",
                    "required_target_multiset": targets,
                    "mutations": gold,
                }
            ],
            "policy_card_anchor_map": _expected_policy_card_anchor_map(),
            "underdefined": [{"reason": "test fixture intentionally omits proof dimensions"}],
        }
        proof_path = directory / (
            "full_spec_reference.json" if name == "full_spec_blind" else "agent_view_reference.json"
        )
        proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
    return bind_v5_reference_receipt(artifacts.dataset_dir, conformance_dir=directory)


def test_blind_gate_missing_proof_is_unknown(artifacts: Any) -> None:
    result = _blind_gate(None, artifacts)

    assert result.status is GateStatusV5.UNKNOWN
    assert "unavailable" in result.errors[0]


def _shared_receipt_shell(suite_id: str) -> dict[str, Any]:
    return {
        "schema_version": SHARED_PROOF_RECEIPT_SCHEMA_V5,
        "proof_scope": "G-SPEC-01",
        "suite_id": suite_id,
        "spec_sha256": SPEC_V5_SHA256,
        "dataset_bindings": [],
        "corpus": {},
        "freeze": {},
        "evaluators": {
            "full_spec_blind": {},
            "agent_view_only": {},
            "production": {},
        },
    }


@pytest.mark.parametrize(
    ("evaluator_slot", "other_slot"),
    [("full_spec", "agent_view"), ("agent_view", "full_spec")],
)
def test_shared_outer_profile_and_nested_wire_identities_are_distinct(
    evaluator_slot: str, other_slot: str
) -> None:
    case = freeze_case(
        {
            "protocol": PROTOCOL,
            "case_id": "v5_dev/ep-a/p1/derive",
            "operation": "derive_phase",
            "suite_id": "v5_dev",
            "phase_index": 1,
            "public_episode": {
                "episode_id": "ep-a",
                "phase1_manifest": {},
                "phase1_slices": [],
                "policy_card": {},
                "q1_slice_ids_p1": [],
                "q2_slice_ids_p1": [],
            },
            "rendered_card": "public rendered card\n",
            "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
            "stimulus": {},
        }
    )
    anchors = {f"RULE-{index:02d}": [f"row-{index}"] for index in range(26)}
    anchors.update({f"BOUNDARY:label-{index:02d}": [f"boundary-{index}"] for index in range(13)})
    result = {
        "protocol": PROTOCOL,
        "evaluator_id": EVALUATOR_WIRE_ID_BY_SLOT[evaluator_slot],
        "case_id": case.case_id,
        "input_sha256": case.input_sha256,
        "status": "ok",
        "payload": {"mutations": [], "policy_card_anchor_map": anchors},
        "materialization": None,
        "coverage": {
            "phase_targets": [],
            "diagnostics": [],
            "rules": [],
            "safety_cases": [],
            "snapshots": [],
        },
        "gaps": [],
    }
    row = {"evaluator_id": evaluator_slot, "result": result}

    assert _shared_outer_stream_profile_matches(row, evaluator_slot)
    assert (
        validate_evaluator_result(result, case, evaluator_slot)["evaluator_id"]
        == (EVALUATOR_WIRE_ID_BY_SLOT[evaluator_slot])
    )

    wrong_outer = {**row, "evaluator_id": EVALUATOR_WIRE_ID_BY_SLOT[evaluator_slot]}
    swapped_outer = {**row, "evaluator_id": other_slot}
    assert not _shared_outer_stream_profile_matches(wrong_outer, evaluator_slot)
    assert not _shared_outer_stream_profile_matches(swapped_outer, evaluator_slot)

    wrong_nested_wire = {**result, "evaluator_id": EVALUATOR_WIRE_ID_BY_SLOT[other_slot]}
    wrong_nested_slot = {**result, "evaluator_id": evaluator_slot}
    with pytest.raises(ConformanceProtocolError, match="wire identity mismatch"):
        validate_evaluator_result(wrong_nested_wire, case, evaluator_slot)
    with pytest.raises(ConformanceProtocolError, match="wire identity mismatch"):
        validate_evaluator_result(wrong_nested_slot, case, evaluator_slot)


def test_shared_runtime_archive_inventory_matches_by_path_and_known_overlap() -> None:
    shared = {
        "path": "src/identitybench/v5/conformance_shared.py",
        "sha256": "a" * 64,
        "size_bytes": 10,
    }
    approved = [{"path": "src/identitybench/v5/validation.py", "sha256": "b" * 64}]
    runtime = [{"path": "src/identitybench/v5/conformance_process.py", "sha256": "c" * 64}]
    reordered_archive = [runtime[0], approved[0]]
    archive_with_shared = [runtime[0], shared, approved[0]]

    assert _shared_runtime_archive_inventory_matches(
        "full_spec_blind", approved, runtime, reordered_archive
    )
    assert _shared_runtime_archive_inventory_matches(
        "production", [*approved, shared], [*runtime, shared], archive_with_shared
    )
    assert not _shared_runtime_archive_inventory_matches(
        "full_spec_blind", [*approved, shared], [*runtime, shared], archive_with_shared
    )


@pytest.mark.parametrize(
    ("evaluator_id", "approved", "runtime", "archive"),
    [
        (
            "production",
            [
                {"path": "src/identitybench/v5/conformance_shared.py", "sha256": "a" * 64},
            ],
            [
                {"path": "src/identitybench/v5/conformance_shared.py", "sha256": "b" * 64},
            ],
            [{"path": "src/identitybench/v5/conformance_shared.py", "sha256": "b" * 64}],
        ),
        (
            "production",
            [
                {"path": "src/identitybench/v5/other.py", "sha256": "a" * 64},
            ],
            [{"path": "src/identitybench/v5/other.py", "sha256": "a" * 64}],
            [{"path": "src/identitybench/v5/other.py", "sha256": "a" * 64}],
        ),
        (
            "production",
            [
                {
                    "path": "src/identitybench/v5/conformance_shared.py",
                    "sha256": "a" * 64,
                },
                {
                    "path": "src/identitybench/v5/conformance_shared.py",
                    "sha256": "a" * 64,
                },
            ],
            [],
            [
                {
                    "path": "src/identitybench/v5/conformance_shared.py",
                    "sha256": "a" * 64,
                }
            ],
        ),
        (
            "agent_view_only",
            [{"path": "src/identitybench/v5/one.py", "sha256": "a" * 64}],
            [{"path": "src/identitybench/v5/two.py", "sha256": "b" * 64}],
            [{"path": "src/identitybench/v5/one.py", "sha256": "a" * 64}],
        ),
        (
            "agent_view_only",
            [{"path": "src/identitybench/v5/one.py", "sha256": "a" * 64}],
            [],
            [
                {"path": "src/identitybench/v5/one.py", "sha256": "c" * 64},
            ],
        ),
        (
            "production",
            [{"path": "src/identitybench/v5/one.py", "sha256": "a" * 64}],
            [],
            [
                {"path": "src/identitybench/v5/one.py", "sha256": "a" * 64},
                {"path": "src/identitybench/v5/one.py", "sha256": "a" * 64},
            ],
        ),
    ],
)
def test_shared_runtime_archive_inventory_rejects_ambiguous_or_mismatched_paths(
    evaluator_id: str,
    approved: list[dict[str, object]],
    runtime: list[dict[str, object]],
    archive: list[dict[str, object]],
) -> None:
    assert not _shared_runtime_archive_inventory_matches(evaluator_id, approved, runtime, archive)


def test_shared_production_audit_null_failure_adapter_is_exact_and_scoped() -> None:
    receipt = {
        "audit": {"synthetic": True},
        "case_id": "synthetic-case",
        "execution_status": "ok",
        "input_sha256": "a" * 64,
        "raw_stderr_b64": "c3RkZXJy",
        "raw_stdout_b64": "c3Rkb3V0",
        "source_closure_sha256": "b" * 64,
        "stderr_sha256": "c" * 64,
        "stdout_sha256": "d" * 64,
    }

    _verify_shared_audit_companions([receipt], [receipt], "production")
    _verify_shared_audit_companions([{**receipt, "failure": None}], [receipt], "production")

    with pytest.raises(SharedRuntimeError, match="failure companion field is not null"):
        _verify_shared_audit_companions(
            [{**receipt, "failure": "synthetic failure"}], [receipt], "production"
        )
    with pytest.raises(SharedRuntimeError, match="raw audit fields differ for production"):
        _verify_shared_audit_companions(
            [{**receipt, "failure": None, "unreviewed": None}], [receipt], "production"
        )
    with pytest.raises(SharedRuntimeError, match="raw audit fields differ for full_spec"):
        _verify_shared_audit_companions([{**receipt, "failure": None}], [receipt], "full_spec")
    with pytest.raises(SharedRuntimeError, match="raw audit binding differs for production"):
        _verify_shared_audit_companions(
            [{**receipt, "failure": None}],
            [{**receipt, "stdout_sha256": "e" * 64}],
            "production",
        )


def test_blind_gate_dispatches_shared_receipt_and_does_not_trust_1702_claim(
    artifacts: Any, tmp_path: Path
) -> None:
    receipt_value = _shared_receipt_shell("v5_dev")
    receipt_value["claimed_status"] = "PASS"
    receipt_value["claimed_case_count"] = 1702
    receipt = tmp_path / "reports" / "v5" / "conformance" / "shared-proof.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(receipt_value, sort_keys=True), encoding="utf-8")

    result = _blind_gate(receipt, artifacts, repository_root=tmp_path)

    assert result.status is GateStatusV5.FAIL
    assert any("unexpected fields" in message for message in result.errors)
    assert not any("bound reference receipt" in message for message in result.errors)


def test_blind_gate_keeps_phase_only_shared_freeze_unknown(artifacts: Any, tmp_path: Path) -> None:
    receipt_value = _shared_receipt_shell("v5_dev")
    receipt_value["proof_scope"] = "diagnostic_phase_batch"
    receipt = tmp_path / "reports" / "v5" / "conformance" / "shared-diagnostic.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(receipt_value, sort_keys=True), encoding="utf-8")

    result = _blind_gate(receipt, artifacts, repository_root=tmp_path)

    assert result.status is GateStatusV5.UNKNOWN
    assert any("phase-only diagnostic" in message for message in result.errors)


def test_shared_freeze_accepts_hash_bound_v3_sidecar_reference(
    artifacts: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_root = tmp_path
    artifact_root = repository_root / "reports" / "v5" / "conformance" / "shared_results" / "v1"
    artifact_root.mkdir(parents=True)

    def write_bound(name: str, raw: bytes) -> dict[str, str]:
        path = artifact_root / name
        if path.exists():
            path.chmod(0o644)
        path.write_bytes(raw)
        path.chmod(0o444)
        return {
            "path": path.relative_to(repository_root).as_posix(),
            "sha256": sha256_bytes(raw),
        }

    corpus = {
        name: write_bound(f"{name}.json", b"{}\n")
        for name in (
            "manifest",
            "phase_inputs",
            "recipes",
            "traces",
            "family_map",
            "public_cases",
            "case_index",
            "coverage",
        )
    }
    scope_id = "synthetic-shared-scope"
    sidecar = {
        "protocol": PROTOCOL,
        "materialization_contract_id": MATERIALIZATION_CONTRACT_ID,
        "score_metric_nullability_contract_id": SCORE_METRIC_NULLABILITY_CONTRACT_ID,
        "scope_id": scope_id,
    }
    sidecar_ref = write_bound("protocol-sidecar.json", canonical_json_bytes(sidecar) + b"\n")
    case_manifest_sha256 = "a" * 64
    case_index_sha256 = "b" * 64
    case_inventory = (("synthetic-case", "c" * 64),)
    freeze = {
        "schema_version": "identitybench.v5.blind-output-freeze.v3",
        "proof_scope": "full_shared_conformance",
        "scope": {
            "case_count": 1702,
            "operations": ["derive_phase", "safety", "score_trace", "validate_submission"],
            "phase_indices": [1, 2],
            "scope_id": scope_id,
            "case_index_sha256": case_index_sha256,
            "turn_context": "T0",
            "steering": "UNSTEERED",
        },
        "protocol_sidecar": sidecar_ref,
        "input_artifacts": {"manifest": corpus["manifest"]},
        "public_inputs": corpus["public_cases"],
    }
    freeze_ref = write_bound("freeze.json", canonical_json_bytes(freeze) + b"\n")

    receipt = _shared_receipt_shell("v5_dev")
    receipt.update({"corpus": corpus, "freeze": freeze_ref})

    monkeypatch.setattr(
        "identitybench.v5.validation._shared_load_dataset_bindings",
        lambda _value, _root, _artifacts, _errors: ({}, []),
    )
    monkeypatch.setattr(
        "identitybench.v5.validation._shared_reconstruct_corpus",
        lambda *_args, **_kwargs: {
            "cases": [
                SimpleNamespace(case_id=case_inventory[0][0], input_sha256=case_inventory[0][1])
            ],
            "case_manifest_sha256": case_manifest_sha256,
            "case_index_sha256": case_index_sha256,
        },
    )

    def verify_freeze(
        _freeze_path: Path,
        _reference: object,
        freeze_value: dict[str, Any],
        *,
        repository_root: Path,
    ) -> tuple[Any, None]:
        verified_sidecar, _bound_artifacts = _verify_full_shared_v3_protocol_sidecar(
            repository_root,
            freeze_value["protocol_sidecar"],
            scope_id=scope_id,
            sha256_bytes=sha256_bytes,
            strict_json_loads=strict_json_loads,
            materialization_contract_id=MATERIALIZATION_CONTRACT_ID,
            score_metric_nullability_contract_id=SCORE_METRIC_NULLABILITY_CONTRACT_ID,
        )
        assert verified_sidecar == sidecar
        token = SimpleNamespace(
            case_inventory=case_inventory,
            proof_scope="full_shared_conformance",
            case_manifest_sha256=case_manifest_sha256,
            case_index_sha256=case_index_sha256,
            assert_current=lambda: None,
        )
        return token, None

    monkeypatch.setattr("identitybench.v5.validation._verify_shared_freeze", verify_freeze)

    errors, _gaps, evidence = _validate_shared_proof_receipt(
        artifact_root / "receipt.json",
        canonical_json_bytes(receipt) + b"\n",
        receipt,
        artifacts,
        repository_root=repository_root,
    )

    assert evidence["blind_freeze_verified"] is True
    assert not any("sidecar" in error for error in errors)

    mismatched_sidecar = {
        **sidecar,
        "materialization_contract_id": "identitybench.v5.materialization-contract.unknown",
    }
    freeze["protocol_sidecar"] = write_bound(
        "protocol-sidecar.json", canonical_json_bytes(mismatched_sidecar) + b"\n"
    )
    freeze_ref = write_bound("freeze.json", canonical_json_bytes(freeze) + b"\n")
    receipt["freeze"] = freeze_ref
    receipt_path = artifact_root / "receipt.json"
    receipt_path.write_bytes(canonical_json_bytes(receipt) + b"\n")

    rejected = _blind_gate(receipt_path, artifacts, repository_root=repository_root)
    assert rejected.status is GateStatusV5.FAIL
    assert any("shared proof validation failed closed" in error for error in rejected.errors)


def test_v3_manifest_uses_its_explicit_ordered_index_and_checkpoint_layout() -> None:
    repo = Path.cwd()
    manifest_path = repo / "reports/v5/conformance/shared_inputs/v3/manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    errors: list[str] = []
    gaps: list[str] = []

    adapted = _shared_v3_manifest_adapter(
        manifest,
        manifest_path=manifest_path,
        repository_root=repo,
        corpus_files={"manifest": (manifest_path.read_bytes(), manifest_path)},
        errors=errors,
        gaps=gaps,
    )

    assert adapted is not None
    assert adapted["all_case_count"] == 1702
    assert len(adapted["all_case_index"]) == 1702
    assert adapted["all_case_index"][0]["canonical_case_bytes"] > 0
    assert adapted["phase_input_artifact"]["row_count"] == 360
    assert set(adapted["stimulus_artifacts"]) == {"recipes", "traces"}
    assert not any("contradictory" in error for error in errors)

    contradictory = {**manifest, "all_case_count": 1702}
    errors = []
    gaps = []
    rejected = _shared_v3_manifest_adapter(
        contradictory,
        manifest_path=manifest_path,
        repository_root=repo,
        corpus_files={"manifest": (manifest_path.read_bytes(), manifest_path)},
        errors=errors,
        gaps=gaps,
    )
    assert rejected is None
    assert any("unsupported fields" in error for error in errors)


@pytest.mark.parametrize(
    ("layout", "supported"),
    [("v3", True), ("v3-cursor-literal", True), ("v3-unreviewed", False)],
)
def test_v3_manifest_adapter_supports_only_reviewed_layout_paths(
    tmp_path: Path, layout: str, supported: bool
) -> None:
    manifest_path = (
        tmp_path / "reports" / "v5" / "conformance" / "shared_inputs" / layout / "manifest.json"
    )
    manifest = {
        "artifact_status": "synthetic",
        "case_index": [],
        "corpus_version": "v3",
        "counts": {
            "episodes": 180,
            "operations": {
                "derive_phase": 360,
                "safety": 497,
                "score_trace": 60,
                "validate_submission": 785,
            },
            "phase_cases": 360,
            "phase_target_tuples": 3435,
            "recipe_cases": 1282,
            "total_cases": 1702,
            "trace_cases": 60,
        },
        "coverage_intent_only": True,
        "envelope_contract": "synthetic",
        "family_metadata": {},
        "normative_inputs": {
            "schemas_path": "src/identitybench/v5/schemas.py",
            "schemas_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
            "spec_path": "SPEC-v5.md",
            "spec_sha256": SPEC_V5_SHA256,
        },
        "observed_coverage": {},
        "packet_sources": [],
        "phase_checkpoint": {},
        "protocol": PROTOCOL,
        "stimulus_sources": [],
    }
    errors: list[str] = []
    gaps: list[str] = []

    adapted = _shared_v3_manifest_adapter(
        manifest,
        manifest_path=manifest_path,
        repository_root=tmp_path,
        corpus_files={"manifest": (canonical_json_bytes(manifest) + b"\n", manifest_path)},
        errors=errors,
        gaps=gaps,
    )

    assert adapted is None
    path_rejection = "v3 shared manifest is not at its versioned public input path"
    if supported:
        assert not any(path_rejection in error for error in errors)
        assert any("full ordered case_index" in gap for gap in gaps)
    else:
        assert any(path_rejection in error for error in errors)


def test_shared_reconstruction_recognizes_v3_before_requiring_all_suites() -> None:
    repo = Path.cwd()
    manifest_path = repo / "reports/v5/conformance/shared_inputs/v3/manifest.json"
    errors: list[str] = []
    gaps: list[str] = []

    result = _shared_reconstruct_corpus(
        {"manifest": (manifest_path.read_bytes(), manifest_path)},
        {},
        errors,
        gaps,
        repository_root=repo,
    )

    assert result is None
    assert not any("complete 1702-case inventory" in gap for gap in gaps)
    assert not any("complete ordered case index" in gap for gap in gaps)
    assert any("all four exact sealed suites" in gap for gap in gaps)


def test_shared_receipt_rejects_tampered_artifact_digest_before_parsing(
    artifacts: Any, tmp_path: Path
) -> None:
    receipt_value = _shared_receipt_shell("v5_dev")
    corpus: dict[str, dict[str, str]] = {}
    for name in (
        "manifest",
        "phase_inputs",
        "recipes",
        "traces",
        "family_map",
        "public_cases",
        "case_index",
        "coverage",
    ):
        artifact_path = tmp_path / "reports" / "v5" / "conformance" / f"{name}.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(b"{}\n")
        corpus[name] = {
            "path": f"reports/v5/conformance/{name}.json",
            "sha256": "0" * 64,
        }
    receipt_value["corpus"] = corpus
    receipt = tmp_path / "reports" / "v5" / "conformance" / "shared-tampered.json"
    receipt.write_text(json.dumps(receipt_value, sort_keys=True), encoding="utf-8")

    result = _blind_gate(receipt, artifacts, repository_root=tmp_path)

    assert result.status is GateStatusV5.FAIL
    assert any("artifact digest mismatch" in message for message in result.errors)


def test_shared_artifact_reader_rejects_paths_outside_public_roots(tmp_path: Path) -> None:
    outside = tmp_path / "src" / "private.json"
    outside.parent.mkdir()
    payload = b'{"public":"test"}'
    outside.write_bytes(payload)
    errors: list[str] = []
    gaps: list[str] = []

    result = _shared_bound_bytes(
        tmp_path,
        {"path": "src/private.json", "sha256": hashlib.sha256(payload).hexdigest()},
        label="test evidence",
        errors=errors,
        gaps=gaps,
    )

    assert result is None
    assert errors == ["shared test evidence artifact is outside public dataset/conformance roots"]
    assert gaps == []


def _shared_qualification_fixture(
    root: Path,
    *,
    evaluator_id: str = "full_spec_blind",
    reviewer: str = "independent-review-task",
    history_mode: str = "paginated",
    audit_prefix_sha256: str = "a" * 64,
    adjudication_prefix_sha256: str = "a" * 64,
    cutoff_timestamp_utc: str = "2026-11-12T12:00:00Z",
    record_version: int = 2,
    record_status: str = "QUALIFIED_ELIGIBLE",
) -> tuple[dict[str, Any], bytes, str, str]:
    capture_raw = b"captured source manifest bytes"
    capture_raw_sha = hashlib.sha256(capture_raw).hexdigest()
    capture_logical_sha = "b" * 64
    author_task, source_profile, author_origin = (
        ("/root/fresh_full_spec_v2", "fresh_full_spec_v3", "fresh_full_spec_v2")
        if evaluator_id == "full_spec_blind"
        else (
            "/root/fresh_card_only_v2",
            "fresh_card_only_v2",
            "fresh_independent_card_only_author_v2",
        )
    )
    if evaluator_id == "full_spec_blind":
        audit = {
            "native_provenance": {
                "child_prefix_capture": {
                    "prefix_bytes": 123,
                    "prefix_sha256": audit_prefix_sha256,
                    "cutoff_source_line": 42,
                    "cutoff_timestamp_utc": cutoff_timestamp_utc,
                    "prefix_matches_initial_capture": True,
                },
                "child_session": {
                    "agent_path": author_task,
                    "session_id": "session-a",
                    "parent_thread_id": "parent-a",
                    "history_mode": history_mode,
                    "cwd_matches_repo": True,
                },
            },
            "scope": {"author_path": author_task},
        }
    else:
        audit = {
            "child_session": {
                "prefix": {
                    "bytes": 123,
                    "line_count": 42,
                    "sha256": audit_prefix_sha256,
                    "matches_rechecked_capture": True,
                    "last_record": {"line": 42, "timestamp": cutoff_timestamp_utc},
                },
                "session_meta": {
                    "agent_path": author_task,
                    "cwd": str(root.resolve()),
                    "history_mode": history_mode,
                    "id": "session-a",
                    "parent_thread_id": "parent-a",
                },
            },
            "root_spawn_evidence": {
                "status": "completed",
                "matched_agent_path": author_task,
                "same_cwd": str(root.resolve()),
            },
            "scope": {"author_path": author_task},
        }
    audit_path = root / "reports" / "v5" / "conformance" / "review" / "origin-audit.json"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_raw = json.dumps(audit, sort_keys=True, separators=(",", ":")).encode()
    audit_path.write_bytes(audit_raw)
    audit_ref = {
        "path": "reports/v5/conformance/review/origin-audit.json",
        "sha256": hashlib.sha256(audit_raw).hexdigest(),
    }
    adjudication = {
        "record_type": "source_origin_adjudication",
        "record_version": record_version,
        "status": record_status,
        "subject": {
            "author_origin": author_origin,
            "author_task": author_task,
            "source_profile": source_profile,
            "evaluator_id": evaluator_id,
        },
        "normative_spec": {"sha256": SPEC_V5_SHA256},
        "scope": {
            "capture_manifest_raw_sha256": capture_raw_sha,
            "capture_manifest_logical_sha256": capture_logical_sha,
            "session_id": "session-a",
            "prefix_bytes": 123,
            "prefix_sha256": adjudication_prefix_sha256,
            "cutoff_line_inclusive": 42,
            "cutoff_timestamp_utc": cutoff_timestamp_utc,
        },
        "evidence": [
            {
                "path": "reports/v5/conformance/review/capture-manifest.json",
                "raw_sha256": capture_raw_sha,
            },
            {"path": audit_ref["path"], "raw_sha256": audit_ref["sha256"]},
        ],
        "independent_review": {"decision": "PASS", "reviewers": [reviewer]},
    }
    adjudication_path = root / "reports" / "v5" / "conformance" / "review" / "adjudication.json"
    (root / "reports" / "v5" / "conformance" / "review" / "capture-manifest.json").write_bytes(
        capture_raw
    )
    adjudication_raw = json.dumps(adjudication, sort_keys=True, separators=(",", ":")).encode()
    adjudication_path.write_bytes(adjudication_raw)
    qualification = {
        "kind": "author_origin",
        "adjudication": {
            "path": "reports/v5/conformance/review/adjudication.json",
            "sha256": hashlib.sha256(adjudication_raw).hexdigest(),
        },
        "origin_audit": audit_ref,
    }
    return qualification, capture_raw, capture_raw_sha, capture_logical_sha


def _shared_v7_qualification_fixture(root: Path, evaluator_id: str) -> dict[str, Any]:
    identities = {
        "full_spec_blind": (
            "bounded_full_spec_v1",
            "fresh_bounded_full_spec_author_v1",
            "/root/bounded_full_spec_reference",
        ),
        "agent_view_only": (
            "fresh_card_only_v2",
            "fresh_independent_card_only_author_v2",
            "/root/fresh_card_only_v2",
        ),
    }
    source_profile, author_origin, author_task = identities[evaluator_id]
    artifact_dir = root / "reports" / "v5" / "conformance" / "shared_results" / "v1" / "v7-test"
    native_dir = root / "reports" / "v5" / "conformance" / "native_audit"
    input_dir = root / "reports" / "v5" / "conformance" / "inputs" / "spec-sections-1-4"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    native_dir.mkdir(parents=True, exist_ok=True)
    input_dir.mkdir(parents=True, exist_ok=True)

    def write_bytes(path: Path, raw: bytes) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path.relative_to(root).as_posix()

    source_files = [
        {"path": f"src/identitybench/v5/synthetic_{index}.py", "sha256": str(index) * 64}
        for index in (1, 2)
    ]
    capture_unsigned = {
        "source_profile": source_profile,
        "author_origin": author_origin,
        "source_files": source_files,
    }
    capture_logical_sha = sha256_bytes(canonical_json_bytes(capture_unsigned))
    capture_value = {**capture_unsigned, "manifest_sha256": capture_logical_sha}
    capture_raw = canonical_json_bytes(capture_value) + b"\n"
    capture_raw_sha = sha256_bytes(capture_raw)
    capture_path = write_bytes(artifact_dir / "capture-manifest.json", capture_raw)
    capture_cutoff = "2026-11-12T21:56:08.577290+00:00"
    prefix_cutoff = "2026-11-12T21:35:14.248Z"
    session_id = "synthetic-v7-session"
    prefix_bytes = 12345
    prefix_sha = "a" * 64
    cutoff_line = 42
    descriptor_raw = canonical_json_bytes({"kind": "synthetic capture descriptor"}) + b"\n"
    descriptor_path = write_bytes(artifact_dir / "capture-run-descriptor.json", descriptor_raw)
    descriptor_sha = sha256_bytes(descriptor_raw)
    common_limits = [
        "Opaque bodies and unresolved operations remain outside this metadata audit.",
        "The local prefix hash is not cryptographic chronology or OS-level completeness proof.",
    ]

    if evaluator_id == "full_spec_blind":
        capture_binding = {
            "logical_sha256_verification": _V7_CANONICAL_CAPTURE_HASH_ALGORITHM,
            "manifest_bytes": len(capture_raw),
            "manifest_canonical_json_sha256_observed": capture_logical_sha,
            "manifest_filesystem_mtime_ns": 1794520568577290233,
            "manifest_filesystem_mtime_utc": capture_cutoff,
            "manifest_logical_sha256": capture_logical_sha,
            "manifest_only_read": True,
            "manifest_path": capture_path,
            "manifest_raw_sha256": capture_raw_sha,
            "mtime_precision_comparison": "synthetic precise mtime comparison",
            "referenced_paths_followed": False,
        }
        audit = {
            "ambiguity_record": {"status": "synthetic"},
            "approved_auditor_script_sha256": sha256_bytes(b"synthetic archived extractor\n"),
            "audit_identity": {
                "author_origin": author_origin,
                "child_session_id": session_id,
                "lane": "fresh isolated full-spec blind reference",
                "source_profile": source_profile,
                "task": f"{author_task} (fresh fork_none)",
                "wire_profile": evaluator_id,
            },
            "audit_kind": _V7_BOUNDED_FULL_SPEC_AUDIT_KIND,
            "author_content_allowlist": ["synthetic public input"],
            "author_self_audit": {"status": "synthetic"},
            "capture_binding": capture_binding,
            "capture_descriptor": {
                "origin": (
                    "root-supplied and independently verified metadata; not opened by this script"
                ),
                "path": descriptor_path,
                "sha256": descriptor_sha,
            },
            "child_log_prefix": {
                "bytes_read_beyond_included_prefix": 514,
                "captured_log_size_bytes_from_fstat": 13000,
                "concrete_unresolved_items": [],
                "excluded_lookahead": {
                    "bytes": 514,
                    "excluded": True,
                    "included_prefix_hash_updated": False,
                    "line": cutoff_line + 1,
                    "outer_type": "event_msg",
                    "payload_inspected": False,
                    "timestamp": "2026-11-12T21:57:36.081Z",
                },
                "log_path": "/tmp/synthetic-v7-session.jsonl",
                "outer_and_payload_type_counts": {},
                "prefix": {
                    "bytes": prefix_bytes,
                    "last_included_outer_timestamp": prefix_cutoff,
                    "line_count": cutoff_line,
                    "sha256": prefix_sha,
                    "start_offset_bytes": 0,
                },
                "record_bound_bytes": 67108864,
                "resolved_metadata_events": [],
                "unread_bytes_after_stop": 1,
            },
            "cutoff_basis": (
                "explicit root-supplied capture-manifest filesystem mtime; weak chronology "
                "metadata, not cryptographic chronology proof"
            ),
            "cutoff_utc": capture_cutoff,
            "extractor_current_sha256": "5" * 64,
            "limitations": common_limits,
            "runtime_capture_attestation": {
                "files_opened_by_this_script": 0,
                "read_mode": "read-only",
                "root_verified_state": "current_equals_pre_equals_post",
                "runtime_file_count": 14,
            },
        }
        scope_excluded = {
            "line": cutoff_line + 1,
            "timestamp_utc": "2026-11-12T21:57:36.081Z",
            "payload": "opaque; excluded from prefix evidence",
        }
        cutoff_character = "Weak filesystem mtime only; not cryptographic chronology."
        root_attestation = {
            "fork_mode": "fork_none",
            "steering_attested": [
                "public normative material",
                "neutral wire protocol",
                "task ownership",
                "explicit user Gate3 permanent consumption override",
                "auxiliary P1 snapshot contract",
            ],
            "not_supplied_attested": [
                "private gold",
                "production implementation or private production results",
                "results",
                "comparison answers",
                "other references",
                "proposed ETag formula",
                "chosen first-stage headline predicate",
            ],
            "evidence_character": (
                "Supplied trusted root attestation; inbound message semantics are "
                "encrypted/opaque and independently unverifiable. Eligibility depends on "
                "the attestation, not plaintext inspection."
            ),
        }
        record_limits = {
            "evidence": (
                "Unsigned local evidence and supplied root attestation; no OS-level completeness, "
                "log authenticity, or global absence claim is made."
            ),
            "chronology": (
                "The capture cutoff filesystem mtime is weak metadata, not cryptographic "
                "chronology; line 2062 remains excluded."
            ),
            "fast_tier": "Requested, not independently verified.",
            "contrary_evidence": (
                "Evidence of actual forbidden exposure overrides this bounded qualification."
            ),
        }
        bounded_audit = {
            "allowed_path_events": 78,
            "unknown_operations": 183,
            "sole_nonallowed_literal": {
                "operation": "WRITE",
                "path": "synthetic",
                "interpretation": "synthetic output",
            },
            "interpretation": (
                "Unknown metadata is not proof of a violation or a blanket clean claim."
            ),
        }
        not_claimed = [
            "source semantic correctness",
            "runtime or dependency behavior",
            "execution result",
            "semantic conformance or G-SPEC result",
            "M5.0 milestone completion",
            "publication readiness",
        ]
        normative_spec = {
            "path": "SPEC-v5.md",
            "sha256": SPEC_V5_SHA256,
            "use": _V7_FULL_SPEC_NORMATIVE_USE,
        }
        spec_raw = b"synthetic authorized spec excerpt\n"
        spec_path = write_bytes(input_dir / "SPEC-v5-sections-1-4.md", spec_raw)
        authorized_spec_input = {
            "path": spec_path,
            "sha256": sha256_bytes(spec_raw),
            "scope": _V7_FULL_SPEC_AUTHORIZED_SPEC_SCOPE,
        }
        note_path = write_bytes(
            native_dir / "bounded-fullspec-prefix-audit.executed.py.txt",
            b"synthetic archived extractor\n",
        )
        evidence = [
            {"path": capture_path, "raw_sha256": capture_raw_sha},
            {"path": descriptor_path, "raw_sha256": descriptor_sha},
            {
                "path": note_path,
                "raw_sha256": sha256_bytes(b"synthetic archived extractor\n"),
                "note": _V7_FULL_SPEC_EXECUTED_AUDIT_NOTE,
            },
            {"path": spec_path, "raw_sha256": sha256_bytes(spec_raw)},
        ]
    else:
        old_prefix = {"bytes": 7000, "line_count": 22, "sha256": "b" * 64}
        suffix = {
            "bytes": 5345,
            "last_included_outer_timestamp": prefix_cutoff,
            "line_count": 20,
            "sha256": "c" * 64,
        }
        prefix = {
            "bytes": old_prefix["bytes"] + suffix["bytes"],
            "line_count": old_prefix["line_count"] + suffix["line_count"],
            "sha256": prefix_sha,
        }
        audit = {
            "audit_kind": _V7_BOUNDED_CARD_AUDIT_KIND,
            "child_session": {
                "audited_prefix_binding": prefix,
                "excluded_lookahead": {
                    "bytes": 1,
                    "excluded": True,
                    "line": prefix["line_count"] + 1,
                    "payload_inspected": False,
                    "timestamp": "2026-11-12T21:57:36.081Z",
                },
                "id": session_id,
                "included_suffix": suffix,
                "log_path": "/tmp/synthetic-v7-session.jsonl",
                "old_prefix": old_prefix,
            },
            "concrete_unresolved_items": [],
            "cutoff": capture_cutoff,
            "cutoff_basis": (
                "captured-manifest filesystem timestamp; binds an audited prefix but is not "
                "cryptographic chronology proof"
            ),
            "declared_author_content_allowlist": ["synthetic public input"],
            "known_source_and_capture_hashes": {
                "author_self_audit_sha256": "6" * 64,
                "manifest_logical_sha256": capture_logical_sha,
                "manifest_raw_sha256": capture_raw_sha,
                "old_audit_sha256": "7" * 64,
                "schema_sha256": "8" * 64,
                "worker_sha256": "9" * 64,
            },
            "limitations": common_limits,
            "outer_and_payload_type_counts": {},
            "resolved_metadata_events": [],
        }
        scope_excluded = {
            "line": cutoff_line + 1,
            "payload": "opaque; excluded from the prefix and findings",
        }
        cutoff_character = "Filesystem mtime only; it is not cryptographic chronology."
        root_attestation = {
            "statement": (
                "Through capture, steering was limited to transport/gap protocol, public "
                "correction topics, and the public v7 release; no case IDs, comparison "
                "results, expected answers, private gold, or new ETag formulas were supplied."
            ),
            "evidence_character": (
                "Encrypted inbound-message semantics are independently unverifiable. "
                "This qualification relies on the trusted root attestation, not plaintext "
                "inspection."
            ),
        }
        record_limits = {
            "evidence": (
                "Unsigned local evidence. No OS-level completeness or global absence claim is "
                "made; post-cutoff activity is excluded."
            ),
            "chronology": (
                "The capture cutoff timestamp is filesystem mtime only, not cryptographic "
                "chronology."
            ),
            "contrary_evidence": (
                "Evidence of actual forbidden exposure overrides this bounded qualification."
            ),
        }
        bounded_audit = {
            "allowed_path_events": 17,
            "unique_paths": 6,
            "established_outside_allowed_paths": 0,
            "result": "No forbidden explicit read is established within the audited prefix.",
            "coverage_limits": {"unresolved_items_total": 100},
        }
        not_claimed = [
            "source semantic correctness",
            "runtime or dependency behavior",
            "execution result",
            "G-SPEC or semantic conformance",
            "M5.0 milestone completion",
            "publication readiness",
        ]
        normative_spec = {"path": "SPEC-v5.md", "sha256": SPEC_V5_SHA256}
        archive_path = write_bytes(
            native_dir / "card-v7-prefix-audit.executed.py.txt", b"synthetic archived extractor\n"
        )
        historical_path = write_bytes(
            root
            / "reports"
            / "v5"
            / "conformance"
            / "review"
            / "fresh-card-only-v2-v6-source-origin-adjudication-2026-11-12.json",
            b"synthetic historical scope note\n",
        )
        evidence = [
            {"path": capture_path, "raw_sha256": capture_raw_sha},
            {
                "path": archive_path,
                "raw_sha256": sha256_bytes(b"synthetic archived extractor\n"),
                "note": _V7_CARD_EXECUTED_AUDIT_NOTE,
            },
            {
                "path": historical_path,
                "raw_sha256": sha256_bytes(b"synthetic historical scope note\n"),
                "note": _V7_CARD_HISTORICAL_SCOPE_NOTE,
            },
        ]

    audit_filename = (
        "bounded-fullspec-prefix-audit.json"
        if evaluator_id == "full_spec_blind"
        else "card-v7-prefix-audit.json"
    )
    audit_path = native_dir / audit_filename
    audit_path_rel = write_bytes(audit_path, canonical_json_bytes(audit) + b"\n")
    audit_sha = sha256_bytes(canonical_json_bytes(audit) + b"\n")
    if evaluator_id == "agent_view_only":
        evidence.insert(1, {"path": audit_path_rel, "raw_sha256": audit_sha})
    else:
        evidence.append({"path": audit_path_rel, "raw_sha256": audit_sha})

    scope = {
        "capture_manifest_raw_sha256": capture_raw_sha,
        "capture_manifest_logical_sha256": capture_logical_sha,
        "capture_manifest_path": capture_path,
        "session_id": session_id,
        "prefix_bytes": prefix_bytes,
        "prefix_sha256": prefix_sha,
        "cutoff_line_inclusive": cutoff_line,
        "cutoff_timestamp_utc": prefix_cutoff,
        "capture_cutoff_timestamp_utc": capture_cutoff,
        "capture_cutoff_timestamp_character": cutoff_character,
        "excluded_lookahead": scope_excluded,
    }
    basis: dict[str, Any] = {
        "trusted_root_orchestration_attestation": root_attestation,
        "bounded_native_audit": bounded_audit,
    }
    if evaluator_id == "agent_view_only":
        basis["historical_prefix_binding"] = {
            "prefix_bytes": 7000,
            "prefix_sha256": "b" * 64,
            "cutoff_line": 22,
        }
    adjudication = {
        "record_type": "source_origin_adjudication",
        "record_version": 2,
        "status": "QUALIFIED_ELIGIBLE",
        "subject": {
            "author_origin": author_origin,
            "author_task": author_task,
            "source_profile": source_profile,
            "evaluator_id": evaluator_id,
        },
        "normative_spec": normative_spec,
        "scope": scope,
        "evidence": evidence,
        "independent_review": {
            "reviewers": [
                {"reviewer": "independent-review-a", "decision": "QUALIFIED_ELIGIBLE"},
                {"reviewer": "independent-review-b", "decision": "QUALIFIED_ELIGIBLE"},
            ],
            "agreement": "Synthetic reviewers independently qualified this bounded origin.",
        },
        "basis": basis,
        "limitations": record_limits,
        "not_claimed": not_claimed,
    }
    if evaluator_id == "full_spec_blind":
        adjudication["authorized_spec_input"] = authorized_spec_input
    adjudication_raw = canonical_json_bytes(adjudication) + b"\n"
    adjudication_path = (
        root / "reports" / "v5" / "conformance" / "review" / f"{evaluator_id}-v7-origin.json"
    )
    adjudication_path_rel = write_bytes(adjudication_path, adjudication_raw)
    qualification = {
        "kind": "author_origin",
        "adjudication": {
            "path": adjudication_path_rel,
            "sha256": sha256_bytes(adjudication_raw),
        },
        "origin_audit": {"path": audit_path_rel, "sha256": audit_sha},
    }
    return {
        "root": root,
        "evaluator_id": evaluator_id,
        "qualification": qualification,
        "adjudication": adjudication,
        "audit": audit,
        "audit_path": audit_path,
        "adjudication_path": adjudication_path,
        "capture_path": root / capture_path,
        "source_profile": source_profile,
        "author_origin": author_origin,
        "author_task": author_task,
        "capture_raw_sha": capture_raw_sha,
        "capture_logical_sha": capture_logical_sha,
        "capture_manifest_path": capture_path,
        "captured_source_files_count": len(source_files),
    }


def _rebind_shared_v7_fixture(fixture: dict[str, Any], *, audit: bool) -> None:
    if audit:
        audit_raw = canonical_json_bytes(fixture["audit"]) + b"\n"
        fixture["audit_path"].write_bytes(audit_raw)
        audit_ref = fixture["qualification"]["origin_audit"]
        audit_ref["sha256"] = sha256_bytes(audit_raw)
        audit_path = audit_ref["path"]
        for row in fixture["adjudication"]["evidence"]:
            if row.get("path") == audit_path:
                row["raw_sha256"] = audit_ref["sha256"]
    adjudication_raw = canonical_json_bytes(fixture["adjudication"]) + b"\n"
    fixture["adjudication_path"].write_bytes(adjudication_raw)
    fixture["qualification"]["adjudication"]["sha256"] = sha256_bytes(adjudication_raw)


def _run_shared_v7_qualification(
    fixture: dict[str, Any],
) -> tuple[bool, list[str], list[str], dict[str, Any]]:
    errors: list[str] = []
    gaps: list[str] = []
    provenance: dict[str, Any] = {}
    qualified = _validate_shared_qualification(
        fixture["evaluator_id"],
        fixture["qualification"],
        source_profile=fixture["source_profile"],
        author_origin=fixture["author_origin"],
        closure_sha="c" * 64,
        capture_raw_sha=fixture["capture_raw_sha"],
        capture_logical_sha=fixture["capture_logical_sha"],
        capture_manifest_path=fixture["capture_manifest_path"],
        captured_source_files_count=fixture["captured_source_files_count"],
        runtime_manifest={},
        repository_root=fixture["root"],
        errors=errors,
        gaps=gaps,
        provenance=provenance,
    )
    return qualified, errors, gaps, provenance


def _source_origin_chain_write_bytes(root: Path, relative_path: str, raw: bytes) -> dict[str, str]:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"path": relative_path, "sha256": sha256_bytes(raw)}


def _source_origin_chain_write_json(
    root: Path, relative_path: str, value: dict[str, Any]
) -> dict[str, str]:
    return _source_origin_chain_write_bytes(
        root, relative_path, canonical_json_bytes(value) + b"\n"
    )


def _source_origin_chain_fixture(
    root: Path,
    *,
    predecessor: bool = False,
    reconstruction: bool = False,
    optional_omission: bool = False,
) -> dict[str, Any]:
    """Build a fully synthetic, two-author chain over the existing v7 fixture."""
    evaluator_id = "full_spec_blind"
    old = _shared_v7_qualification_fixture(root, evaluator_id)
    slot = "full_spec"
    registered = _resolve_source_profile(slot, old["source_profile"])
    capture_path = old["capture_path"]
    source_rows: list[dict[str, str]] = []
    for index, relative in enumerate(registered.source_files):
        raw = f"synthetic captured component {index}: {relative}\n".encode()
        source_path = capture_path.parent / relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(raw)
        source_rows.append({"path": relative, "sha256": sha256_bytes(raw)})
    capture_unsigned = {
        "evaluator_id": slot,
        "source_profile": old["source_profile"],
        "author_origin": old["author_origin"],
        "source_files": source_rows,
    }
    logical_sha = sha256_bytes(canonical_json_bytes(capture_unsigned))
    capture_raw = canonical_json_bytes({**capture_unsigned, "manifest_sha256": logical_sha}) + b"\n"
    capture_path.write_bytes(capture_raw)
    capture_binding = old["audit"]["capture_binding"]
    capture_mtime_ns = capture_binding["manifest_filesystem_mtime_ns"]
    os.utime(capture_path, ns=(capture_mtime_ns, capture_mtime_ns))
    raw_sha = sha256_bytes(capture_raw)
    capture_ref = {
        "path": old["capture_manifest_path"],
        "sha256": raw_sha,
    }
    capture_binding.update(
        {
            "manifest_path": capture_ref["path"],
            "manifest_raw_sha256": raw_sha,
            "manifest_logical_sha256": logical_sha,
            "manifest_bytes": len(capture_raw),
        }
    )
    if "manifest_canonical_json_sha256_observed" in capture_binding:
        capture_binding["manifest_canonical_json_sha256_observed"] = logical_sha
    scope = old["adjudication"]["scope"]
    scope["capture_manifest_raw_sha256"] = raw_sha
    scope["capture_manifest_logical_sha256"] = logical_sha
    if "captured_source_files_count" in scope:
        scope["captured_source_files_count"] = len(source_rows)
    for row in old["adjudication"]["evidence"]:
        if row.get("path") == capture_ref["path"]:
            row["raw_sha256"] = raw_sha
    optional_rows: list[dict[str, str]] = []
    if optional_omission:
        optional_path = "reports/v5/conformance/review/old-self-audit-unavailable.json"
        optional_sha = "d" * 64
        old["adjudication"]["evidence"].append({"path": optional_path, "raw_sha256": optional_sha})
        optional_rows.append(
            {
                "path": optional_path,
                "old_sha256": optional_sha,
                "reason": "The historical optional bytes are unavailable in this synthetic case.",
            }
        )
    old["capture_raw_sha"] = raw_sha
    old["capture_logical_sha"] = logical_sha
    old["captured_source_files_count"] = len(source_rows)
    _rebind_shared_v7_fixture(old, audit=True)

    source_map = {
        "evaluator_id": evaluator_id,
        "source_profile": old["source_profile"],
        "author_origin": old["author_origin"],
        "files": source_rows,
    }
    capture = {
        "manifest": capture_ref,
        "logical_sha256": logical_sha,
        "source_map": source_map,
    }
    audit_ref = dict(old["qualification"]["origin_audit"])
    adjudication_ref = dict(old["qualification"]["adjudication"])
    extractor_evidence = next(
        row
        for row in old["adjudication"]["evidence"]
        if row.get("note") == _V7_FULL_SPEC_EXECUTED_AUDIT_NOTE
    )
    extractor_ref = {
        "path": extractor_evidence["path"],
        "sha256": extractor_evidence["raw_sha256"],
    }
    prior_prefix = old["audit"]["child_log_prefix"]["prefix"]
    author = {
        "task": old["author_task"],
        "session_id": old["audit"]["audit_identity"]["child_session_id"],
        "source_profile": old["source_profile"],
        "author_origin": old["author_origin"],
    }
    historical_prefix = {
        "session_id": author["session_id"],
        "start_offset_bytes": 0,
        "bytes": prior_prefix["bytes"],
        "line_count": prior_prefix["line_count"],
        "sha256": prior_prefix["sha256"],
        "last_included_utc": prior_prefix["last_included_outer_timestamp"],
    }
    nested_refs: set[tuple[str, str]] = {
        (adjudication_ref["path"], adjudication_ref["sha256"]),
        (audit_ref["path"], audit_ref["sha256"]),
        (extractor_ref["path"], extractor_ref["sha256"]),
        (capture_ref["path"], capture_ref["sha256"]),
    }
    baseline = {
        "adjudication": adjudication_ref,
        "audit": audit_ref,
        "executed_extractor": extractor_ref,
        "capture": capture,
        "author": author,
        "prefix": historical_prefix,
    }
    predecessor_record: dict[str, Any] | None = None
    if predecessor:
        extension = {
            "start_offset_bytes": historical_prefix["bytes"],
            "bytes": 10,
            "line_count": 1,
            "sha256": "c" * 64,
            "last_included_utc": "2026-11-12T21:36:14.248Z",
        }
        output_prefix = {
            **historical_prefix,
            "bytes": historical_prefix["bytes"] + extension["bytes"],
            "line_count": historical_prefix["line_count"] + extension["line_count"],
            "sha256": "e" * 64,
            "last_included_utc": extension["last_included_utc"],
        }
        predecessor_extractor = _source_origin_chain_write_bytes(
            root,
            "reports/v5/conformance/native_audit/synthetic-continuation-extractor.py",
            b"synthetic continuation extractor\n",
        )
        nested_refs.add((predecessor_extractor["path"], predecessor_extractor["sha256"]))
        cutoff_text = capture_binding["manifest_filesystem_mtime_utc"]
        report = {
            "audit_kind": "bounded fresh full-spec author-session source-origin metadata audit",
            "audit_identity": old["audit"]["audit_identity"],
            "cutoff_utc": cutoff_text,
            "cutoff_basis": (
                "explicit root-supplied capture-manifest filesystem mtime; weak chronology "
                "metadata, not cryptographic chronology proof"
            ),
            "approved_auditor_script_sha256": predecessor_extractor["sha256"],
            "extractor_current_sha256": predecessor_extractor["sha256"],
            "audit_predecessor": {
                "path": extractor_ref["path"],
                "sha256": extractor_ref["sha256"],
                "used_as_code_lineage_only": True,
            },
            "capture_binding": {
                "manifest_path": capture_ref["path"],
                "manifest_raw_sha256": capture_ref["sha256"],
                "manifest_logical_sha256": logical_sha,
                "manifest_canonical_json_sha256_observed": logical_sha,
                "logical_sha256_verification": (
                    "canonical_sorted_compact_ensure_ascii_false_allow_nan_false_"
                    "after_removing_manifest_sha256"
                ),
                "manifest_filesystem_mtime_utc": cutoff_text,
                "manifest_filesystem_mtime_ns": capture_mtime_ns,
                "mtime_precision_comparison": (
                    "cutoff matched after flooring filesystem nanoseconds to supplied microseconds"
                ),
                "manifest_bytes": len(capture_raw),
                "manifest_only_read": True,
                "referenced_paths_followed": False,
            },
            "capture_source_evidence": {
                "profile_file_count": len(source_rows),
                "manifest_only_read": True,
                "source_bytes_opened_by_auditor": 0,
            },
            "prior_qualified_prefix": {
                "session_id": author["session_id"],
                "bytes": historical_prefix["bytes"],
                "line_count": historical_prefix["line_count"],
                "sha256": historical_prefix["sha256"],
                "last_included_timestamp": historical_prefix["last_included_utc"],
                "historical_bytes_payload_decoded": False,
            },
            "child_log_extension": {
                "log_path": "/synthetic/opaque-session-log.jsonl",
                "qualified_prefix": {
                    "start_offset_bytes": 0,
                    "bytes": historical_prefix["bytes"],
                    "line_count": historical_prefix["line_count"],
                    "sha256": historical_prefix["sha256"],
                    "last_included_outer_timestamp": historical_prefix["last_included_utc"],
                    "payload_decoded": False,
                },
                "included_extension": {
                    "start_offset_bytes": extension["start_offset_bytes"],
                    "bytes": extension["bytes"],
                    "line_count": extension["line_count"],
                    "sha256": extension["sha256"],
                    "last_included_outer_timestamp": extension["last_included_utc"],
                },
                "combined_prefix": {
                    "bytes": output_prefix["bytes"],
                    "line_count": output_prefix["line_count"],
                    "sha256": output_prefix["sha256"],
                },
                "excluded_lookahead": None,
                "outer_and_payload_type_counts": {},
                "resolved_metadata_events": [],
                "concrete_unresolved_items": [],
                "captured_log_size_bytes_from_fstat": output_prefix["bytes"],
                "unread_bytes_after_stop": 0,
                "bytes_read_beyond_included_prefix": 0,
                "record_bound_bytes": 67_108_864,
            },
            "author_content_allowlist": [],
            "optional_corroboration_paths": [],
            "root_approved_public_supplement": {
                "path": "reports/v5/conformance/review/synthetic-public-supplement.json",
                "sha256": "f" * 64,
                "content_opened_by_auditor": False,
            },
            "limitations": ["Synthetic report metadata; no session bytes are present."],
        }
        predecessor_audit_ref = _source_origin_chain_write_json(
            root, "reports/v5/conformance/review/synthetic-continuation-audit.json", report
        )
        nested_refs.add((predecessor_audit_ref["path"], predecessor_audit_ref["sha256"]))
        predecessor_record = {
            "author": dict(author),
            "input_prefix": dict(historical_prefix),
            "extension": extension,
            "output_prefix": output_prefix,
            "audit": predecessor_audit_ref,
            "executed_extractor": predecessor_extractor,
            "output_capture": capture,
        }

    if reconstruction:
        component_rows: list[dict[str, Any]] = []
        for row in source_rows:
            component_ref = {
                "path": f"{Path(old['capture_manifest_path']).parent.as_posix()}/{row['path']}",
                "sha256": row["sha256"],
            }
            nested_refs.add((component_ref["path"], component_ref["sha256"]))
            component_rows.append(
                {"path": row["path"], "sha256": row["sha256"], "provenance": component_ref}
            )
        completeness_evidence = _source_origin_chain_write_bytes(
            root,
            "reports/v5/conformance/review/synthetic-completeness-evidence.bin",
            b"synthetic completeness evidence\n",
        )
        nested_refs.add((completeness_evidence["path"], completeness_evidence["sha256"]))
        inventory = {
            "format": "identitybench.v5.source-origin-reconstructed-inventory.v1",
            "record_version": 1,
            "reconstructed_at_utc": "2026-11-12T22:10:00Z",
            "current_capture": capture,
            "files": component_rows,
            "completeness_decision": {
                "decision": "PASS",
                "reviewer_task": "/root/completeness-reviewer",
                "route": {
                    "model": "gpt-6.1-sol",
                    "reasoning_effort": "xhigh",
                    "provider": "cpa",
                    "service_tier": "fast",
                },
                "evidence": completeness_evidence,
                "reviewed_at_utc": "2026-11-12T22:11:00Z",
            },
            "limitations": ["Synthetic current reconstruction; no historical time is inferred."],
        }
        inventory_ref = _source_origin_chain_write_json(
            root, "reports/v5/conformance/review/synthetic-reconstructed-inventory.json", inventory
        )
        nested_refs.add((inventory_ref["path"], inventory_ref["sha256"]))
        baseline["reconstructed_inventory"] = inventory_ref
    else:
        inventory_ref = None

    spawn_input_sha = sha256_bytes(canonical_json_bytes(source_map))
    successor_task = "/root/repair/full-spec-author-v1"
    successor_session = "synthetic-successor-session"
    successor = {
        "task": successor_task,
        "session_id": successor_session,
        "source_profile": old["source_profile"],
        "author_origin": old["author_origin"],
    }
    spawn = {
        "format": "identitybench.v5.author-spawn-evidence.v1",
        "record_version": 1,
        "task": successor_task,
        "session_id": successor_session,
        "parent_task": "/root",
        "fork_mode": "fork_none",
        "repository_root": str(root.resolve()),
        "input_source_map_sha256": spawn_input_sha,
        "spawned_at_utc": "2026-11-12T22:00:00Z",
    }
    spawn_ref = _source_origin_chain_write_json(
        root, "reports/v5/conformance/review/synthetic-successor-spawn.json", spawn
    )
    nested_refs.add((spawn_ref["path"], spawn_ref["sha256"]))
    successor_root = {
        "subject": {**successor, "evaluator_id": evaluator_id},
        "fork_mode": "fork_none",
        "input_source_map_sha256": spawn_input_sha,
        "issued_at_utc": "2026-11-12T22:00:01Z",
        "allowed_inputs": [],
        "not_supplied": sorted(
            {
                "private gold",
                "production implementation",
                "private production results",
                "results",
                "comparison answers",
                "other references",
                "proposed ETag formula",
                "chosen first-stage headline predicate",
                "oracle",
                "generator",
                "verifier",
                "baselines",
                "sealedoracles",
                "comparisons",
                "reference semantic source",
                "reference semantic results",
            }
        ),
        "statement": "Synthetic root attestation for the new author and source-map input.",
        "evidence_character": "Trusted test fixture metadata, not independent proof.",
        "limitations": ["Source isolation remains UNKNOWN and conditional."],
    }
    successor["spawn_evidence"] = spawn_ref
    successor["root_attestation"] = successor_root
    successor_prefix = {
        "session_id": successor_session,
        "start_offset_bytes": 0,
        "bytes": 700,
        "line_count": 3,
        "sha256": "b" * 64,
        "last_included_utc": "2026-11-12T21:59:00Z",
    }
    successor_extractor = _source_origin_chain_write_bytes(
        root,
        "reports/v5/conformance/native_audit/synthetic-successor-extractor.py",
        b"synthetic successor extractor\n",
    )
    nested_refs.add((successor_extractor["path"], successor_extractor["sha256"]))
    cutoff_record = {
        "timestamp_utc": capture_binding["manifest_filesystem_mtime_utc"],
        "basis": "filesystem_mtime_weak",
    }
    successor_report = {
        "format": "identitybench.v5.successor-source-origin-audit.v1",
        "record_version": 1,
        "evaluator_id": evaluator_id,
        "author": {
            key: successor[key] for key in ("task", "session_id", "source_profile", "author_origin")
        },
        "prefix": successor_prefix,
        "output_capture": capture,
        "cutoff": cutoff_record,
        "executed_extractor_sha256": successor_extractor["sha256"],
        "observed_path_events": [],
        "forbidden_exposure": "NONE_OBSERVED",
        "disclosed_unknowns": [],
    }
    successor_report_ref = _source_origin_chain_write_json(
        root, "reports/v5/conformance/review/synthetic-successor-audit.json", successor_report
    )
    nested_refs.add((successor_report_ref["path"], successor_report_ref["sha256"]))

    historical_subject = {
        "task": author["task"],
        "session_id": author["session_id"],
        "evaluator_id": evaluator_id,
        "source_profile": author["source_profile"],
        "author_origin": author["author_origin"],
    }
    handoff = predecessor_record["output_capture"] if predecessor_record else capture
    if reconstruction:
        handoff = inventory["current_capture"]
    basis_refs = {
        (adjudication_ref["path"], adjudication_ref["sha256"]),
        (audit_ref["path"], audit_ref["sha256"]),
        (extractor_ref["path"], extractor_ref["sha256"]),
        (capture_ref["path"], capture_ref["sha256"]),
        (handoff["manifest"]["path"], handoff["manifest"]["sha256"]),
    }
    if inventory_ref is not None:
        basis_refs.add((inventory_ref["path"], inventory_ref["sha256"]))
    baseline_adjudication = {
        "record_type": "source_origin_baseline_adjudication",
        "record_version": 1,
        "adjudicator": {
            "task": "/root/new-baseline-adjudicator",
            "route": {
                "model": "gpt-6.1-sol",
                "reasoning_effort": "xhigh",
                "provider": "cpa",
                "service_tier": "fast",
            },
            "reviewed_at_utc": "2026-11-12T22:12:00Z",
        },
        "decision": "QUALIFIED_ELIGIBLE",
        "core_bindings": {
            "audit": audit_ref,
            "executed_extractor": extractor_ref,
            "capture": capture,
            "author": author,
            "prefix": historical_prefix,
            "handoff": handoff,
        },
        "omitted_optional_corroboration": optional_rows,
        "root_baseline_attestation": {
            "subject": historical_subject,
            "source_map_sha256": sha256_bytes(canonical_json_bytes(handoff["source_map"])),
            "basis_artifacts": [
                {"path": path, "sha256": digest} for path, digest in sorted(basis_refs)
            ],
            "issued_at_utc": "2026-11-12T22:12:01Z",
            "statement": "Synthetic attestation binds the available historical core and handoff.",
            "evidence_character": "Trusted test fixture metadata; no historical time inferred.",
            "limitations": ["Optional corroboration is limited to the explicit omission list."],
        },
        "limitations": ["Synthetic baseline only; source isolation remains UNKNOWN."],
    }
    baseline_ref = _source_origin_chain_write_json(
        root,
        "reports/v5/conformance/review/synthetic-baseline-adjudication.json",
        baseline_adjudication,
    )
    nested_refs.add((baseline_ref["path"], baseline_ref["sha256"]))
    baseline["baseline_adjudication"] = baseline_ref

    chain: dict[str, Any] = {
        "format": "identitybench.v5.source-origin-chain.v1",
        "record_version": 1,
        "evaluator_id": evaluator_id,
        "historical_baseline": baseline,
        "successor_input": {"capture": handoff, "source_map": handoff["source_map"]},
        "successor_author": successor,
        "successor_audit": {
            "report": successor_report_ref,
            "executed_extractor": successor_extractor,
            "prefix": successor_prefix,
            "cutoff": cutoff_record,
            "disclosed_unknowns": [],
        },
        "successor_output": capture,
        "limitations": ["Synthetic chain; source isolation remains UNKNOWN."],
    }
    if predecessor_record is not None:
        chain["predecessor_continuation"] = predecessor_record
    chain_ref = _source_origin_chain_write_json(
        root, "reports/v5/conformance/review/synthetic-source-origin-chain.json", chain
    )
    review = {
        "format": "identitybench.v5.source-origin-chain-review.v1",
        "record_version": 1,
        "chain": chain_ref,
        "referenced_artifacts": [
            {"path": path, "sha256": digest} for path, digest in sorted(nested_refs)
        ],
        "checks": {
            "baseline_core": "PASS",
            "transitions": "PASS",
            "author_separation": "PASS",
            "artifact_hashes": "PASS",
            "forbidden_exposure": "PASS",
            "omissions": "PASS",
            "coverage_limits": "ACKNOWLEDGED_UNKNOWN",
        },
        "decision": "QUALIFIED_ELIGIBLE",
        "reviewers": [
            {
                "task": "/root/independent-chain-reviewer",
                "decision": "PASS",
                "route": {
                    "model": "gpt-6.1-sol",
                    "reasoning_effort": "xhigh",
                    "provider": "cpa",
                    "service_tier": "fast",
                },
                "reviewed_at_utc": "2026-11-12T22:13:00Z",
            }
        ],
        "limitations": ["Independent review is synthetic test metadata."],
    }
    review_ref = _source_origin_chain_write_json(
        root, "reports/v5/conformance/review/synthetic-source-origin-chain-review.json", review
    )
    qualification = {
        "kind": "author_origin_chain_v1",
        "chain": chain_ref,
        "chain_review": review_ref,
    }
    return {
        "root": root,
        "old": old,
        "chain": chain,
        "review": review,
        "qualification": qualification,
        "nested_refs": nested_refs,
        "chain_path": root / chain_ref["path"],
        "review_path": root / review_ref["path"],
        "chain_ref": chain_ref,
        "review_ref": review_ref,
        "source_profile": old["source_profile"],
        "author_origin": old["author_origin"],
        "capture_raw_sha": raw_sha,
        "capture_logical_sha": logical_sha,
        "capture_manifest_path": old["capture_manifest_path"],
        "captured_source_files_count": len(source_rows),
        "successor_report": successor_report,
        "successor_report_ref": successor_report_ref,
    }


def _source_origin_chain_variant_capture(
    fixture: dict[str, Any],
    label: str,
    *,
    wrong_slot: bool = False,
    edit_first_component: bool = False,
) -> tuple[dict[str, Any], str, str, str | None]:
    root = fixture["root"]
    input_capture = fixture["chain"]["successor_input"]["capture"]
    original_manifest_path = root / input_capture["manifest"]["path"]
    manifest = json.loads(original_manifest_path.read_bytes())
    source_root = original_manifest_path.parent
    variant_root = root / "reports" / "v5" / "conformance" / "shared_results" / "v1" / label
    rows: list[dict[str, str]] = []
    changed_path: str | None = None
    for index, row in enumerate(manifest["source_files"]):
        relative = row["path"]
        raw = (source_root / relative).read_bytes()
        if edit_first_component and index == 0:
            raw += b"synthetic new-author edit\n"
            changed_path = relative
        target = variant_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        rows.append({"path": relative, "sha256": sha256_bytes(raw)})
    unsigned = {
        key: value
        for key, value in manifest.items()
        if key not in {"manifest_sha256", "source_files"}
    }
    if wrong_slot:
        unsigned["evaluator_id"] = "full_spec_blind"
    unsigned["source_files"] = rows
    logical_sha = sha256_bytes(canonical_json_bytes(unsigned))
    raw = canonical_json_bytes({**unsigned, "manifest_sha256": logical_sha}) + b"\n"
    manifest_path = variant_root / "capture-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(raw)
    original_mtime_ns = original_manifest_path.stat().st_mtime_ns
    os.utime(manifest_path, ns=(original_mtime_ns, original_mtime_ns))
    ref = {
        "path": manifest_path.relative_to(root).as_posix(),
        "sha256": sha256_bytes(raw),
    }
    source_map = {
        **input_capture["source_map"],
        "files": rows,
    }
    capture = {"manifest": ref, "logical_sha256": logical_sha, "source_map": source_map}
    return capture, ref["sha256"], logical_sha, changed_path


def _bind_source_origin_chain_output_capture(
    fixture: dict[str, Any], capture: dict[str, Any], raw_sha: str, logical_sha: str
) -> str | None:
    fixture["chain"]["successor_output"] = capture
    fixture["successor_report"]["output_capture"] = capture
    report_ref = _source_origin_chain_write_json(
        fixture["root"], fixture["successor_report_ref"]["path"], fixture["successor_report"]
    )
    fixture["nested_refs"].discard(
        (fixture["successor_report_ref"]["path"], fixture["successor_report_ref"]["sha256"])
    )
    fixture["nested_refs"].add((report_ref["path"], report_ref["sha256"]))
    fixture["successor_report_ref"] = report_ref
    fixture["chain"]["successor_audit"]["report"] = report_ref
    fixture["nested_refs"].add((capture["manifest"]["path"], capture["manifest"]["sha256"]))
    fixture["capture_raw_sha"] = raw_sha
    fixture["capture_logical_sha"] = logical_sha
    fixture["capture_manifest_path"] = capture["manifest"]["path"]
    _rebind_source_origin_chain_fixture(fixture)
    return None


def _rebind_source_origin_chain_fixture(fixture: dict[str, Any]) -> None:
    chain_raw = canonical_json_bytes(fixture["chain"]) + b"\n"
    fixture["chain_path"].write_bytes(chain_raw)
    chain_ref = {"path": fixture["chain_ref"]["path"], "sha256": sha256_bytes(chain_raw)}
    fixture["chain_ref"] = chain_ref
    fixture["review"]["chain"] = chain_ref
    fixture["review"]["referenced_artifacts"] = [
        {"path": path, "sha256": digest} for path, digest in sorted(fixture["nested_refs"])
    ]
    review_raw = canonical_json_bytes(fixture["review"]) + b"\n"
    fixture["review_path"].write_bytes(review_raw)
    review_ref = {"path": fixture["review_ref"]["path"], "sha256": sha256_bytes(review_raw)}
    fixture["review_ref"] = review_ref
    fixture["qualification"] = {
        "kind": "author_origin_chain_v1",
        "chain": chain_ref,
        "chain_review": review_ref,
    }


def _set_predecessor_log_prefix_timestamp(
    fixture: dict[str, Any], timestamp: str
) -> None:
    predecessor = fixture["chain"]["predecessor_continuation"]
    old_ref = predecessor["audit"]
    path = fixture["root"] / old_ref["path"]
    report = json.loads(path.read_bytes())
    report["child_log_extension"]["qualified_prefix"][
        "last_included_outer_timestamp"
    ] = timestamp
    new_ref = _source_origin_chain_write_json(fixture["root"], old_ref["path"], report)
    fixture["nested_refs"].discard((old_ref["path"], old_ref["sha256"]))
    fixture["nested_refs"].add((new_ref["path"], new_ref["sha256"]))
    predecessor["audit"] = new_ref
    _rebind_source_origin_chain_fixture(fixture)


def _run_shared_origin_chain(
    fixture: dict[str, Any],
) -> tuple[bool, list[str], list[str], dict[str, Any]]:
    errors: list[str] = []
    gaps: list[str] = []
    provenance: dict[str, Any] = {}
    old = fixture["old"]
    result = _validate_shared_qualification(
        "full_spec_blind",
        fixture["qualification"],
        source_profile=fixture["source_profile"],
        author_origin=fixture["author_origin"],
        closure_sha="c" * 64,
        capture_raw_sha=fixture["capture_raw_sha"],
        capture_logical_sha=fixture["capture_logical_sha"],
        capture_manifest_path=fixture["capture_manifest_path"],
        captured_source_files_count=fixture["captured_source_files_count"],
        runtime_manifest={},
        repository_root=fixture["root"],
        errors=errors,
        gaps=gaps,
        provenance=provenance,
    )
    assert old["evaluator_id"] == "full_spec_blind"
    return result, errors, gaps, provenance


def test_shared_source_origin_chain_accepts_two_authors_without_predecessor(
    tmp_path: Path,
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path)

    qualified, errors, gaps, provenance = _run_shared_origin_chain(fixture)

    assert qualified, (*errors, *gaps)
    assert errors == []
    assert gaps == []
    assert provenance["qualification_status"] == "QUALIFIED_ELIGIBLE"
    assert provenance["source_isolation"] == "UNKNOWN"
    assert len(provenance["authors"]) == 2
    assert provenance["chain_sha256"] == fixture["chain_ref"]["sha256"]
    assert provenance["chain_review_sha256"] == fixture["review_ref"]["sha256"]
    assert provenance["current_capture_manifest_sha256"] == fixture["capture_raw_sha"]
    assert provenance["current_capture_logical_sha256"] == fixture["capture_logical_sha"]
    assert provenance["current_source_map_sha256"]
    assert provenance["input_source_map_sha256"]


def test_shared_source_origin_chain_rejects_wrong_capture_slot_and_accepts_bound_edits(
    tmp_path: Path,
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path / "wrong-slot")
    wrong_slot_capture, raw_sha, logical_sha, _changed = _source_origin_chain_variant_capture(
        fixture, "current-wrong-slot", wrong_slot=True
    )
    _bind_source_origin_chain_output_capture(fixture, wrong_slot_capture, raw_sha, logical_sha)

    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)

    assert not qualified
    assert any("slot/wire" in item for item in (*errors, *gaps))

    fixture = _source_origin_chain_fixture(tmp_path / "edited-source")
    edited_capture, raw_sha, logical_sha, changed_path = _source_origin_chain_variant_capture(
        fixture, "current-edited-source", edit_first_component=True
    )
    assert changed_path is not None
    _bind_source_origin_chain_output_capture(fixture, edited_capture, raw_sha, logical_sha)
    qualified_without_event, errors, gaps, _provenance = _run_shared_origin_chain(fixture)
    assert not qualified_without_event
    assert any("source edits lack allowed audit events" in item for item in (*errors, *gaps))

    evidence_sha = "8" * 64
    fixture["successor_report"]["observed_path_events"] = [
        {
            "classification": "ALLOWED",
            "path": changed_path,
            "evidence_sha256": evidence_sha,
        }
    ]
    old_report_ref = fixture["successor_report_ref"]
    report_ref = _source_origin_chain_write_json(
        fixture["root"], old_report_ref["path"], fixture["successor_report"]
    )
    fixture["nested_refs"].discard((old_report_ref["path"], old_report_ref["sha256"]))
    fixture["nested_refs"].add((report_ref["path"], report_ref["sha256"]))
    fixture["successor_report_ref"] = report_ref
    fixture["chain"]["successor_audit"]["report"] = report_ref
    _rebind_source_origin_chain_fixture(fixture)
    qualified_with_event, errors, gaps, _provenance = _run_shared_origin_chain(fixture)
    assert qualified_with_event, (*errors, *gaps)


def test_shared_source_origin_chain_rejects_unrepresented_input_transition(
    tmp_path: Path,
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path)
    variant, _raw_sha, _logical_sha, _changed = _source_origin_chain_variant_capture(
        fixture, "unrepresented-input", edit_first_component=True
    )
    fixture["chain"]["successor_input"] = {
        "capture": variant,
        "source_map": variant["source_map"],
    }
    old_spawn_ref = fixture["chain"]["successor_author"]["spawn_evidence"]
    spawn_path = fixture["root"] / old_spawn_ref["path"]
    spawn = json.loads(spawn_path.read_bytes())
    input_map_sha = sha256_bytes(canonical_json_bytes(variant["source_map"]))
    spawn["input_source_map_sha256"] = input_map_sha
    spawn_ref = _source_origin_chain_write_json(fixture["root"], old_spawn_ref["path"], spawn)
    fixture["nested_refs"].discard((old_spawn_ref["path"], old_spawn_ref["sha256"]))
    fixture["nested_refs"].add((spawn_ref["path"], spawn_ref["sha256"]))
    fixture["chain"]["successor_author"]["spawn_evidence"] = spawn_ref
    fixture["chain"]["successor_author"]["root_attestation"]["input_source_map_sha256"] = (
        input_map_sha
    )
    fixture["nested_refs"].add((variant["manifest"]["path"], variant["manifest"]["sha256"]))
    _rebind_source_origin_chain_fixture(fixture)

    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)

    assert not qualified
    assert any("immediate handoff" in item for item in (*errors, *gaps))


def test_shared_source_origin_chain_rejects_predecessor_prefix_misbinding(
    tmp_path: Path,
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path, predecessor=True)
    fixture["chain"]["predecessor_continuation"]["input_prefix"]["session_id"] = "different-session"
    _rebind_source_origin_chain_fixture(fixture)

    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)

    assert not qualified
    assert any("predecessor input prefix" in item for item in (*errors, *gaps)), (
        *errors,
        *gaps,
    )


def test_shared_source_origin_chain_accepts_equivalent_utc_prefix_spellings(
    tmp_path: Path,
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path, predecessor=True)
    predecessor = fixture["chain"]["predecessor_continuation"]
    timestamp = predecessor["input_prefix"]["last_included_utc"]
    assert timestamp.endswith("Z")

    _set_predecessor_log_prefix_timestamp(fixture, f"{timestamp[:-1]}+00:00")

    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)

    assert qualified, (*errors, *gaps)


@pytest.mark.parametrize(
    ("prefix_field", "prefix_timestamp", "observed_timestamp"),
    [
        (
            "prior_qualified_prefix",
            "2026-11-12T21:35:14.248Z",
            "2026-11-12T21:35:14.248+00:00",
        ),
        (
            "qualified_prefix_binding",
            "2026-11-12T21:08:53.755Z",
            "2026-11-12T21:08:53.755+00:00",
        ),
    ],
)
def test_same_utc_instant_accepts_both_audit_prefix_report_shapes(
    prefix_field: str, prefix_timestamp: str, observed_timestamp: str
) -> None:
    report = {
        prefix_field: {"last_included_timestamp": prefix_timestamp},
        "child_log_extension": {
            "qualified_prefix": {
                "last_included_outer_timestamp": observed_timestamp,
            }
        },
    }
    prefix_report = report.get(
        "prior_qualified_prefix", report.get("qualified_prefix_binding")
    )
    observed_prefix = report["child_log_extension"]["qualified_prefix"]

    assert isinstance(prefix_report, dict)
    assert validation_module._same_utc_instant(
        observed_prefix["last_included_outer_timestamp"],
        prefix_report["last_included_timestamp"],
    )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-11-12T21:35:15.248+00:00",
        "2026-11-12T21:35:14.248+01:00",
        "2026-11-12T21:35:14.248000001+00:00",
    ],
)
def test_shared_source_origin_chain_rejects_different_or_unsupported_utc_prefix_times(
    tmp_path: Path, timestamp: str
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path, predecessor=True)
    _set_predecessor_log_prefix_timestamp(fixture, timestamp)

    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)

    assert not qualified
    assert any("does not bind exact prior prefix" in item for item in (*errors, *gaps)), (
        *errors,
        *gaps,
    )


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("2026-11-12T21:35:14.248Z", "2026-11-12T21:35:14.248+00:00", True),
        ("2026-11-12T21:35:14.248000Z", "2026-11-12T21:35:14.248+00:00", True),
        ("2026-11-12T21:35:14.248000001Z", "2026-11-12T21:35:14.248+00:00", False),
        ("2026-11-12T21:35:14.248Z", "2026-11-12T21:35:14.248+01:00", False),
        ("not-a-timestamp", "2026-11-12T21:35:14.248Z", False),
    ],
)
def test_same_utc_instant_compares_full_fractional_precision(
    left: str, right: str, expected: bool
) -> None:
    assert validation_module._same_utc_instant(left, right) is expected


def test_shared_source_origin_chain_binds_historical_prefix_to_author_and_audit(
    tmp_path: Path,
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path)
    chain = fixture["chain"]
    historical = chain["historical_baseline"]
    unrelated_session = "unrelated-historical-session"
    historical["prefix"]["session_id"] = unrelated_session

    baseline_ref = historical["baseline_adjudication"]
    baseline_path = fixture["root"] / baseline_ref["path"]
    baseline = json.loads(baseline_path.read_bytes())
    baseline["core_bindings"]["prefix"]["session_id"] = unrelated_session
    rebound_baseline_ref = _source_origin_chain_write_json(
        fixture["root"], baseline_ref["path"], baseline
    )
    fixture["nested_refs"].discard((baseline_ref["path"], baseline_ref["sha256"]))
    fixture["nested_refs"].add(
        (rebound_baseline_ref["path"], rebound_baseline_ref["sha256"])
    )
    historical["baseline_adjudication"] = rebound_baseline_ref
    _rebind_source_origin_chain_fixture(fixture)

    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)

    assert not qualified
    assert any(
        "historical prefix session" in item or "historical audit claims" in item
        for item in (*errors, *gaps)
    ), (*errors, *gaps)


@pytest.mark.parametrize("mutation", ["session", "extractor"])
def test_shared_source_origin_chain_rejects_predecessor_report_or_archive_misbinding(
    tmp_path: Path, mutation: str
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path, predecessor=True)
    old_ref = fixture["chain"]["predecessor_continuation"]["audit"]
    report_path = fixture["root"] / old_ref["path"]
    report = json.loads(report_path.read_bytes())
    if mutation == "session":
        report["audit_identity"]["child_session_id"] = "different-session"
    else:
        report["approved_auditor_script_sha256"] = "0" * 64
    new_ref = _source_origin_chain_write_json(fixture["root"], old_ref["path"], report)
    fixture["nested_refs"].discard((old_ref["path"], old_ref["sha256"]))
    fixture["nested_refs"].add((new_ref["path"], new_ref["sha256"]))
    fixture["chain"]["predecessor_continuation"]["audit"] = new_ref
    _rebind_source_origin_chain_fixture(fixture)

    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)

    assert not qualified
    assert errors or gaps


@pytest.mark.parametrize("mutation", ["missing", "stale", "symlink"])
def test_shared_source_origin_chain_rejects_missing_stale_or_symlinked_capture_source(
    tmp_path: Path, mutation: str
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path)
    capture_path = (
        fixture["root"] / fixture["chain"]["historical_baseline"]["capture"]["manifest"]["path"]
    )
    capture_value = json.loads(capture_path.read_bytes())
    relative = capture_value["source_files"][0]["path"]
    source_path = capture_path.parent / relative
    if mutation == "missing":
        source_path.unlink()
    elif mutation == "stale":
        source_path.write_bytes(b"stale source snapshot\n")
    else:
        outside = tmp_path / "source-target.bin"
        outside.write_bytes(source_path.read_bytes())
        source_path.unlink()
        source_path.symlink_to(outside)

    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)

    assert not qualified
    assert errors or gaps
    if mutation == "symlink":
        assert any("symlink" in item for item in (*errors, *gaps))


def test_shared_source_origin_chain_rejects_source_only_descriptor_and_wrong_types(
    tmp_path: Path,
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path)
    fixture["chain"]["record_version"] = True
    _rebind_source_origin_chain_fixture(fixture)
    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)
    assert not qualified
    assert any("version" in item for item in (*errors, *gaps))

    errors = []
    gaps = []
    source_only = _validate_shared_qualification(
        "full_spec_blind",
        {"kind": "source_only_capture_not_established", "descriptor": "synthetic"},
        source_profile=fixture["source_profile"],
        author_origin=fixture["author_origin"],
        closure_sha="c" * 64,
        capture_raw_sha=fixture["capture_raw_sha"],
        capture_logical_sha=fixture["capture_logical_sha"],
        capture_manifest_path=fixture["capture_manifest_path"],
        captured_source_files_count=fixture["captured_source_files_count"],
        runtime_manifest={},
        repository_root=fixture["root"],
        errors=errors,
        gaps=gaps,
        provenance={},
    )
    assert not source_only
    assert any("unsupported shape" in item for item in (*errors, *gaps))


def test_shared_source_origin_chain_preserves_disclosed_unknown_coverage_limits(
    tmp_path: Path,
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path)
    unknowns = [
        {
            "code": "bounded_parser_coverage",
            "evidence_sha256": None,
            "reason": "A supported operation family remains outside this metadata audit.",
        },
        {
            "code": "bounded_parser_coverage",
            "evidence_sha256": "a" * 64,
            "reason": "A supported operation family remains outside this metadata audit.",
        },
    ]
    fixture["successor_report"]["disclosed_unknowns"] = unknowns
    fixture["chain"]["successor_audit"]["disclosed_unknowns"] = unknowns
    old_report_ref = fixture["successor_report_ref"]
    report_ref = _source_origin_chain_write_json(
        fixture["root"], old_report_ref["path"], fixture["successor_report"]
    )
    fixture["nested_refs"].discard((old_report_ref["path"], old_report_ref["sha256"]))
    fixture["nested_refs"].add((report_ref["path"], report_ref["sha256"]))
    fixture["successor_report_ref"] = report_ref
    fixture["chain"]["successor_audit"]["report"] = report_ref
    _rebind_source_origin_chain_fixture(fixture)

    qualified, errors, gaps, provenance = _run_shared_origin_chain(fixture)

    assert qualified, (*errors, *gaps)
    assert provenance["source_isolation"] == "UNKNOWN"
    assert provenance["disclosed_unknowns"] == unknowns


def test_shared_source_origin_chain_malformed_historical_author_fails_closed(
    tmp_path: Path,
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path)
    fixture["chain"]["historical_baseline"]["author"]["task"] = []
    _rebind_source_origin_chain_fixture(fixture)

    qualified, errors, gaps, provenance = _run_shared_origin_chain(fixture)

    assert not qualified
    assert errors or gaps
    assert provenance == {}


def test_shared_source_origin_chain_cannot_omit_available_optional_corroboration(
    tmp_path: Path,
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path, optional_omission=True)
    old = fixture["old"]
    omission = fixture["chain"]["historical_baseline"]["baseline_adjudication"]["path"]
    baseline_path = fixture["root"] / omission
    baseline = json.loads(baseline_path.read_bytes())
    old_path = baseline["omitted_optional_corroboration"][0]["path"]
    old_bytes = b"now available optional corroboration\n"
    old_sha = sha256_bytes(old_bytes)
    _source_origin_chain_write_bytes(fixture["root"], old_path, old_bytes)
    old_row = next(row for row in old["adjudication"]["evidence"] if row["path"] == old_path)
    old_row["raw_sha256"] = old_sha
    _rebind_shared_v7_fixture(old, audit=False)
    historical_ref = dict(old["qualification"]["adjudication"])
    fixture["chain"]["historical_baseline"]["adjudication"] = historical_ref
    fixture["nested_refs"].discard(
        (
            fixture["chain"]["historical_baseline"]["adjudication"]["path"],
            next(
                digest for path, digest in fixture["nested_refs"] if path == historical_ref["path"]
            ),
        )
    )
    fixture["nested_refs"].add((historical_ref["path"], historical_ref["sha256"]))
    baseline["omitted_optional_corroboration"][0]["old_sha256"] = old_sha
    for row in baseline["root_baseline_attestation"]["basis_artifacts"]:
        if row["path"] == historical_ref["path"]:
            row["sha256"] = historical_ref["sha256"]
    baseline_ref = _source_origin_chain_write_json(fixture["root"], omission, baseline)
    old_baseline_ref = fixture["chain"]["historical_baseline"]["baseline_adjudication"]
    fixture["nested_refs"].discard((old_baseline_ref["path"], old_baseline_ref["sha256"]))
    fixture["nested_refs"].add((baseline_ref["path"], baseline_ref["sha256"]))
    fixture["chain"]["historical_baseline"]["baseline_adjudication"] = baseline_ref
    _rebind_source_origin_chain_fixture(fixture)

    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)

    assert not qualified
    assert any("bytes remain available" in item for item in (*errors, *gaps))


@pytest.mark.parametrize(
    ("predecessor", "reconstruction", "optional_omission"),
    [(True, False, False), (False, True, False), (False, False, True)],
)
def test_shared_source_origin_chain_accepts_predecessor_and_recovered_baselines(
    tmp_path: Path, predecessor: bool, reconstruction: bool, optional_omission: bool
) -> None:
    fixture = _source_origin_chain_fixture(
        tmp_path,
        predecessor=predecessor,
        reconstruction=reconstruction,
        optional_omission=optional_omission,
    )

    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)

    assert qualified, (*errors, *gaps)
    assert errors == []
    assert gaps == []


def test_shared_source_origin_chain_rejects_malformed_stale_and_rebound_evidence(
    tmp_path: Path,
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path)
    fixture["chain"]["unexpected"] = "extra"
    _rebind_source_origin_chain_fixture(fixture)

    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)

    assert not qualified
    assert any("fields differ" in item for item in (*errors, *gaps))


@pytest.mark.parametrize("bad_raw", [b'{"x": NaN}', b'{"x": 1, "x": 2}'])
def test_shared_source_origin_chain_uses_strict_json_for_nan_and_duplicate_keys(
    tmp_path: Path, bad_raw: bytes
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path)
    fixture["chain_path"].write_bytes(bad_raw)
    fixture["chain_ref"]["sha256"] = sha256_bytes(bad_raw)
    fixture["review"]["chain"] = dict(fixture["chain_ref"])
    review_raw = canonical_json_bytes(fixture["review"]) + b"\n"
    fixture["review_path"].write_bytes(review_raw)
    fixture["review_ref"]["sha256"] = sha256_bytes(review_raw)
    fixture["qualification"] = {
        "kind": "author_origin_chain_v1",
        "chain": fixture["chain_ref"],
        "chain_review": fixture["review_ref"],
    }

    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)

    assert not qualified
    assert any("malformed" in item for item in (*errors, *gaps))


@pytest.mark.parametrize(
    "tamper",
    [
        "successor_task_overlap",
        "successor_session_overlap",
        "wrong_wire_id",
        "prefix_session",
        "prefix_offset",
        "reviewer_overlap",
        "review_mismatch",
        "forbidden_event",
        "wrong_event_type",
        "wrong_extractor_binding",
        "stale_report_ref",
        "missing_report",
    ],
)
def test_shared_source_origin_chain_rejects_transition_and_review_tampering(
    tmp_path: Path, tamper: str
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path)
    if tamper == "successor_task_overlap":
        fixture["chain"]["successor_author"]["task"] = fixture["chain"]["historical_baseline"][
            "author"
        ]["task"]
    elif tamper == "successor_session_overlap":
        fixture["chain"]["successor_author"]["session_id"] = fixture["chain"][
            "historical_baseline"
        ]["author"]["session_id"]
    elif tamper == "wrong_wire_id":
        fixture["chain"]["successor_input"]["source_map"]["evaluator_id"] = "full_spec"
    elif tamper == "prefix_session":
        fixture["chain"]["successor_audit"]["prefix"]["session_id"] = "other-session"
    elif tamper == "prefix_offset":
        fixture["chain"]["successor_audit"]["prefix"]["start_offset_bytes"] = 4
    elif tamper == "reviewer_overlap":
        fixture["review"]["reviewers"][0]["task"] = fixture["chain"]["successor_author"]["task"]
    elif tamper == "review_mismatch":
        fixture["review"]["checks"]["transitions"] = "UNKNOWN"
    elif tamper == "forbidden_event":
        event = {
            "classification": "FORBIDDEN",
            "path": "forbidden-source",
            "evidence_sha256": "9" * 64,
        }
        fixture["successor_report"]["observed_path_events"] = [event]
        fixture["successor_report"]["forbidden_exposure"] = "CONFIRMED"
        report_ref = _source_origin_chain_write_json(
            fixture["root"],
            fixture["successor_report_ref"]["path"],
            fixture["successor_report"],
        )
        fixture["successor_report_ref"] = report_ref
        fixture["chain"]["successor_audit"]["report"] = report_ref
        fixture["nested_refs"].add((report_ref["path"], report_ref["sha256"]))
    elif tamper in {"wrong_event_type", "wrong_extractor_binding"}:
        if tamper == "wrong_event_type":
            fixture["successor_report"]["observed_path_events"] = [
                {
                    "classification": [],
                    "path": "synthetic-path",
                    "evidence_sha256": "9" * 64,
                }
            ]
        else:
            fixture["successor_report"]["executed_extractor_sha256"] = "0" * 64
        old_report_ref = fixture["successor_report_ref"]
        report_ref = _source_origin_chain_write_json(
            fixture["root"], old_report_ref["path"], fixture["successor_report"]
        )
        fixture["nested_refs"].discard((old_report_ref["path"], old_report_ref["sha256"]))
        fixture["nested_refs"].add((report_ref["path"], report_ref["sha256"]))
        fixture["successor_report_ref"] = report_ref
        fixture["chain"]["successor_audit"]["report"] = report_ref
    elif tamper == "stale_report_ref":
        fixture["chain"]["successor_audit"]["report"]["sha256"] = "0" * 64
    elif tamper == "missing_report":
        missing = "reports/v5/conformance/review/not-present.json"
        fixture["chain"]["successor_audit"]["report"]["path"] = missing
        fixture["nested_refs"].add(
            (missing, fixture["chain"]["successor_audit"]["report"]["sha256"])
        )
    _rebind_source_origin_chain_fixture(fixture)

    qualified, errors, gaps, _provenance = _run_shared_origin_chain(fixture)

    assert not qualified
    assert errors or gaps


def test_shared_source_origin_chain_rejects_symlink_and_escaping_chain_refs(
    tmp_path: Path,
) -> None:
    fixture = _source_origin_chain_fixture(tmp_path)
    link = tmp_path / "reports/v5/conformance/review/source-origin-link.json"
    link.symlink_to(fixture["chain_path"])
    fixture["qualification"]["chain"] = {
        "path": link.relative_to(tmp_path).as_posix(),
        "sha256": fixture["chain_ref"]["sha256"],
    }
    qualified_link, link_errors, link_gaps, _provenance = _run_shared_origin_chain(fixture)
    assert not qualified_link
    assert any("symlink" in item for item in (*link_errors, *link_gaps))

    fixture = _source_origin_chain_fixture(tmp_path / "escape")
    fixture["qualification"]["chain"] = {
        "path": "reports/v5/conformance/review/../../outside.json",
        "sha256": fixture["chain_ref"]["sha256"],
    }
    qualified_escape, escape_errors, escape_gaps, _provenance = _run_shared_origin_chain(fixture)
    assert not qualified_escape
    assert any("unsafe" in item or "path" in item for item in (*escape_errors, *escape_gaps))


@pytest.mark.parametrize("evaluator_id", ["full_spec_blind", "agent_view_only"])
def test_shared_v7_origin_is_conditionally_qualified_with_limits_retained(
    tmp_path: Path, evaluator_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _shared_v7_qualification_fixture(tmp_path / evaluator_id, evaluator_id)
    if evaluator_id == "agent_view_only":
        assert _V7_CARD_EXECUTED_AUDIT_SHA256 == (
            "0c479172206a4612c3d3c9970d521fe5825620830618fbc339c14a5d9c8a758c"
        )
        archive_row = next(
            row
            for row in fixture["adjudication"]["evidence"]
            if row.get("note") == _V7_CARD_EXECUTED_AUDIT_NOTE
        )
        monkeypatch.setattr(
            validation_module,
            "_V7_CARD_EXECUTED_AUDIT_SHA256",
            archive_row["raw_sha256"],
        )

    qualified, errors, gaps, provenance = _run_shared_v7_qualification(fixture)

    assert qualified
    assert errors == []
    assert gaps == []
    assert provenance["provenance_mode"] == (
        "conditional_root_attestation_with_bounded_metadata_audit"
    )
    assert provenance["qualification_status"] == "QUALIFIED_ELIGIBLE"
    assert provenance["source_isolation"] == "UNKNOWN"
    assert provenance["not_claimed"]
    assert any(
        "G-SPEC" in value or "semantic conformance" in value for value in provenance["not_claimed"]
    )
    assert "parent_thread_id" in provenance["unknown_raw_claims"]
    assert "history_mode" in provenance["unknown_raw_claims"]
    assert "operating_system_read_completeness" in provenance["unknown_raw_claims"]
    assert provenance["limitations"]["native_audit"]
    if evaluator_id == "agent_view_only":
        assert "raw_author_task_in_native_audit" in provenance["unknown_raw_claims"]
        assert "root_spawn_evidence" in provenance["unknown_raw_claims"]


@pytest.mark.parametrize(
    ("tamper", "expected_text"),
    [
        ("remove", "does not uniquely bind the executed audit archive"),
        ("formatted_py", "does not uniquely bind the executed audit archive"),
        ("replace_and_rehash", "differs from the reviewed archive hash"),
    ],
)
def test_shared_v7_card_origin_requires_reviewed_executed_audit_archive(
    tmp_path: Path,
    tamper: str,
    expected_text: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _shared_v7_qualification_fixture(tmp_path, "agent_view_only")
    archive_row = next(
        row
        for row in fixture["adjudication"]["evidence"]
        if row.get("note") == _V7_CARD_EXECUTED_AUDIT_NOTE
    )
    reviewed_synthetic_sha = archive_row["raw_sha256"]
    monkeypatch.setattr(
        validation_module,
        "_V7_CARD_EXECUTED_AUDIT_SHA256",
        reviewed_synthetic_sha,
    )

    if tamper == "remove":
        fixture["adjudication"]["evidence"].remove(archive_row)
    elif tamper == "formatted_py":
        formatted_path = "reports/v5/conformance/native_audit/card-v7-prefix-audit.executed.py"
        formatted_raw = b"synthetic later formatted extractor\n"
        (fixture["root"] / formatted_path).write_bytes(formatted_raw)
        archive_row["path"] = formatted_path
        archive_row["raw_sha256"] = sha256_bytes(formatted_raw)
    else:
        archive_path = fixture["root"] / archive_row["path"]
        replacement_raw = b"synthetic replacement extractor\n"
        archive_path.write_bytes(replacement_raw)
        archive_row["raw_sha256"] = sha256_bytes(replacement_raw)

    _rebind_shared_v7_fixture(fixture, audit=False)
    qualified, errors, gaps, _provenance = _run_shared_v7_qualification(fixture)

    assert not qualified
    assert any(expected_text in item for item in (*errors, *gaps))


@pytest.mark.parametrize(
    ("tamper", "expected_text"),
    [
        ("identity", "audit identity differs"),
        ("prefix", "scope differs from the native capture claim"),
        ("capture", "capture identity or cutoff differs"),
        ("executed_archive", "executed audit archive differs from the approved script hash"),
        ("evidence", "digest mismatch"),
        ("reviewer", "author reviewed its own source-origin record"),
        ("root_attestation", "lacks explicit root attestation"),
        ("normative_use", "exact v7 SPEC note"),
        ("authorized_spec", "authorized SPEC input is not hash-bound"),
        ("capture_file", "digest mismatch"),
        ("evidence_note", "unsupported note"),
    ],
)
def test_shared_v7_origin_rejects_mandatory_binding_tampering(
    tmp_path: Path, tamper: str, expected_text: str
) -> None:
    fixture = _shared_v7_qualification_fixture(tmp_path, "full_spec_blind")
    if tamper == "identity":
        fixture["audit"]["audit_identity"]["task"] = "/root/other (fresh fork_none)"
        _rebind_shared_v7_fixture(fixture, audit=True)
    elif tamper == "prefix":
        fixture["audit"]["child_log_prefix"]["prefix"]["sha256"] = "d" * 64
        _rebind_shared_v7_fixture(fixture, audit=True)
    elif tamper == "capture":
        fixture["audit"]["capture_binding"]["manifest_raw_sha256"] = "e" * 64
        _rebind_shared_v7_fixture(fixture, audit=True)
    elif tamper == "executed_archive":
        fixture["audit"]["approved_auditor_script_sha256"] = "e" * 64
        _rebind_shared_v7_fixture(fixture, audit=True)
    elif tamper == "evidence":
        audit_path = fixture["qualification"]["origin_audit"]["path"]
        next(row for row in fixture["adjudication"]["evidence"] if row["path"] == audit_path)[
            "raw_sha256"
        ] = "0" * 64
        _rebind_shared_v7_fixture(fixture, audit=False)
    elif tamper == "reviewer":
        fixture["adjudication"]["independent_review"]["reviewers"][0]["reviewer"] = fixture[
            "author_task"
        ]
        _rebind_shared_v7_fixture(fixture, audit=False)
    elif tamper == "root_attestation":
        fixture["adjudication"]["basis"].pop("trusted_root_orchestration_attestation")
        _rebind_shared_v7_fixture(fixture, audit=False)
    elif tamper == "normative_use":
        fixture["adjudication"]["normative_spec"]["use"] = "different use"
        _rebind_shared_v7_fixture(fixture, audit=False)
    elif tamper == "authorized_spec":
        fixture["adjudication"]["authorized_spec_input"]["sha256"] = "f" * 64
        _rebind_shared_v7_fixture(fixture, audit=False)
    elif tamper == "capture_file":
        fixture["capture_path"].write_bytes(b"tampered capture bytes")
    elif tamper == "evidence_note":
        note_row = next(row for row in fixture["adjudication"]["evidence"] if "note" in row)
        note_row["note"] = "unreviewed note"
        _rebind_shared_v7_fixture(fixture, audit=False)

    qualified, errors, gaps, _provenance = _run_shared_v7_qualification(fixture)

    assert not qualified
    assert any(expected_text in item for item in (*errors, *gaps))


def test_shared_native_origin_claims_reject_unknown_audit_layout(tmp_path: Path) -> None:
    errors: list[str] = []
    gaps: list[str] = []

    claims = _shared_native_origin_claims(
        {"audit_kind": "future-unreviewed-layout", "scope": {"author_path": "/root/author"}},
        "full_spec_blind",
        tmp_path,
        errors,
        gaps,
    )

    assert claims is None
    assert not errors
    assert any("unsupported source-origin layout" in gap for gap in gaps)


def _rebind_qualification_timestamp(
    root: Path, qualification: dict[str, Any], timestamp: str
) -> None:
    audit_ref = qualification["origin_audit"]
    audit_path = root / audit_ref["path"]
    audit = json.loads(audit_path.read_bytes())
    if "native_provenance" in audit:
        audit["native_provenance"]["child_prefix_capture"]["cutoff_timestamp_utc"] = timestamp
    else:
        audit["child_session"]["prefix"]["last_record"]["timestamp"] = timestamp
    audit_raw = json.dumps(audit, sort_keys=True, separators=(",", ":")).encode()
    audit_path.write_bytes(audit_raw)
    audit_ref["sha256"] = hashlib.sha256(audit_raw).hexdigest()

    adjudication_ref = qualification["adjudication"]
    adjudication_path = root / adjudication_ref["path"]
    adjudication = json.loads(adjudication_path.read_bytes())
    adjudication["scope"]["cutoff_timestamp_utc"] = timestamp
    for row in adjudication["evidence"]:
        if row.get("path") == audit_ref["path"]:
            row["raw_sha256"] = audit_ref["sha256"]
    adjudication_raw = json.dumps(adjudication, sort_keys=True, separators=(",", ":")).encode()
    adjudication_path.write_bytes(adjudication_raw)
    adjudication_ref["sha256"] = hashlib.sha256(adjudication_raw).hexdigest()


@pytest.mark.parametrize("prefix_sha256", ["a" * 64, "d" * 64])
def test_shared_origin_qualification_accepts_a_bound_fresh_author(
    tmp_path: Path, prefix_sha256: str
) -> None:
    qualification, _capture_raw, raw_sha, logical_sha = _shared_qualification_fixture(
        tmp_path,
        audit_prefix_sha256=prefix_sha256,
        adjudication_prefix_sha256=prefix_sha256,
    )
    errors: list[str] = []
    gaps: list[str] = []

    qualified = _validate_shared_qualification(
        "full_spec_blind",
        qualification,
        source_profile="fresh_full_spec_v3",
        author_origin="fresh_full_spec_v2",
        closure_sha="c" * 64,
        capture_raw_sha=raw_sha,
        capture_logical_sha=logical_sha,
        runtime_manifest={},
        repository_root=tmp_path,
        errors=errors,
        gaps=gaps,
    )

    assert qualified
    assert errors == []
    assert gaps == []


@pytest.mark.parametrize(
    ("reviewer", "history_mode", "adjudication_prefix_sha256"),
    [
        ("/root/fresh_full_spec_v2", "paginated", "a" * 64),
        ("independent-review-task", "", "a" * 64),
        ("independent-review-task", "paginated", "d" * 64),
    ],
)
def test_shared_origin_qualification_rejects_unqualified_provenance(
    tmp_path: Path,
    reviewer: str,
    history_mode: str,
    adjudication_prefix_sha256: str,
) -> None:
    qualification, _capture_raw, raw_sha, logical_sha = _shared_qualification_fixture(
        tmp_path,
        reviewer=reviewer,
        history_mode=history_mode,
        adjudication_prefix_sha256=adjudication_prefix_sha256,
    )
    errors: list[str] = []
    gaps: list[str] = []

    qualified = _validate_shared_qualification(
        "full_spec_blind",
        qualification,
        source_profile="fresh_full_spec_v3",
        author_origin="fresh_full_spec_v2",
        closure_sha="c" * 64,
        capture_raw_sha=raw_sha,
        capture_logical_sha=logical_sha,
        runtime_manifest={},
        repository_root=tmp_path,
        errors=errors,
        gaps=gaps,
    )

    assert not qualified
    assert errors or gaps


def test_shared_origin_qualification_accepts_card_native_audit_layout(
    tmp_path: Path,
) -> None:
    qualification, _capture, raw_sha, logical_sha = _shared_qualification_fixture(
        tmp_path,
        evaluator_id="agent_view_only",
        history_mode="paginated",
    )
    errors: list[str] = []
    gaps: list[str] = []

    qualified = _validate_shared_qualification(
        "agent_view_only",
        qualification,
        source_profile="fresh_card_only_v2",
        author_origin="fresh_independent_card_only_author_v2",
        closure_sha="c" * 64,
        capture_raw_sha=raw_sha,
        capture_logical_sha=logical_sha,
        runtime_manifest={},
        repository_root=tmp_path,
        errors=errors,
        gaps=gaps,
    )

    assert qualified
    assert errors == []
    assert gaps == []


def _copy_card_v2_qualification_evidence(
    source_repo: Path,
    target_repo: Path,
    adjudication: dict[str, Any],
    qualification: dict[str, Any],
) -> None:
    paths = {"SPEC-v5.md", adjudication["scope"]["capture_manifest_path"]}
    paths.update(row["path"] for row in adjudication["evidence"])
    paths.update(
        qualification[key]["path"] for key in ("adjudication", "origin_audit")
    )
    source_root = source_repo.resolve()
    for relative_text in paths:
        relative = Path(relative_text)
        assert not relative.is_absolute()
        assert ".." not in relative.parts
        source = (source_root / relative).resolve(strict=True)
        assert source.is_relative_to(source_root)
        target = target_repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _run_card_v2_qualification(repo: Path) -> tuple[bool, list[str], list[str]]:
    repo = repo.resolve()
    adjudication_path = repo / (
        "reports/v5/conformance/review/"
        "fresh-card-only-v2-v6-source-origin-adjudication-2026-11-12.json"
    )
    adjudication_raw = adjudication_path.read_bytes()
    adjudication = json.loads(adjudication_raw)
    evidence = adjudication["evidence"]
    audit_evidence = next(
        row for row in evidence if row["path"].endswith("fresh-card-only-v2-ready-audit.json")
    )
    capture_path = adjudication["scope"]["capture_manifest_path"]
    capture_raw = (repo / capture_path).read_bytes()
    qualification = {
        "kind": "author_origin",
        "adjudication": {
            "path": adjudication_path.relative_to(repo).as_posix(),
            "sha256": hashlib.sha256(adjudication_raw).hexdigest(),
        },
        "origin_audit": {
            "path": audit_evidence["path"],
            "sha256": audit_evidence["raw_sha256"],
        },
    }
    errors: list[str] = []
    gaps: list[str] = []

    qualified = _validate_shared_qualification(
        "agent_view_only",
        qualification,
        source_profile="fresh_card_only_v2",
        author_origin="fresh_independent_card_only_author_v2",
        closure_sha="c" * 64,
        capture_raw_sha=hashlib.sha256(capture_raw).hexdigest(),
        capture_logical_sha=adjudication["scope"]["capture_manifest_logical_sha256"],
        capture_manifest_path=capture_path,
        captured_source_files_count=adjudication["scope"]["captured_source_files_count"],
        runtime_manifest={},
        repository_root=repo,
        errors=errors,
        gaps=gaps,
    )

    return qualified, errors, gaps


def test_shared_origin_qualification_binds_reviewed_card_v2_artifact(
    tmp_path: Path,
) -> None:
    repo = Path.cwd()
    adjudication_path = repo / (
        "reports/v5/conformance/review/"
        "fresh-card-only-v2-v6-source-origin-adjudication-2026-11-12.json"
    )
    adjudication = json.loads(adjudication_path.read_bytes())
    audit_row = next(
        row
        for row in adjudication["evidence"]
        if row["path"].endswith("fresh-card-only-v2-ready-audit.json")
    )
    audit = json.loads((repo / audit_row["path"]).read_bytes())
    session = audit["child_session"]["session_meta"]
    recorded_origin_root = Path(session["cwd"]).resolve()

    qualified, errors, gaps = _run_card_v2_qualification(repo)
    if repo.resolve() == recorded_origin_root:
        assert qualified
        assert errors == []
        assert gaps == []
    else:
        assert not qualified
        assert errors == ["agent_view_only card-only session cwd differs from repository"]
        assert gaps == [
            "agent_view_only card-only native audit lacks a matched root spawn binding"
        ]

    qualification = {
        "kind": "author_origin",
        "adjudication": {
            "path": adjudication_path.relative_to(repo).as_posix(),
            "sha256": hashlib.sha256(adjudication_path.read_bytes()).hexdigest(),
        },
        "origin_audit": {
            "path": audit_row["path"],
            "sha256": audit_row["raw_sha256"],
        },
    }
    relocated_repo = tmp_path / "relocated-repository"
    _copy_card_v2_qualification_evidence(repo, relocated_repo, adjudication, qualification)
    relocated_qualified, relocated_errors, relocated_gaps = _run_card_v2_qualification(
        relocated_repo
    )

    assert not relocated_qualified
    assert relocated_errors == [
        "agent_view_only card-only session cwd differs from repository"
    ]
    assert relocated_gaps == [
        "agent_view_only card-only native audit lacks a matched root spawn binding"
    ]


def test_shared_origin_qualification_fails_exact_current_ineligible_full_spec_capture() -> None:
    repo = Path.cwd()
    adjudication_path = repo / (
        "reports/v5/conformance/review/"
        "fresh-full-spec-v2-v6-source-origin-ineligible-2026-11-12.json"
    )
    adjudication_raw = adjudication_path.read_bytes()
    adjudication = json.loads(adjudication_raw)
    evidence = adjudication["evidence"]
    audit_evidence = next(
        row for row in evidence if row["path"].endswith("fresh-full-spec-v2-v6-suffix-audit.json")
    )
    capture_path = f"{adjudication['scope']['capture_path']}/capture-manifest.json"
    capture_raw = (repo / capture_path).read_bytes()
    qualification = {
        "kind": "author_origin",
        "adjudication": {
            "path": adjudication_path.relative_to(repo).as_posix(),
            "sha256": hashlib.sha256(adjudication_raw).hexdigest(),
        },
        "origin_audit": {
            "path": audit_evidence["path"],
            "sha256": audit_evidence["raw_sha256"],
        },
    }
    errors: list[str] = []
    gaps: list[str] = []

    qualified = _validate_shared_qualification(
        "full_spec_blind",
        qualification,
        source_profile="fresh_full_spec_v3",
        author_origin="fresh_full_spec_v2",
        closure_sha="c" * 64,
        capture_raw_sha=hashlib.sha256(capture_raw).hexdigest(),
        capture_logical_sha=adjudication["scope"]["capture_manifest_logical_sha256"],
        capture_manifest_path=capture_path,
        captured_source_files_count=15,
        runtime_manifest={},
        repository_root=repo,
        errors=errors,
        gaps=gaps,
    )

    assert not qualified
    assert any("explicitly INELIGIBLE" in error for error in errors)
    assert any("§§1–4-only author allowlist" in error for error in errors)
    assert gaps == []


@pytest.mark.parametrize(
    ("record_version", "record_status"),
    [(1, "QUALIFIED_ELIGIBLE"), (2, "INELIGIBLE")],
)
def test_shared_origin_qualification_does_not_promote_historical_or_ineligible_records(
    tmp_path: Path, record_version: int, record_status: str
) -> None:
    qualification, _capture, raw_sha, logical_sha = _shared_qualification_fixture(
        tmp_path,
        record_version=record_version,
        record_status=record_status,
    )
    errors: list[str] = []
    gaps: list[str] = []

    qualified = _validate_shared_qualification(
        "full_spec_blind",
        qualification,
        source_profile="fresh_full_spec_v3",
        author_origin="fresh_full_spec_v2",
        closure_sha="c" * 64,
        capture_raw_sha=raw_sha,
        capture_logical_sha=logical_sha,
        runtime_manifest={},
        repository_root=tmp_path,
        errors=errors,
        gaps=gaps,
    )

    assert not qualified
    assert not errors
    assert gaps


def test_shared_origin_qualification_rejects_malformed_utc_cutoff_even_when_rebound(
    tmp_path: Path,
) -> None:
    qualification, _capture, raw_sha, logical_sha = _shared_qualification_fixture(tmp_path)
    _rebind_qualification_timestamp(tmp_path, qualification, "not-a-date")
    errors: list[str] = []
    gaps: list[str] = []

    qualified = _validate_shared_qualification(
        "full_spec_blind",
        qualification,
        source_profile="fresh_full_spec_v3",
        author_origin="fresh_full_spec_v2",
        closure_sha="c" * 64,
        capture_raw_sha=raw_sha,
        capture_logical_sha=logical_sha,
        runtime_manifest={},
        repository_root=tmp_path,
        errors=errors,
        gaps=gaps,
    )

    assert not qualified
    assert any("malformed" in gap for gap in gaps)


def test_requested_initial_text_budget_is_separate_from_card_budget(
    artifacts: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from identitybench.v5 import policy_card, runner

    episode = artifacts.episodes[0]
    monkeypatch.setattr(policy_card, "count_policy_card_tokens", lambda _card: 1)
    monkeypatch.setattr(runner, "render_v5_system_prompt", lambda _config: "system")
    monkeypatch.setattr(
        runner,
        "render_v5_episode_opening",
        lambda _episode: " ".join(f"word{index}" for index in range(4_000)),
    )

    assert policy_card.count_policy_card_tokens(episode.policy_card) <= 3_200
    assert _user_requested_initial_text_tokens(episode) > 3_200


@pytest.mark.parametrize("wave_execution_mode", ["COUPLED", "ORACLE_PHASE_RESET"])
def test_offline_baseline_binds_diagnostics_only_to_host_runtime(
    artifacts: Any, monkeypatch: pytest.MonkeyPatch, wave_execution_mode: str
) -> None:
    from identitybench.v5 import baselines, runner

    episode = artifacts.episodes[0]
    oracle = artifacts.oracles[0]
    config = runner.RunConfigV5(wave_execution_mode=wave_execution_mode)
    factory = object()
    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(
        runner,
        "make_v5_diagnostic_runtime_factory",
        lambda supplied_oracle: factory if supplied_oracle is oracle else None,
    )

    async def fake_run(
        baseline_id: str,
        supplied_episode: Any,
        initial_world: Any,
        **kwargs: Any,
    ) -> str:
        calls.append(
            {
                "baseline_id": baseline_id,
                "episode": supplied_episode,
                "initial_world": initial_world,
                "kwargs": kwargs,
            }
        )
        return "execution"

    monkeypatch.setattr(baselines, "run_v5_baseline_on_episode", fake_run)

    result = _run_baseline_with_host_diagnostics("noop_team", episode, oracle, config)

    assert result == "execution"
    assert len(calls) == 1
    assert calls[0]["kwargs"]["runtime_factory"] is factory
    expected_reset = (
        oracle.omniscient_s2_gold if wave_execution_mode == "ORACLE_PHASE_RESET" else None
    )
    assert calls[0]["kwargs"]["phase2_reset_state"] is expected_reset
    assert "oracle" not in calls[0]["kwargs"]


def test_safety_witness_coverage_requires_all_mutation_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import identitybench.v5.verifier as verifier
    from identitybench.v5.schemas import HRSourceMappingMutationV5
    from identitybench.v5.validation import _validate_safety_witnesses

    pairs = ((False, False), (False, True), (True, False), (True, True))
    gold = HRSourceMappingMutationV5(
        mutation_kind="HR_SOURCE_MAPPING_MUTATION",
        target_entity_id="hrmap_probe",
        if_match=("hrmap_probe@0123456789abcdef:1",),
        cite=("HR-AUTH-V5-01:ev_00001",),
    ).model_dump(mode="json")
    evaluator_pairs = iter(pairs)
    monkeypatch.setattr(
        verifier,
        "safety_partition",
        lambda _submitted, _gold, _context: next(evaluator_pairs),
    )
    reference = {
        "safety_witnesses": [
            {
                "witness_id": f"hr-only-{index}",
                "gold": gold,
                "submission": gold,
                "target": {
                    "mutation_kind": "HR_SOURCE_MAPPING_MUTATION",
                    "target_entity_id": "hrmap_probe",
                },
                "stage2_valid": True,
                "expected_overgrant": overgrant,
                "expected_availability_breach": availability,
                "context": {},
            }
            for index, (overgrant, availability) in enumerate(pairs)
        ]
    }
    errors: list[str] = []
    gaps: list[str] = []

    validated = _validate_safety_witnesses(reference, "test", errors, gaps)

    assert len(validated) == 4
    assert errors == []
    assert any("miss mutation kinds" in gap for gap in gaps)
    assert not any("all four predicate outcomes" in gap for gap in gaps)


def test_blind_gate_refuses_sensitive_proof_paths_before_read(
    artifacts: Any, tmp_path: Path
) -> None:
    sensitive_path = tmp_path / ".env"
    sensitive_path.write_text("DO NOT READ", encoding="utf-8")

    result = _blind_gate(sensitive_path, artifacts)

    assert result.status is GateStatusV5.FAIL
    assert "sensitive" in result.errors[0]


def test_blind_gate_rejects_outside_receipt_before_read_and_accepts_public_root(
    artifacts: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "src" / "synthetic.json"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"{}")
    approved = tmp_path / "reports" / "v5" / "conformance" / "synthetic.json"
    approved.parent.mkdir(parents=True)
    approved.write_bytes(b"{}")
    read_paths: list[Path] = []
    original_read_bytes = Path.read_bytes

    def track_read(path: Path) -> bytes:
        read_paths.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", track_read)
    rejected = _blind_gate(outside, artifacts, repository_root=tmp_path)
    assert rejected.status is GateStatusV5.FAIL
    assert outside not in read_paths

    relative_escape = Path("reports/v5/conformance/../../src/synthetic.json")
    rejected_relative = _blind_gate(relative_escape, artifacts, repository_root=tmp_path)
    assert rejected_relative.status is GateStatusV5.FAIL
    assert outside not in read_paths

    outside_alias = approved.parent / "safe-alias.json"
    outside_alias.symlink_to(outside)
    read_paths.clear()
    rejected_alias = _blind_gate(outside_alias, artifacts, repository_root=tmp_path)
    assert rejected_alias.status is GateStatusV5.FAIL
    assert outside_alias.resolve() not in {path.resolve() for path in read_paths}

    read_paths.clear()
    _blind_gate(approved, artifacts, repository_root=tmp_path)
    assert approved.resolve() in {path.resolve() for path in read_paths}


def test_blind_gate_refuses_symlink_to_sensitive_proof_before_read(
    artifacts: Any, tmp_path: Path
) -> None:
    sensitive_path = tmp_path / ".env"
    sensitive_path.write_text("DO NOT READ", encoding="utf-8")
    proof_link = tmp_path / "reports" / "v5" / "conformance" / "receipt.json"
    proof_link.parent.mkdir(parents=True, exist_ok=True)
    proof_link.symlink_to(sensitive_path)

    result = _blind_gate(proof_link, artifacts, repository_root=tmp_path)

    assert result.status is GateStatusV5.FAIL
    assert "resolves to a sensitive path" in result.errors[0]


def test_provenance_input_refuses_symlink_to_sensitive_path(tmp_path: Path) -> None:
    sensitive_path = tmp_path / ".env"
    sensitive_path.write_text("DO NOT READ", encoding="utf-8")
    input_link = tmp_path / "rendered-card.txt"
    input_link.symlink_to(sensitive_path)

    with pytest.raises(V5ArtifactError, match="resolves to a sensitive path"):
        _read_provenance_input(tmp_path, input_link.name)


def test_dataset_loader_rejects_sensitive_symlink_before_read(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "dataset"
    dataset_dir.mkdir()
    (dataset_dir / "SEAL.json").write_text("{}", encoding="utf-8")
    (dataset_dir / "episodes.jsonl").write_text("{}\n", encoding="utf-8")
    sensitive_path = tmp_path / ".env"
    sensitive_path.write_text("DO NOT READ", encoding="utf-8")
    (dataset_dir / "oracles.jsonl").symlink_to(sensitive_path)

    with pytest.raises(ValueError, match="sensitive location"):
        load_v5_dataset(dataset_dir)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("spec_version", "SPEC-v4.1", "spec_version"),
        ("generator_params", {"suite": "v5_lite", "seed": 20261111}, "generator_params"),
        (
            "generator_params",
            {"suite": "v5_dev", "seed": 20261111, "extra": "unexpected"},
            "exactly suite and seed",
        ),
    ],
)
def test_dataset_loader_requires_normalized_seal_provenance(
    artifacts: Any, field: str, value: Any, message: str
) -> None:
    seal_path = artifacts.dataset_dir / "SEAL.json"
    seal = json.loads(seal_path.read_text(encoding="utf-8"))
    seal[field] = value
    seal_path.write_text(json.dumps(seal, sort_keys=True), encoding="utf-8")

    with pytest.raises(V5ArtifactError, match=message):
        load_v5_dataset(artifacts.dataset_dir)


def test_strict_jsonl_loader_reparses_sealed_mutations_as_tagged_models(artifacts: Any) -> None:
    mutations = (
        *artifacts.oracles[0].gold_p1_mutations,
        *artifacts.oracles[0].gold_p2_mutations,
    )

    assert mutations
    assert all(isinstance(mutation, StrictV5Model) for mutation in mutations)


def test_obs_dump_all_cost_includes_all_fresh_partial_authoritative_slices(
    artifacts: Any,
) -> None:
    for episode, oracle in zip(artifacts.episodes, artifacts.oracles, strict=True):
        assert _validate_obs_witness(episode, oracle) == ()


def test_blind_gate_rejects_stale_receipt_and_unverified_zero_hashes(
    artifacts: Any, tmp_path: Path
) -> None:
    receipt = _proof_bundle(artifacts, tmp_path)
    parsed = json.loads(receipt.read_text(encoding="utf-8"))
    parsed["dataset_seal_sha256"] = "0" * 64
    parsed["references"]["full_spec_blind"]["artifact_sha256"] = "0" * 64
    receipt.write_text(json.dumps(parsed, sort_keys=True), encoding="utf-8")

    result = _blind_gate(receipt, artifacts, repository_root=tmp_path)

    assert result.status is GateStatusV5.FAIL
    assert any("dataset_seal_sha256" in error for error in result.errors)
    assert any("digest mismatch" in error for error in result.errors)


def test_blind_gate_recomputes_references_but_stays_unknown_without_provenance(
    artifacts: Any, tmp_path: Path
) -> None:
    receipt = _proof_bundle(artifacts, tmp_path)

    result = _blind_gate(receipt, artifacts, repository_root=tmp_path)

    assert result.status is GateStatusV5.UNKNOWN
    assert len(result.evidence["verified_references"]) == 2
    assert result.evidence["reference_evidence"]["agent_view_only"][
        "policy_card_anchor_map"
    ] == len(_expected_policy_card_anchor_map())
    assert any("review provenance is unavailable" in error for error in result.errors)


def test_shared_expected_coverage_separates_rule_ids_from_anchor_labels() -> None:
    errors: list[str] = []
    gaps: list[str] = []

    coverage = _shared_expected_coverage([], {}, {}, errors, gaps)
    schema_rule_ids = frozenset(get_args(RuleIdV5))
    anchor_labels = frozenset(_expected_policy_card_anchor_map())

    assert coverage is not None
    assert len(schema_rule_ids) == 26
    assert len(anchor_labels) == 39
    assert coverage.rules == schema_rule_ids
    assert coverage.anchor_labels == anchor_labels


@pytest.mark.parametrize("change", ("missing", "mislabeled"))
def test_blind_gate_rejects_missing_or_mislabeled_policy_card_anchor_map(
    artifacts: Any, tmp_path: Path, change: str
) -> None:
    receipt = _proof_bundle(artifacts, tmp_path)
    reference_path = receipt.parent / "agent_view_reference.json"
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    if change == "missing":
        reference.pop("policy_card_anchor_map")
    else:
        reference["policy_card_anchor_map"]["CONN-V5-01"] = ["HR-AUTH-V5-01"]
    reference_path.write_text(json.dumps(reference, sort_keys=True), encoding="utf-8")
    receipt = bind_v5_reference_receipt(artifacts.dataset_dir, conformance_dir=receipt.parent)

    result = _blind_gate(receipt, artifacts, repository_root=tmp_path)

    assert result.status is GateStatusV5.FAIL
    assert any("policy_card_anchor_map" in error for error in result.errors)


def test_blind_gate_fails_when_reference_mutation_differs_from_sealed_gold(
    artifacts: Any, tmp_path: Path
) -> None:
    receipt = _proof_bundle(artifacts, tmp_path)
    reference_path = receipt.parent / "full_spec_reference.json"
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    mutation = reference["phase_expectations"][0]["mutations"][0]
    semantic_field = next(iter(mutation["semantic_fields"]))
    mutation["semantic_fields"][semantic_field] = "tampered reference output"
    reference_path.write_text(json.dumps(reference, sort_keys=True), encoding="utf-8")
    receipt = bind_v5_reference_receipt(artifacts.dataset_dir, conformance_dir=receipt.parent)

    result = _blind_gate(receipt, artifacts, repository_root=tmp_path)

    assert result.status is GateStatusV5.FAIL
    assert any("semantic field" in error for error in result.errors)


def test_reference_receipt_binds_current_inputs_without_claiming_isolation(
    artifacts: Any, tmp_path: Path
) -> None:
    conformance_dir = tmp_path / "conformance" / "v5_dev"
    episodes_sha = hashlib.sha256(artifacts.episodes_bytes).hexdigest()
    for name, scope in (
        ("full_spec_reference.json", "full_spec_blind"),
        ("agent_view_reference.json", "agent_view_only"),
    ):
        (conformance_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (conformance_dir / name).write_text(
            json.dumps(
                {
                    "schema_version": "identitybench.v5.reference-expectations.v1",
                    "proof_scope": scope,
                    "suite_id": "v5_dev",
                    "episodes_sha256": episodes_sha,
                    "phase_expectations": [
                        {"episode_id": artifacts.episodes[0].episode_id, "phase_index": 1}
                    ],
                }
            ),
            encoding="utf-8",
        )

    receipt_path = bind_v5_reference_receipt(
        artifacts.dataset_dir,
        conformance_dir=conformance_dir,
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert receipt["suite_id"] == "v5_dev"
    assert receipt["seed"] == artifacts.seal["seed"]
    assert receipt["spec_sha256"] == (
        "6bc0adfa60b827b8babbdc17713f2b5e2909a8898ccd0242e0806180ab786e43"
    )
    assert receipt["dataset_seal_sha256"] == hashlib.sha256(artifacts.seal_bytes).hexdigest()
    assert receipt["source_sha256"] == _blind_source_identity()
    assert "isolation" not in receipt
    assert receipt["references"]["full_spec_blind"]["artifact_path"] == "full_spec_reference.json"


def test_reference_receipt_rejects_stale_expectation_bytes(artifacts: Any, tmp_path: Path) -> None:
    conformance_dir = tmp_path / "conformance" / "v5_dev"
    conformance_dir.mkdir(parents=True)
    stale = {
        "schema_version": "identitybench.v5.reference-expectations.v1",
        "proof_scope": "full_spec_blind",
        "suite_id": "v5_dev",
        "episodes_sha256": "0" * 64,
        "phase_expectations": [{"episode_id": artifacts.episodes[0].episode_id, "phase_index": 1}],
    }
    (conformance_dir / "full_spec_reference.json").write_text(json.dumps(stale))
    (conformance_dir / "agent_view_reference.json").write_text(
        json.dumps({**stale, "proof_scope": "agent_view_only"})
    )

    with pytest.raises(ValueError, match="stale or bound to different episodes"):
        bind_v5_reference_receipt(artifacts.dataset_dir, conformance_dir=conformance_dir)


def _review_provenance_fixture(
    artifacts: Any, directory: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    input_path = directory / "public-input.json"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text('{"public":true}\n', encoding="utf-8")
    input_sha = hashlib.sha256(input_path.read_bytes()).hexdigest()
    refs: dict[str, dict[str, str]] = {}
    scopes: dict[str, dict[str, Any]] = {}
    forbidden = [
        "src/identitybench/v5/oracle.py",
        "src/identitybench/v5/oracle_p2.py",
        "src/identitybench/v5/oracle_requests.py",
        "src/identitybench/v5/generator.py",
        "src/identitybench/v5/verifier.py",
        "src/identitybench/v5/baselines.py",
        "datasets/v5_*/oracles.jsonl",
    ]
    root = Path(__file__).resolve().parents[2]
    authored_source_paths = {
        "full_spec_blind": root / "tests" / "conformance" / "v5" / "test_conformance_v5.py",
        "agent_view_only": root / "tests" / "conformance" / "v5" / "agent_view_reference.py",
    }
    packet_paths = _write_agent_view_packet_for_test(artifacts, directory / "agent-view")
    packet_path = packet_paths["episodes"]
    for scope, filename in (
        ("full_spec_blind", "full_spec_reference.json"),
        ("agent_view_only", "agent_view_reference.json"),
    ):
        reference_path = directory / filename
        reference_path.write_text("{}\n", encoding="utf-8")
        reference_sha = hashlib.sha256(reference_path.read_bytes()).hexdigest()
        refs[scope] = {"artifact_path": filename, "artifact_sha256": reference_sha}
        author_task = f"/root/{scope}"
        authored_path = authored_source_paths[scope]
        authored_id = f"{scope}-reference-source"
        authored_sha = _blind_source_identity()[authored_path.relative_to(root).as_posix()]
        if scope == "agent_view_only":
            allowed_inputs = [
                {
                    "input_id": "current-packet",
                    "usage": "reference",
                    "artifact_kind": "sanitized_episode_packet",
                    "path": str(packet_path),
                    "sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest(),
                    "origin": "derived_sanitized_packet",
                    "producer": "test packet builder",
                    "producer_source_sha256": {
                        "tests/v5/test_validation.py": hashlib.sha256(
                            Path(__file__).read_bytes()
                        ).hexdigest()
                    },
                },
                {
                    "input_id": "current-card",
                    "usage": "reference",
                    "artifact_kind": "rendered_policy_card",
                    "path": str(packet_paths["card"]),
                    "sha256": hashlib.sha256(packet_paths["card"].read_bytes()).hexdigest(),
                    "origin": "derived_sanitized_packet",
                    "producer": "test packet builder",
                    "producer_source_sha256": {
                        "tests/v5/test_validation.py": hashlib.sha256(
                            Path(__file__).read_bytes()
                        ).hexdigest()
                    },
                },
            ]
            audit = [
                {
                    "input_id": input_id,
                    "path": str(path),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "tool": "read sanitized test input",
                    "access": "read",
                    "phase": "derive",
                    "origin": "derived_sanitized_packet",
                }
                for input_id, path in (
                    ("current-packet", packet_path),
                    ("current-card", packet_paths["card"]),
                )
            ]
            schema_path = root / "src" / "identitybench" / "v5" / "schemas.py"
            schema_sha = hashlib.sha256(schema_path.read_bytes()).hexdigest()
            allowed_inputs.append(
                {
                    "input_id": "schema-source",
                    "usage": "reference",
                    "artifact_kind": "schema_source",
                    "path": str(schema_path),
                    "sha256": schema_sha,
                    "origin": "preexisting_allowed",
                }
            )
            audit.append(
                {
                    "path": str(schema_path),
                    "input_id": "schema-source",
                    "sha256": schema_sha,
                    "tool": "read allowed schema",
                    "access": "read",
                    "phase": "derive",
                    "origin": "preexisting_allowed",
                }
            )
            skill_path = root / ".agents" / "skills" / "blind-conformance" / "SKILL.md"
            skill_sha = hashlib.sha256(skill_path.read_bytes()).hexdigest()
            allowed_inputs.append(
                {
                    "input_id": "blind-conformance-skill",
                    "usage": "reference",
                    "artifact_kind": "skill_source",
                    "path": str(skill_path),
                    "sha256": skill_sha,
                    "origin": "preexisting_allowed",
                }
            )
            audit.append(
                {
                    "path": str(skill_path),
                    "input_id": "blind-conformance-skill",
                    "sha256": skill_sha,
                    "tool": "read allowed skill",
                    "access": "read",
                    "phase": "derive",
                    "origin": "preexisting_allowed",
                }
            )
        else:
            allowed_inputs = [
                {
                    "input_id": "public-test-input",
                    "usage": "reference",
                    "artifact_kind": "other_public",
                    "path": str(input_path),
                    "sha256": input_sha,
                    "origin": "preexisting_allowed",
                }
            ]
            audit = [
                {
                    "path": str(input_path),
                    "input_id": "public-test-input",
                    "sha256": input_sha,
                    "tool": "read input artifact",
                    "access": "read",
                    "phase": "derive",
                    "origin": "preexisting_allowed",
                }
            ]
            spec_path = root / "SPEC-v5.md"
            spec_sha = hashlib.sha256(spec_path.read_bytes()).hexdigest()
            allowed_inputs.append(
                {
                    "input_id": "normative-spec",
                    "usage": "reference",
                    "artifact_kind": "other_public",
                    "path": str(spec_path),
                    "sha256": spec_sha,
                    "origin": "preexisting_allowed",
                }
            )
            audit.append(
                {
                    "path": str(spec_path),
                    "input_id": "normative-spec",
                    "sha256": spec_sha,
                    "tool": "read normative spec",
                    "access": "read",
                    "phase": "derive",
                    "origin": "preexisting_allowed",
                }
            )
        audit.append(
            {
                "path": str(authored_path),
                "tool": "read own reference source",
                "access": "read",
                "phase": "derive",
                "origin": "authored_in_lane",
                "owner_task": author_task,
                "output_id": authored_id,
                "sha256": authored_sha,
            }
        )
        scopes[scope] = {
            "author_task": author_task,
            "context_mode": "fresh_none",
            "authored_outputs": [
                {
                    "output_id": authored_id,
                    "path": str(authored_path),
                    "sha256": authored_sha,
                }
            ],
            "contributors": [],
            "historical_projection_receipts": [],
            "allowed_input_paths": allowed_inputs,
            "forbidden_read_paths": forbidden,
            "declared_read_audit": audit,
            "isolation_disclosures": [],
            "reviewer": {
                "task": "/root/review",
                "verdict": "PASS",
                "reviewed_paths": [
                    {"path": str(reference_path), "sha256": reference_sha},
                    {"path": str(authored_path), "sha256": authored_sha},
                ],
            },
        }
    provenance = {
        "schema_version": "identitybench.v5.reference-review-provenance.v1",
        "suite_id": artifacts.seal["suite"],
        "seed": artifacts.seal["seed"],
        "spec_sha256": "6bc0adfa60b827b8babbdc17713f2b5e2909a8898ccd0242e0806180ab786e43",
        "dataset_seal_sha256": hashlib.sha256(artifacts.seal_bytes).hexdigest(),
        "episodes_sha256": hashlib.sha256(artifacts.episodes_bytes).hexdigest(),
        "oracles_sha256": hashlib.sha256(artifacts.oracles_bytes).hexdigest(),
        "source_sha256": _blind_source_identity(),
        "references": {scope: item["artifact_sha256"] for scope, item in refs.items()},
        "invalidated_author_tasks": [],
        "scopes": scopes,
        "comparison": {
            "task": "/root/review/comparison",
            "command": "uv run pytest tests/conformance/v5 -m not-live",
            "exit_code": 0,
            "output_sha256": "1" * 64,
        },
    }
    return provenance, refs


def _write_agent_view_packet_for_test(artifacts: Any, directory: Path) -> dict[str, Path]:
    from identitybench.v4.jcs import jcs_dumps
    from identitybench.v5.baselines import obs_strategy_q1, obs_strategy_q2

    directory.mkdir(parents=True, exist_ok=True)
    packet_rows = []
    for episode in artifacts.episodes:
        phase_data = []
        for manifest, slices, phase_index in (
            (episode.phase1_manifest, episode.phase1_slices, 1),
            (episode.phase2_base_manifest, episode.phase2_slices, 2),
        ):
            q1_ids = obs_strategy_q1(manifest, episode.policy_card, phase_index)
            q2_ids = obs_strategy_q2(
                manifest,
                tuple(slices[item] for item in q1_ids),
                manifest.work_queue,
                episode.policy_card,
                phase_index,
            )
            phase_data.append((q1_ids, q2_ids, slices))
        (q1_p1, q2_p1, slices_p1), (q1_p2, q2_p2, slices_p2) = phase_data
        packet_rows.append(
            {
                "episode_id": episode.episode_id,
                "policy_card": episode.policy_card.model_dump(mode="json"),
                "phase1_manifest": episode.phase1_manifest.model_dump(mode="json"),
                "phase1_slices": {
                    item: slices_p1[item].model_dump(mode="json")
                    for item in sorted(set(q1_p1) | set(q2_p1))
                },
                "phase2_base_manifest": episode.phase2_base_manifest.model_dump(mode="json"),
                "phase2_slices": {
                    item: slices_p2[item].model_dump(mode="json")
                    for item in sorted(set(q1_p2) | set(q2_p2))
                },
                "q1_slice_ids_p1": list(q1_p1),
                "q2_slice_ids_p1": list(q2_p1),
                "q1_slice_ids_p2": list(q1_p2),
                "q2_slice_ids_p2": list(q2_p2),
            }
        )
    packet_path = directory / "episodes.jsonl"
    packet_path.write_bytes(b"".join(jcs_dumps(row) + b"\n" for row in packet_rows))
    card_path = directory / "rendered-card.txt"
    card_path.write_text(
        artifacts.episodes[0].policy_card.rendered_rules_markdown,
        encoding="utf-8",
    )
    return {"episodes": packet_path, "card": card_path}


def _historical_projection_fixture(
    artifacts: Any, directory: Path, *, review_status: str = "PASS"
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], Path]:
    from identitybench.v4.jcs import jcs_dumps
    from identitybench.v5.validation import (
        _expected_selection_provenance,
        _sanitized_episode_row_v5,
    )

    archive_root = directory / "archive"
    dataset_archive = archive_root / "dataset-before-overwrite" / "episodes.jsonl"
    dataset_archive.parent.mkdir(parents=True, exist_ok=True)
    dataset_archive.write_bytes(artifacts.episodes_bytes)
    packet = archive_root / "delivered-v1" / "episodes.jsonl"
    card = archive_root / "delivered-v1" / "rendered-card.txt"
    packet.parent.mkdir(parents=True, exist_ok=True)
    card_text = artifacts.episodes[0].policy_card.rendered_rules_markdown
    card.write_text(card_text, encoding="utf-8")
    rows = [_sanitized_episode_row_v5(episode) for episode in artifacts.episodes]
    packet_bytes = b"".join(jcs_dumps(row) + b"\n" for row in rows)
    packet.write_bytes(packet_bytes)
    phase_rows = {
        episode.episode_id: {
            "phase1": {
                "q1_slice_ids": _expected_selection_provenance(episode, 1)[-1]["q1_slice_ids"],
                "q2_slice_ids": _expected_selection_provenance(episode, 1)[-1]["q2_slice_ids"],
                "selected_payload_sha256": _expected_selection_provenance(episode, 1)[-1][
                    "selected_payload_sha256"
                ],
            },
            "phase2": {
                "q1_slice_ids": _expected_selection_provenance(episode, 2)[-1]["q1_slice_ids"],
                "q2_slice_ids": _expected_selection_provenance(episode, 2)[-1]["q2_slice_ids"],
                "selected_payload_sha256": _expected_selection_provenance(episode, 2)[-1][
                    "selected_payload_sha256"
                ],
            },
        }
        for episode in artifacts.episodes
    }
    source_card_hash = hashlib.sha256(card_text.encode("utf-8")).hexdigest()
    episode_ids = [episode.episode_id for episode in artifacts.episodes]
    receipt = {
        "schema_version": "identitybench.v5.sanitized-projection-receipt.v1",
        "packet_input_id": "delivered-only-v1",
        "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "packet_archive_path": "delivered-v1/episodes.jsonl",
        "card_input_id": "rendered-card-v1",
        "card_sha256": hashlib.sha256(card.read_bytes()).hexdigest(),
        "card_archive_path": "delivered-v1/rendered-card.txt",
        "source_episodes_path": "datasets/v5_dev/episodes.jsonl",
        "source_episodes_sha256": hashlib.sha256(artifacts.episodes_bytes).hexdigest(),
        "source_episodes_archive_path": "dataset-before-overwrite/episodes.jsonl",
        "source_public_packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "canonical_jcs_jsonl": True,
        "row_key_allowlist": sorted(
            {
                "episode_id",
                "policy_card",
                "phase1_manifest",
                "phase1_slices",
                "phase2_base_manifest",
                "phase2_slices",
                "q1_slice_ids_p1",
                "q2_slice_ids_p1",
                "q1_slice_ids_p2",
                "q2_slice_ids_p2",
            }
        ),
        "row_count": len(rows),
        "rows": phase_rows,
        "extra_row_ids": [],
        "extra_slice_ids_by_episode": {},
        "no_extras": True,
        "source_embedded_card_sha256_by_episode": {
            episode_id: source_card_hash for episode_id in episode_ids
        },
        "packet_embedded_card_sha256_by_episode": {
            episode_id: source_card_hash for episode_id in episode_ids
        },
        "card_override": {"explicit": False, "source_card_sha256": source_card_hash},
        "review": {
            "status": review_status,
            "independent_readback_status": review_status,
            "reviewer": "/root/review",
            "approved_by": "/root",
        },
    }
    receipt_path = archive_root / "projection-receipts" / "v1.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_bytes = jcs_dumps(receipt) + b"\n"
    receipt_path.write_bytes(receipt_bytes)
    receipt_ref = {
        "receipt_id": "projection-v1",
        "artifact_path": str(receipt_path),
        "artifact_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "archive_root": str(archive_root),
    }
    allowed_by_id = {
        "delivered-only-v1": {
            "usage": "historical",
            "artifact_kind": "sanitized_episode_packet",
            "projection_receipt_id": "projection-v1",
            "path": "datasets/v5_dev/episodes.jsonl",
            "archive_path": "delivered-v1/episodes.jsonl",
            "archive_root": str(archive_root),
            "sha256": receipt["packet_sha256"],
        },
        "rendered-card-v1": {
            "usage": "historical",
            "artifact_kind": "rendered_policy_card",
            "projection_receipt_id": "projection-v1",
            "path": "reports/v5/conformance/v5_dev/rendered-card.txt",
            "archive_path": "delivered-v1/rendered-card.txt",
            "archive_root": str(archive_root),
            "sha256": receipt["card_sha256"],
        },
    }
    return [receipt_ref], allowed_by_id, archive_root


def test_review_provenance_requires_actual_audit_and_rejects_forbidden_reads(
    artifacts: Any, tmp_path: Path
) -> None:
    provenance, refs = _review_provenance_fixture(artifacts, tmp_path)
    errors, gaps, evidence = _validate_review_provenance(
        provenance,
        artifacts=artifacts,
        receipt_refs=refs,
    )
    assert errors == []
    assert evidence["agent_view_only"]["sanitized_packet"]["selected_slice_payloads_verified"]
    assert gaps == []

    audit = provenance["scopes"]["full_spec_blind"]["declared_read_audit"]
    audit.append(
        {
            "path": "src/identitybench/v5/oracle.py",
            "tool": "cat",
            "access": "read",
            "phase": "compare",
            "origin": "authored_in_lane",
        }
    )
    errors, _gaps, _evidence = _validate_review_provenance(
        provenance,
        artifacts=artifacts,
        receipt_refs=refs,
    )
    assert any("read forbidden source" in error for error in errors)


def test_review_provenance_rejects_scope_self_review(artifacts: Any, tmp_path: Path) -> None:
    provenance, refs = _review_provenance_fixture(artifacts, tmp_path)
    scope = provenance["scopes"]["full_spec_blind"]
    scope["reviewer"]["task"] = scope["author_task"]

    errors, gaps, _evidence = _validate_review_provenance(
        provenance,
        artifacts=artifacts,
        receipt_refs=refs,
    )

    assert any("reviewer task matches the author task" in error for error in errors)
    assert gaps == []


def test_review_provenance_rejects_contributor_self_review(artifacts: Any, tmp_path: Path) -> None:
    provenance, refs = _review_provenance_fixture(artifacts, tmp_path)
    scope = provenance["scopes"]["agent_view_only"]
    contributor_task = "/root/agent-safety-helper"
    contributor_output_path = (
        Path(__file__).resolve().parents[1] / "conformance" / "v5" / "agent_view_safety.py"
    )
    relative_output_path = contributor_output_path.relative_to(
        Path(__file__).resolve().parents[2]
    ).as_posix()
    output_sha = _blind_source_identity()[relative_output_path]
    output_id = "agent-safety-helper-source"
    contributor_inputs = list(scope["allowed_input_paths"])
    contributor = {
        "task": contributor_task,
        "context_mode": "fresh_none",
        "input_ids": [item["input_id"] for item in contributor_inputs],
        "authored_outputs": [
            {"output_id": output_id, "path": str(contributor_output_path), "sha256": output_sha}
        ],
        "declared_read_audit": [
            {
                "path": item["path"],
                "input_id": item["input_id"],
                "sha256": item["sha256"],
                "origin": item["origin"],
                "tool": "recorded contributor input read",
                "access": "read",
                "phase": "derive",
            }
            for item in contributor_inputs
        ]
        + [
            {
                "path": str(contributor_output_path),
                "sha256": output_sha,
                "origin": "authored_in_lane",
                "owner_task": contributor_task,
                "output_id": output_id,
                "tool": "recorded contributor source read",
                "access": "read",
                "phase": "derive",
            }
        ],
        "isolation_disclosures": [],
        "reviewer": {
            "task": contributor_task,
            "verdict": "PASS",
            "reviewed_paths": [{"path": str(contributor_output_path), "sha256": output_sha}],
        },
    }
    scope["contributors"].append(contributor)

    errors, gaps, _evidence = _validate_review_provenance(
        provenance,
        artifacts=artifacts,
        receipt_refs=refs,
    )

    assert any(
        "reviewer task matches the author task" in error and contributor_task in error
        for error in errors
    )
    assert gaps == []


def test_historical_projection_receipt_revalidates_source_projection_and_selection(
    artifacts: Any, tmp_path: Path
) -> None:
    from identitybench.v5.validation import _validate_historical_projection_receipts

    receipt_refs, allowed_by_id, _archive_root = _historical_projection_fixture(artifacts, tmp_path)
    errors, gaps, evidence = _validate_historical_projection_receipts(
        receipt_refs,
        allowed_by_id=allowed_by_id,
        root=Path(__file__).resolve().parents[2],
    )

    assert errors == []
    assert gaps == []
    assert evidence["projection-v1"]["review_status"] == "PASS"


def test_historical_projection_receipt_accepts_only_complete_empty_slice_inventory(
    artifacts: Any, tmp_path: Path
) -> None:
    from identitybench.v4.jcs import jcs_dumps
    from identitybench.v5.validation import _validate_historical_projection_receipts

    def validate_inventory(inventory: dict[str, Any], label: str) -> tuple[list[str], list[str]]:
        receipt_refs, allowed_by_id, _archive_root = _historical_projection_fixture(
            artifacts, tmp_path / label
        )
        receipt_path = Path(receipt_refs[0]["artifact_path"])
        receipt = json.loads(receipt_path.read_bytes())
        receipt["extra_slice_ids_by_episode"] = inventory
        raw = jcs_dumps(receipt) + b"\n"
        receipt_path.write_bytes(raw)
        receipt_refs[0]["artifact_sha256"] = hashlib.sha256(raw).hexdigest()
        errors, gaps, _evidence = _validate_historical_projection_receipts(
            receipt_refs,
            allowed_by_id=allowed_by_id,
            root=Path(__file__).resolve().parents[2],
        )
        return errors, gaps

    _base_refs, _base_allowed, _base_archive = _historical_projection_fixture(
        artifacts, tmp_path / "valid-inventory"
    )
    base_receipt_path = Path(_base_refs[0]["artifact_path"])
    base_receipt = json.loads(base_receipt_path.read_bytes())
    episode_ids = set(base_receipt["rows"])
    first_episode = sorted(episode_ids)[0]
    complete_empty_inventory = {
        episode_id: {"phase1": [], "phase2": []} for episode_id in episode_ids
    }

    errors, gaps = validate_inventory(complete_empty_inventory, "complete-empty")
    assert errors == []
    assert gaps == []

    malformed_inventories = {
        "incomplete": {first_episode: {"phase1": [], "phase2": []}},
        "unknown-episode": {
            **complete_empty_inventory,
            "unknown_episode": {"phase1": [], "phase2": []},
        },
        "wrong-phases": {
            **complete_empty_inventory,
            first_episode: {"phase1": [], "phase3": []},
        },
        "nonempty": {
            **complete_empty_inventory,
            first_episode: {"phase1": ["sl_extra"], "phase2": []},
        },
    }
    for label, inventory in malformed_inventories.items():
        errors, gaps = validate_inventory(inventory, label)
        assert any("reports extra rows or slices" in error for error in errors), label
        assert gaps == [], label


def test_review_provenance_retains_historical_packet_card_reads(
    artifacts: Any, tmp_path: Path
) -> None:
    provenance, refs = _review_provenance_fixture(artifacts, tmp_path)
    receipt_refs, historical_inputs, archive_root = _historical_projection_fixture(
        artifacts, tmp_path / "history"
    )
    agent_scope = provenance["scopes"]["agent_view_only"]
    agent_scope["historical_projection_receipts"] = receipt_refs
    for input_id, historical in historical_inputs.items():
        record = {
            **historical,
            "input_id": input_id,
            "origin": "derived_sanitized_packet",
            "producer": "archived public packet producer",
            "producer_source_sha256": {"public-projection": "0" * 64},
        }
        agent_scope["allowed_input_paths"].append(record)
        agent_scope["declared_read_audit"].append(
            {
                "path": record["path"],
                "input_id": input_id,
                "sha256": record["sha256"],
                "origin": record["origin"],
                "tool": "read preserved historical sanitized input",
                "access": "read",
                "phase": "derive",
            }
        )

    errors, gaps, evidence = _validate_review_provenance(
        provenance,
        artifacts=artifacts,
        receipt_refs=refs,
    )

    assert errors == []
    assert gaps == []
    assert (
        evidence["agent_view_only"]["historical_projection"]["projection-v1"]["review_status"]
        == "PASS"
    )
    assert archive_root.is_dir()


def test_historical_projection_receipt_keeps_missing_review_unmeasured(
    artifacts: Any, tmp_path: Path
) -> None:
    from identitybench.v5.validation import _validate_historical_projection_receipts

    receipt_refs, allowed_by_id, _archive_root = _historical_projection_fixture(
        artifacts, tmp_path, review_status="PENDING"
    )
    errors, gaps, _evidence = _validate_historical_projection_receipts(
        receipt_refs,
        allowed_by_id=allowed_by_id,
        root=Path(__file__).resolve().parents[2],
    )

    assert errors == []
    assert any("review is incomplete" in gap for gap in gaps)


def test_historical_projection_receipt_rejects_stale_archived_packet(
    artifacts: Any, tmp_path: Path
) -> None:
    from identitybench.v5.validation import _validate_historical_projection_receipts

    receipt_refs, allowed_by_id, archive_root = _historical_projection_fixture(artifacts, tmp_path)
    packet_path = archive_root / "delivered-v1" / "episodes.jsonl"
    packet_path.write_bytes(packet_path.read_bytes() + b"{}\n")

    errors, _gaps, _evidence = _validate_historical_projection_receipts(
        receipt_refs,
        allowed_by_id=allowed_by_id,
        root=Path(__file__).resolve().parents[2],
    )

    assert any("bytes mismatch" in error or "digest mismatch" in error for error in errors)


def test_agent_view_scope_rejects_spec_and_other_reference_reads(
    artifacts: Any, tmp_path: Path
) -> None:
    provenance, refs = _review_provenance_fixture(artifacts, tmp_path)
    repo = Path(__file__).resolve().parents[2]
    spec_path = repo / "SPEC-v5.md"
    other_reference = (
        repo / "reports" / "v5" / "conformance" / "v5_dev" / "full_spec_reference.json"
    )
    assert other_reference.is_file()
    for path in (spec_path, other_reference):
        input_id = f"extra-{path.name}"
        record = {
            "input_id": input_id,
            "usage": "reference",
            "artifact_kind": "other_public",
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "origin": "preexisting_allowed",
        }
        provenance["scopes"]["agent_view_only"]["allowed_input_paths"].append(record)
        provenance["scopes"]["agent_view_only"]["declared_read_audit"].append(
            {
                "path": str(path),
                "tool": "read disallowed source",
                "input_id": input_id,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "access": "read",
                "phase": "derive",
                "origin": "preexisting_allowed",
            }
        )
    full_spec_source = repo / "tests" / "conformance" / "v5" / "test_conformance_v5.py"
    for path in (spec_path, full_spec_source):
        provenance["scopes"]["agent_view_only"]["declared_read_audit"].append(
            {
                "path": str(path),
                "tool": "read claimed authored source",
                "access": "read",
                "phase": "derive",
                "origin": "authored_in_lane",
                "owner_task": provenance["scopes"]["agent_view_only"]["author_task"],
                "output_id": "agent_view_only-reference-source",
            }
        )
    spec_record = {
        "input_id": "full-spec-normative-spec",
        "usage": "reference",
        "artifact_kind": "other_public",
        "path": str(spec_path),
        "sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest(),
        "origin": "preexisting_allowed",
    }
    provenance["scopes"]["full_spec_blind"]["allowed_input_paths"].append(spec_record)
    provenance["scopes"]["full_spec_blind"]["declared_read_audit"].append(
        {
            "path": str(spec_path),
            "input_id": "full-spec-normative-spec",
            "sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest(),
            "tool": "read normative spec",
            "access": "read",
            "phase": "derive",
            "origin": "preexisting_allowed",
        }
    )

    errors, _gaps, _evidence = _validate_review_provenance(
        provenance,
        artifacts=artifacts,
        receipt_refs=refs,
    )

    agent_errors = [error for error in errors if "agent_view_only" in error]
    assert any("SPEC-v5.md" in error and "input contract" in error for error in agent_errors)
    assert any(
        "full_spec_reference.json" in error and "input contract" in error for error in agent_errors
    )
    assert any(
        "test_conformance_v5.py" in error and "owned output inventory" in error
        for error in agent_errors
    )
    assert not any("full_spec_blind" in error and "input contract" in error for error in errors)


def test_agent_view_read_audit_requires_input_identity_hash_and_original_path(
    artifacts: Any, tmp_path: Path
) -> None:
    provenance, refs = _review_provenance_fixture(artifacts, tmp_path)
    audit = provenance["scopes"]["agent_view_only"]["declared_read_audit"]
    audit[0] = {key: value for key, value in audit[0].items() if key != "sha256"}

    errors, _gaps, _evidence = _validate_review_provenance(
        provenance,
        artifacts=artifacts,
        receipt_refs=refs,
    )

    assert any("input hash disagrees" in error for error in errors)


def test_agent_view_scope_allows_only_its_own_new_reference_code() -> None:
    root = Path(__file__).resolve().parents[2]
    output_inventory = {
        ("clean-card-author", "agent-reference"): {
            "owner_task": "clean-card-author",
            "path": (root / "tests/conformance/v5/agent_view_reference.py").as_posix(),
            "sha256": _blind_source_identity()["tests/conformance/v5/agent_view_reference.py"],
        }
    }

    assert _agent_view_input_allowed(
        root,
        {
            "path": "tests/conformance/v5/agent_view_reference.py",
            "origin": "authored_in_lane",
            "artifact_kind": "reference_code",
            "owner_task": "clean-card-author",
            "output_id": "agent-reference",
        },
        output_inventory,
    )
    assert not _agent_view_input_allowed(
        root,
        {
            "path": "tests/conformance/v5/test_conformance_v5.py",
            "origin": "authored_in_lane",
            "artifact_kind": "reference_code",
            "owner_task": "clean-card-author",
            "output_id": "agent-reference",
        },
        output_inventory,
    )
    assert not _agent_view_input_allowed(
        root,
        {
            "path": "SPEC-v5.md",
            "origin": "preexisting_allowed",
            "artifact_kind": "other_public",
        },
        output_inventory,
    )


def test_review_provenance_missing_read_audit_is_incomplete(artifacts: Any, tmp_path: Path) -> None:
    provenance, refs = _review_provenance_fixture(artifacts, tmp_path)
    provenance["scopes"]["agent_view_only"]["declared_read_audit"] = []

    errors, gaps, _evidence = _validate_review_provenance(
        provenance,
        artifacts=artifacts,
        receipt_refs=refs,
    )

    assert errors == []
    assert any("actual read audit is unavailable" in gap for gap in gaps)


def test_review_provenance_rejects_an_invalidated_current_author(
    artifacts: Any, tmp_path: Path
) -> None:
    provenance, refs = _review_provenance_fixture(artifacts, tmp_path)
    current_author = provenance["scopes"]["agent_view_only"]["author_task"]
    provenance["invalidated_author_tasks"] = [current_author]

    errors, _gaps, _evidence = _validate_review_provenance(
        provenance,
        artifacts=artifacts,
        receipt_refs=refs,
    )

    assert any("uses an invalidated author task" in error for error in errors)


def test_agent_view_packet_requires_only_selected_slice_payloads(
    artifacts: Any, tmp_path: Path
) -> None:
    from identitybench.v4.jcs import jcs_dumps
    from identitybench.v5.baselines import obs_strategy_q1, obs_strategy_q2

    packet_rows = []
    for episode in artifacts.episodes:
        phase1_q1 = obs_strategy_q1(episode.phase1_manifest, episode.policy_card, 1)
        phase1_q2 = obs_strategy_q2(
            episode.phase1_manifest,
            tuple(episode.phase1_slices[item] for item in phase1_q1),
            episode.phase1_manifest.work_queue,
            episode.policy_card,
            1,
        )
        phase2_q1 = obs_strategy_q1(episode.phase2_base_manifest, episode.policy_card, 2)
        phase2_q2 = obs_strategy_q2(
            episode.phase2_base_manifest,
            tuple(episode.phase2_slices[item] for item in phase2_q1),
            episode.phase2_base_manifest.work_queue,
            episode.policy_card,
            2,
        )
        packet_rows.append(
            {
                "episode_id": episode.episode_id,
                "policy_card": episode.policy_card.model_dump(mode="json"),
                "phase1_manifest": episode.phase1_manifest.model_dump(mode="json"),
                "phase1_slices": {
                    item: episode.phase1_slices[item].model_dump(mode="json")
                    for item in sorted(set(phase1_q1) | set(phase1_q2))
                },
                "phase2_base_manifest": episode.phase2_base_manifest.model_dump(mode="json"),
                "phase2_slices": {
                    item: episode.phase2_slices[item].model_dump(mode="json")
                    for item in sorted(set(phase2_q1) | set(phase2_q2))
                },
                "q1_slice_ids_p1": list(phase1_q1),
                "q2_slice_ids_p1": list(phase1_q2),
                "q1_slice_ids_p2": list(phase2_q1),
                "q2_slice_ids_p2": list(phase2_q2),
            }
        )
    packet_path = tmp_path / "episodes.jsonl"
    card_path = tmp_path / "rendered-card.txt"
    packet_path.write_bytes(b"".join(jcs_dumps(row) + b"\n" for row in packet_rows))
    card_path.write_text(
        artifacts.episodes[0].policy_card.rendered_rules_markdown,
        encoding="utf-8",
    )
    inputs = [
        {
            "input_id": "current-test-packet",
            "usage": "reference",
            "artifact_kind": "sanitized_episode_packet",
            "path": str(packet_path),
            "sha256": hashlib.sha256(packet_path.read_bytes()).hexdigest(),
            "origin": "derived_sanitized_packet",
            "producer": "test_packet_builder",
            "producer_source_sha256": {"SPEC-v5.md": "0" * 64},
        },
        {
            "input_id": "current-test-card",
            "usage": "reference",
            "artifact_kind": "rendered_policy_card",
            "path": str(card_path),
            "sha256": hashlib.sha256(card_path.read_bytes()).hexdigest(),
            "origin": "derived_sanitized_packet",
            "producer": "test_packet_builder",
            "producer_source_sha256": {"SPEC-v5.md": "0" * 64},
        },
    ]

    errors, gaps, evidence = _validate_agent_view_packet_inputs(
        inputs,
        artifacts=artifacts,
        root=tmp_path,
    )

    assert errors == []
    assert gaps == []
    assert evidence["episode_count"] == len(artifacts.episodes)
    assert evidence["selected_slice_payloads_verified"]

    first_episode = artifacts.episodes[0]
    selected = set(packet_rows[0]["phase1_slices"])
    hidden_slice = next(
        slice_id for slice_id in first_episode.phase1_slices if slice_id not in selected
    )
    packet_rows[0]["phase1_slices"][hidden_slice] = first_episode.phase1_slices[
        hidden_slice
    ].model_dump(mode="json")
    packet_path.write_bytes(b"".join(jcs_dumps(row) + b"\n" for row in packet_rows))
    inputs[0]["sha256"] = hashlib.sha256(packet_path.read_bytes()).hexdigest()
    errors, _gaps, _evidence = _validate_agent_view_packet_inputs(
        inputs,
        artifacts=artifacts,
        root=tmp_path,
    )
    assert any("unqueried slice payloads" in error for error in errors)


@pytest.mark.parametrize(
    ("flip_checks", "flip_matches", "permutation_checks", "permutation_matches", "expected"),
    [
        (0, 0, 0, 0, GateStatusV5.UNKNOWN),
        (10, 10, 10, 9, GateStatusV5.FAIL),
        (10, 10, 10, 10, GateStatusV5.PASS),
    ],
)
def test_g_base_requires_measured_complete_metamorphic_proofs(
    flip_checks: int,
    flip_matches: int,
    permutation_checks: int,
    permutation_matches: int,
    expected: GateStatusV5,
) -> None:
    status, _errors = _g_base_status(
        (),
        flip_checks=flip_checks,
        flip_matches=flip_matches,
        permutation_checks=permutation_checks,
        permutation_matches=permutation_matches,
    )

    assert status is expected


def test_g_base_threshold_failure_is_not_hidden_by_missing_metamorphic_proof() -> None:
    status, errors = _g_base_status(
        ("heuristic exceeded threshold",),
        flip_checks=0,
        flip_matches=0,
        permutation_checks=0,
        permutation_matches=0,
    )

    assert status is GateStatusV5.FAIL
    assert errors == ("heuristic exceeded threshold",)


def test_offline_validation_success_does_not_claim_full_milestone() -> None:
    report = V5ValidationReport(
        suite_id="v5_dev",
        dataset_dir="/tmp/dataset",
        episode_count=12,
        gate_results=(
            GateResultV5("G-TOK-01", GateStatusV5.PASS, {}),
            GateResultV5("G-ECON-01", GateStatusV5.UNKNOWN, {}),
            GateResultV5("G-CAL-01", GateStatusV5.UNKNOWN, {}),
            GateResultV5("G-CAL-02", GateStatusV5.UNKNOWN, {}),
            GateResultV5("G-CAL-03", GateStatusV5.UNKNOWN, {}),
        ),
    )

    assert report.passed
    assert not report.full_milestone_passed
    assert report.live_gates_unmeasured == (
        "G-ECON-01",
        "G-CAL-01",
        "G-CAL-02",
        "G-CAL-03",
    )
    assert report.to_mapping()["offline_passed"] is True
    assert report.to_mapping()["full_milestone_passed"] is False


def test_unmeasured_offline_gate_still_blocks_validation_success() -> None:
    report = V5ValidationReport(
        suite_id="v5_dev",
        dataset_dir="/tmp/dataset",
        episode_count=12,
        gate_results=(
            GateResultV5("G-TOK-01", GateStatusV5.PASS, {}),
            GateResultV5("G-BASE-01", GateStatusV5.UNKNOWN, {}),
            GateResultV5("G-ECON-01", GateStatusV5.UNKNOWN, {}),
        ),
    )

    assert not report.passed


def test_dataset_validation_lists_each_live_calibration_gate(artifacts: Any) -> None:
    from identitybench.v5.validation import validate_v5_dataset

    report = validate_v5_dataset(artifacts.dataset_dir, run_baselines=False)
    gates = {item.gate_id: item.status for item in report.gate_results}

    assert gates["G-ECON-01"] is GateStatusV5.UNKNOWN
    assert gates["G-CAL-01"] is GateStatusV5.UNKNOWN
    assert gates["G-CAL-02"] is GateStatusV5.UNKNOWN
    assert gates["G-CAL-03"] is GateStatusV5.UNKNOWN
    assert set(report.live_gates_unmeasured) == {
        "G-ECON-01",
        "G-CAL-01",
        "G-CAL-02",
        "G-CAL-03",
    }


def test_runtime_noninterference_uses_identical_public_episode_and_hidden_world_twins(
    artifacts: Any,
) -> None:
    checks, matches, errors = _paired_world_noninterference(
        artifacts.episodes[0], artifacts.oracles[0]
    )

    assert checks == 2
    assert matches == checks
    assert errors == ()


def test_negative_gate_uses_measured_directed_rule_and_category_coverage(
    artifacts: Any,
) -> None:
    result = _negative_mutation_gate(artifacts)
    directed = result.evidence["directed_boundary_proof"]

    assert directed["attempted"] == 43
    assert directed["rejected"] == 43
    assert directed["accepted"] == 0
    assert len(directed["mutation_kinds"]) == 12
    assert len(directed["violation_categories"]) == 43
    if len(directed["verified_rule_ids"]) == 26:
        assert result.status is GateStatusV5.PASS, result.errors
    else:
        assert result.status is GateStatusV5.UNKNOWN
        assert len(directed["missing_verified_rule_ids"]) > 0
