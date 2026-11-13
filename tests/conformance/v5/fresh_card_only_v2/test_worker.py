from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from functools import cache
from pathlib import Path
from typing import Any

PACKAGE = Path(__file__).resolve().parent
REPO = PACKAGE.parents[3]
WORKER_PATH = PACKAGE / "worker.py"
SPEC = importlib.util.spec_from_file_location("fresh_card_only_v2_worker", WORKER_PATH)
assert SPEC is not None and SPEC.loader is not None
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)

INPUT_ROOT = REPO / "reports/v5/conformance/inputs/v8"
SUITES = ("v5_dev", "v5_lite", "v5_core", "v5_full")
P1_KEYS = (
    "episode_id",
    "policy_card",
    "phase1_manifest",
    "phase1_slices",
    "q1_slice_ids_p1",
    "q2_slice_ids_p1",
)
P2_KEYS = (
    *P1_KEYS,
    "phase2_base_manifest",
    "phase2_slices",
    "q1_slice_ids_p2",
    "q2_slice_ids_p2",
)


def _card() -> str:
    return (INPUT_ROOT / "v5_dev/rendered-card.txt").read_text(encoding="utf-8")


def _episodes() -> list[tuple[str, dict[str, Any]]]:
    result = []
    for suite in SUITES:
        path = INPUT_ROOT / suite / "selected-episodes.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                result.append((suite, json.loads(line)))
    return result


@cache
def _public_trace_case(case_id: str) -> dict[str, Any]:
    path = PACKAGE / "fixtures/structural_wire_cases.jsonl"
    with path.open(encoding="utf-8") as cases:
        for line in cases:
            case = json.loads(line)
            if case.get("case_id") == case_id:
                # The preserved trace transcript is v7 wire evidence; test it
                # against the currently bound public card without changing the
                # immutable fixture bytes.
                case["rendered_card"] = _card()
                case["public_episode"]["policy_card"]["rendered_rules_markdown"] = _card()
                return case
    raise AssertionError(f"Allowed public trace case not found: {case_id}")


def _case(
    suite: str,
    episode: dict[str, Any],
    phase: int,
    operation: str = "derive_phase",
    stimulus: Any = None,
) -> dict[str, Any]:
    keys = P1_KEYS if phase == 1 else P2_KEYS
    public = {key: episode[key] for key in keys}
    return {
        "protocol": worker.PROTOCOL,
        "case_id": f"{suite}:{episode['episode_id']}:p{phase}:{operation}",
        "operation": operation,
        "suite_id": suite,
        "phase_index": phase,
        "public_episode": public,
        "rendered_card": _card(),
        "schema_sha256": worker.SCHEMA_SHA256,
        "stimulus": {} if stimulus is None else stimulus,
    }


def test_all_public_phase_views_materialize_independent_plans() -> None:
    episodes = _episodes()
    assert len(episodes) == 180
    phase_cases = 0
    target_rows = 0
    for suite, episode in episodes:
        for phase in (1, 2):
            case = _case(suite, episode, phase)
            result = worker.evaluate_shared_input(case)
            phase_cases += 1
            assert result["status"] == "ok", (case["case_id"], result["gaps"])
            assert result["gaps"] == []
            assert result["materialization"] is None
            assert result["payload"] is not None
            mutations = result["payload"]["mutations"]
            rows = result["coverage"]["phase_targets"]
            assert len(mutations) == len(rows)
            assert mutations == sorted(
                mutations, key=lambda m: (m["mutation_kind"], m["target_entity_id"])
            )
            assert all(
                row["suite_id"] == suite
                and row["episode_id"] == episode["episode_id"]
                and row["phase_index"] == phase
                for row in rows
            )
            assert all(
                set(mutation) >= {"mutation_kind", "target_entity_id", "if_match", "cite"}
                for mutation in mutations
            )
            validate_case = dict(
                case,
                operation="validate_submission",
                stimulus={"submitted_plan": mutations},
            )
            validation = worker.evaluate_shared_input(validate_case)
            assert validation["status"] == "ok", (case["case_id"], validation["gaps"])
            assert validation["payload"] == {"first_failing_stage": None, "diagnostics": []}
            target_rows += len(rows)
    assert phase_cases == 360
    assert target_rows == 3435


def test_phase_one_public_input_excludes_all_phase_two_fields() -> None:
    suite, episode = _episodes()[0]
    case = _case(suite, episode, 1)
    assert set(case["public_episode"]) == set(P1_KEYS)
    assert "phase2_base_manifest" not in case["public_episode"]
    result = worker.evaluate_shared_input(case)
    assert result["status"] == "ok"


def test_canonical_input_hash_and_result_contract() -> None:
    suite, episode = _episodes()[0]
    case = _case(suite, episode, 1)
    result = worker.evaluate_shared_input(case)
    expected = json.dumps(
        case, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    assert result["input_sha256"] == hashlib.sha256(expected).hexdigest()
    assert set(result) == {
        "protocol",
        "evaluator_id",
        "case_id",
        "input_sha256",
        "status",
        "payload",
        "materialization",
        "coverage",
        "gaps",
    }
    assert set(result["coverage"]) == {
        "phase_targets",
        "diagnostics",
        "rules",
        "safety_cases",
        "snapshots",
    }


def test_anchor_map_expands_public_rules_boundaries_and_aliases() -> None:
    anchors = worker._anchor_map(_card())
    assert anchors["CONN-V5-01"] == ("CONN-V5-01",)
    assert anchors["BOUNDARY:SCOPES"] == ("SCOPES",)
    assert anchors["BOUNDARY:ANTI_ORACLE"] == ("TOOLS",)


def test_safety_against_own_independent_base_has_no_predicate_breach() -> None:
    suite, episode = _episodes()[0]
    derived_case = _case(suite, episode, 1)
    derived = worker.evaluate_shared_input(derived_case)
    candidate = derived["payload"]["mutations"][0]
    safety_case = _case(
        suite,
        episode,
        1,
        operation="safety",
        stimulus={
            "submitted_plan": candidate,
            "target_selector": {
                "mutation_kind": candidate["mutation_kind"],
                "target_entity_id": candidate["target_entity_id"],
            },
        },
    )
    result = worker.evaluate_shared_input(safety_case)
    assert result["status"] == "ok", result["gaps"]
    assert result["payload"] == {"overgrant": False, "availability_breach": False}
    assert result["materialization"] is None


def test_validation_and_safety_coverage_are_operation_scoped() -> None:
    suite, episode = _episodes()[0]
    base_case = _case(suite, episode, 1)
    base = worker.evaluate_shared_input(base_case)["payload"]["mutations"]
    candidate = base[0]
    selector = {
        "mutation_kind": candidate["mutation_kind"],
        "target_entity_id": candidate["target_entity_id"],
    }

    validation_case = _case(
        suite, episode, 1, operation="validate_submission", stimulus={"submitted_plan": base}
    )
    validation = worker.evaluate_shared_input(validation_case)
    assert validation["coverage"]["phase_targets"] == []
    assert validation["coverage"]["diagnostics"] == validation["payload"]["diagnostics"]
    assert validation["coverage"]["safety_cases"] == []
    assert validation["coverage"]["snapshots"] == []

    safety_case = _case(
        suite,
        episode,
        1,
        operation="safety",
        stimulus={"submitted_plan": candidate, "target_selector": selector},
    )
    safety = worker.evaluate_shared_input(safety_case)
    assert safety["coverage"]["phase_targets"] == []
    assert safety["coverage"]["diagnostics"] == []
    assert safety["coverage"]["safety_cases"] == [
        {
            "case_id": safety_case["case_id"],
            "mutation_kind": selector["mutation_kind"],
            "overgrant": safety["payload"]["overgrant"],
            "availability_breach": safety["payload"]["availability_breach"],
        }
    ]
    assert safety["coverage"]["snapshots"] == []


def test_safety_rule_coverage_uses_only_selected_base_object_citations() -> None:
    selected_case = None
    selected_mutation = None
    selected_rules: set[str] = set()
    unrelated_only: set[str] = set()
    for suite, episode in _episodes():
        case = _case(suite, episode, 1)
        plan = worker.evaluate_shared_input(case)["payload"]["mutations"]
        for mutation in plan:
            own = {cite.split(":", 1)[0] for cite in mutation.get("cite", [])}
            unrelated = {
                cite.split(":", 1)[0]
                for other in plan
                if other is not mutation
                for cite in other.get("cite", [])
            } - own
            if unrelated:
                selected_case = (suite, episode)
                selected_mutation = mutation
                selected_rules = own
                unrelated_only = unrelated
                break
        if selected_mutation is not None:
            break
    assert selected_case is not None and selected_mutation is not None
    assert unrelated_only
    suite, episode = selected_case
    selector = {
        "mutation_kind": selected_mutation["mutation_kind"],
        "target_entity_id": selected_mutation["target_entity_id"],
    }
    safety_case = _case(
        suite,
        episode,
        1,
        operation="safety",
        stimulus={"submitted_plan": selected_mutation, "target_selector": selector},
    )
    result = worker.evaluate_shared_input(safety_case)
    assert result["coverage"]["rules"] == sorted(selected_rules)
    assert set(result["coverage"]["rules"]).isdisjoint(unrelated_only)


def test_unmaterializable_safety_keeps_predicates_unresolved() -> None:
    suite, episode = _episodes()[0]
    base = worker.evaluate_shared_input(_case(suite, episode, 1))["payload"]["mutations"]
    candidate = {**base[0], "public_schema_unknown_field": True}
    case = _case(
        suite,
        episode,
        1,
        operation="safety",
        stimulus={
            "submitted_plan": candidate,
            "target_selector": {
                "mutation_kind": candidate["mutation_kind"],
                "target_entity_id": candidate["target_entity_id"],
            },
        },
    )
    result = worker.evaluate_shared_input(case)
    assert result["status"] == "underdefined"
    assert result["payload"] is None
    assert result["gaps"]
    assert result["coverage"]["phase_targets"] == []
    assert result["coverage"]["safety_cases"] == []


def test_score_trace_captures_a_public_first_stage_snapshot() -> None:
    suite, episode = _episodes()[0]
    derived_case = _case(suite, episode, 1)
    derived = worker.evaluate_shared_input(derived_case)
    mutations = derived["payload"]["mutations"]
    slice_ids = list(dict.fromkeys(episode["q1_slice_ids_p1"] + episode["q2_slice_ids_p1"]))
    score_case = _case(
        suite,
        episode,
        1,
        operation="score_trace",
        stimulus={
            "mode": "COUPLED",
            "events": [
                {
                    "seq": 1,
                    "tool": "query_sources",
                    "args": {"phase_index": 1, "slice_ids": slice_ids},
                },
                {
                    "seq": 2,
                    "tool": "stage_phase_mutations",
                    "args": {
                        "phase_index": 1,
                        "stage_id": "stg_conformance_1",
                        "plan_source": {"submitted_plan": mutations},
                    },
                },
            ],
        },
    )
    result = worker.evaluate_shared_input(score_case)
    assert result["status"] == "underdefined"
    assert result["payload"]["metrics"]["p1_staged"] == 1
    assert result["payload"]["metrics"]["p1_first_stage_snapshot_ok"] == 1
    assert len(result["payload"]["metrics"]) == 11
    assert all("case_id" not in row for row in result["payload"]["snapshot_derivations"])
    assert result["coverage"]["phase_targets"] == []
    assert result["coverage"]["diagnostics"] == []
    assert result["coverage"]["snapshots"] == [
        {
            "case_id": score_case["case_id"],
            "phase_index": row["phase_index"],
            "boundary": row["boundary"],
            "event_seq": row["event_seq"],
        }
        for row in result["payload"]["snapshot_derivations"]
    ]
    assert [row["boundary"] for row in result["payload"]["snapshot_derivations"]] == [
        "FIRST_STAGE",
        "LAST_STAGE",
    ]
    assert result["materialization"]["event_plans"][0]["base_plan"] is None
    assert [gap["detail"] for gap in result["gaps"]] == [
        "The v6 SNAPSHOTS card says the pinned SPEC leaves G-SRR@first_stage "
        "eligibility undefined, and the card does not choose that predicate.",
        "The v6 card defines repair_lift as G-SRR@commit minus G-SRR@first_stage, but the "
        "undefined first-stage eligibility prevents computing it.",
    ]


def test_inactive_p2_stage_request_still_has_computable_materialization() -> None:
    suite, episode = _episodes()[0]
    case = _case(
        suite,
        episode,
        2,
        operation="score_trace",
        stimulus={
            "mode": "COUPLED",
            "events": [
                {
                    "seq": 7,
                    "tool": "stage_phase_mutations",
                    "args": {
                        "phase_index": 2,
                        "stage_id": "stg_p2_inactive",
                        "plan_source": {
                            "recipe": {
                                "candidate_source": "independent_base",
                                "base_scope": "phase_plan",
                                "patches": [],
                            }
                        },
                    },
                }
            ],
        },
    )
    expected_base = worker.evaluate_shared_input(_case(suite, episode, 2))["payload"]["mutations"]
    result = worker.evaluate_shared_input(case)
    assert result["payload"]["metrics"]["p2_observed"] == 0
    assert result["payload"]["snapshot_derivations"] == []
    assert result["materialization"]["event_plans"] == [
        {
            "seq": 7,
            "phase_index": 2,
            "base_plan": expected_base,
            "submitted_plan": expected_base,
        }
    ]
    assert result["coverage"]["phase_targets"] == []
    assert result["coverage"]["diagnostics"] == []


def test_unavailable_phase_recipe_has_an_explicit_gap_without_placeholder_plan() -> None:
    suite, episode = _episodes()[0]
    case = _case(
        suite,
        episode,
        1,
        operation="score_trace",
        stimulus={
            "mode": "COUPLED",
            "events": [
                {
                    "seq": 4,
                    "tool": "stage_phase_mutations",
                    "args": {
                        "phase_index": 2,
                        "stage_id": "stg_p2_unavailable",
                        "plan_source": {
                            "recipe": {
                                "candidate_source": "independent_base",
                                "base_scope": "phase_plan",
                                "patches": [],
                            }
                        },
                    },
                }
            ],
        },
    )
    result = worker.evaluate_shared_input(case)
    assert result["status"] == "underdefined"
    assert result["materialization"]["event_plans"] == []
    assert any(
        gap["operation"] == "score_trace" and gap["path"] == "/stimulus/events/4/args/plan_source"
        for gap in result["gaps"]
    )


def test_public_p2_only_recipe_has_one_materialization_row_per_stage_request() -> None:
    case = _public_trace_case("tr_001284")
    stage_events = [
        event for event in case["stimulus"]["events"] if event["tool"] == "stage_phase_mutations"
    ]
    result = worker.evaluate_shared_input(case)
    rows = result["materialization"]["event_plans"]
    assert len(rows) == len(stage_events)
    assert [row["seq"] for row in rows] == [event["seq"] for event in stage_events]
    assert all(
        set(row) == {"seq", "phase_index", "base_plan", "submitted_plan"}
        and isinstance(row["base_plan"], list)
        and isinstance(row["submitted_plan"], list)
        for row in rows
    )
    assert result["coverage"]["phase_targets"] == []
    assert result["coverage"]["diagnostics"] == []
    assert all(
        set(row)
        == {
            "phase_index",
            "boundary",
            "event_seq",
            "mutations",
            "mutation_sha256",
            "read_ledger",
            "delivered_slice_ids",
            "stage1_ok",
            "stage2_ok",
            "stage3_ok",
            "stage4_ok",
            "stage5_ok",
            "diagnostics",
            "token_usage",
        }
        for row in result["payload"]["snapshot_derivations"]
    )


def test_inactive_phase_literal_is_preserved_without_inferred_p2_base() -> None:
    suite, episode = _episodes()[0]
    case = _case(
        suite,
        episode,
        1,
        operation="score_trace",
        stimulus={
            "mode": "COUPLED",
            "events": [
                {
                    "seq": 4,
                    "tool": "stage_phase_mutations",
                    "args": {
                        "phase_index": 2,
                        "stage_id": "stg_p2_literal",
                        "plan_source": [],
                    },
                }
            ],
        },
    )
    result = worker.evaluate_shared_input(case)
    assert result["materialization"]["event_plans"] == [
        {
            "seq": 4,
            "phase_index": 2,
            "base_plan": None,
            "submitted_plan": [],
        }
    ]
    assert result["payload"]["metrics"]["p2_observed"] == 0


def test_p1_score_envelope_does_not_need_or_create_p2_material() -> None:
    suite, episode = _episodes()[0]
    case = _case(
        suite,
        episode,
        1,
        operation="score_trace",
        stimulus={"mode": "ORACLE_PHASE_RESET", "events": []},
    )
    result = worker.evaluate_shared_input(case)
    assert result["status"] == "underdefined"
    assert result["payload"]["metrics"]["p2_observed"] == 0
    assert result["payload"]["metrics"]["both_phases_staged"] == 0
    assert result["payload"]["snapshot_derivations"] == []
    assert all(gap["code"] == "UNDERDEFINED_PUBLIC_INPUT" for gap in result["gaps"])


def test_score_trace_replays_both_phases_and_cosign_gates() -> None:
    suite, episode = _episodes()[0]
    p1_derive = worker.evaluate_shared_input(_case(suite, episode, 1))
    p2_derive = worker.evaluate_shared_input(_case(suite, episode, 2))
    p1_mutations = p1_derive["payload"]["mutations"]
    p2_mutations = p2_derive["payload"]["mutations"]
    public_case = _case(
        suite, episode, 2, operation="score_trace", stimulus={"mode": "COUPLED", "events": []}
    )
    p2_view = worker.PublicView(public_case)
    p1_ids = list(dict.fromkeys(episode["q1_slice_ids_p1"] + episode["q2_slice_ids_p1"]))
    p2_ids = list(dict.fromkeys(episode["q1_slice_ids_p2"] + episode["q2_slice_ids_p2"]))
    events = [
        {"seq": 1, "tool": "query_sources", "args": {"phase_index": 1, "slice_ids": p1_ids}},
        {
            "seq": 2,
            "tool": "stage_phase_mutations",
            "args": {
                "phase_index": 1,
                "stage_id": "stg_p1_complete",
                "plan_source": {"submitted_plan": p1_mutations},
            },
        },
        {
            "seq": 3,
            "tool": "commit_phase_mutations",
            "args": {"phase_index": 1, "stage_id": "stg_p1_complete"},
        },
        {"seq": 4, "tool": "query_sources", "args": {"phase_index": 2, "slice_ids": p2_ids}},
        {
            "seq": 5,
            "tool": "stage_phase_mutations",
            "args": {
                "phase_index": 2,
                "stage_id": "stg_p2_complete",
                "plan_source": {"submitted_plan": p2_mutations},
            },
        },
    ]
    for scope_kind, target_id, actor_id in sorted(
        worker._cosign_requirements(p2_mutations, p2_view)
    ):
        events.append(
            {
                "seq": len(events) + 1,
                "tool": "request_cosign",
                "args": {
                    "phase_index": 2,
                    "stage_id": "stg_p2_complete",
                    "scope_kind": scope_kind,
                    "target_entity_id": target_id,
                    "actor_account_id": actor_id,
                },
            }
        )
    events.append(
        {
            "seq": len(events) + 1,
            "tool": "commit_phase_mutations",
            "args": {"phase_index": 2, "stage_id": "stg_p2_complete"},
        }
    )
    public_case["stimulus"] = {"mode": "COUPLED", "events": events}
    result = worker.evaluate_shared_input(public_case)
    metrics = result["payload"]["metrics"]
    assert result["status"] == "underdefined"
    assert metrics["p2_observed"] == 1
    assert metrics["both_phases_staged"] == 1
    assert metrics["p1_staged"] == 1
    assert metrics["G-SRR"] == 1
    assert metrics["Sem-SRR@commit"] == 1
    assert metrics["Action-SRR@last_stage"] == 1
    assert metrics["P1-Action-SRR@last_stage"] == 1
    assert metrics["G-PROP"] == 1


def test_unmaterializable_phase_view_is_explicit() -> None:
    suite, episode = _episodes()[0]
    case = _case(suite, episode, 1)
    case["public_episode"]["phase2_base_manifest"] = episode["phase2_base_manifest"]
    result = worker.evaluate_shared_input(case)
    assert result["status"] == "underdefined"
    assert result["gaps"][0]["code"] == "UNMATERIALIZABLE_INPUT"
    assert result["gaps"][0]["path"] == "/public_episode"


def test_engagement_liveness_and_conversion_grace_use_card_boundaries() -> None:
    assert worker.SCHEMAS is not None
    assert set(worker.SCHEMAS.EngagementStatusV5.__args__) == {
        "ACTIVE",
        "TERMINATED",
        "FUTURE_HIRE",
        "CONTRACTOR_TO_FTE_PENDING",
    }
    suite, episode = _episodes()[0]
    view = worker.PublicView(_case(suite, episode, 1))
    active = {
        "eng_status": "ACTIVE",
        "start_s": 100,
        "end_s": 200,
        "retro_terminated_effective_s": None,
    }
    assert view.eng_live(active, 100)
    assert view.eng_live(active, 199)
    assert not view.eng_live(active, 200)
    pending = {
        "eng_status": "CONTRACTOR_TO_FTE_PENDING",
        "conversion_start_s": 120,
        "conversion_grace_deadline_s": 200,
        "retro_terminated_effective_s": None,
    }
    assert view.eng_live(pending, 100)
    assert worker.conversion_grace_active(pending, 119) is False
    assert worker.conversion_grace_active(pending, 120)
    assert worker.conversion_grace_active(pending, 199)
    assert worker.conversion_grace_active(pending, 200) is False
    assert not view.eng_live({**pending, "conversion_start_s": None}, 100)
    assert not view.eng_live({**pending, "conversion_grace_deadline_s": None}, 100)
    assert not view.eng_live({**pending, "retro_terminated_effective_s": 100}, 100)
    assert not view.eng_live({**pending, "eng_status": "TERMINATED"}, 100)
    assert not view.eng_live({**pending, "eng_status": "FUTURE_HIRE"}, 100)
    assert not view.eng_live({**pending, "eng_status": "PENDING"}, 100)
    assert not worker.conversion_grace_active({**pending, "eng_status": "ACTIVE"}, 120)


def _pending_account_mutation_at(clock: int) -> tuple[dict[str, Any], str]:
    suite, episode = _episodes()[0]
    public_episode = copy.deepcopy(episode)
    case = _case(suite, public_episode, 1)
    case["public_episode"]["phase1_manifest"]["clock_epoch_s"] = clock
    view = worker.PublicView(case)
    grants = view.rows("grants")
    roles = view.rows("roles")
    selected: tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]] | None = None
    for account_id, account in view.rows("idp_accounts").items():
        if account.get("status") != "ACTIVE":
            continue
        person = view.get("hr_persons", account.get("immutable_person_ref"))
        if person is None:
            continue
        if not any(
            e.get("eng_id") == account.get("bound_engagement_ref") for e in person["engagements"]
        ):
            continue
        for grant_id in account.get("direct_grant_ids", []):
            grant = grants.get(grant_id)
            role = roles.get(grant.get("role_id")) if grant else None
            if (
                grant
                and role
                and grant.get("grant_mode") == "standing"
                and role.get("privilege_tier") in {"admin", "critical"}
            ):
                selected = (account_id, account, person, grant)
                break
        if selected is not None:
            break
    assert selected is not None
    account_id, account, person, grant = selected
    engagement = next(
        e for e in person["engagements"] if e.get("eng_id") == account["bound_engagement_ref"]
    )
    account["group_ids"] = []
    account["direct_grant_ids"] = [grant["grant_id"]]
    grant["scope_path"] = "/*"
    person["engagements"] = [engagement]
    engagement.update(
        {
            "affiliation": "CONTRACTOR",
            "eng_status": "CONTRACTOR_TO_FTE_PENDING",
            "conversion_start_s": 100,
            "conversion_grace_deadline_s": 200,
            "retro_terminated_effective_s": None,
        }
    )
    mutation = worker.Deriver(case, view).derive_account_recon_mutation(account_id)
    return mutation, grant["grant_id"]


