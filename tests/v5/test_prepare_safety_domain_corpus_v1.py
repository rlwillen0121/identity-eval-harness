from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import identitybench.v5.conformance_safety_domain as safety_domain
import identitybench.v5.validation as validation

ROOT = Path(__file__).resolve().parents[2]
COORDINATOR_PATH = (
    ROOT / "reports/v5/conformance/shared_results/v1/full-conformance-v3/coordinators/"
    "prepare-safety-domain-corpus-v1.py"
)
SPEC = importlib.util.spec_from_file_location("prepare_safety_domain_corpus_v1", COORDINATOR_PATH)
assert SPEC is not None and SPEC.loader is not None
coordinator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coordinator)
ORIGINAL_AGREEMENT = safety_domain.agree_reference_materializations_v1


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _account_row(target: str) -> dict[str, Any]:
    return {
        "mutation_kind": "ACCOUNT_RECON_MUTATION",
        "target_entity_id": target,
        "if_match": ["acct_base@0123456789abcdef:1"],
        "cite": ["UAR-V5-01:ev_demo"],
        "account_action": "KEEP_ACTIVE",
        "bound_person_id": "pers_demo",
        "bound_engagement_id": "eng_demo",
        "retained_grant_ids": ["grant_demo"],
    }


def _public_case(stimulus: dict[str, Any], *, case_id: str = "source/case") -> dict[str, Any]:
    return {
        "protocol": coordinator.WIRE_PROTOCOL,
        "case_id": case_id,
        "operation": "safety",
        "suite_id": "v5_dev",
        "phase_index": 1,
        "public_episode": {
            "episode_id": "ep_synthetic",
            "phase1_manifest": {},
            "phase1_slices": [],
            "policy_card": {"spec_version": "5.0"},
            "q1_slice_ids_p1": [],
            "q2_slice_ids_p1": [],
        },
        "rendered_card": "synthetic public card\n",
        "schema_sha256": coordinator.SCHEMA_SHA256,
        "stimulus": stimulus,
    }


def _source_case(rows: list[dict[str, Any]]) -> dict[str, Any]:
    target = rows[0]
    return _public_case(
        {
            "target_selector": {
                "mutation_kind": target["mutation_kind"],
                "target_entity_id": target["target_entity_id"],
            },
            "submitted_plan": rows,
        }
    )


def test_request_plan_preserves_multrow_literal_and_adds_independent_base_request() -> None:
    rows = [_account_row("acct_demo"), _account_row("acct_extra")]
    source_case = _source_case(rows)
    context = {
        key: source_case[key]
        for key in ("suite_id", "phase_index", "public_episode", "rendered_card", "schema_sha256")
    }
    context_hashes = {"rendered_card": sha256(context["rendered_card"].encode())}
    _, requests, materialization_source = coordinator._case_request_set(
        {}, source_case, context, context_hashes
    )

    assert materialization_source == "literal_plan_input"
    assert [row["purpose"] for row in requests] == ["primary_safety", "literal_plan_base"]
    safety_case = json.loads(base64.b64decode(requests[0]["case_b64"]))
    assert safety_case["stimulus"]["submitted_plan"] == rows
    derive_case = json.loads(base64.b64decode(requests[1]["case_b64"]))
    assert derive_case["operation"] == "derive_phase"
    assert derive_case["stimulus"] == {}
    assert derive_case["public_episode"] == safety_case["public_episode"]


