from __future__ import annotations

from types import SimpleNamespace

import pytest

from identitybench.v5 import schemas as s
from identitybench.v5.generator import (
    _build_phase2_world,
    _build_world,
    _regime_overlay,
    _sealed_jml_events,
    build_v5_episode,
    plan_v5_cells,
)
from identitybench.v5.oracle import (
    _PhaseContext,
    evaluate_phase,
    evaluate_phase_with_trace,
    live_version,
)
from identitybench.v5.oracle_p2 import (
    Phase2InfeasibilityV5,
    _campaign_quorum_sets,
    _interval_union_measure,
    _Phase2WorkingState,
    _phase22_uar,
    _phase24_optimization,
    _post_uar_live_controls,
    _retro_taint,
    _token_active_interval,
    _verify_post_uar_invariants,
    phase2_feasibility,
)
from identitybench.v5.runtime import apply_phase1


def _token(
    token_id: str,
    token_kind: str,
    *,
    parent_token_id: str | None = None,
    binding_kind: str = "BOUND_DPOP",
    cae_capable_spoke: bool = True,
    issued_at_s: int = 100,
    token_exp_s: int = 300,
    revoke_ack_at_s: int | None = None,
    next_spoke_poll_sync_at_s: int = 200,
) -> s.CAETokenV5:
    return s.CAETokenV5(
        token_id=token_id,
        account_id="acct_owner",
        token_kind=token_kind,
        parent_token_id=parent_token_id,
        binding_kind=binding_kind,
        cae_capable_spoke=cae_capable_spoke,
        issued_at_s=issued_at_s,
        token_exp_s=token_exp_s,
        revoke_ack_at_s=revoke_ack_at_s,
        next_spoke_poll_sync_at_s=next_spoke_poll_sync_at_s,
        evidence_id=f"ev_{token_id}",
    )


def test_cae_refresh_cutoff_push_and_bearer_rules_are_distinct() -> None:
    incident = SimpleNamespace(event_ts_s=100)
    parent = _token("tok_refresh", "REFRESH", issued_at_s=0, revoke_ack_at_s=120)
    dpop = _token("tok_dpop", "ACCESS", parent_token_id="tok_refresh")
    bearer = _token(
        "tok_bearer",
        "ACCESS",
        parent_token_id="tok_refresh",
        binding_kind="BEARER_UNBOUND",
    )
    tokens = {token.token_id: token for token in (parent, dpop, bearer)}

    assert _token_active_interval(incident, parent, tokens, {}, {}) is None
    assert _token_active_interval(incident, dpop, tokens, {}, {}) == (100, 120)
    assert _token_active_interval(incident, bearer, tokens, {}, {}) == (100, 200)


def test_refresh_expiry_alone_does_not_push_to_existing_child() -> None:
    incident = SimpleNamespace(event_ts_s=100)
    parent = _token("tok_refresh", "REFRESH", issued_at_s=0, token_exp_s=120)
    child = _token("tok_access", "ACCESS", parent_token_id="tok_refresh")
    tokens = {parent.token_id: parent, child.token_id: child}

    assert _token_active_interval(incident, child, tokens, {}, {}) == (100, 200)


def test_child_at_parent_mint_cutoff_is_not_active() -> None:
    incident = SimpleNamespace(event_ts_s=100)
    parent = _token("tok_refresh", "REFRESH", issued_at_s=0, token_exp_s=120)
    child = _token(
        "tok_access",
        "ACCESS",
        parent_token_id="tok_refresh",
        issued_at_s=120,
    )
    tokens = {parent.token_id: parent, child.token_id: child}

    assert _token_active_interval(incident, child, tokens, {}, {}) is None


def test_interval_union_uses_half_open_overlap_and_gaps() -> None:
    assert _interval_union_measure([(1, 4), (3, 8), (9, 10), None]) == 8


