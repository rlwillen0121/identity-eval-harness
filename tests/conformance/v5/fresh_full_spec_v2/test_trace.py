"""Focused replay tests for public envelope availability and trace inputs."""

from __future__ import annotations

import copy

from .apply import compose_phase2_context_for_scoring
from .common import SCHEMA_SHA256, build_public_world
from .submission import _derive_gold
from .test_submission import _seed
from .trace import _cosign_requirements, _phase_case
from .worker import evaluate_shared_input


def _trace_case(phase: int, events: list[dict]) -> dict:
    seed = _seed(phase)
    case = {key: copy.deepcopy(value) for key, value in seed.items() if key != "_gold"}
    case["operation"] = "score_trace"
    case["stimulus"] = {"mode": "COUPLED", "events": events}
    return case


def test_phase_one_trace_uses_six_key_projection_without_future_fields() -> None:
    result = evaluate_shared_input(_trace_case(1, []))

    assert result["status"] == "underdefined"
    assert [gap["code"] for gap in result["gaps"]] == ["UNDERDEFINED_PUBLIC_INPUT"]
    assert result["gaps"][0]["path"] == "/SPEC-v5.md/4.1/G-SRR@first_stage"
    metrics = result["payload"]["metrics"]
    assert len(metrics) == 11
    assert metrics["G-SRR@first_stage"] is None
    assert metrics["repair_lift"] is None
    assert result["payload"]["snapshot_derivations"] == []


def test_phase_two_call_before_phase_one_commit_does_not_require_open_p2_data() -> None:
    result = evaluate_shared_input(
        _trace_case(
            2,
            [
                {
                    "seq": 1,
                    "tool": "query_sources",
                    "args": {"phase_index": 2, "slice_ids": ["unopened"]},
                }
            ],
        )
    )

    assert result["status"] == "underdefined"
    assert [gap["code"] for gap in result["gaps"]] == ["UNDERDEFINED_PUBLIC_INPUT"]
    assert result["gaps"][0]["path"] == "/SPEC-v5.md/4.1/G-SRR@first_stage"


def test_phase_two_call_after_p1_commit_requires_ten_key_projection() -> None:
    case = _trace_case(1, [])
    episode = case["public_episode"]
    query_ids = episode["q1_slice_ids_p1"] + episode["q2_slice_ids_p1"]
    gold = _seed(1)["_gold"]
    case["stimulus"]["events"] = [
        {
            "seq": 1,
            "tool": "query_sources",
            "args": {"phase_index": 1, "slice_ids": query_ids},
        },
        {
            "seq": 2,
            "tool": "stage_phase_mutations",
            "args": {
                "phase_index": 1,
                "stage_id": "stg_p1_commit",
                "plan_source": {"submitted_plan": gold},
            },
        },
        {
            "seq": 3,
            "tool": "commit_phase_mutations",
            "args": {"phase_index": 1, "stage_id": "stg_p1_commit"},
        },
        {
            "seq": 4,
            "tool": "query_sources",
            "args": {"phase_index": 2, "slice_ids": ["p2_slice"]},
        },
    ]

    result = evaluate_shared_input(case)

    assert result["status"] == "underdefined"
    assert result["gaps"][0]["code"] == "UNMATERIALIZABLE_INPUT"
    assert result["gaps"][0]["path"] == "/public_episode/phase2_base_manifest"
    assert len(result["payload"]["metrics"]) == 11
    assert all(value is None for value in result["payload"]["metrics"].values())