def test_pending_conversion_prunes_standing_admin_grant_only_in_grace() -> None:
    before_start, grant_id = _pending_account_mutation_at(50)
    assert before_start["account_action"] == "KEEP_ACTIVE"
    assert grant_id in before_start["retained_grant_ids"]
    in_grace, same_grant_id = _pending_account_mutation_at(100)
    assert same_grant_id == grant_id
    assert in_grace["account_action"] == "KEEP_ACTIVE"
    assert grant_id not in in_grace["retained_grant_ids"]
    after_deadline, _ = _pending_account_mutation_at(200)
    assert after_deadline["account_action"] == "DISABLE_ACCOUNT"


def test_pending_contractor_is_live_for_allowed_owner_eligibility() -> None:
    suite, episode = _episodes()[0]
    case = _case(suite, episode, 1)
    view = worker.PublicView(case)
    deriver = worker.Deriver(case, view)
    person_id, person = next(iter(view.rows("hr_persons").items()))
    existing = person["engagements"][0]
    now = view.manifest["clock_epoch_s"]
    pending = {
        **existing,
        "affiliation": "CONTRACTOR",
        "eng_status": "CONTRACTOR_TO_FTE_PENDING",
        "conversion_start_s": now + 10,
        "conversion_grace_deadline_s": now + 100,
        "retro_terminated_effective_s": None,
    }
    person["engagements"] = [pending]
    eligible, resolved_person, resolved_engagement = deriver._person_worker_eligible(
        person_id, allow_contractor=True
    )
    assert eligible
    assert resolved_person is person
    assert resolved_engagement == pending
    contractor_disallowed, _, _ = deriver._person_worker_eligible(person_id, allow_contractor=False)
    assert not contractor_disallowed


def _aws_two_edge_finding() -> tuple[dict[str, Any], list[str]]:
    suite, episode = _episodes()[0]
    case = _case(suite, episode, 1)
    view = worker.PublicView(case)
    deriver = worker.Deriver(case, view)
    deriver.derive()
    selected: tuple[str, dict[str, Any], dict[str, Any]] | None = None
    for task_id, task in view.rows("cloud_ciem_tasks").items():
        for principal in task.get("principals", []):
            if (
                principal.get("cloud_provider") == "AWS"
                and principal.get("aws_passrole_target_actions")
                and principal.get("aws_assumerole_chain_actions")
                and set(principal["aws_passrole_target_actions"]).isdisjoint(
                    principal["aws_assumerole_chain_actions"]
                )
                and deriver._linked_principal_live(principal.get("linked_principal_id"))
            ):
                selected = (task_id, task, principal)
                break
        if selected is not None:
            break
    assert selected is not None
    task_id, task, principal = selected
    targets = sorted(
        set(principal["aws_passrole_target_actions"])
        | set(principal["aws_assumerole_chain_actions"])
    )
    controls = ["iam:PassRole", "sts:AssumeRole", "compute:RunWorkload"]
    principal.update(
        {
            "aws_same_account_resource": True,
            "aws_identity_allow": controls,
            "aws_resource_policy_allow": [],
            "aws_permission_boundary_allow": controls,
            "aws_scp_allow": controls,
            "aws_explicit_deny": [],
            "aws_trust_policy_valid": True,
        }
    )
    task["forbidden_standing_cloud_actions"] = targets
    mutation = deriver.derive_cloud_ciem_mutation(task_id)
    finding = next(row for row in mutation["findings"] if row["cprin_id"] == principal["cprin_id"])
    return finding, targets


def test_aws_multiple_enabled_edges_union_both_target_sets() -> None:
    finding, targets = _aws_two_edge_finding()
    assert finding["effective_forbidden_actions"] == targets
    assert finding["remediation_action"] == "STRIP_ESCALATION_EDGE"


def test_aws_passrole_edge_precedes_assumerole_primary_label() -> None:
    finding, _ = _aws_two_edge_finding()
    assert finding["escalation_vector"] == "AWS_PASSROLE_EXEC"


def test_v7_connector_reconciliation_visits_cursor_0_page() -> None:
    suite, episode = _episodes()[0]
    case = _case(suite, episode, 1)
    view = worker.PublicView(case)
    deriver = worker.Deriver(case, view)
    pages = list(view.rows("connector_pages").values())
    for connector_id, connector in view.rows("connector_schemas").items():
        start_pages = [
            page
            for page in pages
            if page.get("connector_id") == connector_id and page.get("cursor_in") == "cursor_0"
        ]
        if (
            start_pages
            and connector.get("auth_state") == "AUTH_OK"
            and not (
                connector.get("transport_kind") == "JDBC_SQL"
                and connector.get("sync_mode") == "DELTA_CURSOR"
                and not connector.get("has_delete_audit_view")
            )
        ):
            mutation = deriver.finish(deriver.derive_connector_sync_mutation(connector_id))
            page_cites = set(mutation.get("cite", []))
            assert any(f"CONN-V5-01:{page['evidence_id']}" in page_cites for page in start_pages)
            return
    raise AssertionError("The public v7 packet has no eligible cursor_0 connector page.")


def _connector_sandbox(
    sync_mode: str,
    pages: list[dict[str, Any]],
    baseline: tuple[str, ...] = (),
) -> dict[str, Any]:
    suite, episode = _episodes()[0]
    case = _case(suite, episode, 1)
    view = worker.PublicView(case)
    connector_id, connector = next(iter(view.rows("connector_schemas").items()))
    connector.update(
        {
            "transport_kind": "REST_WEB_SERVICE",
            "provisioning_mode": "AUTOMATED_WRITE",
            "sync_mode": sync_mode,
            "auth_state": "AUTH_OK",
            "has_delete_audit_view": True,
            "required_fields": ["account_id", "status"],
            "mapping_rules": [],
            "unmapped_policy_override": "QUARANTINE_DRIFT",
            "local_checkpoint_etag": "checkpoint-local",
            "baseline_reconciled_account_ids": list(baseline),
        }
    )
    for page in pages:
        page["connector_id"] = connector_id
    view.tables["connector_pages"] = {page["page_id"]: page for page in pages}
    return worker.Deriver(case, view).derive_connector_sync_mutation(connector_id)


def _connector_page(
    page_id: str,
    cursor_in: str,
    attempt_index: int,
    http_status: int,
    next_cursor: str | None,
    records: list[list[list[Any]]],
    checkpoint: str | None = None,
) -> dict[str, Any]:
    return {
        "page_id": page_id,
        "connector_id": "public-test-connector",
        "cursor_in": cursor_in,
        "attempt_index": attempt_index,
        "http_status": http_status,
        "next_cursor": next_cursor,
        "base_checkpoint_etag": checkpoint,
        "records": records,
        "evidence_id": f"ev_public_{page_id}",
    }


def _connector_record(version: int, status: str) -> list[list[Any]]:
    return [
        ["account_id", "acct_public_versioned"],
        ["status", status],
        ["raw_entitlements", []],
        ["record_version", version],
    ]


def test_delta_checkpoint_is_checked_on_first_success_after_429() -> None:
    result = _connector_sandbox(
        "DELTA_CURSOR",
        [
            _connector_page("retry_0", "cursor_0", 0, 429, None, []),
            _connector_page("retry_1", "cursor_0", 1, 200, None, [], "checkpoint-stale"),
        ],
    )
    assert result["halt_reason"] == "HALT_CHECKPOINT_DRIFT"
    assert result["reconciled_account_ids"] == []


def test_delta_matching_checkpoint_after_429_can_finish_at_cursor_end() -> None:
    result = _connector_sandbox(
        "DELTA_CURSOR",
        [
            _connector_page("retry_match_0", "cursor_0", 0, 429, None, []),
            _connector_page(
                "retry_match_1", "cursor_0", 1, 200, "cursor_end", [], "checkpoint-local"
            ),
        ],
        baseline=("acct_public_baseline",),
    )
    assert result["halted_fail_closed"] is False
    assert result["halt_reason"] == "NONE"
    assert result["reconciled_account_ids"] == ["acct_public_baseline"]


def test_connector_null_next_cursor_halts() -> None:
    result = _connector_sandbox(
        "FULL_SNAPSHOT",
        [_connector_page("null_next", "cursor_0", 0, 200, None, [])],
    )
    assert result["halted_fail_closed"] is True
    assert result["halt_reason"] == "HALT_CURSOR_OR_HTTP"


def test_connector_cursor_end_stops_without_a_missing_page_halt() -> None:
    result = _connector_sandbox(
        "FULL_SNAPSHOT",
        [_connector_page("terminal", "cursor_0", 0, 200, "cursor_end", [])],
    )
    assert result["halted_fail_closed"] is False
    assert result["halt_reason"] == "NONE"


def test_rest_record_versions_are_monotonic_for_full_and_delta_modes() -> None:
    for sync_mode in ("FULL_SNAPSHOT", "DELTA_CURSOR"):
        result = _connector_sandbox(
            sync_mode,
            [
                _connector_page(
                    "version_2",
                    "cursor_0",
                    0,
                    200,
                    "cursor_1",
                    [_connector_record(2, "ACTIVE")],
                    "checkpoint-local",
                ),
                _connector_page(
                    "version_1",
                    "cursor_1",
                    0,
                    200,
                    "cursor_end",
                    [_connector_record(1, "TOMBSTONE")],
                ),
                _connector_page("terminal_ignored", "cursor_end", 0, 200, None, []),
            ],
        )
        assert result["halted_fail_closed"] is False
        assert "acct_public_versioned" in result["reconciled_account_ids"]


def test_rest_equal_record_versions_replace_with_last_row_for_full_and_delta() -> None:
    for sync_mode in ("FULL_SNAPSHOT", "DELTA_CURSOR"):
        result = _connector_sandbox(
            sync_mode,
            [
                _connector_page(
                    "equal_active",
                    "cursor_0",
                    0,
                    200,
                    "cursor_1",
                    [_connector_record(2, "ACTIVE")],
                    "checkpoint-local",
                ),
                _connector_page(
                    "equal_tombstone",
                    "cursor_1",
                    0,
                    200,
                    "cursor_end",
                    [_connector_record(2, "TOMBSTONE")],
                ),
                _connector_page("equal_terminal_ignored", "cursor_end", 0, 200, None, []),
            ],
        )
        assert result["halted_fail_closed"] is False
        assert "acct_public_versioned" not in result["reconciled_account_ids"]


def _synthetic_deriver(phase_index: int = 1) -> tuple[Any, Any]:
    suite, episode = _episodes()[0]
    case = _case(suite, episode, phase_index)
    view = worker.PublicView(case)
    return worker.Deriver(case, view), view