def test_retro_taint_only_considers_people_in_targeted_incidents() -> None:
    target = SimpleNamespace(
        person_id="pers_target",
        engagements=[SimpleNamespace(retro_terminated_effective_s=100)],
    )
    unrelated = SimpleNamespace(
        person_id="pers_unrelated",
        engagements=[SimpleNamespace(retro_terminated_effective_s=10)],
    )
    ticket_target = SimpleNamespace(
        ticket_id="tkt_target",
        approved_by_account_id="acct_target",
        approved_at_s=100,
        evidence_id="ev_ticket_target",
    )
    ticket_unrelated = SimpleNamespace(
        ticket_id="tkt_unrelated",
        approved_by_account_id="acct_unrelated",
        approved_at_s=20,
        evidence_id="ev_ticket_unrelated",
    )
    state = SimpleNamespace(
        cae_incidents={"inc_target": SimpleNamespace(person_id="pers_target")},
        hr_persons={"pers_target": target, "pers_unrelated": unrelated},
        change_tickets={
            ticket_target.ticket_id: ticket_target,
            ticket_unrelated.ticket_id: ticket_unrelated,
        },
        transfer_bridges={},
        prior_phase_ledger=SimpleNamespace(
            account_by_id={
                "acct_target": SimpleNamespace(bound_person_id="pers_target"),
                "acct_unrelated": SimpleNamespace(bound_person_id="pers_unrelated"),
            }
        ),
    )
    ctx = SimpleNamespace(
        state=state,
        queue=SimpleNamespace(target_cae_incident_ids=("inc_target",)),
    )
    work = _Phase2WorkingState(
        ctx=ctx,
        grants={},
        standing={},
        eligible={},
        active_account_ids=set(),
        bound_person_by_account={},
        bound_engagement_by_account={},
        retro_tainted_ids=set(),
        retro_tainted_evidence={},
    )

    assert _retro_taint(work) == ({"tkt_target"}, {"tkt_target": "ev_ticket_target"})


def _world_primitives() -> tuple[dict[str, object], dict[str, object]]:
    catalog = s.ScopeCatalogV5(leaf_scopes=("/dept/finance/payroll", "/dept/finance/treasury"))
    admin_role = s.RoleV5(
        role_id="role_admin",
        app_id="app_finance",
        privilege_tier="admin",
        capabilities=("approve",),
        evidence_id="ev_role_admin",
    )
    reviewer_role = s.RoleV5(
        role_id="role_reviewer",
        app_id="app_finance",
        privilege_tier="standard",
        capabilities=("read",),
        evidence_id="ev_role_reviewer",
    )
    subject = s.HRPersonV5(
        person_id="pers_subject",
        emp_immutable_id="employee_subject",
        primary_email="subject@example.test",
        engagements=(
            s.EngagementV5(
                eng_id="eng_subject",
                affiliation="FTE",
                eng_status="ACTIVE",
                dept_path="/dept/finance",
                start_s=0,
            ),
        ),
        etag="a" * 16,
        evidence_id="ev_person_subject",
    )
    reviewer = s.HRPersonV5(
        person_id="pers_reviewer",
        emp_immutable_id="employee_reviewer",
        primary_email="reviewer@example.test",
        engagements=(
            s.EngagementV5(
                eng_id="eng_reviewer",
                affiliation="FTE",
                eng_status="ACTIVE",
                dept_path="/dept/finance",
                start_s=0,
            ),
        ),
        etag="b" * 16,
        evidence_id="ev_person_reviewer",
    )
    subject_account = s.IdPAccountV5(
        account_id="acct_subject",
        subkind="human_primary",
        login_email="subject@example.test",
        immutable_person_ref="pers_subject",
        status="ACTIVE",
        etag="c" * 16,
        evidence_id="ev_account_subject",
    )
    reviewer_account = s.IdPAccountV5(
        account_id="acct_reviewer",
        subkind="human_primary",
        login_email="reviewer@example.test",
        immutable_person_ref="pers_reviewer",
        status="ACTIVE",
        etag="d" * 16,
        evidence_id="ev_account_reviewer",
    )
    grant = s.GrantV5(
        grant_id="grant_review",
        account_id="acct_subject",
        role_id="role_admin",
        grant_mode="standing",
        scope_path="/dept/finance",
        sod_prune_priority=1,
        evidence_id="ev_grant_review",
    )
    state = {
        "scope_catalog": catalog,
        "roles": {admin_role.role_id: admin_role, reviewer_role.role_id: reviewer_role},
        "hr_persons": {subject.person_id: subject, reviewer.person_id: reviewer},
        "idp_accounts": {
            subject_account.account_id: subject_account,
            reviewer_account.account_id: reviewer_account,
        },
        "grants": {grant.grant_id: grant},
    }
    return state, {"grant": grant, "subject": subject, "reviewer": reviewer}


