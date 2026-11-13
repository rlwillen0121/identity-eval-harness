"""Focused tests for only this lane's independent public-input reducer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from .common import PROTOCOL, SCHEMA_SHA256, canonical_json, sha256_bytes
from .worker import evaluate_shared_input

ROOT = Path(__file__).resolve().parents[4]
SUITES = ("v5_dev", "v5_lite", "v5_core", "v5_full")


def _case(suite: str, row: dict[str, Any], card: str, phase: int) -> dict[str, Any]:
    if phase == 1:
        keys = (
            "episode_id",
            "policy_card",
            "phase1_manifest",
            "phase1_slices",
            "q1_slice_ids_p1",
            "q2_slice_ids_p1",
        )
    else:
        keys = (
            "episode_id",
            "policy_card",
            "phase1_manifest",
            "phase1_slices",
            "q1_slice_ids_p1",
            "q2_slice_ids_p1",
            "phase2_base_manifest",
            "phase2_slices",
            "q1_slice_ids_p2",
            "q2_slice_ids_p2",
        )
    public_episode = {key: row[key] for key in keys}
    return {
        "protocol": PROTOCOL,
        "case_id": f"{suite}:{row['episode_id']}:p{phase}",
        "operation": "derive_phase",
        "suite_id": suite,
        "phase_index": phase,
        "public_episode": public_episode,
        "rendered_card": card,
        "schema_sha256": SCHEMA_SHA256,
        "stimulus": {},
    }


def _public_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for suite in SUITES:
        directory = ROOT / "reports/v5/conformance/inputs/v6" / suite
        card = (directory / "rendered-card.txt").read_text(encoding="utf-8")
        with (directory / "selected-episodes.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                cases.append(_case(suite, row, card, 1))
                cases.append(_case(suite, row, card, 2))
    return cases


@pytest.mark.parametrize("case", _public_cases(), ids=lambda c: c["case_id"])
def test_phase_derivation_materializes_complete_mutations(case: dict[str, Any]) -> None:
    result = evaluate_shared_input(case)
    assert result["status"] == "ok"
    assert result["protocol"] == PROTOCOL
    assert result["case_id"] == case["case_id"]
    assert result["input_sha256"] == sha256_bytes(canonical_json(case))
    assert result["materialization"] is None
    assert set(result["coverage"]) == {
        "phase_targets",
        "diagnostics",
        "rules",
        "safety_cases",
        "snapshots",
    }
    if result["status"] == "ok":
        assert not result["gaps"]
    else:
        assert result["gaps"]
    assert set(result["payload"]) == {"mutations", "policy_card_anchor_map"}
    assert len(result["payload"]["policy_card_anchor_map"]) == 39
    assert result["payload"]["mutations"] == sorted(
        result["payload"]["mutations"],
        key=lambda m: (m["mutation_kind"], m["target_entity_id"]),
    )
    target_keys = {
        (x["mutation_kind"], x["target_entity_id"]) for x in result["coverage"]["phase_targets"]
    }
    mutation_keys = {
        (x["mutation_kind"], x["target_entity_id"]) for x in result["payload"]["mutations"]
    }
    assert mutation_keys == target_keys
    for mutation in result["payload"]["mutations"]:
        assert mutation["if_match"] == sorted(set(mutation["if_match"]))
        assert mutation["cite"] == sorted(set(mutation["cite"]))
    assert canonical_json(case)