def test_atomic_literal_uses_root_replace_materialization_and_route_keeps_context() -> None:
    candidate = _account_row("acct_demo")
    source_case = _public_case(
        {
            "target_selector": {
                "mutation_kind": candidate["mutation_kind"],
                "target_entity_id": candidate["target_entity_id"],
            },
            "submitted_plan": candidate,
        }
    )
    context = {
        key: source_case[key]
        for key in ("suite_id", "phase_index", "public_episode", "rendered_card", "schema_sha256")
    }
    _, requests, materialization_source = coordinator._case_request_set(
        {}, source_case, context, {"rendered_card": sha256(context["rendered_card"].encode())}
    )

    assert materialization_source == "literal_root_replace_recipe"
    aux_case = json.loads(base64.b64decode(requests[1]["case_b64"]))
    assert aux_case["operation"] == "validate_submission"
    assert aux_case["stimulus"] == {
        "target_selector": source_case["stimulus"]["target_selector"],
        "recipe": {
            "candidate_source": "independent_base",
            "base_scope": "target_mutation",
            "patches": [{"op": "replace", "path": "", "value": candidate}],
        },
    }

    full_plan_raw = canonical([candidate])
    routed = coordinator._route_case_request(
        "source/case",
        canonical(source_case),
        base_plan_raw=full_plan_raw,
        pre_patch_plan_raw=full_plan_raw,
        submitted_plan_raw=full_plan_raw,
        materialization_provenance={"full_spec": {}, "agent_view": {}},
    )
    routed_case = json.loads(base64.b64decode(routed["case_b64"]))
    for key in source_case.keys() - {"case_id", "operation", "stimulus"}:
        assert routed_case[key] == source_case[key]
    assert routed_case["case_id"] == "v4sd/source/case/validate_submission"
    assert routed_case["operation"] == "validate_submission"
    assert routed_case["stimulus"]["submitted_plan"] == [candidate]
    assert routed_case["stimulus"]["target_selector"] == source_case["stimulus"][
        "target_selector"
    ]
    assert routed["projection_context"]["source_case_b64"] == base64.b64encode(
        canonical(source_case)
    ).decode("ascii")
    assert routed["projection_context"]["source_stimulus_sha256"] == sha256(
        canonical(source_case["stimulus"])
    )

    atomic_source_case = _public_case(
        {
            "target_selector": source_case["stimulus"]["target_selector"],
            "submitted_plan": candidate,
        }
    )
    expanded_plan = [_account_row("acct_base"), candidate]
    expanded_raw = canonical(expanded_plan)
    atomic_route = coordinator._route_case_request(
        "source/atomic",
        canonical(atomic_source_case),
        base_plan_raw=canonical([_account_row("acct_base")]),
        pre_patch_plan_raw=canonical([_account_row("acct_base")]),
        submitted_plan_raw=expanded_raw,
        materialization_provenance={"full_spec": {}, "agent_view": {}},
    )
    atomic_routed_case = json.loads(base64.b64decode(atomic_route["case_b64"]))
    assert atomic_routed_case["stimulus"]["submitted_plan"] == expanded_plan
    assert atomic_routed_case["stimulus"]["target_selector"] == atomic_source_case["stimulus"][
        "target_selector"
    ]
    for key in atomic_source_case.keys() - {"case_id", "operation", "stimulus"}:
        assert atomic_routed_case[key] == atomic_source_case[key]
    assert atomic_route["projection_context"]["projection_kind"] == (
        "atomic_literal_to_literal_full_plan_array"
    )
    assert atomic_route["projection_context"]["source_stimulus_sha256"] == sha256(
        canonical(atomic_source_case["stimulus"])
    )
    assert base64.b64decode(atomic_route["projection_context"]["submitted_plan_b64"]) == (
        expanded_raw
    )


def _source_binding(lane: str, capture_root: str) -> dict[str, Any]:
    return {
        "lane": lane,
        "wire_evaluator_id": coordinator.WIRE_ID[lane],
        "source_profile": coordinator.PROFILE_ID[lane],
        "author_origin": coordinator.AUTHOR_ORIGIN[lane],
        "capture_root": capture_root,
        "capture_manifest_sha256": "a" * 64,
        "capture_logical_sha256": "b" * 64,
        "capture_file_count": 14 if lane == "full_spec" else 2,
        "source_map_sha256": "c" * 64,
        "source_closure_sha256": f"{lane[0]}" * 64,
        "runtime_manifest_sha256": "d" * 64,
        "runtime_archive_sha256": "e" * 64,
        "dependency_archive_sha256": "f" * 64,
        "qualification": {
            "kind": "author_origin_chain_v1",
            "chain": {"path": "reports/v5/conformance/chain.json", "sha256": "1" * 64},
            "chain_review": {"path": "reports/v5/conformance/review.json", "sha256": "2" * 64},
        },
        "qualification_provenance": {"qualification_status": "QUALIFIED_ELIGIBLE"},
        "capture_run_descriptor_sha256": "3" * 64,
        "capture_manifest": {"path": "reports/capture/capture-manifest.json", "sha256": "4" * 64},
        "capture_run_descriptor": {"path": "reports/capture/run.json", "sha256": "5" * 64},
        "runtime_manifest": {"path": "reports/runtime/manifest.json", "sha256": "6" * 64},
        "runtime_archive": {"path": "reports/runtime/source-manifest.json", "sha256": "7" * 64},
        "dependency_archive": {
            "path": "reports/runtime/dependency-manifest.json",
            "sha256": "8" * 64,
        },
    }


def _write_ref(tmp_path: Path, relative: str, raw: bytes) -> dict[str, str]:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"path": relative, "sha256": sha256(raw)}