def _generated_phase2(
    *, unique_quorum_app: bool = False
) -> tuple[s.WorldStateV5, s.PhaseWorkQueueV5, dict[str, object]]:
    cell = next(cell for cell in plan_v5_cells("v5_dev") if cell.family_id == "F01")
    if not unique_quorum_app:
        episode, sealed = build_v5_episode(cell)
        _, ids, _ = _build_world(cell, _regime_overlay(cell.regime_id, cell))
        return (
            sealed.omniscient_s2_gold,
            episode.phase2_base_manifest.work_queue,
            ids,
        )

    phase1_state, ids, profile = _build_world(cell, _regime_overlay(cell.regime_id, cell))
    if unique_quorum_app:
        account_id = ids["account_names"]["reviewer"]
        reviewer_scope = _bound_account_scope(phase1_state, account_id)
        grant_id = "grant_quorum_unique"
        role = s.RoleV5(
            role_id="role_quorum_unique",
            app_id="app_quorum_unique",
            privilege_tier="admin",
            capabilities=("cap_quorum_unique",),
            evidence_id="ev_role_quorum_unique",
        )
        grant = s.GrantV5(
            grant_id=grant_id,
            account_id=account_id,
            role_id=role.role_id,
            grant_mode="standing",
            scope_path=reviewer_scope,
            sod_prune_priority=100,
            evidence_id="ev_grant_quorum_unique",
        )
        account = phase1_state.idp_accounts[account_id]
        phase1_state = phase1_state.model_copy(
            update={
                "roles": {**phase1_state.roles, role.role_id: role},
                "grants": {**phase1_state.grants, grant_id: grant},
                "idp_accounts": {
                    **phase1_state.idp_accounts,
                    account_id: account.model_copy(
                        update={"direct_grant_ids": (*account.direct_grant_ids, grant_id)}
                    ),
                },
            }
        )

    q1 = s.PhaseWorkQueueV5(
        target_connector_ids=tuple(sorted(phase1_state.connector_schemas)),
        target_hr_mapping_ids=tuple(sorted(phase1_state.hr_source_mappings)),
        target_human_account_ids=tuple(sorted(phase1_state.idp_accounts)),
        target_nhi_ids=tuple(sorted(phase1_state.target_nhis)),
        target_agent_ids=tuple(sorted(phase1_state.enterprise_agents)),
        target_ciem_task_ids=tuple(sorted(phase1_state.cloud_ciem_tasks)),
        target_jml_workflow_ids=tuple(sorted(phase1_state.jml_workflow_tasks)),
        target_opt_task_ids=(ids["opt"]["p1"],),
    )
    p1_mutations = evaluate_phase(
        phase1_state,
        q1,
        sealed_jml_adversarial_events=_sealed_jml_events(cell, ids, profile),
    )
    committed = apply_phase1(phase1_state, p1_mutations, work_queue=q1)
    p2_environment, _ = _build_phase2_world(phase1_state, ids, profile, cell)
    phase2_state = p2_environment.model_copy(
        update={
            "prior_phase_ledger": committed.prior_phase_ledger,
            "world_rev": committed.world_rev,
        }
    )
    q2 = s.PhaseWorkQueueV5(
        target_opt_task_ids=(ids["opt"]["p2"],),
        target_cae_incident_ids=tuple(sorted(phase2_state.cae_incidents)),
        target_uar_campaign_ids=tuple(sorted(phase2_state.uar_campaigns)),
        target_jit_request_ids=tuple(sorted(phase2_state.jit_requests)),
        target_agent_delegation_ids=tuple(sorted(phase2_state.agent_delegation_requests)),
    )
    return phase2_state, q2, ids


def _bound_account_scope(state: s.WorldStateV5, account_id: str) -> str:
    account = state.idp_accounts[account_id]
    assert account.immutable_person_ref is not None
    assert account.bound_engagement_ref is not None
    person = state.hr_persons[account.immutable_person_ref]
    return person.engagements_by_id[account.bound_engagement_ref].dept_path


def _p1_engagement_scope(state: s.WorldStateV5, account_id: str) -> str:
    outcome = state.prior_phase_ledger.account_by_id[account_id]
    assert outcome.bound_person_id is not None
    assert outcome.bound_engagement_id is not None
    person = state.hr_persons[outcome.bound_person_id]
    return person.engagements_by_id[outcome.bound_engagement_id].dept_path