def test_coupled_p2_recipe_uses_account_state_from_non_gold_p1() -> None:
    seed = _seed(2)
    case = {key: copy.deepcopy(value) for key, value in seed.items() if key != "_gold"}
    p1_case = _phase_case(case, 1)
    p2_case = _phase_case(case, 2)
    p1_world = build_public_world(p1_case)
    p2_world = build_public_world(p2_case)
    p1_gold, _ = _derive_gold(p1_case, p1_world)
    p1_actual = copy.deepcopy(p1_gold)
    changed_account = next(
        row
        for row in p1_actual
        if row["mutation_kind"] == "ACCOUNT_RECON_MUTATION"
        and row["account_action"] == "KEEP_ACTIVE"
        and row["retained_grant_ids"]
    )
    changed_account["retained_grant_ids"] = changed_account["retained_grant_ids"][1:]
    coupled_p2_world = compose_phase2_context_for_scoring(p2_world, p1_world, p1_actual, p1_gold)
    coupled_p2_gold, _ = _derive_gold(coupled_p2_world.case, coupled_p2_world)

    p1_slices = (
        case["public_episode"]["q1_slice_ids_p1"] + case["public_episode"]["q2_slice_ids_p1"]
    )
    p2_slices = (
        case["public_episode"]["q1_slice_ids_p2"] + case["public_episode"]["q2_slice_ids_p2"]
    )
    events = [
        {
            "seq": 1,
            "tool": "query_sources",
            "args": {"phase_index": 1, "slice_ids": p1_slices},
        },
        {
            "seq": 2,
            "tool": "stage_phase_mutations",
            "args": {
                "phase_index": 1,
                "stage_id": "stg_p1_account_change",
                "plan_source": {"submitted_plan": p1_actual},
            },
        },
        {
            "seq": 3,
            "tool": "commit_phase_mutations",
            "args": {"phase_index": 1, "stage_id": "stg_p1_account_change"},
        },
        {
            "seq": 4,
            "tool": "query_sources",
            "args": {"phase_index": 2, "slice_ids": p2_slices},
        },
        {
            "seq": 5,
            "tool": "stage_phase_mutations",
            "args": {
                "phase_index": 2,
                "stage_id": "stg_p2_coupled_account",
                "plan_source": {
                    "recipe": {
                        "candidate_source": "independent_base",
                        "base_scope": "phase_plan",
                        "patches": [],
                    }
                },
            },
        },
    ]
    seq = 6
    for scope, target, actor in sorted(_cosign_requirements(coupled_p2_gold)):
        events.append(
            {
                "seq": seq,
                "tool": "request_cosign",
                "args": {
                    "phase_index": 2,
                    "stage_id": "stg_p2_coupled_account",
                    "scope_kind": scope,
                    "target_entity_id": target,
                    "actor_account_id": actor,
                },
            }
        )
        seq += 1
    events.append(
        {
            "seq": seq,
            "tool": "commit_phase_mutations",
            "args": {"phase_index": 2, "stage_id": "stg_p2_coupled_account"},
        }
    )
    case["operation"] = "score_trace"
    case["schema_sha256"] = SCHEMA_SHA256
    case["stimulus"] = {"mode": "COUPLED", "events": events}

    result = evaluate_shared_input(case)

    assert result["status"] == "underdefined"
    assert result["gaps"][0]["path"] == "/SPEC-v5.md/4.1/G-SRR@first_stage"
    assert not any("current_etag" in gap["path"] for gap in result["gaps"])
    p2_materialization = next(
        plan for plan in result["materialization"]["event_plans"] if plan["phase_index"] == 2
    )
    assert p2_materialization["base_plan"] == coupled_p2_gold
    assert p2_materialization["submitted_plan"] == coupled_p2_gold
    assert {row["phase_index"] for row in result["coverage"]["snapshots"]} == {1, 2}
    assert result["payload"]["metrics"]["G-PROP"] == 1, [
        {
            "phase": row["phase_index"],
            "boundary": row["boundary"],
            "flags": [row[f"stage{stage}_ok"] for stage in range(1, 6)],
            "diagnostics": row["diagnostics"],
        }
        for row in result["payload"]["snapshot_derivations"]
    ]
    assert any(
        row["phase_index"] == 2
        and row["boundary"] == "COMMIT"
        and row["mutations"] == coupled_p2_gold
        for row in result["payload"]["snapshot_derivations"]
    )