def _live_engagement(
    eng_id: str, now: int, *, dept_path: str = "/*", manager_person_id: str | None = None
) -> dict[str, Any]:
    return {
        "eng_id": eng_id,
        "affiliation": "FTE",
        "eng_status": "ACTIVE",
        "dept_path": dept_path,
        "manager_person_id": manager_person_id,
        "start_s": now - 100,
        "end_s": None,
        "conversion_start_s": None,
        "conversion_grace_deadline_s": None,
        "retro_terminated_effective_s": None,
    }


def _synthetic_person(person_id: str, email: str, *engagements: dict[str, Any]) -> dict[str, Any]:
    return {
        "person_id": person_id,
        "emp_immutable_id": f"emp_{person_id}",
        "primary_email": email,
        "historical_emails": [],
        "engagements": list(engagements),
        "lifecycle_gen": 1,
        "etag": "a" * 64,
        "evidence_id": f"ev_{person_id}",
    }


def _synthetic_account(
    account_id: str,
    person_id: str,
    engagement_id: str,
    email: str,
    grant_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "subkind": "HUMAN",
        "login_email": email,
        "immutable_person_ref": person_id,
        "bound_engagement_ref": engagement_id,
        "status": "ACTIVE",
        "group_ids": [],
        "direct_grant_ids": [] if grant_ids is None else list(grant_ids),
        "account_gen": 1,
        "etag": "b" * 64,
        "evidence_id": f"ev_{account_id}",
    }


def _install_sod_control_world(
    deriver: Any,
    view: Any,
    *,
    subject_conflicts: bool = True,
    preserve_if_live: bool = True,
) -> None:
    now = deriver.now
    leaf = view.scope_leaves[0]
    view.tables["hr_persons"] = {
        "pers_subject": _synthetic_person(
            "pers_subject", "subject@example.test", _live_engagement("eng_subject", now)
        ),
        "pers_monitor": _synthetic_person(
            "pers_monitor", "monitor@example.test", _live_engagement("eng_monitor", now)
        ),
    }
    view.tables["idp_accounts"] = {
        "acct_subject": _synthetic_account(
            "acct_subject",
            "pers_subject",
            "eng_subject",
            "subject@example.test",
            ["grant_a", "grant_b"] if subject_conflicts else [],
        ),
        "acct_monitor": _synthetic_account(
            "acct_monitor", "pers_monitor", "eng_monitor", "monitor@example.test", ["grant_watch"]
        ),
    }
    view.tables["roles"] = {
        "role_a": {
            "role_id": "role_a",
            "app_id": "app_x",
            "privilege_tier": "read",
            "capabilities": ["cap_a"],
            "is_jit_capable_target": True,
        },
        "role_b": {
            "role_id": "role_b",
            "app_id": "app_x",
            "privilege_tier": "read",
            "capabilities": ["cap_b"],
            "is_jit_capable_target": True,
        },
        "role_watch": {
            "role_id": "role_watch",
            "app_id": "app_x",
            "privilege_tier": "read",
            "capabilities": ["cap_watch"],
            "is_jit_capable_target": True,
        },
    }
    view.tables["grants"] = {
        "grant_a": {
            "grant_id": "grant_a",
            "account_id": "acct_subject",
            "role_id": "role_a",
            "grant_mode": "standing",
            "scope_path": leaf,
            "sod_prune_priority": 1,
        },
        "grant_b": {
            "grant_id": "grant_b",
            "account_id": "acct_subject",
            "role_id": "role_b",
            "grant_mode": "standing",
            "scope_path": leaf,
            "sod_prune_priority": 2,
        },
        "grant_watch": {
            "grant_id": "grant_watch",
            "account_id": "acct_monitor",
            "role_id": "role_watch",
            "grant_mode": "standing",
            "scope_path": "/*",
            "sod_prune_priority": 1,
        },
    }
    view.tables["transfer_bridges"] = {}
    view.tables["groups"] = {}
    view.tables["compensating_controls"] = {
        "cc_test": {
            "cc_id": "cc_test",
            "monitored_person_id": "pers_subject",
            "cap_a": "cap_a",
            "cap_b": "cap_b",
            "covered_scope_path": "/*",
            "monitor_account_id": "acct_monitor",
            "required_monitor_capability": "cap_watch",
            "audit_connector_id": "conn_audit",
            "preserve_if_live": preserve_if_live,
            "evidence_id": "ev_cc_test",
        }
    }
    view.tables["connector_schemas"] = {
        "conn_audit": {
            "connector_id": "conn_audit",
            "app_id": "app_x",
            "transport_kind": "REST_WEB_SERVICE",
            "provisioning_mode": "AUTOMATED_WRITE",
            "sync_mode": "FULL_SNAPSHOT",
            "auth_state": "AUTH_OK",
            "required_fields": ["account_id", "status", "raw_entitlements"],
            "mapping_rules": [],
            "unmapped_policy_override": "USE_OVERLAY_DEFAULT",
            "baseline_reconciled_account_ids": [],
            "audit_watermark_s": now,
            "etag": "d" * 64,
            "evidence_id": "ev_conn_audit",
        }
    }
    view.tables["connector_pages"] = {
        "page_audit": {
            "page_id": "page_audit",
            "connector_id": "conn_audit",
            "cursor_in": "cursor_0",
            "attempt_index": 0,
            "http_status": 200,
            "next_cursor": "cursor_end",
            "records": [],
            "evidence_id": "ev_page_audit",
        }
    }
    view.overlay["forbidden_sod_rules"] = [
        {"rule_code": "PAIR", "cap_a": "cap_a", "cap_b": "cap_b", "scope_mode": "SAME_LEAF_SCOPE"}
    ]
    deriver.access.sod = view.overlay["forbidden_sod_rules"]
    view.overlay["freshness_sla_s"] = 1000


def test_person_worker_eligibility_is_existential_and_binding_conflict_remains() -> None:
    deriver, view = _synthetic_deriver()
    now = deriver.now
    person = _synthetic_person(
        "pers_multi",
        "multi@example.test",
        _live_engagement("eng_a", now),
        _live_engagement("eng_b", now),
    )
    view.tables["hr_persons"] = {"pers_multi": person}
    ok, actual_person, engagement = deriver._person_worker_eligible("pers_multi", False)
    assert ok and actual_person is person and engagement is not None
    view.tables["idp_accounts"] = {
        "acct_multi": _synthetic_account("acct_multi", "pers_multi", "", "multi@example.test")
    }
    mutation = deriver.derive_account_recon_mutation("acct_multi")
    assert mutation["account_action"] == "ESCALATE_BINDING_CONFLICT"


def test_jml_sod_skips_peer_already_quarantined_by_tier_rule() -> None:
    deriver, view = _synthetic_deriver()
    leaf = view.scope_leaves[0]
    view.overlay["forbidden_sod_rules"] = [
        {"rule_code": "PAIR", "cap_a": "cap_a", "cap_b": "cap_b", "scope_mode": "SAME_LEAF_SCOPE"}
    ]
    deriver.access.sod = view.overlay["forbidden_sod_rules"]
    view.tables["roles"] = {
        "role_vendor_admin": {
            "role_id": "role_vendor_admin",
            "privilege_tier": "elevated",
            "capabilities": ["cap_a"],
            "is_jit_capable_target": True,
        },
        "role_read": {
            "role_id": "role_read",
            "privilege_tier": "read",
            "capabilities": ["cap_b"],
            "is_jit_capable_target": True,
        },
    }
    view.tables["jml_workflow_tasks"] = {
        "wfl_test": {
            "workflow_task_id": "wfl_test",
            "birthright_catalog": [
                {
                    "rule_id": "brule_vendor",
                    "role_id": "role_vendor_admin",
                    "allowed_affiliations": ["VENDOR"],
                    "dept_prefix": "/*",
                    "grant_mode": "eligible",
                    "scope_path": leaf,
                    "evidence_id": "ev_vendor",
                },
                {
                    "rule_id": "brule_read",
                    "role_id": "role_read",
                    "allowed_affiliations": ["VENDOR"],
                    "dept_prefix": "/*",
                    "grant_mode": "eligible",
                    "scope_path": leaf,
                    "evidence_id": "ev_read",
                },
            ],
            "visible_trace_events": [],
            "evidence_id": "ev_workflow",
        }
    }
    result = deriver.derive_jml_workflow_mutation("wfl_test")
    assert result["quarantined_birthright_rule_ids"] == ["brule_vendor"]
    assert result["active_birthright_rule_ids"] == ["brule_read"]

    view.tables["roles"]["role_vendor_admin"]["privilege_tier"] = "read"
    result = deriver.derive_jml_workflow_mutation("wfl_test")
    assert result["quarantined_birthright_rule_ids"] == ["brule_read", "brule_vendor"]


def test_optimization_uses_public_tier_weights_for_added_and_standing_roles() -> None:
    expected = {"read": 1, "standard": 2, "elevated": 4, "admin": 8, "critical": 16}
    for tier, weight in expected.items():
        deriver, view = _synthetic_deriver()
        now = deriver.now
        view.tables["hr_persons"] = {
            "pers_user": _synthetic_person(
                "pers_user", "user@example.test", _live_engagement("eng_user", now)
            )
        }
        view.tables["idp_accounts"] = {
            "acct_user": _synthetic_account(
                "acct_user", "pers_user", "eng_user", "user@example.test", ["grant_old"]
            )
        }
        view.tables["roles"] = {
            "role_old": {
                "role_id": "role_old",
                "app_id": "app_x",
                "privilege_tier": "elevated",
                "capabilities": ["cap_old"],
                "is_jit_capable_target": True,
            },
            "role_new": {
                "role_id": "role_new",
                "app_id": "app_x",
                "privilege_tier": tier,
                "capabilities": ["cap_new"],
                "is_jit_capable_target": True,
            },
        }
        view.tables["grants"] = {
            "grant_old": {
                "grant_id": "grant_old",
                "account_id": "acct_user",
                "role_id": "role_old",
                "grant_mode": "standing",
                "scope_path": "/*",
                "sod_prune_priority": 1,
            }
        }
        view.tables["groups"] = {}
        view.overlay["forbidden_sod_rules"] = []
        deriver.access.sod = []
        candidate = {
            "candidate_id": "cand_add",
            "action_kind": "ASSIGN_ROLE_STANDING",
            "target_person_id": "pers_user",
            "role_id": "role_new",
            "scope_path": "/*",
            "action_cost": 0,
        }
        view.tables["optimization_tasks"] = {
            "opt_test": {
                "task_id": "opt_test",
                "phase_kind": "P1_ZSP_REFACTOR",
                "required_person_caps": [["pers_user", "cap_new", "/*"]],
                "excluded_conflict_person_ids": [],
                "max_assignments_per_person": 1,
                "candidates": [candidate],
                "evidence_id": "ev_opt",
            }
        }
        result = deriver.derive_optimization_mutation("opt_test")
        assert result["selected_ids"] == ["cand_add"]
        assert result["objective_value"] == 4 + weight


def test_sod_live_cc_mitigation_and_optimize_preserve_if_live() -> None:
    deriver, view = _synthetic_deriver()
    _install_sod_control_world(deriver, view)
    view.tables["compensating_controls"]["cc_test"]["etag"] = "c" * 16
    assert len(deriver._live_controls_for_person("pers_subject")) == 1
    account_result = deriver.finish(deriver.derive_account_recon_mutation("acct_subject"))
    assert account_result["retained_grant_ids"] == ["grant_a", "grant_b"]
    assert view.version("cc_test") in account_result["if_match"]
    assert "CC-V5-01:ev_cc_test" in account_result["cite"]
    assert "CC-V5-01:ev_conn_audit" in account_result["cite"]
    assert "CC-V5-01:ev_acct_monitor" in account_result["cite"]
    conversion = {
        "candidate_id": "cand_convert_watch",
        "action_kind": "CONVERT_GRANT_TO_ELIGIBLE",
        "target_person_id": "pers_monitor",
        "target_grant_id": "grant_watch",
        "action_cost": 0,
    }
    task = {
        "phase_kind": "P1_ZSP_REFACTOR",
        "required_person_caps": [],
        "candidates": [conversion],
        "max_assignments_per_person": 1,
    }
    assert deriver._optimization_feasible(task, [], "OPT-V5-01")
    assert not deriver._optimization_feasible(task, [conversion], "OPT-V5-01")

    deriver, view = _synthetic_deriver()
    _install_sod_control_world(deriver, view)
    other_leaf = next((leaf for leaf in view.scope_leaves if leaf != view.scope_leaves[0]), None)
    assert other_leaf is not None
    view.tables["compensating_controls"]["cc_test"]["covered_scope_path"] = other_leaf
    scoped_result = deriver.derive_account_recon_mutation("acct_subject")
    assert scoped_result["retained_grant_ids"] == ["grant_a"]

    deriver, view = _synthetic_deriver()
    _install_sod_control_world(deriver, view)
    view.tables["connector_schemas"]["conn_audit"]["audit_watermark_s"] = (
        deriver.now - view.overlay["freshness_sla_s"] - 1
    )
    stale_result = deriver.derive_account_recon_mutation("acct_subject")
    assert stale_result["retained_grant_ids"] == ["grant_a"]

    deriver, view = _synthetic_deriver()
    _install_sod_control_world(deriver, view, subject_conflicts=False, preserve_if_live=True)
    assert not deriver._optimization_feasible(task, [conversion], "OPT-V5-01")
    view.tables["compensating_controls"]["cc_test"]["preserve_if_live"] = False
    assert deriver._optimization_feasible(task, [conversion], "OPT-V5-01")


def test_opt2_reviewer_cannot_be_subordinate_to_excluded_person() -> None:
    deriver, view = _synthetic_deriver()
    now = deriver.now
    view.tables["hr_persons"] = {
        "pers_excluded": _synthetic_person(
            "pers_excluded", "excluded@example.test", _live_engagement("eng_excluded", now)
        ),
        "pers_reviewer": _synthetic_person(
            "pers_reviewer",
            "reviewer@example.test",
            _live_engagement("eng_reviewer", now, manager_person_id="pers_excluded"),
        ),
    }
    view.tables["idp_accounts"] = {
        "acct_reviewer": _synthetic_account(
            "acct_reviewer", "pers_reviewer", "eng_reviewer", "reviewer@example.test"
        )
    }
    task = {
        "phase_kind": "P2_REVIEWER_ASSIGNMENT",
        "required_review_scopes": ["review_scope"],
        "excluded_conflict_person_ids": ["pers_excluded"],
        "max_assignments_per_person": 1,
    }
    candidate = {
        "candidate_id": "cand_reviewer",
        "action_kind": "SELECT_REVIEWER_ACCOUNT",
        "target_person_id": "pers_reviewer",
        "candidate_account_id": "acct_reviewer",
        "covered_scopes": ["review_scope"],
        "action_cost": 0,
    }
    assert not deriver._optimization_feasible(task, [candidate], "OPT-V5-02")
    view.tables["hr_persons"]["pers_reviewer"]["engagements"][0]["manager_person_id"] = None
    assert deriver._optimization_feasible(task, [candidate], "OPT-V5-02")


def test_cae_parent_idp_and_effective_deadlines_propagate_transitively() -> None:
    deriver, view = _synthetic_deriver()
    view.tables["cae_incidents"] = {
        "inc_chain": {
            "incident_id": "inc_chain",
            "person_id": "pers_none",
            "trigger_kind": "CREDENTIAL_COMPROMISE",
            "event_ts_s": 0,
            "tokens": [
                {
                    "token_id": "tok_root",
                    "account_id": "acct_root",
                    "token_kind": "REFRESH",
                    "parent_token_id": None,
                    "binding_kind": "BEARER_UNBOUND",
                    "cae_capable_spoke": False,
                    "issued_at_s": 0,
                    "token_exp_s": 100,
                    "revoke_ack_at_s": 10,
                    "next_spoke_poll_sync_at_s": 100,
                },
                {
                    "token_id": "tok_middle",
                    "account_id": "acct_middle",
                    "token_kind": "REFRESH",
                    "parent_token_id": "tok_root",
                    "binding_kind": "BEARER_UNBOUND",
                    "cae_capable_spoke": False,
                    "issued_at_s": 1,
                    "token_exp_s": 100,
                    "revoke_ack_at_s": 20,
                    "next_spoke_poll_sync_at_s": 100,
                },
                {
                    "token_id": "tok_leaf",
                    "account_id": "acct_leaf",
                    "token_kind": "ACCESS",
                    "parent_token_id": "tok_middle",
                    "binding_kind": "BOUND_DPOP",
                    "cae_capable_spoke": True,
                    "issued_at_s": 15,
                    "token_exp_s": 100,
                    "revoke_ack_at_s": 90,
                    "next_spoke_poll_sync_at_s": 100,
                },
            ],
            "etag": "c" * 64,
            "evidence_id": "ev_incident",
        }
    }
    view.overlay["cae_sla_s"] = 10
    mutation = deriver.derive_cae_containment_mutation("inc_chain")
    assert mutation["cae_computed_exposure_s"] == 0
    assert mutation["cae_sla_breached"] is False
    view.tables["cae_incidents"]["inc_chain"]["tokens"][2]["issued_at_s"] = 5
    mutation = deriver.derive_cae_containment_mutation("inc_chain")
    assert mutation["cae_computed_exposure_s"] == 5