def test_uar_quorum_base_excludes_reviewed_grants_but_final_includes_them() -> None:
    values, rows = _world_primitives()
    campaign = s.UARCampaignV5(
        campaign_id="camp_review",
        app_id="app_finance",
        scope_path="/dept/finance",
        admin_role_id="role_admin",
        m_standing=1,
        m_eligible=0,
        items=(
            s.UARItemV5(
                item_id="item_review",
                account_id="acct_subject",
                grant_id=rows["grant"].grant_id,
                assigned_reviewer_account_id="acct_reviewer",
                used_within_lookback=True,
                evidence_id="ev_item_review",
            ),
        ),
        etag="e" * 16,
        evidence_id="ev_campaign",
    )
    state = SimpleNamespace(**values, groups={}, clock_epoch_s=500)
    ctx = _PhaseContext(state=state, queue=SimpleNamespace(), phase=2)
    work = _Phase2WorkingState(
        ctx=ctx,
        grants=dict(state.grants),
        standing={"acct_subject": {rows["grant"].grant_id}},
        eligible={"acct_subject": set()},
        active_account_ids={"acct_subject"},
        bound_person_by_account={"acct_subject": "pers_subject"},
        bound_engagement_by_account={"acct_subject": rows["subject"].engagements[0]},
        retro_tainted_ids=set(),
        retro_tainted_evidence={},
    )

    assert _campaign_quorum_sets(work, campaign) == (set(), set())
    assert _campaign_quorum_sets(work, campaign, exclude_reviewed=False) == (
        {"pers_subject"},
        set(),
    )


def test_uar_item_without_any_valid_reviewer_is_escalated_and_not_in_i_rev() -> None:
    values, rows = _world_primitives()
    campaign = s.UARCampaignV5(
        campaign_id="camp_review",
        app_id="app_finance",
        scope_path="/dept/finance",
        admin_role_id="role_admin",
        m_standing=0,
        m_eligible=0,
        items=(
            s.UARItemV5(
                item_id="item_review",
                account_id="acct_subject",
                grant_id=rows["grant"].grant_id,
                assigned_reviewer_account_id="acct_subject",
                used_within_lookback=False,
                evidence_id="ev_item_review",
            ),
        ),
        etag="e" * 16,
        evidence_id="ev_campaign",
    )
    ledger = SimpleNamespace(
        account_by_id={
            "acct_subject": SimpleNamespace(
                p1_action="KEEP_ACTIVE",
                bound_person_id="pers_subject",
                bound_engagement_id="eng_subject",
                effective_standing_grant_ids=(rows["grant"].grant_id,),
                effective_eligible_grant_ids=(),
            )
        },
        nhi_by_id={},
        agent_by_id={},
        connector_by_id={},
    )
    state = SimpleNamespace(
        **values,
        groups={},
        clock_epoch_s=500,
        phase_index=2,
        prior_phase_ledger=ledger,
        uar_campaigns={campaign.campaign_id: campaign},
        baseline_nhis={},
        target_nhis={},
        baseline_agents={},
        enterprise_agents={},
        compenstation_controls={},
        compensating_controls={},
        connector_schemas={},
        overlay=SimpleNamespace(
            uar_preserve_standing_floor_default=False,
            enforce_cross_nhi_owner_sod=False,
            enforce_sponsor_agent_sod=False,
            freshness_sla_s=1000,
            forbidden_sod_rules=(),
        ),
    )
    queue = SimpleNamespace(
        target_uar_campaign_ids=(campaign.campaign_id,),
        target_opt_task_ids=(),
    )
    ctx = _PhaseContext(state=state, queue=queue, phase=2)
    work = _Phase2WorkingState(
        ctx=ctx,
        grants=dict(state.grants),
        standing={"acct_subject": {rows["grant"].grant_id}},
        eligible={"acct_subject": set()},
        active_account_ids={"acct_subject", "acct_reviewer"},
        bound_person_by_account={
            "acct_subject": "pers_subject",
            "acct_reviewer": "pers_reviewer",
        },
        bound_engagement_by_account={
            "acct_subject": rows["subject"].engagements[0],
            "acct_reviewer": rows["reviewer"].engagements[0],
        },
        retro_tainted_ids=set(),
        retro_tainted_evidence={},
    )

    mutations = _phase22_uar(work)

    assert mutations[0].uar_decisions == (("item_review", "ESCALATE"),)
    context = ctx.semantic[("UAR_CAMPAIGN_MUTATION", campaign.campaign_id)]
    assert context["valid_reviewers"]["item_review"] == ()
    assert context["locked_item_ids"] == ()