def _build_synthetic_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stage2_status: str = "ELIGIBLE",
    mismatch: bool = False,
    omit_receipt: tuple[str, str] | None = None,
    atomic_literal: bool = False,
) -> tuple[bytes, bytes, list[str], dict[str, Any]]:
    monkeypatch.setattr(coordinator, "assert_protocol_pins", lambda _root: None)
    schema_raw = b"synthetic public schema bytes"
    schema_sha = sha256(schema_raw)
    monkeypatch.setattr(coordinator, "SCHEMA_SHA256", schema_sha)
    monkeypatch.setattr(coordinator.shared, "PINNED_PUBLIC_SCHEMA_SHA256", schema_sha)
    monkeypatch.setattr(safety_domain, "PINNED_SCHEMA_SHA256_V1", schema_sha)
    for lane in ("full_spec", "agent_view"):
        capture_root = tmp_path / f"capture-{lane}"
        schema_path = capture_root / "src/identitybench/v5/schemas.py"
        schema_path.parent.mkdir(parents=True)
        schema_path.write_bytes(schema_raw)

    row = _account_row("acct_demo")
    other = _account_row("acct_extra")
    submitted = [row, other]
    source_case = (
        _public_case(
            {
                "target_selector": {
                    "mutation_kind": row["mutation_kind"],
                    "target_entity_id": row["target_entity_id"],
                },
                "submitted_plan": row,
            }
        )
        if atomic_literal
        else _source_case(submitted)
    )
    source_raw = canonical(source_case)
    source_id = source_case["case_id"]
    source_index_row = {
        "canonical_case_bytes": len(source_raw),
        "canonical_case_sha256": sha256(source_raw),
        "case_id": source_id,
        "episode_id": source_case["public_episode"]["episode_id"],
        "input_ref": "phase/source-case",
        "operation": "safety",
        "phase_index": source_case["phase_index"],
        "stimulus_record_id": source_id,
        "stimulus_record_index": 0,
        "stimulus_source_file": "stimuli/recipes.jsonl",
        "suite_id": source_case["suite_id"],
    }
    source_binding_list = {
        lane: _source_binding(lane, f"capture-{lane}") for lane in ("full_spec", "agent_view")
    }
    monkeypatch.setattr(
        coordinator,
        "_load_capture_and_runtime",
        lambda lane, _source, _root: source_binding_list[lane],
    )
    source_input = {
        "capture_root": "captures/current",
        "capture_manifest": {"path": "captures/current/capture-manifest.json", "sha256": "4" * 64},
        "capture_run_descriptor": {"path": "captures/run.json", "sha256": "5" * 64},
        "runtime_manifest": {"path": "runtime/manifest.json", "sha256": "6" * 64},
        "runtime_archive": {"path": "runtime/source-manifest.json", "sha256": "7" * 64},
        "dependency_archive": {"path": "runtime/dependency-manifest.json", "sha256": "8" * 64},
        "qualification": {
            "kind": "author_origin_chain_v1",
            "chain": {"path": "reports/v5/conformance/chain.json", "sha256": "1" * 64},
            "chain_review": {
                "path": "reports/v5/conformance/review.json",
                "sha256": "2" * 64,
            },
        },
    }
    preserved_ref = _write_ref(tmp_path, "source-v3/corpus.json", b"preserved v3 corpus")
    source_aux_ref = _write_ref(tmp_path, "source-v3/source-row.json", b"source row evidence")
    preserved_artifacts = [preserved_ref, source_aux_ref]
    synthetic_tuple = {
        "suite_id": "v5_dev",
        "episode_id": "ep_synthetic",
        "phase_index": 1,
        "mutation_kind": row["mutation_kind"],
        "target_entity_id": row["target_entity_id"],
    }
    synthetic_inventory = {
        "snapshot_manifest_sha256": "f" * 64,
        "source_manifest_sha256": "e" * 64,
        "source_case_index": [source_index_row],
        "safety_case_index": [source_index_row],
        "phase_target_tuple_inventory": [synthetic_tuple],
        "coverage_declarations": {
            "diagnostic_categories": [f"D{index}" for index in range(43)],
            "rule_ids": [f"R{index}" for index in range(26)],
            "policy_card_anchor_labels": [f"A{index}" for index in range(39)],
            "family_ids": [f"F{index:02d}" for index in range(1, 13)],
            "phase_target_tuple_count": 1,
            "phase_target_tuple_inventory": [synthetic_tuple],
            "trace_case_count": 0,
            "snapshot_cells": [f"S{index}" for index in range(6)],
            "global_safety_truth_pairs": [{"overgrant": False, "avail_breach": False}],
            "source_manifest_sha256": "e" * 64,
            "case_index_sha256": "a" * 64,
            "case_manifest_sha256": "b" * 64,
        },
        "preserved_artifacts": preserved_artifacts,
    }
    monkeypatch.setattr(
        coordinator,
        "_approved_v3_source_inventory",
        lambda _root: synthetic_inventory,
    )
    source_bundle = {
        "format": coordinator.SOURCE_BUNDLE_FORMAT,
        "source_corpus_version": "v3",
        "source_manifest_sha256": synthetic_inventory["source_manifest_sha256"],
        "source_snapshot_manifest_sha256": synthetic_inventory["snapshot_manifest_sha256"],
        "source_case_index": [source_index_row],
        "preserved_artifacts": preserved_artifacts,
        "cases": [
            {
                "case_b64": base64.b64encode(source_raw).decode(),
                "case_sha256": sha256(source_raw),
                "v8_binding_id": "v8/episode-a",
                "lineage": source_index_row,
                "source_artifacts": preserved_artifacts,
            }
        ],
    }
    source_bundle_raw = canonical(source_bundle)
    context = {
        key: source_case[key]
        for key in ("suite_id", "phase_index", "public_episode", "rendered_card", "schema_sha256")
    }
    public_data = {
        "packet": b"public packet",
        "rendered_card": source_case["rendered_card"].encode(),
        "spec": b"public spec",
        "schema": schema_raw,
        "family_map": b"public family map artifact",
    }
    public_names = list(public_data)
    public_refs = {
        name: _write_ref(tmp_path, f"v8/{name}.json", raw) for name, raw in public_data.items()
    }
    release = {
        "format": coordinator.V8_RELEASE_FORMAT,
        "release_id": "v8",
        "release_status": "released",
        "source_bundle_sha256": sha256(source_bundle_raw),
        "author_sources": {"full_spec": source_input, "agent_view": source_input},
        "v8_contexts": {
            "v8/episode-a": {
                **context,
                "public_input_names": public_names,
                "public_input_artifacts": [public_refs[name] for name in public_names],
            }
        },
    }
    release_raw = canonical(release)
    plan_raw = coordinator.build_request_plan(
        source_bundle_raw,
        release_raw,
        approved_v8_release_sha256=sha256(release_raw),
        repository_root=tmp_path,
    )
    plan = json.loads(plan_raw)
    source_entry = plan["source_cases"][0]
    requests = plan["requests"]
    new_case = json.loads(base64.b64decode(source_entry["new_safety_case_b64"]))
    context_hashes = source_entry["v8_public_input_hashes"]
    plan_sha = json.loads(plan_raw)["request_plan_sha256"]
    receipt_rows: list[dict[str, Any]] = []
    events: list[str] = []
    observation = SimpleNamespace(
        case_sha256=sha256(canonical(new_case)),
        schema_sha256=schema_sha,
        card_sha256=context_hashes["rendered_card"],
        stimulus_sha256=sha256(canonical(new_case["stimulus"])),
        selected_candidate_sha256=sha256(canonical(row)),
        agreed_materialization_sha256="a" * 64,
        stage2_status=stage2_status,
        criterion_gaps=(),
    )

    def tracked_inspect(*args: Any, **kwargs: Any) -> Any:
        events.append("stage2")
        return observation

    monkeypatch.setattr(safety_domain, "inspect_stage2_domain_v1", tracked_inspect)

    def tracked_agreement(*args: Any, **kwargs: Any) -> Any:
        events.append("agreement")
        return ORIGINAL_AGREEMENT(*args, **kwargs)

    monkeypatch.setattr(safety_domain, "agree_reference_materializations_v1", tracked_agreement)

    case_manifest = plan["request_case_manifest_sha256"]
    for request in requests:
        request_case = json.loads(base64.b64decode(request["case_b64"]))
        for lane in ("full_spec", "agent_view"):
            if omit_receipt == (request["request_id"], lane):
                continue
            if request["operation"] == "derive_phase":
                mutations = [row, other]
                if mismatch and lane == "agent_view":
                    mutations = [row]
                payload = {"mutations": mutations, "policy_card_anchor_map": {}}
                materialization = None
            elif request["operation"] == "validate_submission":
                payload = {}
                materialization = {
                    "base_plan": [row, other],
                    "submitted_plan": [row, other],
                }
            else:
                # The two synthetic safety payloads deliberately differ. The
                # coordinator must not inspect or compare semantic fields.
                payload = {
                    "overgrant": lane == "full_spec",
                    "availability_breach": lane == "agent_view",
                }
                materialization = None
            wire = {
                "protocol": coordinator.WIRE_PROTOCOL,
                "evaluator_id": coordinator.WIRE_ID[lane],
                "case_id": request_case["case_id"],
                "input_sha256": request["case_sha256"],
                "status": "ok",
                "payload": payload,
                "materialization": materialization,
                "coverage": {
                    "phase_targets": [],
                    "diagnostics": [],
                    "rules": [],
                    "safety_cases": [],
                    "snapshots": [],
                },
                "gaps": [],
            }
            stdout = canonical(wire)
            audit = {
                "audit_protocol": "shared-audit-v1",
                "evaluator_id": coordinator.WIRE_ID[lane],
                "read_paths": [],
                "import_names": [],
                "import_paths": [],
                "write_attempts": [],
                "blocked_events": [],
                "network_attempts": [],
                "author_stdout_bytes": 0,
                "author_stderr_bytes": 0,
            }
            stderr = canonical(audit)
            source_binding = source_binding_list[lane]
            outer = {
                "protocol": coordinator.WIRE_PROTOCOL,
                "evaluator_id": coordinator.EVALUATOR_ID[lane],
                "source_profile": source_binding["source_profile"],
                "author_origin": source_binding["author_origin"],
                "case_id": request_case["case_id"],
                "input_sha256": request["case_sha256"],
                "source_closure_sha256": source_binding["source_closure_sha256"],
                "case_manifest_sha256": case_manifest,
                "execution_status": "ok",
                "result": wire,
                "audit": audit,
                "result_sha256": sha256(stdout),
                "stdout_sha256": sha256(stdout),
                "stderr_sha256": sha256(stderr),
                "raw_stdout_b64": base64.b64encode(stdout).decode(),
                "raw_stderr_b64": base64.b64encode(stderr).decode(),
                "exit_code": 0,
            }
            receipt_raw = canonical(outer)
            relative = f"receipts/{request['request_id'].replace('/', '_')}-{lane}.json"
            receipt_path = tmp_path / relative
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_bytes(receipt_raw)
            receipt_rows.append(
                {
                    "request_id": request["request_id"],
                    "lane": lane,
                    "receipt": {"path": relative, "sha256": sha256(receipt_raw)},
                }
            )
    receipt_set: dict[str, Any] = {
        "format": coordinator.RECEIPT_SET_FORMAT,
        "request_plan_sha256": plan_sha,
        "receipts": receipt_rows,
    }
    receipt_set_raw = canonical(coordinator._seal_object(receipt_set, "receipt_set_sha256"))
    return plan_raw, receipt_set_raw, events, {
        "source_entry": source_entry,
        "source_bundle_raw": source_bundle_raw,
        "source_inventory": synthetic_inventory,
    }