def _install_conflict_rule(deriver: Any, cap_a: str = "cap_a", cap_b: str = "cap_b") -> None:
    rules = [
        {
            "rule_code": "PAIR",
            "cap_a": cap_a,
            "cap_b": cap_b,
            "scope_mode": "SAME_LEAF_SCOPE",
        }
    ]
    deriver.view.overlay["forbidden_sod_rules"] = rules
    deriver.access.sod = rules


def _nhi_candidate(
    nhi_id: str, owner: str, now: int, *, environment: str = "dev"
) -> dict[str, Any]:
    return {
        "nhi_id": nhi_id,
        "subkind": "workload_identity",
        "environment": environment,
        "capabilities": ["cap_a"],
        "scope_path": "/*",
        "signals": [
            {
                "signal_id": f"sig_{nhi_id}",
                "source": "iac_state_attestation",
                "candidate_person_id": owner,
                "synced_at_s": now,
                "evidence_id": f"ev_sig_{nhi_id}",
            }
        ],
        "evidence_id": f"ev_{nhi_id}",
    }


def _install_nhi_owner(deriver: Any, view: Any, *, grant_mode: str | None = None) -> None:
    now = deriver.now
    view.tables["hr_persons"] = {
        "pers_owner": _synthetic_person(
            "pers_owner", "owner@example.test", _live_engagement("eng_owner", now)
        )
    }
    view.tables["idp_accounts"] = {
        "acct_owner": _synthetic_account(
            "acct_owner",
            "pers_owner",
            "eng_owner",
            "owner@example.test",
            ["grant_owner"] if grant_mode is not None else [],
        )
    }
    view.tables["roles"] = {
        "role_owner": {
            "role_id": "role_owner",
            "app_id": "app_x",
            "privilege_tier": "standard",
            "capabilities": ["cap_b"],
            "is_jit_capable_target": True,
        }
    }
    view.tables["grants"] = (
        {
            "grant_owner": {
                "grant_id": "grant_owner",
                "account_id": "acct_owner",
                "role_id": "role_owner",
                "grant_mode": grant_mode,
                "scope_path": "/*",
                "sod_prune_priority": 1,
            }
        }
        if grant_mode is not None
        else {}
    )
    view.tables["groups"] = {}
    view.tables["baseline_nhis"] = {}
    view.tables["target_nhis"] = {
        "nhi_candidate": _nhi_candidate("nhi_candidate", "pers_owner", now)
    }
    view.overlay["enforce_cross_nhi_owner_sod"] = False
    view.overlay["max_prod_nhis_per_owner"] = 1
    _install_conflict_rule(deriver)


def test_nhi_cross_owner_flag_and_standing_only_capability_set() -> None:
    deriver, view = _synthetic_deriver()
    _install_nhi_owner(deriver, view)
    view.overlay["enforce_cross_nhi_owner_sod"] = True
    view.tables["baseline_nhis"] = {
        "bnhi_owner": {
            "bnhi_id": "bnhi_owner",
            "owner_person_id": "pers_owner",
            "environment": "prod",
            "status": "ACTIVE",
            "last_rotated_at_s": deriver.now,
            "capabilities": ["cap_b"],
            "scope_path": "/*",
            "evidence_id": "ev_bnhi_owner",
        }
    }
    assert (
        deriver.derive_nhi_governance_mutation("nhi_candidate")["nhi_action"]
        == "DISABLE_ORPHAN"
    )

    view.overlay["enforce_cross_nhi_owner_sod"] = False
    assert (
        deriver.derive_nhi_governance_mutation("nhi_candidate")["nhi_action"]
        == "ATTRIBUTE_OWNER"
    )


def test_nhi_cross_owner_flag_includes_prior_target_attribution() -> None:
    deriver, view = _synthetic_deriver()
    _install_nhi_owner(deriver, view)
    view.overlay["enforce_cross_nhi_owner_sod"] = True
    previous = _nhi_candidate("nhi_previous", "pers_owner", deriver.now)
    previous["capabilities"] = ["cap_b"]
    view.tables["target_nhis"]["nhi_previous"] = previous
    previous_result = deriver.derive_nhi_governance_mutation("nhi_previous")
    deriver.derived_by_target[("NHI_GOVERNANCE_MUTATION", "nhi_previous")] = previous_result
    assert (
        deriver.derive_nhi_governance_mutation("nhi_candidate")["nhi_action"]
        == "DISABLE_ORPHAN"
    )

    deriver, view = _synthetic_deriver()
    _install_nhi_owner(deriver, view)
    view.overlay["enforce_cross_nhi_owner_sod"] = True
    previous = _nhi_candidate("nhi_previous", "pers_owner", deriver.now)
    previous["capabilities"] = ["cap_b"]
    view.tables["target_nhis"]["nhi_previous"] = previous
    view._prior_maps["nhi_outcomes"] = {
        "nhi_previous": {
            "nhi_id": "nhi_previous",
            "p1_nhi_action": "ATTRIBUTE_OWNER",
            "resolved_owner_person_id": "pers_owner",
        }
    }
    assert (
        deriver.derive_nhi_governance_mutation("nhi_candidate")["nhi_action"]
        == "DISABLE_ORPHAN"
    )

    deriver, view = _synthetic_deriver()
    _install_nhi_owner(deriver, view, grant_mode="eligible")
    assert (
        deriver.derive_nhi_governance_mutation("nhi_candidate")["nhi_action"]
        == "ATTRIBUTE_OWNER"
    )


def test_nhi_prod_owner_cap_counts_only_earlier_prod_attributions() -> None:
    for previous_environment, expected in (
        ("staging", "ATTRIBUTE_OWNER"),
        ("prod", "DISABLE_ORPHAN"),
    ):
        deriver, view = _synthetic_deriver()
        _install_nhi_owner(deriver, view)
        view.tables["target_nhis"]["nhi_previous"] = _nhi_candidate(
            "nhi_previous", "pers_owner", deriver.now, environment=previous_environment
        )
        previous = deriver.derive_nhi_governance_mutation("nhi_previous")
        deriver.derived_by_target[("NHI_GOVERNANCE_MUTATION", "nhi_previous")] = previous
        view.tables["target_nhis"]["nhi_candidate"] = _nhi_candidate(
            "nhi_candidate", "pers_owner", deriver.now, environment="prod"
        )
        actual = deriver.derive_nhi_governance_mutation("nhi_candidate")["nhi_action"]
        assert actual == expected


def _install_agent_candidate(
    deriver: Any, view: Any, *, cross_sod: bool, footprint_source: str
) -> None:
    now = deriver.now
    _install_nhi_owner(deriver, view)
    view.tables["hr_persons"]["pers_owner"]["etag"] = "f" * 16
    view.tables["idp_accounts"]["acct_owner"]["etag"] = "e" * 16
    view.overlay["enforce_sponsor_agent_sod"] = cross_sod
    view.tables["enterprise_agents"] = {
        "agt_candidate": {
            "agent_id": "agt_candidate",
            "environment": "dev",
            "manifest_digest_etag": "a" * 64,
            "approved_registry_digest_etag": "a" * 64,
            "attestation_synced_at_s": now,
            "candidate_sponsor_person_id": "pers_owner",
            "tool_bindings": [
                {
                    "binding_id": "atg_candidate",
                    "tool_connector_id": "conn_tool",
                    "requested_mode": "AUTONOMOUS_BOUNDED",
                    "privilege_tier": "standard",
                    "capabilities": ["cap_a"],
                    "scope_path": "/*",
                    "evidence_id": "ev_binding_candidate",
                }
            ],
            "agent_gen": 1,
            "etag": "a" * 16,
            "evidence_id": "ev_agent_candidate",
        }
    }
    view.tables["connector_schemas"] = {
        "conn_tool": {
            "connector_id": "conn_tool",
            "app_id": "app_x",
            "transport_kind": "REST_WEB_SERVICE",
            "provisioning_mode": "AUTOMATED_WRITE",
            "sync_mode": "FULL_SNAPSHOT",
            "auth_state": "AUTH_OK",
            "required_fields": ["account_id", "status", "raw_entitlements"],
            "mapping_rules": [],
            "unmapped_policy_override": "USE_OVERLAY_DEFAULT",
            "baseline_reconciled_account_ids": [],
            "audit_watermark_s": now,
            "sync_gen": 1,
            "etag": "b" * 16,
            "evidence_id": "ev_conn_tool",
        }
    }
    view.tables["connector_pages"] = {
        "page_tool": {
            "page_id": "page_tool",
            "connector_id": "conn_tool",
            "cursor_in": "cursor_0",
            "attempt_index": 0,
            "http_status": 200,
            "next_cursor": "cursor_end",
            "records": [],
            "evidence_id": "ev_page_tool",
        }
    }
    view.tables["baseline_nhis"] = {}
    view.tables["baseline_agents"] = {}
    view.tables["idp_accounts"]["acct_owner"]["direct_grant_ids"] = []
    view.tables["grants"] = {}
    if footprint_source == "Kstand":
        view.tables["grants"] = {
            "grant_owner": {
                "grant_id": "grant_owner",
                "account_id": "acct_owner",
                "role_id": "role_owner",
                "grant_mode": "standing",
                "scope_path": "/*",
                "sod_prune_priority": 1,
                "evidence_id": "ev_grant_owner",
            }
        }
        view.tables["idp_accounts"]["acct_owner"]["direct_grant_ids"] = ["grant_owner"]
    elif footprint_source == "Fnhi":
        view.tables["baseline_nhis"] = {
            "bnhi_owner": {
                "bnhi_id": "bnhi_owner",
                "owner_person_id": "pers_owner",
                "environment": "prod",
                "status": "ACTIVE",
                "last_rotated_at_s": now,
                "capabilities": ["cap_b"],
                "scope_path": "/*",
                "evidence_id": "ev_bnhi_owner",
            }
        }
    elif footprint_source == "Kspon":
        view.tables["baseline_agents"] = {
            "bagt_owner": {
                "bagt_id": "bagt_owner",
                "sponsor_person_id": "pers_owner",
                "environment": "prod",
                "status": "ACTIVE",
                "autonomous_capabilities": ["cap_b"],
                "scope_path": "/*",
                "evidence_id": "ev_bagt_owner",
            }
        }
    elif footprint_source == "Kgroup":
        view.tables["groups"] = {
            "grp_owner": {
                "group_id": "grp_owner",
                "effect": "ALLOW",
                "scope_path": "/*",
                "granted_role_ids": ["role_owner"],
                "member_of_group_ids": [],
                "evidence_id": "ev_group_owner",
            }
        }
        view.tables["idp_accounts"]["acct_owner"]["group_ids"] = ["grp_owner"]
    elif footprint_source == "prior_agent":
        view.tables["enterprise_agents"]["agt_previous"] = {
            "agent_id": "agt_previous",
            "environment": "dev",
            "manifest_digest_etag": "a" * 64,
            "approved_registry_digest_etag": "a" * 64,
            "attestation_synced_at_s": now,
            "candidate_sponsor_person_id": "pers_owner",
            "tool_bindings": [
                {
                    "binding_id": "atg_previous",
                    "tool_connector_id": "conn_tool",
                    "requested_mode": "AUTONOMOUS_BOUNDED",
                    "privilege_tier": "standard",
                    "capabilities": ["cap_b"],
                    "scope_path": "/*",
                    "evidence_id": "ev_binding_previous",
                }
            ],
            "evidence_id": "ev_agent_previous",
        }
        deriver.derived_by_target[("AGENT_REGISTRY_MUTATION", "agt_previous")] = {
            "mutation_kind": "AGENT_REGISTRY_MUTATION",
            "target_entity_id": "agt_previous",
            "agent_action": "REGISTER_ACTIVE",
            "resolved_sponsor_person_id": "pers_owner",
            "active_autonomous_binding_ids": ["atg_previous"],
        }


def test_agent_sponsor_cross_sod_uses_standing_nhi_and_registered_agent_footprints() -> None:
    source_evidence = {
        "Kstand": "ev_grant_owner",
        "Kgroup": "ev_group_owner",
        "Fnhi": "ev_bnhi_owner",
        "Kspon": "ev_bagt_owner",
        "prior_agent": "ev_agent_previous",
    }
    for source in source_evidence:
        deriver, view = _synthetic_deriver()
        _install_agent_candidate(deriver, view, cross_sod=True, footprint_source=source)
        quarantined = deriver.derive_agent_registry_mutation("agt_candidate")
        assert quarantined["agent_action"] == "QUARANTINE_SOD_VIOLATION"
        assert quarantined["active_autonomous_binding_ids"] == []
        assert quarantined["active_hitl_binding_ids"] == []
        finished = deriver.finish(quarantined)
        assert f"AGENT-REG-V5-01:{source_evidence[source]}" in finished["cite"]
        assert view.version("agt_candidate") in finished["if_match"]
        assert view.version("pers_owner") in finished["if_match"]
        assert view.version("acct_owner") in finished["if_match"]
        assert view.version("conn_tool") in finished["if_match"]
        assert "AGENT-REG-V5-01:ev_binding_candidate" in finished["cite"]
        assert "AGENT-REG-V5-01:ev_conn_tool" in finished["cite"]
        view.overlay["enforce_sponsor_agent_sod"] = False
        assert (
            deriver.derive_agent_registry_mutation("agt_candidate")["agent_action"]
            == "REGISTER_ACTIVE"
        )


def _install_opt_grant_world(
    deriver: Any, view: Any, *, include_allow_group: bool = False, include_nhi: bool = False
) -> str:
    now = deriver.now
    leaf = view.scope_leaves[0]
    view.tables["hr_persons"] = {
        "pers_opt": _synthetic_person(
            "pers_opt", "opt@example.test", _live_engagement("eng_opt", now)
        )
    }
    view.tables["idp_accounts"] = {
        "acct_opt": _synthetic_account(
            "acct_opt", "pers_opt", "eng_opt", "opt@example.test", ["grant_old"]
        )
    }
    view.tables["roles"] = {
        "role_old": {
            "role_id": "role_old",
            "app_id": "app_x",
            "privilege_tier": "standard",
            "capabilities": ["cap_a"],
            "is_jit_capable_target": True,
        },
        "role_new": {
            "role_id": "role_new",
            "app_id": "app_x",
            "privilege_tier": "standard",
            "capabilities": ["cap_a"],
            "is_jit_capable_target": True,
        },
        "role_group": {
            "role_id": "role_group",
            "app_id": "app_x",
            "privilege_tier": "standard",
            "capabilities": ["cap_a"],
            "is_jit_capable_target": True,
        },
    }
    view.tables["grants"] = {
        "grant_old": {
            "grant_id": "grant_old",
            "account_id": "acct_opt",
            "role_id": "role_old",
            "grant_mode": "standing",
            "scope_path": leaf,
            "sod_prune_priority": 1,
        }
    }
    view.tables["groups"] = (
        {
            "grp_allow": {
                "group_id": "grp_allow",
                "effect": "ALLOW",
                "scope_path": leaf,
                "granted_role_ids": ["role_group"],
                "member_of_group_ids": [],
            }
        }
        if include_allow_group
        else {}
    )
    if include_allow_group:
        view.tables["idp_accounts"]["acct_opt"]["group_ids"] = ["grp_allow"]
    view.tables["baseline_nhis"] = (
        {
            "bnhi_opt": {
                "bnhi_id": "bnhi_opt",
                "owner_person_id": "pers_opt",
                "environment": "prod",
                "status": "ACTIVE",
                "last_rotated_at_s": now,
                "capabilities": ["cap_b"],
                "scope_path": leaf,
                "evidence_id": "ev_bnhi_opt",
            }
        }
        if include_nhi
        else {}
    )
    view.tables["baseline_agents"] = {}
    view.tables["compensating_controls"] = {}
    view.overlay["forbidden_sod_rules"] = []
    deriver.access.sod = []
    view.overlay["enforce_cross_nhi_owner_sod"] = include_nhi
    view.overlay["enforce_sponsor_agent_sod"] = False
    return leaf


def test_opt_conversion_rebuilds_capability_union_and_is_candidate_order_independent() -> None:
    deriver, view = _synthetic_deriver()
    leaf = _install_opt_grant_world(deriver, view)
    conversion = {
        "candidate_id": "cand_convert",
        "action_kind": "CONVERT_GRANT_TO_ELIGIBLE",
        "target_person_id": "pers_opt",
        "target_grant_id": "grant_old",
        "action_cost": 0,
    }
    removed_post = deriver._optimization_post_state([conversion], "OPT-V5-01")
    assert removed_post is not None
    assert ("cap_a", leaf) not in removed_post[0]["pers_opt"]

    deriver, view = _synthetic_deriver()
    leaf = _install_opt_grant_world(deriver, view, include_allow_group=True)
    task = {
        "phase_kind": "P1_ZSP_REFACTOR",
        "required_person_caps": [["pers_opt", "cap_a", leaf]],
        "candidates": [conversion],
        "max_assignments_per_person": 1,
    }
    assert deriver._optimization_feasible(task, [conversion], "OPT-V5-01")
    group_post = deriver._optimization_post_state([conversion], "OPT-V5-01")
    assert group_post is not None
    assert ("cap_a", leaf) in group_post[0]["pers_opt"]

    deriver, view = _synthetic_deriver()
    leaf = _install_opt_grant_world(deriver, view)
    assignment = {
        "candidate_id": "cand_assign",
        "action_kind": "ASSIGN_ROLE_STANDING",
        "target_person_id": "pers_opt",
        "role_id": "role_new",
        "scope_path": leaf,
        "action_cost": 0,
    }
    task["required_person_caps"] = [["pers_opt", "cap_a", leaf]]
    task["candidates"] = [conversion, assignment]
    forward = deriver._optimization_post_state([conversion, assignment], "OPT-V5-01")
    reverse = deriver._optimization_post_state([assignment, conversion], "OPT-V5-01")
    assert forward is not None and reverse is not None
    assert forward[0] == reverse[0]
    assert ("cap_a", leaf) in forward[0]["pers_opt"]