def test_optimizer_semantic_feasibility_closures_remain_task_local() -> None:
    values, _rows = _world_primitives()
    extra_account = s.IdPAccountV5(
        account_id="acct_other",
        subkind="human_primary",
        login_email="other@example.test",
        immutable_person_ref="pers_other",
        status="ACTIVE",
        etag="f" * 16,
        evidence_id="ev_account_other",
    )
    extra_person = s.HRPersonV5(
        person_id="pers_other",
        emp_immutable_id="employee_other",
        primary_email="other@example.test",
        engagements=(
            s.EngagementV5(
                eng_id="eng_other",
                affiliation="FTE",
                eng_status="ACTIVE",
                dept_path="/dept/finance",
                start_s=0,
            ),
        ),
        etag="1" * 16,
        evidence_id="ev_person_other",
    )
    cand_one = s.OptCandidateActionV5(
        candidate_id="cand_one",
        action_kind="SELECT_REVIEWER_ACCOUNT",
        candidate_account_id="acct_subject",
        role_id="role_reviewer",
        covered_scopes=("/dept/finance/payroll",),
        action_cost=1,
    )
    cand_two = s.OptCandidateActionV5(
        candidate_id="cand_two",
        action_kind="SELECT_REVIEWER_ACCOUNT",
        candidate_account_id="acct_other",
        role_id="role_reviewer",
        covered_scopes=("/dept/finance/treasury",),
        action_cost=2,
    )
    task_one = s.OptimizationTaskV5(
        task_id="opt_one",
        phase_kind="P2_REVIEWER_ASSIGNMENT",
        required_review_scopes=("/dept/finance/payroll",),
        candidates=(cand_one,),
        etag="2" * 16,
        evidence_id="ev_opt_one",
    )
    task_two = s.OptimizationTaskV5(
        task_id="opt_two",
        phase_kind="P2_REVIEWER_ASSIGNMENT",
        required_review_scopes=("/dept/finance/treasury",),
        candidates=(cand_two,),
        etag="3" * 16,
        evidence_id="ev_opt_two",
    )
    values["idp_accounts"] = {**values["idp_accounts"], extra_account.account_id: extra_account}
    values["hr_persons"] = {**values["hr_persons"], extra_person.person_id: extra_person}
    prior_ledger = SimpleNamespace(
        account_by_id={
            account_id: SimpleNamespace(
                bound_person_id=person_id,
                current_account_gen=1,
                current_etag="a" * 16,
            )
            for account_id, person_id in (
                ("acct_subject", "pers_subject"),
                ("acct_reviewer", "pers_reviewer"),
                ("acct_other", "pers_other"),
            )
        },
        nhi_by_id={},
        agent_by_id={},
        connector_by_id={},
    )
    state = SimpleNamespace(
        **values,
        groups={},
        clock_epoch_s=500,
        phase_index=2,
        prior_phase_ledger=prior_ledger,
        historical_sessions=(),
        optimization_tasks={task_one.task_id: task_one, task_two.task_id: task_two},
        overlay=SimpleNamespace(forbidden_sod_rules=()),
    )
    queue = SimpleNamespace(target_opt_task_ids=(task_one.task_id, task_two.task_id))
    ctx = _PhaseContext(state=state, queue=queue, phase=2)
    work = _Phase2WorkingState(
        ctx=ctx,
        grants=dict(state.grants),
        standing={},
        eligible={},
        active_account_ids={"acct_subject", "acct_reviewer", "acct_other"},
        bound_person_by_account={
            "acct_subject": "pers_subject",
            "acct_reviewer": "pers_reviewer",
            "acct_other": "pers_other",
        },
        bound_engagement_by_account={},
        retro_tainted_ids=set(),
        retro_tainted_evidence={},
        provisional_sessions=[],
    )

    mutations = _phase24_optimization(work)

    assert [mutation.selected_ids for mutation in mutations] == [("cand_one",), ("cand_two",)]
    first_feasibility = ctx.semantic[("OPTIMIZATION_MUTATION", "opt_one")]["feasibility"]
    second_feasibility = ctx.semantic[("OPTIMIZATION_MUTATION", "opt_two")]["feasibility"]
    assert first_feasibility(("cand_one",)).feasible
    assert second_feasibility(("cand_two",)).feasible