def test_equal_materialization_precedes_checker_and_partial_coverage_never_freezes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_raw, receipt_set_raw, events, _ = _build_synthetic_inputs(tmp_path, monkeypatch)
    review_raw = coordinator.consume_receipts(plan_raw, receipt_set_raw, repository_root=tmp_path)
    review = json.loads(review_raw)

    assert events == ["agreement", "stage2"]
    assert review["status"] == "blocked"
    assert "frozen_corpus" not in review
    assert review["blockers"][-1]["code"] == "MUTATION_KIND_COVERAGE_INCOMPLETE"
    assert review["cases"][0]["status"] == "ELIGIBLE"
    assert review["cases"][0]["source_case_id"] == "source/case"
    assert review["observed_coverage"]["other_observed_coverage"] == "NOT_OBSERVED"
    assert review["coverage_declarations"] == json.loads(plan_raw)["coverage_declarations"]
    provenance = review["cases"][0]["reference_materialization_provenance"]
    for lane in ("full_spec", "agent_view"):
        fields = provenance[lane]["materialization_fields"]
        assert fields["base_plan"]["request_id"] == "source/case/aux/literal_plan_base"
        assert fields["base_plan"]["value_source"] == "derive_phase_payload"
        assert fields["submitted_plan"]["request_id"] == "source/case"
        assert fields["submitted_plan"]["value_source"] == "literal_case_input"
        assert fields["selected_submitted_atomic"]["json_pointer"].endswith("[0]")