def test_opt_cross_sod_flags_use_post_state_for_nhi_and_sponsor_agents() -> None:
    for flag in ("enforce_cross_nhi_owner_sod", "enforce_sponsor_agent_sod"):
        deriver, view = _synthetic_deriver()
        leaf = _install_opt_grant_world(
            deriver, view, include_nhi=flag == "enforce_cross_nhi_owner_sod"
        )
        _install_conflict_rule(deriver)
        if flag == "enforce_sponsor_agent_sod":
            view.tables["baseline_agents"] = {
                "bagt_opt": {
                    "bagt_id": "bagt_opt",
                    "sponsor_person_id": "pers_opt",
                    "environment": "prod",
                    "status": "ACTIVE",
                    "autonomous_capabilities": ["cap_b"],
                    "scope_path": leaf,
                    "evidence_id": "ev_bagt_opt",
                }
            }
            view.overlay[flag] = True
        task = {
            "phase_kind": "P1_ZSP_REFACTOR",
            "required_person_caps": [],
            "max_assignments_per_person": 1,
        }
        assert not deriver._optimization_feasible(task, [], "OPT-V5-01")
        assert deriver._optimization_overgrant(task, [])
        view.overlay[flag] = False
        assert deriver._optimization_feasible(task, [], "OPT-V5-01")
        assert not deriver._optimization_overgrant(task, [])
        view.overlay[flag] = True
        conversion = {
            "candidate_id": "cand_convert",
            "action_kind": "CONVERT_GRANT_TO_ELIGIBLE",
            "target_person_id": "pers_opt",
            "target_grant_id": "grant_old",
            "action_cost": 0,
        }
        assert deriver._optimization_feasible(task, [conversion], "OPT-V5-01")
        assert not deriver._optimization_overgrant(task, [conversion])


def test_opt1_provenance_contains_only_touched_accounts_and_relevant_live_controls() -> None:
    deriver, view = _synthetic_deriver()
    _install_sod_control_world(deriver, view, subject_conflicts=True, preserve_if_live=False)
    now = deriver.now
    view.tables["hr_persons"]["pers_other"] = _synthetic_person(
        "pers_other", "other@example.test", _live_engagement("eng_other", now)
    )
    view.tables["compensating_controls"]["cc_unrelated"] = {
        "cc_id": "cc_unrelated",
        "monitored_person_id": "pers_other",
        "cap_a": "cap_a",
        "cap_b": "cap_b",
        "covered_scope_path": "/*",
        "monitor_account_id": "acct_monitor",
        "required_monitor_capability": "cap_watch",
        "audit_connector_id": "conn_audit",
        "preserve_if_live": True,
        "etag": "e" * 64,
        "evidence_id": "ev_cc_unrelated",
    }
    view.tables["roles"]["role_new"] = {
        "role_id": "role_new",
        "app_id": "app_x",
        "privilege_tier": "read",
        "capabilities": ["cap_new"],
        "is_jit_capable_target": True,
    }
    candidate = {
        "candidate_id": "cand_new",
        "action_kind": "ASSIGN_ROLE_STANDING",
        "target_person_id": "pers_subject",
        "role_id": "role_new",
        "scope_path": view.scope_leaves[0],
        "action_cost": 0,
    }
    view.tables["optimization_tasks"] = {
        "opt_test": {
            "task_id": "opt_test",
            "phase_kind": "P1_ZSP_REFACTOR",
            "required_person_caps": [["pers_subject", "cap_new", view.scope_leaves[0]]],
            "excluded_conflict_person_ids": [],
            "max_assignments_per_person": 1,
            "candidates": [candidate],
            "etag": "f" * 64,
            "evidence_id": "ev_opt_test",
        }
    }
    view.tables["compensating_controls"]["cc_test"]["etag"] = "c" * 64
    result = deriver.derive_optimization_mutation("opt_test")
    result = deriver.finish(result)
    assert result["selected_ids"] == ["cand_new"]
    required_versions = {
        view.version("opt_test"),
        view.version("acct_subject"),
        view.version("cc_test"),
    }
    assert None not in required_versions
    assert required_versions.issubset(set(result["if_match"]))
    assert view.version("cc_unrelated") not in result["if_match"]
    assert set(result["cite"]) == {"OPT-V5-01:ev_opt_test", "OPT-V5-01:ev_cc_test"}


def test_opt1_excluded_only_person_is_not_in_objective_population() -> None:
    deriver, view = _synthetic_deriver()
    leaf = _install_opt_grant_world(deriver, view)
    now = deriver.now
    view.tables["hr_persons"]["pers_excluded"] = _synthetic_person(
        "pers_excluded",
        "excluded@example.test",
        _live_engagement("eng_excluded", now),
    )
    view.tables["idp_accounts"]["acct_excluded"] = _synthetic_account(
        "acct_excluded",
        "pers_excluded",
        "eng_excluded",
        "excluded@example.test",
        ["grant_excluded"],
    )
    view.tables["roles"]["role_excluded"] = {
        "role_id": "role_excluded",
        "app_id": "app_x",
        "privilege_tier": "critical",
        "capabilities": ["cap_z"],
        "is_jit_capable_target": True,
    }
    view.tables["roles"]["role_new"]["privilege_tier"] = "read"
    view.tables["roles"]["role_new"]["capabilities"] = ["cap_new"]
    view.tables["grants"]["grant_excluded"] = {
        "grant_id": "grant_excluded",
        "account_id": "acct_excluded",
        "role_id": "role_excluded",
        "grant_mode": "standing",
        "scope_path": leaf,
        "sod_prune_priority": 1,
    }
    candidate = {
        "candidate_id": "cand_opt",
        "action_kind": "ASSIGN_ROLE_STANDING",
        "target_person_id": "pers_opt",
        "role_id": "role_new",
        "scope_path": leaf,
        "action_cost": 0,
    }
    task = {
        "task_id": "opt_objective",
        "phase_kind": "P1_ZSP_REFACTOR",
        "required_person_caps": [["pers_opt", "cap_new", leaf]],
        "excluded_conflict_person_ids": ["pers_excluded"],
        "max_assignments_per_person": 1,
        "candidates": [candidate],
        "etag": "f" * 16,
        "evidence_id": "ev_opt_objective",
    }
    view.tables["optimization_tasks"] = {"opt_objective": task}
    excluded_only = deriver.derive_optimization_mutation("opt_objective")
    assert excluded_only["selected_ids"] == ["cand_opt"]
    assert excluded_only["objective_value"] == 3

    task["required_person_caps"].append(["pers_excluded", "cap_z", leaf])
    required_person = deriver.derive_optimization_mutation("opt_objective")
    assert required_person["selected_ids"] == ["cand_opt"]
    assert required_person["objective_value"] == 19


def test_agent_prod_sponsor_cap_counts_active_baseline_prod_agents() -> None:
    for environment, baseline_status, expected in (
        ("prod", "ACTIVE", "SUSPEND_SPONSOR_INVALID"),
        ("prod", "SUSPENDED", "REGISTER_ACTIVE"),
        ("staging", "ACTIVE", "REGISTER_ACTIVE"),
    ):
        deriver, view = _synthetic_deriver()
        _install_agent_candidate(deriver, view, cross_sod=False, footprint_source="none")
        view.tables["enterprise_agents"]["agt_candidate"]["environment"] = environment
        view.overlay["max_prod_agents_per_sponsor"] = 1
        view.tables["baseline_agents"] = {
            "bagt_sponsor": {
                "bagt_id": "bagt_sponsor",
                "sponsor_person_id": "pers_owner",
                "environment": "prod",
                "status": baseline_status,
                "autonomous_capabilities": ["cap_baseline"],
                "scope_path": "/*",
                "evidence_id": "ev_bagt_sponsor",
            }
        }
        result = deriver.finish(deriver.derive_agent_registry_mutation("agt_candidate"))
        assert result["agent_action"] == expected
        if expected == "SUSPEND_SPONSOR_INVALID":
            assert result["resolved_sponsor_person_id"] is None
            assert result["active_autonomous_binding_ids"] == []
            assert "AGENT-REG-V5-01:ev_bagt_sponsor" in result["cite"]
            assert set(result["if_match"]) == {
                view.version("agt_candidate"),
                view.version("pers_owner"),
            }
        else:
            assert set(result["if_match"]) == {
                view.version("agt_candidate"),
                view.version("pers_owner"),
                view.version("acct_owner"),
                view.version("conn_tool"),
            }
        assert worker.ATOMIC_ADAPTER.validate_json(worker.canonical_bytes(result))


def test_agent_prod_sponsor_cap_counts_an_earlier_prod_registration() -> None:
    deriver, view = _synthetic_deriver()
    _install_agent_candidate(deriver, view, cross_sod=False, footprint_source="none")
    view.tables["enterprise_agents"]["agt_candidate"]["environment"] = "prod"
    view.tables["enterprise_agents"]["agt_previous"] = {
        "agent_id": "agt_previous",
        "environment": "prod",
        "manifest_digest_etag": "c" * 16,
        "approved_registry_digest_etag": "c" * 16,
        "attestation_synced_at_s": deriver.now,
        "candidate_sponsor_person_id": "pers_owner",
        "tool_bindings": [],
        "agent_gen": 1,
        "etag": "d" * 16,
        "evidence_id": "ev_agent_previous",
    }
    deriver.derived_by_target[("AGENT_REGISTRY_MUTATION", "agt_previous")] = {
        "mutation_kind": "AGENT_REGISTRY_MUTATION",
        "target_entity_id": "agt_previous",
        "agent_action": "REGISTER_ACTIVE",
        "resolved_sponsor_person_id": "pers_owner",
        "active_autonomous_binding_ids": [],
    }
    view.overlay["max_prod_agents_per_sponsor"] = 1
    result = deriver.finish(deriver.derive_agent_registry_mutation("agt_candidate"))
    assert result["agent_action"] == "SUSPEND_SPONSOR_INVALID"
    assert "AGENT-REG-V5-01:ev_agent_previous" in result["cite"]


def test_agent_prod_sponsor_cap_excludes_earlier_nonprod_registration() -> None:
    deriver, view = _synthetic_deriver()
    _install_agent_candidate(deriver, view, cross_sod=False, footprint_source="none")
    view.tables["enterprise_agents"]["agt_candidate"]["environment"] = "prod"
    view.tables["enterprise_agents"]["agt_previous"] = {
        "agent_id": "agt_previous",
        "environment": "staging",
        "manifest_digest_etag": "c" * 16,
        "approved_registry_digest_etag": "c" * 16,
        "attestation_synced_at_s": deriver.now,
        "candidate_sponsor_person_id": "pers_owner",
        "tool_bindings": [],
        "agent_gen": 1,
        "etag": "d" * 16,
        "evidence_id": "ev_agent_previous",
    }
    deriver.derived_by_target[("AGENT_REGISTRY_MUTATION", "agt_previous")] = {
        "mutation_kind": "AGENT_REGISTRY_MUTATION",
        "target_entity_id": "agt_previous",
        "agent_action": "REGISTER_ACTIVE",
        "resolved_sponsor_person_id": "pers_owner",
        "active_autonomous_binding_ids": [],
    }
    view.overlay["max_prod_agents_per_sponsor"] = 1
    result = deriver.finish(deriver.derive_agent_registry_mutation("agt_candidate"))
    assert result["agent_action"] == "REGISTER_ACTIVE"
    assert "AGENT-REG-V5-01:ev_agent_previous" not in result["cite"]


def test_agent_prod_cap_sources_are_cited_when_sponsor_account_check_short_circuits() -> None:
    deriver, view = _synthetic_deriver()
    _install_agent_candidate(deriver, view, cross_sod=False, footprint_source="none")
    view.tables["enterprise_agents"]["agt_candidate"]["environment"] = "prod"
    view.tables["idp_accounts"] = {}
    view.tables["baseline_agents"] = {
        "bagt_sponsor": {
            "bagt_id": "bagt_sponsor",
            "sponsor_person_id": "pers_owner",
            "environment": "prod",
            "status": "ACTIVE",
            "autonomous_capabilities": ["cap_baseline"],
            "scope_path": "/*",
            "evidence_id": "ev_bagt_sponsor",
        }
    }
    view.tables["enterprise_agents"]["agt_previous"] = {
        "agent_id": "agt_previous",
        "environment": "prod",
        "manifest_digest_etag": "c" * 16,
        "approved_registry_digest_etag": "c" * 16,
        "attestation_synced_at_s": deriver.now,
        "candidate_sponsor_person_id": "pers_owner",
        "tool_bindings": [],
        "agent_gen": 1,
        "etag": "d" * 16,
        "evidence_id": "ev_agent_previous",
    }
    deriver.derived_by_target[("AGENT_REGISTRY_MUTATION", "agt_previous")] = {
        "mutation_kind": "AGENT_REGISTRY_MUTATION",
        "target_entity_id": "agt_previous",
        "agent_action": "REGISTER_ACTIVE",
        "resolved_sponsor_person_id": "pers_owner",
        "active_autonomous_binding_ids": [],
    }
    view.overlay["max_prod_agents_per_sponsor"] = 3

    result = deriver.finish(deriver.derive_agent_registry_mutation("agt_candidate"))

    assert result["agent_action"] == "SUSPEND_SPONSOR_INVALID"
    assert result["cite"] == [
        "AGENT-REG-V5-01:ev_agent_candidate",
        "AGENT-REG-V5-01:ev_agent_previous",
        "AGENT-REG-V5-01:ev_bagt_sponsor",
        "AGENT-REG-V5-01:ev_pers_owner",
    ]


def test_agent_halted_connector_outcome_drops_auth_ok_binding() -> None:
    for phase_index, http_status, expected in (
        (1, 500, "SUSPEND_NO_ACTIVE_TOOLS"),
        (1, 200, "REGISTER_ACTIVE"),
        (2, 500, "SUSPEND_NO_ACTIVE_TOOLS"),
        (2, 200, "REGISTER_ACTIVE"),
    ):
        deriver, view = _synthetic_deriver(phase_index)
        _install_agent_candidate(deriver, view, cross_sod=False, footprint_source="none")
        if phase_index == 1:
            view.tables["connector_pages"]["page_tool"]["http_status"] = http_status
            connector_outcome = deriver.derive_connector_sync_mutation("conn_tool")
            assert connector_outcome["halted_fail_closed"] is (http_status == 500)
            deriver.derived_by_target[("CONNECTOR_SYNC_MUTATION", "conn_tool")] = connector_outcome
        else:
            view._prior_maps["connector_outcomes"] = {
                "conn_tool": {
                    "connector_id": "conn_tool",
                    "halted_fail_closed": http_status == 500,
                }
            }
        deriver.pre, deriver.cites, deriver.rules = set(), set(), set()
        result = deriver.finish(deriver.derive_agent_registry_mutation("agt_candidate"))
        assert result["agent_action"] == expected
        if expected == "SUSPEND_NO_ACTIVE_TOOLS":
            assert result["active_autonomous_binding_ids"] == []
            assert set(result["if_match"]) == {
                view.version("agt_candidate"),
                view.version("pers_owner"),
                view.version("acct_owner"),
                view.version("conn_tool"),
            }
            assert "AGENT-REG-V5-01:ev_binding_candidate" in result["cite"]
            assert "AGENT-REG-V5-01:ev_conn_tool" in result["cite"]


def test_agent_stale_attestation_keeps_sponsor_precondition_without_sponsor_cite() -> None:
    deriver, view = _synthetic_deriver(phase_index=2)
    _install_agent_candidate(deriver, view, cross_sod=False, footprint_source="none")
    view.tables["enterprise_agents"]["agt_candidate"]["manifest_digest_etag"] = "a" * 16
    view.tables["enterprise_agents"]["agt_candidate"]["approved_registry_digest_etag"] = "b" * 16
    result = deriver.finish(deriver.derive_agent_registry_mutation("agt_candidate"))
    assert result["agent_action"] == "SUSPEND_UNATTESTED_OR_STALE"
    assert set(result["if_match"]) == {
        view.version("agt_candidate"),
        view.version("pers_owner"),
    }
    assert result["cite"] == ["AGENT-REG-V5-01:ev_agent_candidate"]


def test_agent_register_cites_the_binding_evidence_and_tool_schema() -> None:
    deriver, view = _synthetic_deriver(phase_index=2)
    _install_agent_candidate(deriver, view, cross_sod=False, footprint_source="none")
    result = deriver.finish(deriver.derive_agent_registry_mutation("agt_candidate"))
    assert result["agent_action"] == "REGISTER_ACTIVE"
    assert "AGENT-REG-V5-01:ev_binding_candidate" in result["cite"]
    assert "AGENT-REG-V5-01:ev_conn_tool" in result["cite"]
    assert set(result["if_match"]) == {
        view.version("agt_candidate"),
        view.version("pers_owner"),
        view.version("acct_owner"),
        view.version("conn_tool"),
    }


