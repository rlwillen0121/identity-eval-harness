"""Focused synthetic tests for the production shared-conformance adapter."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter, ValidationError

from identitybench.v5 import conformance_production as production
from identitybench.v5.conformance_production import (
    _g_prop_metric,
    _patch_one,
    _PatchPathAbsent,
    _phase_stage_args,
    _read_ledger,
    evaluate_shared_input,
)
from identitybench.v5.conformance_shared import (
    PINNED_PUBLIC_SCHEMA_SHA256,
    PROTOCOL,
    freeze_case,
    sha256_json,
    validate_evaluator_result,
)
from identitybench.v5.runtime import RuntimeV5, ViewV5
from identitybench.v5.schemas import (
    COMPACT_TABLE_COLUMNS_V5,
    CAEIncidentV5,
    CAETokenV5,
    CompactTableV5,
    DeliveredSliceV5,
    JITRequestV5,
    OptCandidateActionV5,
    OptimizationMutationV5,
    OptimizationTaskV5,
    PhaseManifestV5,
    PhaseWorkQueueV5,
    PolicyCardV5,
    PolicyOverlayV5,
    PriorAccountOutcomeV5,
    PriorPhaseLedgerV5,
    ScopeCatalogV5,
    ScopedSoDRuleV5,
    SliceDescriptorV5,
    WorldStateV5,
    encode_compact_table_v5,
)
from identitybench.v5.verifier import evaluate_phase as verify_phase


def test_rfc6902_add_replace_remove_preserve_array_order() -> None:
    document = {"items": ["first", "last"], "old": True}

    document = _patch_one(document, {"op": "add", "path": "/items/1", "value": "middle"})
    document = _patch_one(document, {"op": "replace", "path": "/old", "value": False})
    document = _patch_one(document, {"op": "remove", "path": "/items/0"})

    assert document == {"items": ["middle", "last"], "old": False}


def test_rfc6902_copy_deep_copies_and_decodes_escaped_member() -> None:
    document = {"source/a": {"values": [1]}, "target": {}}

    updated = _patch_one(
        document,
        {"op": "copy", "from": "/source~1a", "path": "/target/copied"},
    )
    updated["source/a"]["values"].append(2)

    assert updated["target"]["copied"] == {"values": [1]}


def test_rfc6902_missing_path_is_classified_separately() -> None:
    with pytest.raises(_PatchPathAbsent):
        _patch_one({"present": {}}, {"op": "replace", "path": "/present/missing", "value": 1})


def test_rfc6902_root_replacement_is_supported() -> None:
    assert _patch_one({"before": 1}, {"op": "replace", "path": "", "value": [3, 2]}) == [3, 2]


def test_strict_json_validation_accepts_json_arrays_for_tuple_fields() -> None:
    manifest = {
        "phase_index": 1,
        "clock_epoch_s": 50,
        "query_budget": 1,
        "work_queue": {"target_opt_task_ids": ["opt_001"]},
        "source_directory": [
            {
                "slice_id": "sl_p1",
                "source_system": "fixture",
                "table_names": ["hr_persons"],
                "scope_filter": "global=*",
                "authority_tier": "AUTHORITATIVE_PRIMARY",
                "is_complete_for_scope": True,
                "watermark_s": 50,
                "row_count": 1,
                "credit_cost": 1,
            }
        ],
    }

    with pytest.raises(ValidationError):
        PhaseManifestV5.model_validate(manifest, strict=True)

    parsed = production._strict_json_validate(TypeAdapter(PhaseManifestV5), manifest)

    assert parsed.work_queue.target_opt_task_ids == ("opt_001",)
    assert parsed.source_directory[0].table_names == ("hr_persons",)


def test_gap_path_conversion_emits_rfc6901_pointers() -> None:
    assert production._json_pointer("stimulus.recipe.patches") == "/stimulus/recipe/patches"
    assert production._json_pointer("x~y.a/b") == "/x~0y/a~1b"


@pytest.mark.parametrize("operation", ["validate_submission", "score_trace"])
def test_target_mutation_recipe_replaces_one_row_in_full_plan(operation: str) -> None:
    base_plan = [
        {"mutation_kind": "KIND_A", "target_entity_id": "target_a", "value": 1},
        {"mutation_kind": "KIND_B", "target_entity_id": "target_b", "value": "before"},
        {"mutation_kind": "KIND_C", "target_entity_id": "target_c", "value": 3},
    ]
    source = {
        "recipe": {
            "candidate_source": "independent_base",
            "base_scope": "target_mutation",
            "patches": [{"op": "replace", "path": "/value", "value": "after"}],
        },
        "target_selector": {"mutation_kind": "KIND_B", "target_entity_id": "target_b"},
    }

    candidate, materialization = production._materialize_candidate_source(
        {"operation": operation, "stimulus": {}}, source, base_plan
    )

    expected = [
        base_plan[0],
        {"mutation_kind": "KIND_B", "target_entity_id": "target_b", "value": "after"},
        base_plan[2],
    ]
    assert candidate == expected
    assert materialization == {"base_plan": base_plan, "submitted_plan": expected}
    assert base_plan[1]["value"] == "before"


def test_safety_target_mutation_materialization_is_focused_on_selected_row() -> None:
    base_plan = [
        {"mutation_kind": "KIND_A", "target_entity_id": "target_a", "value": 1},
        {"mutation_kind": "KIND_B", "target_entity_id": "target_b", "value": "before"},
    ]
    selected = {"mutation_kind": "KIND_B", "target_entity_id": "target_b"}
    source = {
        "recipe": {
            "candidate_source": "independent_base",
            "base_scope": "target_mutation",
            "patches": [{"op": "replace", "path": "/value", "value": "after"}],
        },
        "target_selector": selected,
    }

    candidate, materialization = production._materialize_candidate_source(
        {"operation": "safety", "stimulus": {"target_selector": selected}},
        source,
        base_plan,
    )

    assert candidate == {**base_plan[1], "value": "after"}
    assert materialization == {
        "base_plan": base_plan[1],
        "submitted_plan": candidate,
    }


def test_safety_phase_plan_materialization_is_focused_on_selected_row() -> None:
    base_plan = [
        {"mutation_kind": "KIND_A", "target_entity_id": "target_a", "value": 1},
        {"mutation_kind": "KIND_B", "target_entity_id": "target_b", "value": "before"},
    ]
    selector = {"mutation_kind": "KIND_B", "target_entity_id": "target_b"}
    source = {
        "recipe": {
            "candidate_source": "independent_base",
            "base_scope": "phase_plan",
            "patches": [{"op": "replace", "path": "/1/value", "value": "after"}],
        },
        "target_selector": selector,
    }

    candidate, materialization = production._materialize_candidate_source(
        {"operation": "safety", "stimulus": {"target_selector": selector}},
        source,
        base_plan,
    )

    assert candidate[1] == {**base_plan[1], "value": "after"}
    assert materialization == {
        "base_plan": base_plan[1],
        "submitted_plan": candidate[1],
    }


def test_literal_score_plan_has_null_base_and_keeps_exact_json() -> None:
    literal = [{"z": [3, 1], "mutation_kind": "literal"}]
    candidate, materialization = production._materialize_candidate_source(
        {"operation": "score_trace", "stimulus": {}},
        {"submitted_plan": literal},
        [{"mutation_kind": "independent"}],
    )

    base_plan, submitted_plan = production._score_event_plan_materialization(
        candidate, materialization
    )
    assert base_plan is None
    assert submitted_plan is literal


def test_rule_coverage_uses_materialized_base_citations_only() -> None:
    materialization = {
        "base_plan": [{"cite": ["BASE-RULE:ev_base"]}],
        "submitted_plan": [{"cite": ["CANDIDATE-RULE:ev_candidate"]}],
        "event_plans": [
            {
                "base_plan": None,
                "submitted_plan": [{"cite": ["LITERAL-RULE:ev_literal"]}],
            },
            {
                "base_plan": [{"cite": ["TRACE-RULE:ev_trace"]}],
                "submitted_plan": [{"cite": ["OTHER-RULE:ev_other"]}],
            },
        ],
    }

    assert production._materialized_base_rule_prefixes(materialization) == [
        "BASE-RULE",
        "TRACE-RULE",
    ]


def test_snapshot_rows_preserve_staged_order_and_event_bindings() -> None:
    rows = [
        {"mutation_kind": "KIND_Z", "target_entity_id": "z"},
        {"mutation_kind": "KIND_A", "target_entity_id": "a"},
    ]
    snapshot_rows = production._snapshot_mutation_rows(rows)
    assert snapshot_rows == rows
    assert sha256_json(snapshot_rows) == sha256_json(rows)

    event_plan_rows = [
        {"seq": 4, "phase_index": 1},
        {"seq": 9, "phase_index": 2},
    ]
    production._assert_stage_event_plan_bijection([(4, 1), (9, 2)], event_plan_rows)
    with pytest.raises(production._UnsupportedTrace):
        production._assert_stage_event_plan_bijection([(4, 1), (9, 2)], event_plan_rows[::-1])

    commit_snapshot = SimpleNamespace(phase_index=2)
    assert (
        production._snapshot_event_binding(
            {id(commit_snapshot): (12, 2, "commit_phase_mutations")},
            commit_snapshot,
            boundary="COMMIT",
        )
        == 12
    )
    with pytest.raises(production._UnsupportedTrace):
        production._snapshot_event_binding(
            {id(commit_snapshot): (12, 2, "stage_phase_mutations")},
            commit_snapshot,
            boundary="COMMIT",
        )


def test_public_transition_projection_clones_in_runtime_without_episode_metadata() -> None:
    catalog = ScopeCatalogV5(leaf_scopes=("/org/public",))
    overlay = PolicyOverlayV5(
        freshness_sla_s=600,
        rotation_sla_s=600,
        cae_sla_s=120,
        conversion_grace_s=600,
        break_glass_cooldown_s=600,
        allow_contractor_nhi_owners=False,
        allow_contractor_agent_sponsors=False,
        enforce_cross_nhi_owner_sod=True,
        enforce_sponsor_agent_sod=True,
        permit_hitl_step_up_for_autonomous=True,
        require_immutable_hr_correlation=False,
        connector_unmapped_policy_default="QUARANTINE_DRIFT",
        uar_preserve_standing_floor_default=True,
        owner_excluded_dept_prefixes=(),
        max_prod_nhis_per_owner=4,
        max_prod_agents_per_sponsor=4,
        max_agent_delegation_hops=4,
        critical_jit_tiers=("critical",),
        jit_ttl_cap_by_tier=(
            ("read", 600),
            ("standard", 600),
            ("elevated", 600),
            ("admin", 600),
            ("critical", 600),
        ),
        forbidden_sod_rules=(
            ScopedSoDRuleV5(
                rule_code="sod_fixture",
                cap_a="approve",
                cap_b="pay",
                scope_mode="SAME_LEAF_SCOPE",
            ),
        ),
        temporal_sod_rules=(),
    )
    card = PolicyCardV5(
        spec_version="5.0",
        scope_catalog=catalog,
        overlay=overlay,
        rendered_rules_markdown="public test policy",
    )

    def manifest(
        phase: int,
        slice_id: str,
        clock: int,
        *,
        table_names: tuple[str, ...] = ("hr_persons",),
        row_count: int = 1,
    ) -> PhaseManifestV5:
        return PhaseManifestV5(
            phase_index=phase,
            clock_epoch_s=clock,
            query_budget=1,
            work_queue=PhaseWorkQueueV5(
                target_opt_task_ids=("opt_fixture",),
                target_cae_incident_ids=("inc_fixture",) if phase == 2 else (),
                target_jit_request_ids=("req_fixture",) if phase == 2 else (),
            ),
            source_directory=(
                SliceDescriptorV5(
                    slice_id=slice_id,
                    source_system="fixture",
                    table_names=table_names,
                    scope_filter="global=*",
                    authority_tier="AUTHORITATIVE_PRIMARY",
                    is_complete_for_scope=True,
                    watermark_s=clock,
                    row_count=row_count,
                    credit_cost=1,
                ),
            ),
            invalidated_entity_ids=("pers_fixture",) if phase == 2 else (),
        )

    phase1_manifest = manifest(1, "sl_p1", 50)
    phase2_manifest = manifest(
        2,
        "sl_p2",
        100,
        table_names=("hr_persons", "cae_incidents", "jit_requests"),
        row_count=3,
    )

    def delivered_slice(slice_id: str, watermark: int) -> DeliveredSliceV5:
        return DeliveredSliceV5(
            slice_id=slice_id,
            authority_tier="AUTHORITATIVE_PRIMARY",
            watermark_s=watermark,
            tables=(
                CompactTableV5(
                    table_name="hr_persons",
                    columns=COMPACT_TABLE_COLUMNS_V5["hr_persons"],
                    rows=(),
                ),
            ),
        )

    phase1_slices = {"sl_p1": delivered_slice("sl_p1", 50)}
    incident = CAEIncidentV5(
        incident_id="inc_fixture",
        person_id="pers_fixture",
        trigger_kind="HR_TERMINATION_EVENT",
        event_ts_s=100,
        tokens=(
            CAETokenV5(
                token_id="tok_fixture",
                account_id="acct_fixture",
                token_kind="REFRESH",
                binding_kind="BOUND_DPOP",
                cae_capable_spoke=True,
                issued_at_s=90,
                token_exp_s=1000,
                next_spoke_poll_sync_at_s=120,
                evidence_id="ev_token_fixture",
            ),
        ),
        etag="0123456789abcdef",
        evidence_id="ev_incident_fixture",
    )
    jit_request = JITRequestV5(
        request_id="req_fixture",
        eval_seq=1,
        requester_account_id="acct_fixture",
        requested_role_id="role_fixture",
        requested_scope_path="/org/public",
        target_resource="app_fixture",
        request_channel="STANDARD_TICKET",
        proposed_approver_account_id="acct_fixture",
        requested_ttl_s=60,
        etag="0123456789abcdef",
        evidence_id="ev_jit_fixture",
    )
    phase2_slices = {
        "sl_p2": DeliveredSliceV5(
            slice_id="sl_p2",
            authority_tier="AUTHORITATIVE_PRIMARY",
            watermark_s=100,
            tables=(
                CompactTableV5(
                    table_name="hr_persons",
                    columns=COMPACT_TABLE_COLUMNS_V5["hr_persons"],
                    rows=(),
                ),
                encode_compact_table_v5("cae_incidents", (incident,)),
                encode_compact_table_v5("jit_requests", (jit_request,)),
            ),
        )
    }
    phase1_view = ViewV5.from_slices(
        phase1_manifest,
        tuple(phase1_slices.values()),
        policy_card=card,
    )
    public_phase2_view = ViewV5.from_slices(
        phase2_manifest,
        tuple(phase2_slices.values()),
        policy_card=card,
        base_view=phase1_view,
        invalidated_entity_ids=phase2_manifest.invalidated_entity_ids,
    )
    public_phase2_state = public_phase2_view.to_policy_state()
    projection = production._public_transition_projection(
        episode_id="ep_public_projection",
        policy_card=card,
        phase1_manifest=phase1_manifest,
        phase1_slices=phase1_slices,
        phase2_base_manifest=phase2_manifest,
        phase2_slices=phase2_slices,
    )
    world = WorldStateV5(
        phase_index=1,
        world_rev=0,
        clock_epoch_s=50,
        scope_catalog=catalog,
        overlay=overlay,
        hr_persons={},
        roles={},
        grants={},
        groups={},
        idp_accounts={},
        transfer_bridges={},
        compensating_controls={},
        connector_schemas={},
        connector_pages=(),
        hr_source_mappings={},
        baseline_nhis={},
        target_nhis={},
        baseline_agents={},
        enterprise_agents={},
        cloud_ciem_tasks={},
        optimization_tasks={},
        jml_workflow_tasks={},
    )

    assert set(projection.model_fields_set) == {
        "episode_id",
        "policy_card",
        "phase1_manifest",
        "phase1_slices",
        "phase2_base_manifest",
        "phase2_slices",
        "phase2_delta_person_ids",
    }
    assert projection.phase2_delta_person_ids == ("pers_fixture",)
    assert set(public_phase2_state.cae_incidents) == {"inc_fixture"}
    assert set(public_phase2_state.jit_requests) == {"req_fixture"}

    def ledger(etag: str) -> PriorPhaseLedgerV5:
        return PriorPhaseLedgerV5(
            through_phase_index=1,
            account_outcomes=(
                PriorAccountOutcomeV5(
                    account_id="acct_fixture",
                    p1_action="KEEP_ACTIVE",
                    current_account_gen=1,
                    current_etag=etag,
                ),
            ),
            nhi_outcomes=(),
            agent_outcomes=(),
            connector_outcomes=(),
            hr_mapping_outcomes=(),
            ciem_outcomes=(),
            workflow_outcomes=(),
        )

    gold_induced_state2 = public_phase2_state.model_copy(
        update={"world_rev": 1, "prior_phase_ledger": ledger("0123456789abcdef")},
        deep=True,
    )
    actual_induced_state2 = public_phase2_state.model_copy(
        update={"world_rev": 2, "prior_phase_ledger": ledger("fedcba9876543210")},
        deep=True,
    )
    gold_context = production._phase2_context_from_induced(public_phase2_state, gold_induced_state2)
    actual_context = production._phase2_context_from_induced(
        public_phase2_state, actual_induced_state2
    )
    assert gold_context.cae_incidents == public_phase2_state.cae_incidents
    assert gold_context.jit_requests == public_phase2_state.jit_requests
    assert gold_context.clock_epoch_s == public_phase2_state.clock_epoch_s == 100
    assert gold_context.phase_index == actual_context.phase_index == 2
    assert gold_context.world_rev == 1
    assert actual_context.world_rev == 2
    assert gold_context.prior_phase_ledger != actual_context.prior_phase_ledger

    runtime = RuntimeV5(production._runtime_episode_argument(projection), world)
    cloned_projection = runtime.episode

    assert cloned_projection is not projection
    assert cloned_projection.episode_id == "ep_public_projection"
    assert cloned_projection.phase2_delta_person_ids == ("pers_fixture",)
    assert not hasattr(cloned_projection, "family_id")


def test_adapter_does_not_normalize_unexpected_public_model_validation_error() -> None:
    case = {
        "protocol": PROTOCOL,
        "case_id": "synthetic-bad-public-packet",
        "operation": "derive_phase",
        "suite_id": "v5_dev",
        "phase_index": 1,
        "public_episode": {
            "episode_id": "ep_synthetic",
            "phase1_manifest": {},
            "phase1_slices": {},
            "policy_card": {},
            "q1_slice_ids_p1": [],
            "q2_slice_ids_p1": [],
        },
        "rendered_card": "",
        "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
        "stimulus": {},
    }

    with pytest.raises(ValidationError):
        evaluate_shared_input(case)


def test_score_trace_preserves_supplied_phase_and_stage_identity() -> None:
    stage_args = {"phase_index": 2, "stage_id": "stg_public_2", "plan_source": {}}
    cosign_args = {
        "phase_index": 2,
        "stage_id": "stg_public_2",
        "scope_kind": "CRITICAL_JIT",
        "target_entity_id": "req_public",
        "actor_account_id": "acct_public",
    }
    commit_args = {"phase_index": 2, "stage_id": "stg_public_2"}

    stage_identity = _phase_stage_args(stage_args)
    assert _phase_stage_args(cosign_args) == stage_identity
    assert _phase_stage_args(commit_args) == stage_identity
    assert stage_identity == (2, "stg_public_2")


def test_g_prop_uses_actual_induced_state_only_for_coupled_mode() -> None:
    assert (
        _g_prop_metric(
            "COUPLED",
            p1_committed=True,
            p2_committed=True,
            phase2_feasible=True,
            reference_p2_pass=False,
            actual_p2_pass=True,
        )
        == 1
    )
    assert (
        _g_prop_metric(
            "COUPLED",
            p1_committed=True,
            p2_committed=True,
            phase2_feasible=True,
            reference_p2_pass=True,
            actual_p2_pass=False,
        )
        == 0
    )
    assert (
        _g_prop_metric(
            "ORACLE_PHASE_RESET",
            p1_committed=True,
            p2_committed=True,
            phase2_feasible=False,
            reference_p2_pass=True,
            actual_p2_pass=False,
        )
        == 1
    )


def test_phase_local_p2_uses_delivered_ledger_and_trace_replay_uses_own_p1_ledger(
    monkeypatch,
) -> None:
    supplied_ledger = object()
    own_p1_ledger = object()
    public_phase2_state = SimpleNamespace(prior_phase_ledger=supplied_ledger)
    induced_gold_state2 = SimpleNamespace(prior_phase_ledger=own_p1_ledger)
    gold_reference_state2 = SimpleNamespace(prior_phase_ledger=own_p1_ledger)
    initial_state = object()
    manifest1 = SimpleNamespace(work_queue=object())
    manifest2 = SimpleNamespace(work_queue=object())
    phase2_view = SimpleNamespace()
    projection = object()

    monkeypatch.setattr(
        production,
        "_both_phase_inputs",
        lambda _case: (
            None,
            manifest1,
            {},
            manifest2,
            {},
            None,
            phase2_view,
            initial_state,
            public_phase2_state,
            projection,
        ),
    )
    observed_states = []
    p1_mutations = (object(),)
    monkeypatch.setattr(
        production.oracle,
        "evaluate_phase_with_trace",
        lambda state, _queue: (
            observed_states.append(state)
            or SimpleNamespace(mutations=p1_mutations, trace_by_target={})
        ),
    )
    monkeypatch.setattr(
        production,
        "induce_phase2_world",
        lambda _state, mutations, _queue, _episode: (
            induced_gold_state2 if mutations == p1_mutations else object()
        ),
    )
    monkeypatch.setattr(
        production,
        "_phase2_context_from_induced",
        lambda _public_state, induced_state: (
            gold_reference_state2 if induced_state is induced_gold_state2 else object()
        ),
    )

    production._phase_gold({"phase_index": 2})
    assert observed_states == [public_phase2_state]
    assert public_phase2_state.prior_phase_ledger is not own_p1_ledger

    observed_states.clear()
    production._phase_gold({"phase_index": 2}, use_induced_phase1_ledger=True)
    assert observed_states == [initial_state, gold_reference_state2]
    assert gold_reference_state2.prior_phase_ledger is own_p1_ledger


def test_p1_only_trace_replays_rejections_and_gaps_on_transition(monkeypatch) -> None:
    public_episode = {
        "episode_id": "ep_p1_only",
        "phase1_manifest": {},
        "phase1_slices": {},
        "policy_card": {},
        "q1_slice_ids_p1": [],
        "q2_slice_ids_p1": [],
    }
    case = {
        "protocol": PROTOCOL,
        "case_id": "p1-only-score-trace",
        "operation": "score_trace",
        "suite_id": "v5_dev",
        "phase_index": 1,
        "public_episode": public_episode,
        "rendered_card": "",
        "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
        "stimulus": {
            "mode": "ORACLE_PHASE_RESET",
            "events": [
                {
                    "seq": 1,
                    "tool": "query_sources",
                    "args": {"phase_index": 1, "slice_ids": ["sl_p1"]},
                },
                {
                    "seq": 2,
                    "tool": "stage_phase_mutations",
                    "args": {
                        "phase_index": 1,
                        "stage_id": "stg_p1_only",
                        "plan_source": {
                            "submitted_plan": [
                                {
                                    "mutation_kind": "OPTIMIZATION_MUTATION",
                                    "target_entity_id": "opt_fixture",
                                    "if_match": ["opt_fixture@0123456789abcdef:1"],
                                    "cite": ["OPT-V5-01:ev_opt_fixture"],
                                    "selected_ids": [],
                                    "objective_value": 0,
                                }
                            ]
                        },
                    },
                },
                {
                    "seq": 3,
                    "tool": "commit_phase_mutations",
                    "args": {"phase_index": 1, "stage_id": "stg_never_staged"},
                },
            ],
        },
    }
    card = PolicyCardV5(
        spec_version="5.0",
        scope_catalog=ScopeCatalogV5(leaf_scopes=("/org/public",)),
        overlay=PolicyOverlayV5(
            freshness_sla_s=600,
            rotation_sla_s=600,
            cae_sla_s=120,
            conversion_grace_s=600,
            break_glass_cooldown_s=600,
            allow_contractor_nhi_owners=False,
            allow_contractor_agent_sponsors=False,
            enforce_cross_nhi_owner_sod=True,
            enforce_sponsor_agent_sod=True,
            permit_hitl_step_up_for_autonomous=True,
            require_immutable_hr_correlation=False,
            connector_unmapped_policy_default="QUARANTINE_DRIFT",
            uar_preserve_standing_floor_default=True,
            owner_excluded_dept_prefixes=(),
            max_prod_nhis_per_owner=4,
            max_prod_agents_per_sponsor=4,
            max_agent_delegation_hops=4,
            critical_jit_tiers=("critical",),
            jit_ttl_cap_by_tier=(
                ("read", 600),
                ("standard", 600),
                ("elevated", 600),
                ("admin", 600),
                ("critical", 600),
            ),
            forbidden_sod_rules=(
                ScopedSoDRuleV5(
                    rule_code="sod_p1_fixture",
                    cap_a="approve",
                    cap_b="pay",
                    scope_mode="SAME_LEAF_SCOPE",
                ),
            ),
            temporal_sod_rules=(),
        ),
        rendered_rules_markdown="public test policy",
    )
    manifest = PhaseManifestV5(
        phase_index=1,
        clock_epoch_s=100,
        query_budget=1,
        work_queue=PhaseWorkQueueV5(target_opt_task_ids=("opt_fixture",)),
        source_directory=(
            SliceDescriptorV5(
                slice_id="sl_p1",
                source_system="fixture",
                table_names=("optimization_tasks",),
                scope_filter="global=*",
                authority_tier="AUTHORITATIVE_PRIMARY",
                is_complete_for_scope=True,
                watermark_s=100,
                row_count=1,
                credit_cost=1,
            ),
        ),
    )
    optimization_task = OptimizationTaskV5(
        task_id="opt_fixture",
        phase_kind="P1_ZSP_REFACTOR",
        candidates=(
            OptCandidateActionV5(
                candidate_id="cand_fixture",
                action_kind="ASSIGN_ROLE_STANDING",
                target_person_id="pers_fixture",
                role_id="role_fixture",
                scope_path="/org/public",
                action_cost=1,
            ),
        ),
        etag="0123456789abcdef",
        evidence_id="ev_opt_fixture",
    )
    p1_slice = DeliveredSliceV5(
        slice_id="sl_p1",
        authority_tier="AUTHORITATIVE_PRIMARY",
        watermark_s=100,
        tables=(encode_compact_table_v5("optimization_tasks", (optimization_task,)),),
    )
    slices1 = {"sl_p1": p1_slice}
    public_episode.update(
        {
            "phase1_manifest": manifest.model_dump(mode="json"),
            "phase1_slices": {
                slice_id: delivered.model_dump(mode="json")
                for slice_id, delivered in slices1.items()
            },
            "policy_card": card.model_dump(mode="json"),
            "q1_slice_ids_p1": ["sl_p1"],
            "q2_slice_ids_p1": [],
        }
    )
    case["rendered_card"] = card.rendered_rules_markdown
    initial_state = ViewV5.from_slices(
        manifest, tuple(slices1.values()), policy_card=card
    ).to_policy_state()
    monkeypatch.setattr(
        production,
        "_typed_public_inputs",
        lambda _case: (card, manifest, slices1, object(), initial_state),
    )
    monkeypatch.setattr(
        production,
        "_both_phase_inputs",
        lambda _case: pytest.fail("P1-only path loaded P2 fields"),
    )
    fixture_mutation = OptimizationMutationV5(
        mutation_kind="OPTIMIZATION_MUTATION",
        target_entity_id="opt_fixture",
        if_match=("opt_fixture@0123456789abcdef:1",),
        cite=("OPT-V5-01:ev_opt_fixture",),
        selected_ids=(),
        objective_value=0,
    )
    fixture_trace = SimpleNamespace(
        mutation=fixture_mutation,
        preconditions=fixture_mutation.if_match,
        citations=fixture_mutation.cite,
        context={
            "feasibility": lambda _selected: SimpleNamespace(
                feasible=True,
                policy_ok=True,
                coverage_ok=True,
            )
        },
    )
    monkeypatch.setattr(
        production.oracle,
        "evaluate_phase_with_trace",
        lambda _state, _queue: SimpleNamespace(
            mutations=(fixture_mutation,),
            trace_by_target={("OPTIMIZATION_MUTATION", "opt_fixture"): fixture_trace},
        ),
    )

    direct_runtime = RuntimeV5(
        production._PublicPhase1RuntimeProjectionV5(
            episode_id="ep_p1_only",
            policy_card=card,
            phase1_manifest=manifest,
            phase1_slices=slices1,
        ),
        initial_state,
    )
    direct_receipt = direct_runtime.commit_phase_mutations(1, "stg_never_staged")
    assert direct_receipt.status == "COMMIT_BLOCKED_STRUCTURAL"
    assert direct_receipt.world_rev == 0
    assert direct_receipt.phase2_manifest is None
    assert direct_runtime.phase2_opened is False

    instances = []
    commit_receipts = []

    class RecordingRuntime(RuntimeV5):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)

        def commit_phase_mutations(self, phase_index, stage_id):
            receipt = super().commit_phase_mutations(phase_index, stage_id)
            commit_receipts.append(receipt)
            return receipt

    monkeypatch.setattr(production, "RuntimeV5", RecordingRuntime)

    payload, _coverage, materialization = production._score_trace(case)

    assert set(instances[0].episode.model_fields_set) == {
        "episode_id",
        "policy_card",
        "phase1_manifest",
        "phase1_slices",
    }
    assert instances[0].wave_execution_mode == "COUPLED"
    assert commit_receipts[0].status == "COMMIT_BLOCKED_STRUCTURAL"
    assert commit_receipts[0].world_rev == 0
    assert commit_receipts[0].phase2_manifest is None
    assert instances[0].phase_index == 1
    assert instances[0].phase2_opened is False
    assert payload["metrics"]["p2_observed"] == 0
    assert materialization == {
        "event_plans": [
            {
                "seq": 2,
                "phase_index": 1,
                "base_plan": None,
                "submitted_plan": [
                    {
                        "mutation_kind": "OPTIMIZATION_MUTATION",
                        "target_entity_id": "opt_fixture",
                        "if_match": ["opt_fixture@0123456789abcdef:1"],
                        "cite": ["OPT-V5-01:ev_opt_fixture"],
                        "selected_ids": [],
                        "objective_value": 0,
                    }
                ],
            }
        ]
    }

    stage_only_result = production.evaluate_shared_input(case)
    assert stage_only_result["status"] == "underdefined"
    assert stage_only_result["materialization"] == materialization
    assert stage_only_result["payload"]["metrics"]["G-SRR@first_stage"] is None
    assert stage_only_result["payload"]["metrics"]["repair_lift"] is None
    assert stage_only_result["gaps"] == [
        {
            "code": "UNDERDEFINED_PUBLIC_INPUT",
            "operation": "score_trace",
            "case_id": "p1-only-score-trace",
            "path": "/public_episode",
            "detail": (
                "first-stage scenario eligibility is unresolved by the public contract; "
                "G-SRR@first_stage and repair_lift are uncomputed without P2 context"
            ),
        }
    ]
    validate_evaluator_result(stage_only_result, freeze_case(case), "production")

    empty_trace_case = {
        **case,
        "stimulus": {"mode": "ORACLE_PHASE_RESET", "events": []},
    }
    empty_trace_result = production.evaluate_shared_input(empty_trace_case)
    assert empty_trace_result["status"] == "underdefined"
    assert empty_trace_result["materialization"] == {"event_plans": []}
    assert empty_trace_result["payload"]["metrics"]["G-SRR@first_stage"] is None
    assert empty_trace_result["payload"]["metrics"]["repair_lift"] is None
    assert empty_trace_result["gaps"] == stage_only_result["gaps"]
    validate_evaluator_result(empty_trace_result, freeze_case(empty_trace_case), "production")

    case["stimulus"]["events"][2]["args"]["stage_id"] = "stg_p1_only"
    transition_result = production.evaluate_shared_input(case)
    assert transition_result["status"] == "underdefined"
    assert transition_result["materialization"] == materialization
    assert transition_result["coverage"]["snapshots"] == _coverage["snapshots"]
    assert transition_result["coverage"]["rules"] == _coverage["rules"]
    assert transition_result["payload"]["snapshot_derivations"] == payload["snapshot_derivations"]
    transition_metrics = transition_result["payload"]["metrics"]
    assert set(transition_metrics) == set(payload["metrics"])
    assert (
        transition_metrics["p1_first_stage_snapshot_ok"]
        == payload["metrics"]["p1_first_stage_snapshot_ok"]
    )
    assert transition_metrics["p1_staged"] == 1
    assert (
        transition_metrics["P1-Action-SRR@last_stage"]
        == payload["metrics"]["P1-Action-SRR@last_stage"]
    )
    assert transition_metrics["G-SRR@first_stage"] is None
    assert transition_metrics["repair_lift"] is None
    assert transition_metrics["Sem-SRR@commit"] == 0
    assert transition_metrics["Action-SRR@last_stage"] == 0
    assert transition_metrics["G-PROP"] == 0
    assert transition_metrics["G-SRR"] == 0
    assert transition_metrics["p2_observed"] == 0
    assert transition_metrics["both_phases_staged"] == 0
    assert not any(
        row["boundary"] == "COMMIT" for row in transition_result["payload"]["snapshot_derivations"]
    )
    assert transition_result["gaps"] == [
        {
            "code": "UNDERDEFINED_PUBLIC_INPUT",
            "operation": "score_trace",
            "case_id": "p1-only-score-trace",
            "path": "/public_episode/phase2_delta_person_ids",
            "detail": (
                "successful P1 commit requires unavailable P2 transition field "
                "phase2_delta_person_ids"
            ),
        },
        {
            "code": "UNDERDEFINED_PUBLIC_INPUT",
            "operation": "score_trace",
            "case_id": "p1-only-score-trace",
            "path": "/public_episode",
            "detail": (
                "first-stage scenario eligibility is unresolved by the public contract; "
                "G-SRR@first_stage and repair_lift are uncomputed without P2 context"
            ),
        },
    ]
    validate_evaluator_result(transition_result, freeze_case(case), "production")

    case["stimulus"]["events"][1]["args"]["phase_index"] = 2
    case["stimulus"]["events"][2]["args"]["stage_id"] = "stg_never_staged"
    rejected_p2_result = production.evaluate_shared_input(case)
    assert rejected_p2_result["status"] == "underdefined"
    assert rejected_p2_result["payload"]["metrics"]["p2_observed"] == 0
    assert rejected_p2_result["gaps"] == [
        {
            "code": "UNDERDEFINED_PUBLIC_INPUT",
            "operation": "score_trace",
            "case_id": "p1-only-score-trace",
            "path": "/public_episode",
            "detail": (
                "first-stage scenario eligibility is unresolved by the public contract; "
                "G-SRR@first_stage and repair_lift are uncomputed without P2 context"
            ),
        }
    ]
    validate_evaluator_result(rejected_p2_result, freeze_case(case), "production")

    case["stimulus"]["events"][1]["args"]["plan_source"] = {
        "recipe": {
            "candidate_source": "independent_base",
            "base_scope": "phase_plan",
            "patches": [],
        }
    }
    missing_p2_base_result = production.evaluate_shared_input(case)
    assert missing_p2_base_result["status"] == "underdefined"
    assert missing_p2_base_result["gaps"] == [
        {
            "code": "UNMATERIALIZABLE_INPUT",
            "operation": "score_trace",
            "case_id": "p1-only-score-trace",
            "path": "/stimulus/events",
            "detail": "phase-2 plan recipe requires a public phase-2 base plan",
        }
    ]


def test_reset_runtime_starts_from_gold_induced_phase2_state(monkeypatch) -> None:
    public_reset_state = object()
    gold_reset_state = object()
    gold_induced_state = object()
    gold_p1_mutations = (object(),)
    manifest1 = SimpleNamespace(work_queue=object())
    manifest2 = SimpleNamespace(work_queue=object())
    view2 = SimpleNamespace(to_policy_state=lambda: public_reset_state)
    episode = object()

    monkeypatch.setattr(
        production,
        "_both_phase_inputs",
        lambda _case: (
            None,
            manifest1,
            {},
            manifest2,
            {},
            None,
            view2,
            object(),
            public_reset_state,
            episode,
        ),
    )
    monkeypatch.setattr(
        production.oracle,
        "evaluate_phase_with_trace",
        lambda _state, _queue: SimpleNamespace(
            mutations=gold_p1_mutations,
            trace_by_target={},
        ),
    )
    monkeypatch.setattr(
        production,
        "induce_phase2_world",
        lambda _state, mutations, _queue, actual_episode: (
            gold_induced_state
            if mutations == gold_p1_mutations and actual_episode is episode
            else object()
        ),
    )
    monkeypatch.setattr(
        production,
        "_phase2_context_from_induced",
        lambda public_state, induced_state: (
            gold_reset_state
            if public_state is public_reset_state and induced_state is gold_induced_state
            else object()
        ),
    )
    phase_snapshots = SimpleNamespace(first_stage=None, last_stage=None, commit=None)

    instances = []

    class RuntimeStub:
        def __init__(self, *_args, **kwargs):
            self.reset_state = kwargs["phase2_reset_state"]
            self.snapshots = []
            self.phase2_opened = False
            instances.append(self)

        def set_usage_totals(self, **_kwargs):
            pass

        def phase_snapshots(self, _phase_index):
            return phase_snapshots

    monkeypatch.setattr(production, "RuntimeV5", RuntimeStub)
    production._score_trace(
        {
            "case_id": "synthetic-reset",
            "phase_index": 2,
            "stimulus": {"mode": "ORACLE_PHASE_RESET", "events": []},
        }
    )

    assert instances[0].reset_state is gold_reset_state


def test_p2_ledger_evicts_stale_p1_person_evidence_but_keeps_refresh() -> None:
    case = {
        "phase_index": 2,
        "public_episode": {
            "phase1_slices": {
                "p1_old": {
                    "tables": [
                        {
                            "table_name": "hr_persons",
                            "columns": ["person_id", "evidence_id", "nested"],
                            "rows": [
                                [
                                    "pers_refreshed",
                                    "ev_old_person",
                                    {"evidence_id": "ev_nested_history"},
                                ]
                            ],
                        }
                    ]
                }
            },
            "phase2_slices": {
                "p2_refresh": {
                    "tables": [
                        {
                            "table_name": "hr_persons",
                            "columns": ["person_id", "evidence_id", "nested"],
                            "rows": [
                                [
                                    "pers_refreshed",
                                    "ev_new_person",
                                    {"evidence_id": "ev_new_nested"},
                                ]
                            ],
                        }
                    ]
                }
            },
            "q1_slice_ids_p1": ["p1_old"],
            "q2_slice_ids_p1": [],
            "q1_slice_ids_p2": ["p2_refresh"],
            "q2_slice_ids_p2": [],
            "phase2_base_manifest": {"invalidated_entity_ids": ["pers_refreshed"]},
        },
    }
    card = SimpleNamespace(overlay=SimpleNamespace(temporal_sod_rules=()))

    read_ledger = _read_ledger(case, card)

    target = ("OPTIMIZATION_MUTATION", "opt_test")

    def citation_result(evidence_id: str):
        citation = f"OPT-V5-01:{evidence_id}"
        mutation = OptimizationMutationV5(
            mutation_kind="OPTIMIZATION_MUTATION",
            target_entity_id="opt_test",
            if_match=("opt_test@0123456789abcdef:1",),
            cite=(citation,),
            selected_ids=(),
            objective_value=0,
        )
        trace = SimpleNamespace(
            mutation=mutation,
            preconditions=mutation.if_match,
            citations=mutation.cite,
            context={
                "feasibility": lambda _selected: SimpleNamespace(policy_ok=True, coverage_ok=True)
            },
        )
        return verify_phase(
            phase_index=2,
            required_targets=(target,),
            mutations=(mutation,),
            trace_by_target={target: trace},
            read_ledger=read_ledger,
            stage1_ok=True,
            stage2_ok=True,
            stage2_valid_by_target={target: True},
        )

    refreshed_citation = citation_result("ev_new_person")
    stale_citation = citation_result("ev_old_person")
    nested_citation = citation_result("ev_nested_history")

    assert refreshed_citation.first_failing_stage is None
    assert refreshed_citation.stage4_ok
    assert stale_citation.first_failing_stage == 4
    assert not stale_citation.stage4_ok
    assert nested_citation.first_failing_stage is None


@pytest.mark.parametrize(
    ("operation", "stimulus", "failure_target"),
    [
        ("derive_phase", {}, "_derive"),
        ("validate_submission", {"submitted_plan": []}, "_submission_value"),
        (
            "safety",
            {
                "submitted_plan": [],
                "target_selector": {
                    "mutation_kind": "OPTIMIZATION_MUTATION",
                    "target_entity_id": "opt_fixture",
                },
            },
            "_submission_value",
        ),
        ("score_trace", {"mode": "COUPLED", "events": []}, "_score_trace"),
    ],
)
def test_unexpected_operation_errors_escape_evaluator(
    monkeypatch, operation: str, stimulus: dict[str, object], failure_target: str
) -> None:
    case = {
        "protocol": PROTOCOL,
        "case_id": f"unexpected-{operation}",
        "operation": operation,
        "suite_id": "v5_dev",
        "phase_index": 1,
        "public_episode": {},
        "rendered_card": "",
        "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
        "stimulus": stimulus,
    }

    def fail_unexpected(*_args, **_kwargs):
        raise RuntimeError("unexpected fixture failure")

    monkeypatch.setattr(production, "validate_public_case", lambda value: value)
    monkeypatch.setattr(production, failure_target, fail_unexpected)

    with pytest.raises(RuntimeError, match="unexpected fixture failure"):
        production.evaluate_shared_input(case)


@pytest.mark.parametrize("operation", ["validate_submission", "safety"])
def test_unexpected_recipe_materialization_errors_escape_evaluator(
    monkeypatch, operation: str
) -> None:
    stimulus: dict[str, object] = {
        "recipe": {
            "candidate_source": "independent_base",
            "base_scope": "phase_plan",
            "patches": [],
        }
    }
    if operation == "safety":
        stimulus["target_selector"] = {
            "mutation_kind": "OPTIMIZATION_MUTATION",
            "target_entity_id": "opt_fixture",
        }
    case = {
        "protocol": PROTOCOL,
        "case_id": f"unexpected-recipe-{operation}",
        "operation": operation,
        "suite_id": "v5_dev",
        "phase_index": 1,
        "public_episode": {},
        "rendered_card": "",
        "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
        "stimulus": stimulus,
    }

    def fail_unexpected(*_args, **_kwargs):
        raise RuntimeError("unexpected recipe failure")

    monkeypatch.setattr(production, "validate_public_case", lambda value: value)
    monkeypatch.setattr(production, "_materialize_source", fail_unexpected)
    if operation == "validate_submission":
        monkeypatch.setattr(
            production,
            "_validate_submission",
            lambda _case: {"first_failing_stage": None, "diagnostics": []},
        )
    else:
        monkeypatch.setattr(
            production,
            "_safety",
            lambda _case: (
                {"overgrant": False, "availability_breach": False},
                {
                    "phase_targets": [],
                    "diagnostics": [],
                    "rules": [],
                    "safety_cases": [],
                    "snapshots": [],
                },
                [],
            ),
        )

    with pytest.raises(RuntimeError, match="unexpected recipe failure"):
        production.evaluate_shared_input(case)


def test_typed_patch_path_error_remains_a_gap(monkeypatch) -> None:
    case = {
        "protocol": PROTOCOL,
        "case_id": "typed-patch-path-gap",
        "operation": "validate_submission",
        "suite_id": "v5_dev",
        "phase_index": 1,
        "public_episode": {},
        "rendered_card": "",
        "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
        "stimulus": {
            "recipe": {
                "candidate_source": "independent_base",
                "base_scope": "phase_plan",
                "patches": [],
            }
        },
    }
    monkeypatch.setattr(production, "validate_public_case", lambda value: value)
    monkeypatch.setattr(
        production,
        "_validate_submission",
        lambda _case: {"first_failing_stage": None, "diagnostics": []},
    )

    def missing_path(*_args, **_kwargs):
        raise production._PatchPathAbsent("member is absent")

    monkeypatch.setattr(
        production,
        "_materialize_source",
        missing_path,
    )

    result = production.evaluate_shared_input(case)

    assert result["status"] == "underdefined"
    assert result["gaps"] == [
        {
            "code": "PATCH_PATH_ABSENT",
            "operation": "validate_submission",
            "case_id": "typed-patch-path-gap",
            "path": "/stimulus/recipe/patches",
            "detail": "member is absent",
        }
    ]


def test_typed_unsupported_trace_error_remains_unsupported(monkeypatch) -> None:
    case = {
        "protocol": PROTOCOL,
        "case_id": "typed-unsupported-trace",
        "operation": "score_trace",
        "suite_id": "v5_dev",
        "phase_index": 1,
        "public_episode": {},
        "rendered_card": "",
        "schema_sha256": PINNED_PUBLIC_SCHEMA_SHA256,
        "stimulus": {"mode": "COUPLED", "events": []},
    }

    def unsupported(_case, **_kwargs):
        raise production._UnsupportedTrace("unsupported public event contract")

    monkeypatch.setattr(production, "validate_public_case", lambda value: value)
    monkeypatch.setattr(production, "_score_trace", unsupported)

    result = production.evaluate_shared_input(case)

    assert result["status"] == "unsupported"
    assert result["gaps"] == [
        {
            "code": "UNSUPPORTED_OPERATION",
            "operation": "score_trace",
            "case_id": "typed-unsupported-trace",
            "path": "/stimulus/events",
            "detail": "unsupported public event contract",
        }
    ]