def test_phase2_feasibility_uses_exact_p1_engagement_for_uar_quorum() -> None:
    state, queue, ids = _generated_phase2(unique_quorum_app=True)
    account_id = ids["account_names"]["reviewer"]
    account_outcome = state.prior_phase_ledger.account_by_id[account_id]
    pinned_scope = _p1_engagement_scope(state, account_id)
    assert account_outcome.p1_action == "KEEP_ACTIVE"
    assert "grant_quorum_unique" in account_outcome.effective_standing_grant_ids
    assert state.grants["grant_quorum_unique"].scope_path == pinned_scope
    assert phase2_feasibility(state, queue).feasible

    base_campaign = next(iter(state.uar_campaigns.values()))
    campaign = s.UARCampaignV5(
        campaign_id="camp_exact_engagement_quorum",
        app_id="app_quorum_unique",
        scope_path=pinned_scope,
        admin_role_id="role_quorum_unique",
        m_standing=1,
        m_eligible=0,
        items=(
            s.UARItemV5(
                item_id="item_exact_engagement_quorum",
                account_id=account_id,
                grant_id="grant_quorum_unique",
                assigned_reviewer_account_id=base_campaign.items[0].assigned_reviewer_account_id,
                used_within_lookback=True,
                evidence_id="ev_item_exact_engagement_quorum",
            ),
        ),
        backup_reviewer_account_ids=tuple(sorted(state.idp_accounts)),
        etag="9" * 16,
        evidence_id="ev_campaign_exact_engagement_quorum",
    )
    state = state.model_copy(update={"uar_campaigns": {campaign.campaign_id: campaign}})
    queue = queue.model_copy(update={"target_uar_campaign_ids": (campaign.campaign_id,)})

    assert phase2_feasibility(state, queue).feasible

    pinned_person_id = account_outcome.bound_person_id
    pinned_engagement_id = account_outcome.bound_engagement_id
    assert pinned_person_id is not None and pinned_engagement_id is not None
    person = state.hr_persons[pinned_person_id]
    pinned = person.engagements_by_id[pinned_engagement_id]
    other_live = s.EngagementV5(
        eng_id="eng_other_live_after_pin",
        affiliation="FTE",
        eng_status="ACTIVE",
        dept_path=pinned.dept_path,
        start_s=state.clock_epoch_s - 60,
    )
    inactive_pin = pinned.model_copy(update={"end_s": state.clock_epoch_s - 1})
    changed_person = person.model_copy(
        update={
            "engagements": (
                *(
                    inactive_pin if engagement.eng_id == pinned_engagement_id else engagement
                    for engagement in person.engagements
                ),
                other_live,
            ),
            "lifecycle_gen": person.lifecycle_gen + 1,
            "etag": "8" * 16,
            "evidence_id": "ev_person_exact_engagement_quorum_p2",
        }
    )
    changed_state = state.model_copy(
        update={"hr_persons": {**state.hr_persons, pinned_person_id: changed_person}}
    )

    result = phase2_feasibility(changed_state, queue)
    assert not result.feasible
    assert result.reason is not None and campaign.campaign_id in result.reason


def test_phase2_feasibility_accepts_tied_existential_optimum_that_gold_rejects() -> None:
    state, queue, _ids = _generated_phase2()
    task_id = queue.target_opt_task_ids[0]
    task = state.optimization_tasks[task_id]
    assert len(task.candidates) == 2
    first, second = task.candidates
    tied_task = task.model_copy(
        update={
            "candidates": (
                first.model_copy(update={"action_cost": 1}),
                second.model_copy(update={"action_cost": 1}),
            )
        }
    )
    tied_state = state.model_copy(
        update={"optimization_tasks": {**state.optimization_tasks, task_id: tied_task}}
    )

    assert phase2_feasibility(tied_state, queue).feasible
    with pytest.raises(ValueError, match="lacks a unique feasible optimum"):
        evaluate_phase_with_trace(tied_state, queue)