def test_v8_connector_page_and_itsm_provenance_rows_are_conditional() -> None:
    deriver, view = _synthetic_deriver()
    _install_agent_candidate(deriver, view, cross_sod=False, footprint_source="none")
    view.tables["connector_schemas"]["conn_tool"]["etag"] = "a" * 16
    view.tables["connector_pages"]["page_tool"]["etag"] = "b" * 16

    plain = deriver.finish(deriver.derive_connector_sync_mutation("conn_tool"))

    assert view.version("conn_tool") in plain["if_match"]
    assert view.version("page_tool") not in plain["if_match"]
    assert "CONN-V5-01:ev_page_tool" in plain["cite"]
    assert "ITSM-V5-01:ev_conn_tool" not in plain["cite"]

    deriver, view = _synthetic_deriver()
    _install_agent_candidate(deriver, view, cross_sod=False, footprint_source="none")
    connector = view.tables["connector_schemas"]["conn_tool"]
    connector.update(
        {
            "provisioning_mode": "DISCONNECTED_ITSM_TICKET",
            "mapping_rules": [["raw_entitlement", "role_not_held"]],
            "itsm_primary_fulfiller_account_id": "acct_owner",
            "itsm_backup_fulfiller_account_id": None,
            "itsm_bridge_healthy": True,
            "open_itsm_tickets": [],
        }
    )
    view.tables["connector_pages"]["page_tool"]["records"] = [
        [
            ["account_id", "acct_owner"],
            ["status", "ACTIVE"],
            ["raw_entitlements", ["raw_entitlement"]],
        ]
    ]
    connected = deriver.finish(deriver.derive_connector_sync_mutation("conn_tool"))

    assert connected["itsm_ticket_actions"]
    assert view.version("page_tool") not in connected["if_match"]
    assert view.version("acct_owner") in connected["if_match"]
    assert view.version("pers_owner") in connected["if_match"]
    assert "CONN-V5-01:ev_acct_owner" in connected["cite"]
    assert "ITSM-V5-01:ev_conn_tool" in connected["cite"]
    assert "ITSM-V5-01:ev_pers_owner" in connected["cite"]


def test_v8_hr_feed_evidence_is_cited_but_only_hrc_and_connectors_add_versions() -> None:
    def run(source_kind: str) -> tuple[dict[str, Any], Any]:
        deriver, view = _synthetic_deriver()
        now = deriver.now
        person = _synthetic_person("pers_hr", "hr@example.test", _live_engagement("eng_hr", now))
        feed = {
            "feed_id": "feed_hr",
            "source_kind": source_kind,
            "connector_id": "conn_hr",
            "correlation_strategy": "IMMUTABLE_ID_OR_EMAIL",
            "authorized_affiliations": ["FTE"],
            "status_code_map": [["ACTIVE_RAW", "ACTIVE"]],
            "precedence_rank": 1,
            "assertions": [
                {
                    "person_id": "pers_hr",
                    "emp_immutable_id": "emp_pers_hr",
                    "email": "hr@example.test",
                    "affiliation": "FTE",
                    "raw_status_code": "ACTIVE_RAW",
                }
            ],
            "etag": "c" * 16,
            "evidence_id": "ev_feed_hr",
        }
        view.tables["hr_source_mappings"] = {
            "hrmap_test": {
                "mapping_task_id": "hrmap_test",
                "mapping_gen": 1,
                "feeds": [feed],
                "etag": "d" * 16,
                "evidence_id": "ev_hrmap_test",
            }
        }
        view.tables["connector_schemas"] = {
            "conn_hr": {
                "connector_id": "conn_hr",
                "auth_state": "AUTH_OK",
                "audit_watermark_s": now,
                "sync_gen": 1,
                "etag": "e" * 16,
                "evidence_id": "ev_conn_hr",
            }
        }
        view.tables["hr_persons"] = {"pers_hr": person}
        view.overlay["freshness_sla_s"] = 1000
        view.overlay["require_immutable_hr_correlation"] = False
        result = deriver.finish(deriver.derive_hr_source_mapping_mutation("hrmap_test"))
        return result, view

    accepted, accepted_view = run("HRIS_SOURCE")
    assert accepted_view.version("hrmap_test") in accepted["if_match"]
    assert accepted_view.version("conn_hr") in accepted["if_match"]
    assert accepted_view.version("pers_hr") in accepted["if_match"]
    assert accepted_view.version("feed_hr") not in accepted["if_match"]
    assert "HR-AUTH-V5-01:ev_feed_hr" in accepted["cite"]
    assert "HR-AUTH-V5-01:ev_pers_hr" in accepted["cite"]

    rejected, rejected_view = run("TARGET_APP_SPOKE")
    assert rejected_view.version("pers_hr") not in rejected["if_match"]
    assert "HR-AUTH-V5-01:ev_feed_hr" in rejected["cite"]
    assert "HR-AUTH-V5-01:ev_pers_hr" not in rejected["cite"]


def test_v8_account_binding_prefix_and_cc_version_citation_split() -> None:
    deriver, view = _synthetic_deriver()
    _install_sod_control_world(deriver, view, subject_conflicts=True, preserve_if_live=False)
    account = view.tables["idp_accounts"]["acct_subject"]
    account["immutable_person_ref"] = None
    view.tables["compensating_controls"]["cc_test"]["etag"] = "c" * 16

    result = deriver.finish(deriver.derive_account_recon_mutation("acct_subject"))

    assert result["account_action"] == "KEEP_ACTIVE"
    assert "BIND-V5-01b:ev_acct_subject" in result["cite"]
    assert "BIND-V5-01a:ev_acct_subject" not in result["cite"]
    assert view.version("cc_test") in result["if_match"]
    assert view.version("conn_audit") not in result["if_match"]
    assert view.version("acct_monitor") not in result["if_match"]
    assert "CC-V5-01:ev_cc_test" in result["cite"]
    assert "CC-V5-01:ev_conn_audit" in result["cite"]
    assert "CC-V5-01:ev_acct_monitor" in result["cite"]


def test_v8_nhi_attribute_and_disable_have_distinct_provenance() -> None:
    deriver, view = _synthetic_deriver()
    _install_nhi_owner(deriver, view)
    nhi = view.tables["target_nhis"]["nhi_candidate"]
    nhi["etag"] = "f" * 16
    nhi["nhi_gen"] = 1
    nhi["signals"] = [
        {
            "signal_id": "sig_nhi_candidate",
            "source": "vault_rotator_principal",
            "rotator_account_id": "acct_owner",
            "synced_at_s": deriver.now,
            "evidence_id": "ev_sig_nhi_candidate",
        }
    ]

    attributed = deriver.finish(deriver.derive_nhi_governance_mutation("nhi_candidate"))

    assert attributed["nhi_action"] == "ATTRIBUTE_OWNER"
    assert view.version("nhi_candidate") in attributed["if_match"]
    assert view.version("pers_owner") in attributed["if_match"]
    assert view.version("acct_owner") in attributed["if_match"]
    assert "NHI-V5-01:ev_sig_nhi_candidate" in attributed["cite"]
    assert "NHI-V5-01:ev_pers_owner" in attributed["cite"]
    assert "NHI-V5-01:ev_acct_owner" in attributed["cite"]

    deriver.pre, deriver.cites, deriver.rules = set(), set(), set()
    nhi["signals"][0]["synced_at_s"] = deriver.now - view.overlay["freshness_sla_s"] - 1
    disabled = deriver.finish(deriver.derive_nhi_governance_mutation("nhi_candidate"))

    assert disabled["nhi_action"] == "DISABLE_ORPHAN"
    assert view.version("pers_owner") not in disabled["if_match"]
    assert view.version("acct_owner") not in disabled["if_match"]
    assert "NHI-V5-01:ev_sig_nhi_candidate" in disabled["cite"]
    assert "NHI-V5-01:ev_pers_owner" not in disabled["cite"]


def test_v8_ciem_linked_principal_is_cited_only_when_orphaned() -> None:
    def run(status: str) -> tuple[dict[str, Any], Any]:
        deriver, view = _synthetic_deriver()
        now = deriver.now
        view.tables["hr_persons"] = {
            "pers_cloud": _synthetic_person(
                "pers_cloud", "cloud@example.test", _live_engagement("eng_cloud", now)
            )
        }
        view.tables["idp_accounts"] = {
            "acct_cloud": {
                **_synthetic_account("acct_cloud", "pers_cloud", "eng_cloud", "cloud@example.test"),
                "status": status,
            }
        }
        principal = {
            "cprin_id": "cprin_cloud",
            "linked_principal_id": "acct_cloud",
            "cloud_provider": "AWS",
            "aws_identity_allow": [],
            "aws_resource_policy_allow": [],
            "aws_same_account_resource": True,
            "aws_permission_boundary_allow": None,
            "aws_scp_allow": [],
            "aws_explicit_deny": [],
            "aws_passrole_target_actions": [],
            "aws_assumerole_chain_actions": [],
            "aws_trust_policy_valid": False,
        }
        view.tables["cloud_ciem_tasks"] = {
            "ciem_test": {
                "ciem_task_id": "ciem_test",
                "ciem_gen": 1,
                "principals": [principal],
                "forbidden_standing_cloud_actions": [],
                "etag": "d" * 16,
                "evidence_id": "ev_ciem_test",
            }
        }
        result = deriver.finish(deriver.derive_cloud_ciem_mutation("ciem_test"))
        return result, view

    live, live_view = run("ACTIVE")
    assert live_view.version("ciem_test") in live["if_match"]
    assert live_view.version("acct_cloud") in live["if_match"]
    assert "CIEM-V5-01:ev_ciem_test" in live["cite"]
    assert "CIEM-V5-01:ev_acct_cloud" not in live["cite"]

    orphan, orphan_view = run("DISABLED")
    assert orphan_view.version("acct_cloud") in orphan["if_match"]
    assert "CIEM-V5-01:ev_acct_cloud" in orphan["cite"]


def test_v8_jml_catalog_rows_are_citations_without_pre_versions() -> None:
    deriver, view = _synthetic_deriver()
    leaf = view.scope_leaves[0]
    candidate = {
        "rule_id": "brule_v8",
        "role_id": "role_v8",
        "allowed_affiliations": ["FTE"],
        "dept_prefix": "/*",
        "grant_mode": "eligible",
        "scope_path": leaf,
        "etag": "a" * 16,
        "evidence_id": "ev_brule_v8",
    }
    role = {
        "role_id": "role_v8",
        "app_id": "app_x",
        "privilege_tier": "read",
        "capabilities": ["cap_a"],
        "is_jit_capable_target": True,
        "etag": "b" * 16,
        "evidence_id": "ev_role_v8",
    }
    view.tables["roles"] = {"role_v8": role}
    view.tables["jml_workflow_tasks"] = {
        "wfl_v8": {
            "workflow_task_id": "wfl_v8",
            "workflow_gen": 1,
            "birthright_catalog": [candidate],
            "visible_trace_events": [
                {"event_id": "event_v8", "evidence_id": "ev_event_v8", "etag": "c" * 16}
            ],
            "etag": "d" * 16,
            "evidence_id": "ev_wfl_v8",
        }
    }

    result = deriver.finish(deriver.derive_jml_workflow_mutation("wfl_v8"))

    assert view.version("wfl_v8") in result["if_match"]
    assert view.version("role_v8") not in result["if_match"]
    assert "JML-WF-V5-01:ev_brule_v8" in result["cite"]
    assert "JML-WF-V5-01:ev_role_v8" in result["cite"]
    assert "JML-WF-V5-01:ev_event_v8" in result["cite"]


def test_v8_opt2_cites_only_the_selected_reviewer_bundle() -> None:
    deriver, view = _synthetic_deriver()
    leaf = view.scope_leaves[0]
    now = deriver.now
    view.tables["hr_persons"] = {
        "pers_r1": _synthetic_person("pers_r1", "r1@example.test", _live_engagement("eng_r1", now)),
        "pers_r2": _synthetic_person("pers_r2", "r2@example.test", _live_engagement("eng_r2", now)),
    }
    view.tables["idp_accounts"] = {
        "acct_r1": _synthetic_account("acct_r1", "pers_r1", "eng_r1", "r1@example.test"),
        "acct_r2": _synthetic_account("acct_r2", "pers_r2", "eng_r2", "r2@example.test"),
    }
    view.tables["roles"] = {
        "role_r1": {"role_id": "role_r1", "etag": "a" * 16, "evidence_id": "ev_role_r1"},
        "role_r2": {"role_id": "role_r2", "etag": "b" * 16, "evidence_id": "ev_role_r2"},
    }
    view.tables["optimization_tasks"] = {
        "opt_review": {
            "task_id": "opt_review",
            "phase_kind": "P2_REVIEWER_ASSIGNMENT",
            "required_review_scopes": [leaf],
            "excluded_conflict_person_ids": [],
            "max_assignments_per_person": 1,
            "candidates": [
                {
                    "candidate_id": "cand_r1",
                    "action_kind": "SELECT_REVIEWER_ACCOUNT",
                    "target_person_id": "pers_r1",
                    "role_id": "role_r1",
                    "candidate_account_id": "acct_r1",
                    "covered_scopes": [leaf],
                    "action_cost": 1,
                },
                {
                    "candidate_id": "cand_r2",
                    "action_kind": "SELECT_REVIEWER_ACCOUNT",
                    "target_person_id": "pers_r2",
                    "role_id": "role_r2",
                    "candidate_account_id": "acct_r2",
                    "covered_scopes": [leaf],
                    "action_cost": 2,
                },
            ],
            "etag": "c" * 16,
            "evidence_id": "ev_opt_review",
        }
    }

    result = deriver.finish(deriver.derive_optimization_mutation("opt_review"))

    assert result["selected_ids"] == ["cand_r1"]
    assert view.version("acct_r1") in result["if_match"]
    assert view.version("pers_r1") in result["if_match"]
    assert view.version("acct_r2") not in result["if_match"]
    assert view.version("pers_r2") not in result["if_match"]
    assert set(result["cite"]) == {
        "OPT-V5-02:ev_opt_review",
        "OPT-V5-02:ev_acct_r1",
        "OPT-V5-02:ev_pers_r1",
        "OPT-V5-02:ev_role_r1",
    }


def test_v8_cae_tokens_are_citations_not_preconditions() -> None:
    deriver, view = _synthetic_deriver()
    now = deriver.now
    view.tables["hr_persons"] = {
        "pers_cae": _synthetic_person(
            "pers_cae", "cae@example.test", _live_engagement("eng_cae", now)
        )
    }
    token = {
        "token_id": "tok_cae",
        "token_kind": "ACCESS",
        "issued_at_s": now - 10,
        "token_exp_s": now + 100,
        "revoke_ack_at_s": None,
        "parent_token_id": None,
        "next_spoke_poll_sync_at_s": now + 20,
        "cae_capable_spoke": False,
        "binding_kind": "BEARER",
        "etag": "a" * 16,
        "evidence_id": "ev_tok_cae",
    }
    view.tables["cae_incidents"] = {
        "inc_cae": {
            "incident_id": "inc_cae",
            "person_id": "pers_cae",
            "trigger_kind": "BENIGN_SESSION_RENEWAL",
            "event_ts_s": now,
            "tokens": [token],
            "etag": "b" * 16,
            "evidence_id": "ev_inc_cae",
        }
    }

    result = deriver.finish(deriver.derive_cae_containment_mutation("inc_cae"))

    assert view.version("inc_cae") in result["if_match"]
    assert view.version("pers_cae") in result["if_match"]
    assert "CAE-V5-01:ev_inc_cae" in result["cite"]
    assert "CAE-V5-01:ev_tok_cae" in result["cite"]


def test_v8_uar_inactive_person_is_precondition_but_item_is_citation_only() -> None:
    deriver, view = _synthetic_deriver()
    now = deriver.now
    person = _synthetic_person(
        "pers_uar", "uar@example.test", _live_engagement("eng_uar", now)
    )
    view.tables["hr_persons"] = {"pers_uar": person}
    view.tables["idp_accounts"] = {
        "acct_inactive": _synthetic_account(
            "acct_inactive", "pers_uar", "eng_uar", "uar@example.test"
        )
    }
    deriver.derived_by_target[("ACCOUNT_RECON_MUTATION", "acct_inactive")] = {
        "mutation_kind": "ACCOUNT_RECON_MUTATION",
        "target_entity_id": "acct_inactive",
        "account_action": "DISABLE_ACCOUNT",
        "bound_person_id": "pers_uar",
        "bound_engagement_id": "eng_uar",
        "retained_grant_ids": [],
    }
    item = {
        "item_id": "uar_item",
        "account_id": "acct_inactive",
        "grant_id": "grant_missing",
        "evidence_id": "ev_uar_item",
        "etag": "c" * 16,
    }
    view.tables["uar_campaigns"] = {
        "camp_v8": {
            "campaign_id": "camp_v8",
            "items": [item],
            "backup_reviewer_account_ids": [],
            "candidate_custodian_account_ids": [],
            "preserve_standing_floor_override": "FALSE",
            "scope_path": "/*",
            "admin_role_id": "role_missing",
            "etag": "d" * 16,
            "evidence_id": "ev_camp_v8",
        }
    }

    result = deriver.finish(deriver.derive_uar_campaign_mutation("camp_v8"))

    assert view.version("camp_v8") in result["if_match"]
    assert view.version("pers_uar") in result["if_match"]
    assert view.version("acct_inactive") not in result["if_match"]
    assert "UAR-V5-01:ev_camp_v8" in result["cite"]
    assert "UAR-V5-01:ev_uar_item" in result["cite"]
    assert "UAR-V5-01:ev_pers_uar" in result["cite"]
    assert "UAR-V5-01:ev_acct_inactive" not in result["cite"]