def test_invalid_candidate_reroute_keeps_literal_and_public_context_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_raw, receipt_set_raw, _, _ = _build_synthetic_inputs(
        tmp_path, monkeypatch, stage2_status="INELIGIBLE"
    )
    review = json.loads(
        coordinator.consume_receipts(plan_raw, receipt_set_raw, repository_root=tmp_path)
    )

    assert review["status"] == "blocked"
    assert "frozen_corpus" not in review
    assert len(review["reroute_requests"]) == 1
    route = json.loads(base64.b64decode(review["reroute_requests"][0]["case_b64"]))
    original = json.loads(base64.b64decode(review["cases"][0]["new_safety_case_b64"]))
    assert {key: route[key] for key in route if key not in {"case_id", "operation"}} == {
        key: original[key] for key in original if key not in {"case_id", "operation"}
    }
    assert route["stimulus"]["submitted_plan"] == original["stimulus"]["submitted_plan"]
    assert route["case_id"] == "v4sd/source/case/validate_submission"
    assert review["cases"][0]["routing_status"] == "validation_reroute_blocked"


def test_atomic_literal_reroute_lineage_binds_full_plan_and_preserves_original_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_raw, receipt_set_raw, _, _ = _build_synthetic_inputs(
        tmp_path,
        monkeypatch,
        stage2_status="INELIGIBLE",
        atomic_literal=True,
    )
    review = json.loads(
        coordinator.consume_receipts(plan_raw, receipt_set_raw, repository_root=tmp_path)
    )

    assert review["status"] == "blocked"
    assert "frozen_corpus" not in review
    assert len(review["reroute_requests"]) == 1, review
    route_request = review["reroute_requests"][0]
    route_case = json.loads(base64.b64decode(route_request["case_b64"]))
    original_safety_case = json.loads(
        base64.b64decode(review["cases"][0]["new_safety_case_b64"])
    )
    lineage = review["lineage"][0]
    original_stimulus_sha = sha256(canonical(original_safety_case["stimulus"]))
    route_stimulus_sha = sha256(canonical(route_case["stimulus"]))

    assert route_case["operation"] == "validate_submission"
    assert isinstance(original_safety_case["stimulus"]["submitted_plan"], dict)
    assert isinstance(route_case["stimulus"]["submitted_plan"], list)
    assert route_case["stimulus"]["submitted_plan"] == [
        _account_row("acct_demo"),
        _account_row("acct_extra"),
    ]
    assert lineage["validation_projection_kind"] == (
        "atomic_literal_to_literal_full_plan_array"
    )
    assert lineage["destination_case_sha256"] == route_request["case_sha256"]
    assert lineage["destination_stimulus_sha256"] == route_stimulus_sha
    assert lineage["validation_route_stimulus_sha256"] == route_stimulus_sha
    assert lineage["original_safety_stimulus_sha256"] == original_stimulus_sha
    assert original_stimulus_sha != route_stimulus_sha