def test_phase2_feasibility_checks_global_quorum_after_all_campaigns() -> None:
    state, queue, ids = _generated_phase2(unique_quorum_app=True)
    account_id = ids["account_names"]["reviewer"]
    pinned_scope = _p1_engagement_scope(state, account_id)
    backup_reviewers = tuple(sorted(state.idp_accounts))
    campaigns: dict[str, s.UARCampaignV5] = {}
    for campaign_id, standing_floor, preserve_floor in (
        ("camp_global_floor_first", 1, "TRUE"),
        ("camp_global_floor_second", 0, "FALSE"),
    ):
        campaign = s.UARCampaignV5(
            campaign_id=campaign_id,
            app_id="app_quorum_unique",
            scope_path=pinned_scope,
            admin_role_id="role_quorum_unique",
            m_standing=standing_floor,
            m_eligible=0,
            preserve_standing_floor_override=preserve_floor,
            items=(
                s.UARItemV5(
                    item_id=f"item_{campaign_id}",
                    account_id=account_id,
                    grant_id="grant_quorum_unique",
                    assigned_reviewer_account_id=ids["account_names"]["manager"],
                    used_within_lookback=False,
                    evidence_id=f"ev_item_{campaign_id}",
                ),
            ),
            backup_reviewer_account_ids=backup_reviewers,
            etag=("a" if standing_floor else "b") * 16,
            evidence_id=f"ev_{campaign_id}",
        )
        campaigns[campaign.campaign_id] = campaign

    state = state.model_copy(update={"uar_campaigns": campaigns})
    queue = queue.model_copy(update={"target_uar_campaign_ids": tuple(sorted(campaigns))})

    result = phase2_feasibility(state, queue)
    assert not result.feasible
    assert result.reason is not None and "camp_global_floor_first" in result.reason


def test_non_tainted_expired_bridge_strip_has_preconditions_without_bridge_citation() -> None:
    state, queue, ids = _generated_phase2()
    bridge_id = ids["bridge"]["mover"]
    bridge = state.transfer_bridges[bridge_id]
    account_id = ids["account_names"]["mover"]
    grant_id = ids["grant_names"]["mover_deploy"]
    grant = state.grants[grant_id]
    assert bridge.account_id == account_id
    assert bridge.grant_id == grant_id
    assert bridge.expires_at_s > state.clock_epoch_s
    expired_bridge = bridge.model_copy(
        update={
            "expires_at_s": state.clock_epoch_s - 1,
            "etag": "7" * 16,
            "evidence_id": "ev_expired_non_tainted_bridge",
        }
    )
    state = state.model_copy(
        update={"transfer_bridges": {**state.transfer_bridges, bridge_id: expired_bridge}}
    )
    campaign = s.UARCampaignV5(
        campaign_id="camp_postshock_bridge_provenance",
        app_id=state.roles[grant.role_id].app_id,
        scope_path=grant.scope_path,
        admin_role_id=ids["role_names"]["deploy_admin"],
        m_standing=0,
        m_eligible=0,
        preserve_standing_floor_override="FALSE",
        items=(
            s.UARItemV5(
                item_id="item_postshock_bridge_provenance",
                account_id=account_id,
                grant_id=grant_id,
                assigned_reviewer_account_id=ids["account_names"]["reviewer"],
                used_within_lookback=True,
                evidence_id="ev_item_postshock_bridge_provenance",
            ),
        ),
        backup_reviewer_account_ids=tuple(sorted(state.idp_accounts)),
        etag="6" * 16,
        evidence_id="ev_campaign_postshock_bridge_provenance",
    )
    state = state.model_copy(update={"uar_campaigns": {campaign.campaign_id: campaign}})
    queue = queue.model_copy(update={"target_uar_campaign_ids": (campaign.campaign_id,)})

    result = evaluate_phase_with_trace(state, queue)
    mutation = next(
        mutation
        for mutation in result.mutations
        if mutation.mutation_kind == "UAR_CAMPAIGN_MUTATION"
    )
    approver_person = state.prior_phase_ledger.account_by_id[
        bridge.approved_by_account_id
    ].bound_person_id
    assert approver_person is not None
    assert live_version(bridge_id, state) in mutation.if_match
    assert live_version(approver_person, state) in mutation.if_match
    assert f"UAR-V5-01:{expired_bridge.evidence_id}" not in mutation.cite
    assert f"UAR-V5-01:{state.hr_persons[approver_person].evidence_id}" not in mutation.cite