def test_v8_jit_approval_uses_gate_specific_citations() -> None:
    deriver, view = _synthetic_deriver()
    now = deriver.now
    leaf = view.scope_leaves[0]
    view.tables["hr_persons"] = {
        "pers_req": _synthetic_person(
            "pers_req", "req@example.test", _live_engagement("eng_req", now)
        ),
        "pers_appr": _synthetic_person(
            "pers_appr", "appr@example.test", _live_engagement("eng_appr", now)
        ),
    }
    view.tables["idp_accounts"] = {
        "acct_req": _synthetic_account("acct_req", "pers_req", "eng_req", "req@example.test"),
        "acct_appr": _synthetic_account(
            "acct_appr", "pers_appr", "eng_appr", "appr@example.test", ["grant_admin"]
        ),
    }
    view.tables["roles"] = {
        "role_req": {
            "role_id": "role_req",
            "app_id": "app_x",
            "privilege_tier": "read",
            "capabilities": ["cap_a"],
            "is_jit_capable_target": True,
            "evidence_id": "ev_role_req",
        },
        "role_admin": {
            "role_id": "role_admin",
            "app_id": "app_x",
            "privilege_tier": "admin",
            "capabilities": ["cap_admin"],
            "is_jit_capable_target": True,
        },
    }
    view.tables["grants"] = {
        "grant_admin": {
            "grant_id": "grant_admin",
            "account_id": "acct_appr",
            "role_id": "role_admin",
            "grant_mode": "standing",
            "scope_path": "/*",
        }
    }
    view.tables["jit_requests"] = {
        "req_jit": {
            "request_id": "req_jit",
            "requester_account_id": "acct_req",
            "requested_role_id": "role_req",
            "target_resource": "app_x",
            "requested_scope_path": leaf,
            "requested_ttl_s": 600,
            "request_channel": "EMERGENCY_BREAK_GLASS",
            "change_ticket_id": None,
            "proposed_approver_account_id": "acct_appr",
            "eval_seq": 1,
            "etag": "e" * 16,
            "evidence_id": "ev_req_jit",
        }
    }
    view.overlay["jit_ttl_cap_by_tier"] = [["read", 1800]]
    view.overlay["critical_jit_tiers"] = ["critical"]

    result = deriver.finish(deriver.derive_jit_decision_mutation("req_jit"))

    assert result["jit_decision"] == "APPROVE_JIT_COSIGN"
    assert view.version("req_jit") in result["if_match"]
    assert view.version("acct_req") in result["if_match"]
    assert view.version("pers_req") in result["if_match"]
    assert view.version("acct_appr") not in result["if_match"]
    assert set(result["cite"]) == {
        "JIT-V5-01.P1:ev_acct_req",
        "JIT-V5-01.P2:ev_req_jit",
        "JIT-V5-01.P3:ev_req_jit",
        "JIT-V5-01.P4:ev_acct_appr",
    }

    request = view.tables["jit_requests"]["req_jit"]
    request["change_ticket_id"] = "tkt_jit"
    view.tables["change_tickets"] = {
        "tkt_jit": {
            "ticket_id": "tkt_jit",
            "external_ticket_status": "APPROVED_EXTERNAL",
            "approved_at_s": now - 10,
            "expires_at_s": now + 1000,
            "bound_requester_account_id": "acct_req",
            "target_resource": "app_x",
            "approved_role_id": "role_req",
            "approved_scope_path": leaf,
            "approved_by_account_id": "acct_appr",
            "single_use": False,
            "etag": "f" * 16,
            "evidence_id": "ev_tkt_jit",
        }
    }
    deriver.pre, deriver.cites, deriver.rules = set(), set(), set()
    ticketed = deriver.finish(deriver.derive_jit_decision_mutation("req_jit"))
    assert ticketed["jit_decision"] == "APPROVE_JIT_COSIGN"
    assert view.version("tkt_jit") in ticketed["if_match"]
    assert view.version("acct_appr") in ticketed["if_match"]
    assert view.version("pers_appr") in ticketed["if_match"]
    assert "JIT-V5-01.P2:ev_tkt_jit" in ticketed["cite"]
    assert "JIT-V5-01.P2:ev_req_jit" not in ticketed["cite"]

    request["target_resource"] = "app_wrong"
    deriver.pre, deriver.cites, deriver.rules = set(), set(), set()
    p1_denial = deriver.finish(deriver.derive_jit_decision_mutation("req_jit"))
    assert p1_denial["jit_decision"] == "DENY"
    assert set(p1_denial["cite"]) == {
        "JIT-V5-01.P1:ev_req_jit",
        "JIT-V5-01.P1:ev_role_req",
    }


def test_v8_agent_delegation_approval_uses_gate_specific_citations() -> None:
    deriver, view = _synthetic_deriver()
    now = deriver.now
    leaf = view.scope_leaves[0]
    view.tables["hr_persons"] = {
        "pers_sponsor": _synthetic_person(
            "pers_sponsor", "sponsor@example.test", _live_engagement("eng_sponsor", now)
        ),
        "pers_actor": _synthetic_person(
            "pers_actor", "actor@example.test", _live_engagement("eng_actor", now)
        ),
    }
    view.tables["idp_accounts"] = {
        "acct_sponsor": _synthetic_account(
            "acct_sponsor", "pers_sponsor", "eng_sponsor", "sponsor@example.test"
        ),
        "acct_actor": _synthetic_account(
            "acct_actor", "pers_actor", "eng_actor", "actor@example.test", ["grant_actor"]
        ),
    }
    view.tables["roles"] = {
        "role_delegate": {
            "role_id": "role_delegate",
            "app_id": "app_x",
            "privilege_tier": "standard",
            "capabilities": ["cap_tool"],
            "is_jit_capable_target": True,
        }
    }
    view.tables["grants"] = {
        "grant_actor": {
            "grant_id": "grant_actor",
            "account_id": "acct_actor",
            "role_id": "role_delegate",
            "grant_mode": "standing",
            "scope_path": leaf,
        }
    }
    view.tables["enterprise_agents"] = {
        "agt_delegate": {
            "agent_id": "agt_delegate",
            "agent_gen": 1,
            "environment": "dev",
            "candidate_sponsor_person_id": "pers_sponsor",
            "tool_bindings": [
                {
                    "binding_id": "bind_delegate",
                    "requested_mode": "AUTONOMOUS_BOUNDED",
                    "capabilities": ["cap_tool"],
                    "scope_path": leaf,
                }
            ],
            "etag": "a" * 16,
            "evidence_id": "ev_agt_delegate",
        }
    }
    view._prior_maps["agent_outcomes"] = {
        "agt_delegate": {
            "agent_id": "agt_delegate",
            "p1_agent_action": "REGISTER_ACTIVE",
            "resolved_sponsor_person_id": "pers_sponsor",
            "active_autonomous_binding_ids": ["bind_delegate"],
            "active_hitl_binding_ids": [],
        }
    }
    view.tables["agent_delegation_requests"] = {
        "dlreq_v8": {
            "request_id": "dlreq_v8",
            "agent_id": "agt_delegate",
            "delegator_account_id": "acct_actor",
            "requested_role_id": "role_delegate",
            "target_resource": "app_x",
            "requested_scope_path": leaf,
            "delegation_hop": 1,
            "parent_delegation_request_id": None,
            "requested_ttl_s": 600,
            "proposed_hitl_approver_account_id": None,
            "workflow_id": "workflow_v8",
            "etag": "b" * 16,
            "evidence_id": "ev_dlreq_v8",
        }
    }
    view.overlay["max_agent_delegation_hops"] = 2
    view.overlay["jit_ttl_cap_by_tier"] = [["standard", 1800]]
    view.overlay["critical_jit_tiers"] = ["critical"]

    result = deriver.finish(deriver.derive_agent_delegation_mutation("dlreq_v8"))

    assert result["delegation_decision"] == "APPROVE_DELEGATION"
    assert view.version("dlreq_v8") in result["if_match"]
    assert view.version("agt_delegate") in result["if_match"]
    assert view.version("acct_actor") in result["if_match"]
    assert view.version("pers_actor") in result["if_match"]
    assert view.version("pers_sponsor") in result["if_match"]
    assert set(result["cite"]) == {
        "AGENT-DEL-V5-01.D1:ev_agt_delegate",
        "AGENT-DEL-V5-01.D2:ev_acct_actor",
        "AGENT-DEL-V5-01.D3:ev_dlreq_v8",
    }

    view._prior_maps["agent_outcomes"]["agt_delegate"]["p1_agent_action"] = (
        "SUSPEND_SPONSOR_INVALID"
    )
    deriver.pre, deriver.cites, deriver.rules = set(), set(), set()
    d1_denial = deriver.finish(deriver.derive_agent_delegation_mutation("dlreq_v8"))
    assert d1_denial["delegation_decision"] == "DENY_DELEGATION"
    assert set(d1_denial["cite"]) == {
        "AGENT-DEL-V5-01.D1:ev_dlreq_v8",
        "AGENT-DEL-V5-01.D1:ev_agt_delegate",
    }


def _obo_test_world() -> tuple[Any, Any, str]:
    deriver, view = _synthetic_deriver()
    now = deriver.now
    leaf = view.scope_leaves[0]
    view.tables["hr_persons"] = {
        "pers_sponsor": _synthetic_person(
            "pers_sponsor", "sponsor@example.test", _live_engagement("eng_sponsor", now)
        ),
        "pers_actor": _synthetic_person(
            "pers_actor", "actor@example.test", _live_engagement("eng_actor", now)
        ),
    }
    view.tables["idp_accounts"] = {
        "acct_sponsor": _synthetic_account(
            "acct_sponsor", "pers_sponsor", "eng_sponsor", "sponsor@example.test"
        ),
        "acct_actor": _synthetic_account(
            "acct_actor",
            "pers_actor",
            "eng_actor",
            "actor@example.test",
            ["grant_hist", "grant_current"],
        ),
    }
    view.tables["roles"] = {
        "role_hist": {
            "role_id": "role_hist",
            "app_id": "app_x",
            "privilege_tier": "standard",
            "capabilities": ["cap_hist"],
            "is_jit_capable_target": True,
        },
        "role_current": {
            "role_id": "role_current",
            "app_id": "app_x",
            "privilege_tier": "standard",
            "capabilities": ["cap_current"],
            "is_jit_capable_target": True,
        },
    }
    view.tables["grants"] = {
        "grant_hist": {
            "grant_id": "grant_hist",
            "account_id": "acct_actor",
            "role_id": "role_hist",
            "grant_mode": "standing",
            "scope_path": "/*",
        },
        "grant_current": {
            "grant_id": "grant_current",
            "account_id": "acct_actor",
            "role_id": "role_current",
            "grant_mode": "standing",
            "scope_path": "/*",
        },
    }
    view.tables["enterprise_agents"] = {
        "agt_delegate": {
            "agent_id": "agt_delegate",
            "agent_gen": 1,
            "environment": "dev",
            "candidate_sponsor_person_id": "pers_sponsor",
            "tool_bindings": [
                {
                    "binding_id": "bind_hist",
                    "requested_mode": "AUTONOMOUS_BOUNDED",
                    "capabilities": ["cap_hist"],
                    "scope_path": "/*",
                },
                {
                    "binding_id": "bind_current",
                    "requested_mode": "AUTONOMOUS_BOUNDED",
                    "capabilities": ["cap_current"],
                    "scope_path": "/*",
                },
            ],
            "etag": "a" * 16,
            "evidence_id": "ev_agt_delegate",
        }
    }
    view._prior_maps["agent_outcomes"] = {
        "agt_delegate": {
            "agent_id": "agt_delegate",
            "p1_agent_action": "REGISTER_ACTIVE",
            "resolved_sponsor_person_id": "pers_sponsor",
            "active_autonomous_binding_ids": ["bind_hist", "bind_current"],
            "active_hitl_binding_ids": [],
        }
    }
    view.tables["agent_delegation_requests"] = {}
    view.tables["historical_sessions"] = {}
    view.overlay["max_agent_delegation_hops"] = 2
    view.overlay["jit_ttl_cap_by_tier"] = [["standard", 1800]]
    view.overlay["critical_jit_tiers"] = ["critical"]
    view.overlay["temporal_sod_rules"] = []
    view.overlay["forbidden_sod_rules"] = []
    deriver.access.sod = []
    return deriver, view, leaf


def _obo_request(
    request_id: str,
    *,
    eval_seq: int,
    role_id: str = "role_current",
    workflow_id: str = "workflow_current",
    account_id: str = "acct_actor",
    scope_path: str = "/*",
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "eval_seq": eval_seq,
        "workflow_id": workflow_id,
        "agent_id": "agt_delegate",
        "delegator_account_id": account_id,
        "parent_delegation_request_id": None,
        "delegation_hop": 1,
        "requested_role_id": role_id,
        "requested_scope_path": scope_path,
        "target_resource": "app_x",
        "proposed_hitl_approver_account_id": None,
        "requested_ttl_s": 600,
        "etag": (hex(eval_seq)[2:] * 64)[:16],
        "evidence_id": f"ev_{request_id}",
    }


def _add_temporal_rule(
    view: Any,
    *,
    role_a: str = "role_hist",
    role_b: str = "role_current",
    cooldown_s: int = 300,
    scope_mode: str = "SAME_LEAF_SCOPE",
) -> dict[str, Any]:
    rule = {
        "tsod_id": "tsod_test",
        "role_a_id": role_a,
        "role_b_id": role_b,
        "cooldown_s": cooldown_s,
        "scope_mode": scope_mode,
        "evidence_id": "ev_tsod_test",
    }
    view.overlay["temporal_sod_rules"] = [rule]
    return rule


def _add_history_session(
    view: Any,
    *,
    now: int,
    account_id: str,
    person_id: str,
    engagement_id: str,
    role_id: str = "role_hist",
    scope_path: str = "/*",
    start_s: int | None = None,
    scheduled_end_s: int | None = None,
    revoked_at_s: int | None = None,
    audit_events_count: int = 1,
) -> None:
    if person_id not in view.tables["hr_persons"]:
        view.tables["hr_persons"][person_id] = _synthetic_person(
            person_id,
            f"{person_id}@example.test",
            _live_engagement(engagement_id, now),
        )
    elif not any(
        engagement.get("eng_id") == engagement_id
        for engagement in view.tables["hr_persons"][person_id].get("engagements", [])
    ):
        view.tables["hr_persons"][person_id]["engagements"].append(
            _live_engagement(engagement_id, now)
        )
    if account_id not in view.tables["idp_accounts"]:
        view.tables["idp_accounts"][account_id] = _synthetic_account(
            account_id,
            person_id,
            engagement_id,
            f"{person_id}@example.test",
        )
    start = now - 100 if start_s is None else start_s
    scheduled = now - 10 if scheduled_end_s is None else scheduled_end_s
    view.tables["historical_sessions"][f"sess_{account_id}"] = {
        "session_id": f"sess_{account_id}",
        "account_id": account_id,
        "role_id": role_id,
        "scope_path": scope_path,
        "session_kind": "AGENT_OBO",
        "start_s": start,
        "scheduled_end_s": scheduled,
        "revoked_at_s": revoked_at_s,
        "audit_events_count": audit_events_count,
        "last_audit_event_s": max(start, now - 1) if audit_events_count else None,
        "evidence_id": f"ev_sess_{account_id}",
    }


def _derive_and_record_obo(deriver: Any, request_id: str) -> dict[str, Any]:
    deriver.pre, deriver.cites, deriver.rules = set(), set(), set()
    mutation = deriver.derive_agent_delegation_mutation(request_id)
    result = deriver.finish(mutation)
    deriver.derived_by_target[("AGENT_DELEGATION_MUTATION", request_id)] = result
    return result


def test_obo_d3b_blocks_same_person_history_across_accounts_and_cites_witnesses() -> None:
    deriver, view, _ = _obo_test_world()
    now = deriver.now
    _add_temporal_rule(view, cooldown_s=300)
    _add_history_session(
        view,
        now=now,
        account_id="acct_actor_history",
        person_id="pers_actor",
        engagement_id="eng_actor_history",
        scheduled_end_s=now + 100,
    )
    request = _obo_request("dlreq_d3b", eval_seq=1)
    view.tables["agent_delegation_requests"][request["request_id"]] = request

    result = _derive_and_record_obo(deriver, request["request_id"])

    assert result["delegation_decision"] == "DENY_DELEGATION"
    assert "AGENT-DEL-V5-01.D3:ev_dlreq_d3b" in result["cite"]
    assert "AGENT-DEL-V5-01.D3:ev_sess_acct_actor_history" in result["cite"]
    assert "AGENT-DEL-V5-01.D3:ev_tsod_test" in result["cite"]
    assert "AGENT-DEL-V5-01.D1:ev_dlreq_d3b" not in result["cite"]