def test_unknown_and_disagreement_are_retained_without_freezing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_raw, receipt_set_raw, _, _ = _build_synthetic_inputs(
        tmp_path / "unknown", monkeypatch, stage2_status="UNKNOWN"
    )
    unknown = json.loads(
        coordinator.consume_receipts(
            plan_raw, receipt_set_raw, repository_root=tmp_path / "unknown"
        )
    )
    assert unknown["status"] == "blocked"
    assert "frozen_corpus" not in unknown
    assert unknown["cases"][0]["status"] == "UNKNOWN"
    assert unknown["cases"][0]["source_case_id"] == "source/case"
    assert unknown["preserved_v3_case_index"][0]["source_case_id"] == "source/case"

    mismatch_root = tmp_path / "mismatch"
    plan_raw, receipt_set_raw, events, _ = _build_synthetic_inputs(
        mismatch_root, monkeypatch, mismatch=True
    )
    disagreement = json.loads(
        coordinator.consume_receipts(plan_raw, receipt_set_raw, repository_root=mismatch_root)
    )
    assert events[-1] == "agreement"
    assert "stage2" not in events
    assert disagreement["status"] == "blocked"
    assert "frozen_corpus" not in disagreement
    assert disagreement["cases"][0]["status"] == "UNKNOWN"
    assert disagreement["cases"][0]["code"] == "MATERIALIZATION_DISAGREEMENT"
    assert disagreement["cases"][0]["source_case_id"] == "source/case"