def test_post_uar_live_cc_requires_fresh_nonhalted_monitoring_connector() -> None:
    values, rows = _world_primitives()
    subject_account = values["idp_accounts"]["acct_subject"]
    reviewer_account = values["idp_accounts"]["acct_reviewer"]
    subject_grant = rows["grant"]
    pay_role = s.RoleV5(
        role_id="role_pay",
        app_id="app_finance",
        privilege_tier="standard",
        capabilities=("pay",),
        evidence_id="ev_role_pay",
    )
    monitor_role = s.RoleV5(
        role_id="role_monitor",
        app_id="app_finance",
        privilege_tier="standard",
        capabilities=("monitor",),
        evidence_id="ev_role_monitor",
    )
    pay_grant = s.GrantV5(
        grant_id="grant_pay",
        account_id=subject_account.account_id,
        role_id=pay_role.role_id,
        grant_mode="standing",
        scope_path="/dept/finance",
        sod_prune_priority=2,
        evidence_id="ev_grant_pay",
    )
    monitor_grant = s.GrantV5(
        grant_id="grant_monitor",
        account_id=reviewer_account.account_id,
        role_id=monitor_role.role_id,
        grant_mode="standing",
        scope_path="/dept/finance",
        sod_prune_priority=3,
        evidence_id="ev_grant_monitor",
    )
    rule = s.ScopedSoDRuleV5(
        rule_code="sod_approve_pay",
        cap_a="approve",
        cap_b="pay",
        scope_mode="SAME_LEAF_SCOPE",
    )
    control = SimpleNamespace(
        cc_id="cc_subject",
        monitored_person_id="pers_subject",
        cap_a="approve",
        cap_b="pay",
        covered_scope_path="/dept/finance",
        monitor_account_id="acct_reviewer",
        required_monitor_capability="monitor",
        audit_connector_id="conn_monitor",
        preserve_if_live=True,
    )
    connector = SimpleNamespace(audit_watermark_s=500)
    connector_outcome = SimpleNamespace(halted_fail_closed=False)
    ledger = SimpleNamespace(
        account_by_id={},
        connector_by_id={"conn_monitor": connector_outcome},
    )
    overlay = SimpleNamespace(
        uar_preserve_standing_floor_default=False,
        freshness_sla_s=1_000,
        forbidden_sod_rules=(rule,),
    )
    state_fields = {
        **values,
        "roles": {
            **values["roles"],
            pay_role.role_id: pay_role,
            monitor_role.role_id: monitor_role,
        },
        "grants": {
            **values["grants"],
            pay_grant.grant_id: pay_grant,
            monitor_grant.grant_id: monitor_grant,
        },
    }
    state = SimpleNamespace(
        **state_fields,
        groups={},
        clock_epoch_s=500,
        overlay=overlay,
        compensating_controls={control.cc_id: control},
        connector_schemas={"conn_monitor": connector},
        prior_phase_ledger=ledger,
    )
    ctx = _PhaseContext(state=state, queue=SimpleNamespace(), phase=2)
    work = _Phase2WorkingState(
        ctx=ctx,
        grants={
            subject_grant.grant_id: subject_grant,
            pay_grant.grant_id: pay_grant,
            monitor_grant.grant_id: monitor_grant,
        },
        standing={
            "acct_subject": {subject_grant.grant_id, pay_grant.grant_id},
            "acct_reviewer": {monitor_grant.grant_id},
        },
        eligible={},
        active_account_ids={"acct_subject", "acct_reviewer"},
        bound_person_by_account={
            "acct_subject": "pers_subject",
            "acct_reviewer": "pers_reviewer",
        },
        bound_engagement_by_account={
            "acct_subject": rows["subject"].engagements[0],
            "acct_reviewer": rows["reviewer"].engagements[0],
        },
        retro_tainted_ids=set(),
        retro_tainted_evidence={},
    )

    _verify_post_uar_invariants(work, [])
    assert _post_uar_live_controls(work) == (control,)

    connector_outcome.halted_fail_closed = True
    assert not _post_uar_live_controls(work)
    with pytest.raises(Phase2InfeasibilityV5, match="post-UAR LIVE_CC invariant"):
        _verify_post_uar_invariants(work, [])