def test_obo_d3b_exact_cooldown_end_and_revoked_end_are_not_blocking() -> None:
    deriver, view, _ = _obo_test_world()
    now = deriver.now
    _add_temporal_rule(view, cooldown_s=100)
    _add_history_session(
        view,
        now=now,
        account_id="acct_actor_history",
        person_id="pers_actor",
        engagement_id="eng_actor_history",
        start_s=now - 200,
        scheduled_end_s=now + 100,
        revoked_at_s=now - 100,
    )
    request = _obo_request("dlreq_exact_end", eval_seq=1)
    view.tables["agent_delegation_requests"][request["request_id"]] = request

    result = _derive_and_record_obo(deriver, request["request_id"])

    assert result["delegation_decision"] == "APPROVE_DELEGATION"
    assert not any(cite.endswith("ev_tsod_test") for cite in result["cite"])


def test_obo_d3b_future_zero_audit_and_same_role_guards_do_not_block() -> None:
    now_cases = (
        ("future_positive_audit", 100, 1, "role_hist", "role_current"),
        ("zero_audit", -100, 0, "role_hist", "role_current"),
        ("same_role", -100, 1, "role_current", "role_current"),
    )
    for label, start_offset, audit_count, history_role, rule_role in now_cases:
        deriver, view, _ = _obo_test_world()
        now = deriver.now
        _add_temporal_rule(
            view,
            role_a=history_role,
            role_b=rule_role,
            cooldown_s=300,
        )
        _add_history_session(
            view,
            now=now,
            account_id=f"acct_guard_{label}",
            person_id="pers_actor",
            engagement_id=f"eng_guard_{label}",
            role_id=history_role,
            start_s=now + start_offset,
            scheduled_end_s=now + 500,
            audit_events_count=audit_count,
        )
        request = _obo_request(f"dlreq_{label}", eval_seq=1)
        view.tables["agent_delegation_requests"][request["request_id"]] = request

        result = _derive_and_record_obo(deriver, request["request_id"])

        assert result["delegation_decision"] == "APPROVE_DELEGATION", label
        assert not any(cite.endswith("ev_tsod_test") for cite in result["cite"]), label


def test_obo_d3b_requires_same_person_and_scope_unless_rule_is_global() -> None:
    deriver, view, other_leaf = _obo_test_world()
    now = deriver.now
    second_leaf = next((leaf for leaf in view.scope_leaves if leaf != other_leaf), None)
    assert second_leaf is not None
    _add_temporal_rule(view, cooldown_s=300, scope_mode="SAME_LEAF_SCOPE")
    _add_history_session(
        view,
        now=now,
        account_id="acct_other_person",
        person_id="pers_other",
        engagement_id="eng_other",
        scope_path=other_leaf,
    )
    request = _obo_request("dlreq_other_person", eval_seq=1)
    view.tables["agent_delegation_requests"][request["request_id"]] = request

    result = _derive_and_record_obo(deriver, request["request_id"])

    assert result["delegation_decision"] == "APPROVE_DELEGATION"

    deriver, view, first_leaf = _obo_test_world()
    now = deriver.now
    second_leaf = next(leaf for leaf in view.scope_leaves if leaf != first_leaf)
    _add_temporal_rule(view, cooldown_s=300, scope_mode="SAME_LEAF_SCOPE")
    _add_history_session(
        view,
        now=now,
        account_id="acct_actor_other_leaf",
        person_id="pers_actor",
        engagement_id="eng_actor_other_leaf",
        scope_path=first_leaf,
    )
    request = _obo_request("dlreq_same_leaf_scope", eval_seq=1, scope_path=second_leaf)
    view.tables["agent_delegation_requests"][request["request_id"]] = request

    result = _derive_and_record_obo(deriver, request["request_id"])

    assert result["delegation_decision"] == "APPROVE_DELEGATION"

    deriver, view, first_leaf = _obo_test_world()
    now = deriver.now
    second_leaf = next(leaf for leaf in view.scope_leaves if leaf != first_leaf)
    _add_temporal_rule(view, cooldown_s=300, scope_mode="GLOBAL")
    _add_history_session(
        view,
        now=now,
        account_id="acct_actor_other_leaf",
        person_id="pers_actor",
        engagement_id="eng_actor_other_leaf",
        scope_path=first_leaf,
    )
    request = _obo_request("dlreq_global_scope", eval_seq=1, scope_path=second_leaf)
    view.tables["agent_delegation_requests"][request["request_id"]] = request

    result = _derive_and_record_obo(deriver, request["request_id"])

    assert result["delegation_decision"] == "DENY_DELEGATION"
    assert "AGENT-DEL-V5-01.D3:ev_sess_acct_actor_other_leaf" in result["cite"]
    assert "AGENT-DEL-V5-01.D3:ev_tsod_test" in result["cite"]


def test_obo_approved_prior_eval_seq_history_blocks_across_workflows() -> None:
    deriver, view, _ = _obo_test_world()
    _add_temporal_rule(view, cooldown_s=300)
    first = _obo_request(
        "dlreq_prior_obo",
        eval_seq=1,
        role_id="role_hist",
        workflow_id="workflow_one",
    )
    later = _obo_request(
        "dlreq_later_obo",
        eval_seq=2,
        role_id="role_current",
        workflow_id="workflow_two",
    )
    view.tables["agent_delegation_requests"] = {
        first["request_id"]: first,
        later["request_id"]: later,
    }

    first_result = _derive_and_record_obo(deriver, first["request_id"])
    later_result = _derive_and_record_obo(deriver, later["request_id"])

    assert first_result["delegation_decision"] == "APPROVE_DELEGATION"
    assert later_result["delegation_decision"] == "DENY_DELEGATION"
    assert "AGENT-DEL-V5-01.D3:ev_dlreq_prior_obo" in later_result["cite"]
    assert "AGENT-DEL-V5-01.D3:ev_tsod_test" in later_result["cite"]


def test_obo_d3a_conflict_has_precedence_over_temporal_d3b_citations() -> None:
    deriver, view, _ = _obo_test_world()
    now = deriver.now
    _add_temporal_rule(view, cooldown_s=300)
    view.overlay["forbidden_sod_rules"] = [
        {
            "rule_code": "WORKFLOW_PAIR",
            "cap_a": "cap_hist",
            "cap_b": "cap_current",
            "scope_mode": "SAME_LEAF_SCOPE",
        }
    ]
    deriver.access.sod = view.overlay["forbidden_sod_rules"]
    view.tables["idp_accounts"]["acct_actor"]["direct_grant_ids"] = ["grant_current"]
    _add_history_session(
        view,
        now=now,
        account_id="acct_actor_history",
        person_id="pers_actor",
        engagement_id="eng_actor_history",
        scheduled_end_s=now + 100,
    )
    request = _obo_request("dlreq_d3a_precedence", eval_seq=1)
    view.tables["agent_delegation_requests"][request["request_id"]] = request

    result = _derive_and_record_obo(deriver, request["request_id"])

    assert result["delegation_decision"] == "DENY_DELEGATION"
    assert "AGENT-DEL-V5-01.D3:ev_dlreq_d3a_precedence" in result["cite"]
    assert "AGENT-DEL-V5-01.D3:ev_sess_acct_actor_history" in result["cite"]
    assert not any(cite.endswith("ev_tsod_test") for cite in result["cite"])


def test_obo_d1_failure_precedes_a_temporal_d3b_blocker() -> None:
    deriver, view, _ = _obo_test_world()
    now = deriver.now
    _add_temporal_rule(view, cooldown_s=300)
    _add_history_session(
        view,
        now=now,
        account_id="acct_actor_history",
        person_id="pers_actor",
        engagement_id="eng_actor_history",
    )
    view._prior_maps["agent_outcomes"]["agt_delegate"]["p1_agent_action"] = (
        "SUSPEND_UNATTESTED_OR_STALE"
    )
    request = _obo_request("dlreq_d1_precedence", eval_seq=1)
    view.tables["agent_delegation_requests"][request["request_id"]] = request

    result = _derive_and_record_obo(deriver, request["request_id"])

    assert result["delegation_decision"] == "DENY_DELEGATION"
    assert "AGENT-DEL-V5-01.D1:ev_dlreq_d1_precedence" in result["cite"]
    assert "AGENT-DEL-V5-01.D1:ev_agt_delegate" in result["cite"]
    assert not any(cite.endswith("ev_tsod_test") for cite in result["cite"])


def test_obo_d3b_consumes_earlier_approved_jit_session_history() -> None:
    deriver, view, leaf = _obo_test_world()
    _add_temporal_rule(view, cooldown_s=300)
    now = deriver.now
    view.tables["hr_persons"]["pers_approver"] = _synthetic_person(
        "pers_approver", "approver@example.test", _live_engagement("eng_approver", now)
    )
    view.tables["idp_accounts"]["acct_approver"] = _synthetic_account(
        "acct_approver",
        "pers_approver",
        "eng_approver",
        "approver@example.test",
        ["grant_admin"],
    )
    view.tables["roles"]["role_admin"] = {
        "role_id": "role_admin",
        "app_id": "app_x",
        "privilege_tier": "admin",
        "capabilities": ["cap_admin"],
        "is_jit_capable_target": True,
    }
    view.tables["grants"]["grant_admin"] = {
        "grant_id": "grant_admin",
        "account_id": "acct_approver",
        "role_id": "role_admin",
        "grant_mode": "standing",
        "scope_path": "/*",
    }
    jit_request = {
        "request_id": "req_prior_jit",
        "requester_account_id": "acct_actor",
        "requested_role_id": "role_hist",
        "target_resource": "app_x",
        "requested_scope_path": leaf,
        "requested_ttl_s": 600,
        "request_channel": "EMERGENCY_BREAK_GLASS",
        "change_ticket_id": None,
        "proposed_approver_account_id": "acct_approver",
        "eval_seq": 1,
        "etag": "e" * 16,
        "evidence_id": "ev_req_prior_jit",
    }
    view.tables["jit_requests"] = {jit_request["request_id"]: jit_request}
    deriver.pre, deriver.cites, deriver.rules = set(), set(), set()
    jit = deriver.finish(deriver.derive_jit_decision_mutation(jit_request["request_id"]))
    deriver.derived_by_target[("JIT_DECISION_MUTATION", jit_request["request_id"])] = jit
    request = _obo_request("dlreq_after_jit", eval_seq=2)
    view.tables["agent_delegation_requests"][request["request_id"]] = request

    result = _derive_and_record_obo(deriver, request["request_id"])

    assert jit["jit_decision"] == "APPROVE_JIT_COSIGN"
    assert result["delegation_decision"] == "DENY_DELEGATION"
    assert "AGENT-DEL-V5-01.D3:ev_req_prior_jit" in result["cite"]
    assert "AGENT-DEL-V5-01.D3:ev_tsod_test" in result["cite"]


def test_uar_uses_public_cae_retro_taint_for_bridge_and_bound_person_provenance() -> None:
    deriver, view = _synthetic_deriver(phase_index=2)
    now = deriver.now
    old_dept, current_dept = view.scope_leaves[:2]
    person = _synthetic_person(
        "pers_subject",
        "subject@example.test",
        _live_engagement("eng_subject", now, dept_path=current_dept),
    )
    person["engagements"][0]["prior_dept_path"] = old_dept
    person["engagements"][0]["dept_transfer_effective_at_s"] = now - 1
    reviewer = _synthetic_person(
        "pers_reviewer", "reviewer@example.test", _live_engagement("eng_reviewer", now)
    )
    view.tables["hr_persons"] = {"pers_subject": person, "pers_reviewer": reviewer}
    view.tables["idp_accounts"] = {
        "acct_subject": _synthetic_account(
            "acct_subject", "pers_subject", "eng_subject", "subject@example.test", ["grant_old"]
        ),
        "acct_reviewer": _synthetic_account(
            "acct_reviewer", "pers_reviewer", "eng_reviewer", "reviewer@example.test"
        ),
    }
    view._prior_maps["account_outcomes"] = {
        "acct_subject": {
            "account_id": "acct_subject",
            "p1_action": "KEEP_ACTIVE",
            "bound_person_id": "pers_subject",
            "bound_engagement_id": "eng_subject",
            "effective_standing_grant_ids": ["grant_old"],
            "effective_eligible_grant_ids": [],
        },
        "acct_reviewer": {
            "account_id": "acct_reviewer",
            "p1_action": "KEEP_ACTIVE",
            "bound_person_id": "pers_reviewer",
            "bound_engagement_id": "eng_reviewer",
            "effective_standing_grant_ids": [],
            "effective_eligible_grant_ids": [],
        },
    }
    view.tables["roles"] = {
        "role_old": {
            "role_id": "role_old",
            "app_id": "app_x",
            "privilege_tier": "read",
            "capabilities": ["cap_old"],
            "is_jit_capable_target": True,
        }
    }
    view.tables["grants"] = {
        "grant_old": {
            "grant_id": "grant_old",
            "account_id": "acct_subject",
            "role_id": "role_old",
            "grant_mode": "standing",
            "scope_path": old_dept,
            "sod_prune_priority": 1,
            "evidence_id": "ev_grant_old",
        }
    }
    bridge_id = "brg_shock_old"
    view.tables["transfer_bridges"] = {
        bridge_id: {
            "bridge_id": bridge_id,
            "account_id": "acct_subject",
            "grant_id": "grant_old",
            "from_dept_path": old_dept,
            "to_dept_path": current_dept,
            "target_resource": "app_x",
            "approved_by_account_id": "acct_reviewer",
            "approved_at_s": now - 10,
            "expires_at_s": now + 100,
            "etag": "c" * 16,
            "evidence_id": "ev_bridge_shock_old",
        }
    }
    view.tables["uar_campaigns"] = {
        "camp_shock": {
            "campaign_id": "camp_shock",
            "app_id": "app_x",
            "scope_path": current_dept,
            "admin_role_id": "role_old",
            "m_standing": 0,
            "m_eligible": 0,
            "preserve_standing_floor_override": "FALSE",
            "backup_reviewer_account_ids": [],
            "candidate_custodian_account_ids": [],
            "items": [
                {
                    "item_id": "item_shock_old",
                    "account_id": "acct_subject",
                    "grant_id": "grant_old",
                    "assigned_reviewer_account_id": "acct_reviewer",
                    "used_within_lookback": True,
                    "evidence_id": "ev_item_shock_old",
                }
            ],
            "etag": "d" * 16,
            "evidence_id": "ev_camp_shock",
        }
    }
    view.manifest["invalidated_entity_ids"] = ["pers_subject"]
    deriver.derived_by_target[("CAE_CONTAINMENT_MUTATION", "inc_shock")] = {
        "mutation_kind": "CAE_CONTAINMENT_MUTATION",
        "target_entity_id": "inc_shock",
        "retro_invalidated_ids": [bridge_id],
    }

    result = deriver.finish(deriver.derive_uar_campaign_mutation("camp_shock"))

    assert bridge_id not in view.manifest["invalidated_entity_ids"]
    assert result["uar_decisions"] == [["item_shock_old", "REVOKE"]]
    assert view.version(bridge_id) in result["if_match"]
    assert view.version("pers_subject") in result["if_match"]
    assert view.version("acct_subject") not in result["if_match"]
    assert "UAR-V5-01:ev_bridge_shock_old" in result["cite"]
    assert "UAR-V5-01:ev_pers_subject" in result["cite"]


def test_account_cc_provenance_excludes_live_control_used_only_by_pruned_grant() -> None:
    deriver, view = _synthetic_deriver()
    _install_sod_control_world(deriver, view)
    leaf = view.scope_leaves[0]
    account = view.tables["idp_accounts"]["acct_subject"]
    account["group_ids"] = ["grp_allow_two_caps"]
    view.tables["groups"] = {
        "grp_allow_two_caps": {
            "group_id": "grp_allow_two_caps",
            "effect": "ALLOW",
            "scope_path": leaf,
            "capabilities": ["cap_a", "cap_c"],
            "granted_role_ids": [],
            "evidence_id": "ev_grp_allow_two_caps",
        }
    }
    view.tables["compensating_controls"]["cc_test"]["covered_scope_path"] = leaf
    view.tables["compensating_controls"]["cc_test"]["etag"] = "c" * 16
    deriver.access.sod = [
        {
            "rule_code": "PAIR_A",
            "cap_a": "cap_a",
            "cap_b": "cap_b",
            "scope_mode": "SAME_LEAF_SCOPE",
        },
        {
            "rule_code": "PAIR_C",
            "cap_a": "cap_c",
            "cap_b": "cap_b",
            "scope_mode": "SAME_LEAF_SCOPE",
        },
    ]
    view.overlay["forbidden_sod_rules"] = deriver.access.sod

    result = deriver.finish(deriver.derive_account_recon_mutation("acct_subject"))

    assert result["retained_grant_ids"] == ["grant_a"]
    assert view.version("cc_test") not in result["if_match"]
    assert "CC-V5-01:ev_cc_test" not in result["cite"]
    assert "CC-V5-01:ev_conn_audit" not in result["cite"]
    assert "CC-V5-01:ev_acct_monitor" not in result["cite"]