def test_missing_receipt_is_unknown_and_changed_origin_binding_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_raw, receipt_set_raw, _, _ = _build_synthetic_inputs(
        tmp_path,
        monkeypatch,
        omit_receipt=("source/case/aux/literal_plan_base", "agent_view"),
    )
    missing = json.loads(
        coordinator.consume_receipts(plan_raw, receipt_set_raw, repository_root=tmp_path)
    )
    assert missing["status"] == "blocked"
    assert "frozen_corpus" not in missing
    assert missing["cases"][0]["status"] == "UNKNOWN"
    assert any(row["code"] == "RECEIPT_MISSING" for row in missing["blockers"])

    stale_readback = {
        "qualification_status": "QUALIFIED_ELIGIBLE",
        "evaluator_id": "full_spec_blind",
        "source_profile": "bounded_full_spec_v1",
        "author_origin": "fresh_bounded_full_spec_author_v1",
        "chain_sha256": "1" * 64,
        "chain_review_sha256": "2" * 64,
        "current_capture_manifest_sha256": "0" * 64,
        "current_capture_logical_sha256": "b" * 64,
        "current_source_map_sha256": "c" * 64,
        "input_source_map_sha256": "d" * 64,
        "current_capture_manifest_path": "reports/capture/manifest.json",
        "source_isolation": "UNKNOWN",
        "authors": [{"task": "a", "session_id": "s1"}, {"task": "b", "session_id": "s2"}],
        "prefix_session_ids": ["s1", "s2"],
        "coverage_limits": ["bounded capture only"],
    }
    with pytest.raises(coordinator.CoordinatorInputError, match="misbound"):
        coordinator._validate_qualification_readback(
            lane="full_spec",
            qualification={
                "kind": "author_origin_chain_v1",
                "chain": {"path": "reports/v5/conformance/chain.json", "sha256": "1" * 64},
                "chain_review": {
                    "path": "reports/v5/conformance/review.json",
                    "sha256": "2" * 64,
                },
            },
            provenance=stale_readback,
            capture_raw_sha="a" * 64,
            capture_logical_sha="b" * 64,
            capture_manifest_path="reports/capture/manifest.json",
            source_map_sha="c" * 64,
        )


def test_missing_and_false_origin_consumers_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    args = {
        "lane": "full_spec",
        "qualification": {
            "kind": "author_origin_chain_v1",
            "chain": {"path": "reports/v5/conformance/chain.json", "sha256": "1" * 64},
            "chain_review": {"path": "reports/v5/conformance/review.json", "sha256": "2" * 64},
        },
        "source_profile": "bounded_full_spec_v1",
        "author_origin": "fresh_bounded_full_spec_author_v1",
        "closure_sha": "e" * 64,
        "capture_raw_sha": "a" * 64,
        "capture_logical_sha": "b" * 64,
        "capture_manifest_path": ROOT / "reports/capture/manifest.json",
        "captured_source_files_count": 14,
        "runtime_manifest": {},
        "repository_root": ROOT,
    }
    monkeypatch.delattr(validation, "_validate_shared_qualification")
    with pytest.raises(coordinator.CoordinatorInputError, match="unavailable"):
        coordinator._validate_source_origin_chain(**args)

    monkeypatch.setattr(
        validation, "_validate_shared_qualification", lambda *_args, **_kwargs: False, raising=False
    )
    with pytest.raises(coordinator.CoordinatorInputError, match="not qualified"):
        coordinator._validate_source_origin_chain(**args)


@pytest.mark.parametrize("tamper", ["result_echo", "audit_echo", "stderr_invalid_json"])
def test_outer_receipt_raw_streams_and_audit_must_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    plan_raw, receipt_set_raw, events, _ = _build_synthetic_inputs(tmp_path, monkeypatch)
    receipt_set = json.loads(receipt_set_raw)
    row = receipt_set["receipts"][0]
    receipt_path = tmp_path / row["receipt"]["path"]
    outer = json.loads(receipt_path.read_bytes())
    if tamper == "result_echo":
        outer["result"]["status"] = "unsupported"
    elif tamper == "audit_echo":
        outer["audit"]["read_paths"] = ["/synthetic/unbound/read"]
    else:
        stderr = b"not-json"
        outer["raw_stderr_b64"] = base64.b64encode(stderr).decode()
        outer["stderr_sha256"] = sha256(stderr)
    receipt_raw = canonical(outer)
    receipt_path.write_bytes(receipt_raw)
    row["receipt"]["sha256"] = sha256(receipt_raw)
    tampered_receipt_set_raw = canonical(
        coordinator._seal_object(receipt_set, "receipt_set_sha256")
    )

    review = json.loads(
        coordinator.consume_receipts(plan_raw, tampered_receipt_set_raw, repository_root=tmp_path)
    )
    assert review["status"] == "blocked"
    assert "frozen_corpus" not in review
    assert events == []


def test_fixed_v3_public_inventory_is_complete_and_identity_only() -> None:
    inventory = coordinator._approved_v3_source_inventory(ROOT)

    assert len(inventory["source_case_index"]) == 1702
    assert len(inventory["safety_case_index"]) == 497
    assert inventory["coverage_declarations"]["trace_case_count"] == 60
    assert len(inventory["phase_target_tuple_inventory"]) == 3435
    assert len({
        (
            row["suite_id"],
            row["episode_id"],
            row["phase_index"],
            row["mutation_kind"],
            row["target_entity_id"],
        )
        for row in inventory["phase_target_tuple_inventory"]
    }) == 3435
    assert len(inventory["coverage_declarations"]["diagnostic_categories"]) == 43
    assert len(inventory["coverage_declarations"]["rule_ids"]) == 26
    assert len(inventory["coverage_declarations"]["policy_card_anchor_labels"]) == 39
    assert len(inventory["coverage_declarations"]["family_ids"]) == 12
    assert len(inventory["coverage_declarations"]["snapshot_cells"]) == 6
    assert len(inventory["coverage_declarations"]["global_safety_truth_pairs"]) == 4


def test_source_bundle_rejects_index_omission_duplicate_and_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, _, extras = _build_synthetic_inputs(tmp_path, monkeypatch)
    original = json.loads(extras["source_bundle_raw"])
    inventory = extras["source_inventory"]
    for mutation in (
        "index_omission",
        "index_duplicate",
        "case_omission",
        "case_duplicate",
        "case_substitution",
    ):
        bundle = json.loads(extras["source_bundle_raw"])
        if mutation == "index_omission":
            bundle["source_case_index"] = []
        elif mutation == "index_duplicate":
            bundle["source_case_index"].append(bundle["source_case_index"][0])
        elif mutation == "case_omission":
            bundle["cases"] = []
        elif mutation == "case_duplicate":
            bundle["cases"].append(bundle["cases"][0])
        else:
            bundle["cases"][0]["case_sha256"] = "0" * 64
        with pytest.raises(coordinator.CoordinatorInputError):
            coordinator._load_source_bundle(
                canonical(bundle),
                tmp_path,
                source_inventory=inventory,
            )
    assert original["source_case_index"] == inventory["source_case_index"]


def test_receipt_consumption_rechecks_embedded_release_and_coordinator_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "artifact"
    plan_raw, receipt_set_raw, events, extras = _build_synthetic_inputs(artifact_root, monkeypatch)
    source_ref = extras["source_inventory"]["preserved_artifacts"][0]
    (artifact_root / source_ref["path"]).write_bytes(b"changed preserved source bytes")
    stale_source = json.loads(
        coordinator.consume_receipts(plan_raw, receipt_set_raw, repository_root=artifact_root)
    )
    assert stale_source["status"] == "blocked"
    assert stale_source["blockers"][0]["code"] == "REQUEST_PLAN_BINDING_STALE"
    assert "frozen_corpus" not in stale_source
    assert events == []

    artifact_root = tmp_path / "public-artifact"
    plan_raw, receipt_set_raw, events, _ = _build_synthetic_inputs(artifact_root, monkeypatch)
    plan = json.loads(plan_raw)
    packet_ref = plan["source_cases"][0]["v8_public_input_artifacts"]["packet"]
    (artifact_root / packet_ref["path"]).write_bytes(b"changed public packet bytes")
    stale_artifact = json.loads(
        coordinator.consume_receipts(plan_raw, receipt_set_raw, repository_root=artifact_root)
    )
    assert stale_artifact["status"] == "blocked"
    assert stale_artifact["blockers"][0]["code"] == "REQUEST_PLAN_BINDING_STALE"
    assert "frozen_corpus" not in stale_artifact
    assert events == []

    coordinator_root = tmp_path / "coordinator"
    plan_raw, receipt_set_raw, events, _ = _build_synthetic_inputs(coordinator_root, monkeypatch)
    plan = json.loads(plan_raw)
    plan["coordinator_sha256"] = "0" * 64
    tampered_plan_raw = canonical(coordinator._seal_object(plan, "request_plan_sha256"))
    receipt_set = json.loads(receipt_set_raw)
    receipt_set["request_plan_sha256"] = json.loads(tampered_plan_raw)["request_plan_sha256"]
    tampered_receipt_set_raw = canonical(
        coordinator._seal_object(receipt_set, "receipt_set_sha256")
    )
    stale_coordinator = json.loads(
        coordinator.consume_receipts(
            tampered_plan_raw, tampered_receipt_set_raw, repository_root=coordinator_root
        )
    )
    assert stale_coordinator["status"] == "blocked"
    assert stale_coordinator["blockers"][0]["code"] == "REQUEST_PLAN_BINDING_STALE"
    assert "frozen_corpus" not in stale_coordinator
    assert events == []
