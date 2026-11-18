from __future__ import annotations

from typing import Any

from identitybench.v5 import schemas as s
from identitybench.v5.generator import _build_world, _regime_overlay, plan_v5_cells
from identitybench.v5.oracle import _PhaseContext, evaluate_phase
from identitybench.v5.oracle_p2 import _initialize_working_state
from identitybench.v5.oracle_requests import evaluate_requests
from identitybench.v5.runtime import apply_phase1
from identitybench.v5.scope_algebra import is_subordinate


def _phase2_work() -> tuple[Any, dict[str, Any]]:
    cell = next(cell for cell in plan_v5_cells("v5_dev", seed=391) if cell.family_id == "F06")
    state, ids, _ = _build_world(cell, _regime_overlay(cell.regime_id, cell))
    p1_queue = s.PhaseWorkQueueV5(
        target_connector_ids=tuple(sorted(state.connector_schemas)),
        target_hr_mapping_ids=tuple(sorted(state.hr_source_mappings)),
        target_human_account_ids=tuple(sorted(state.idp_accounts)),
        target_nhi_ids=tuple(sorted(state.target_nhis)),
        target_agent_ids=tuple(sorted(state.enterprise_agents)),
        target_ciem_task_ids=tuple(sorted(state.cloud_ciem_tasks)),
        target_jml_workflow_ids=tuple(sorted(state.jml_workflow_tasks)),
        target_opt_task_ids=(ids["opt"]["p1"],),
    )
    p1_gold = evaluate_phase(state, p1_queue)
    phase2 = apply_phase1(state, p1_gold)
    p2_queue = s.PhaseWorkQueueV5(target_opt_task_ids=(ids["opt"]["p2"],))
    context = _PhaseContext(state=phase2, queue=p2_queue, phase=2)
    return _initialize_working_state(context), ids


def _independent_accounts(work: Any) -> tuple[str, str]:
    for requester in sorted(work.active_account_ids):
        requester_person = work.bound_person_by_account[requester]
        for approver in sorted(work.active_account_ids):
            approver_person = work.bound_person_by_account[approver]
            if requester_person != approver_person and not is_subordinate(
                approver_person,
                requester_person,
                work.clock,
                work.state.hr_persons,
            ):
                return requester, approver
    raise AssertionError("fixture has no independent active account pair")


def _set_jit_setup(
    work: Any,
    request_specs: tuple[tuple[str, int, str | None, str], ...],
    *,
    capabilities: tuple[str, ...] = ("cap_request_test",),
    role_id: str = "role_test_jit",
    channel: str = "STANDARD_TICKET",
) -> tuple[str, str, str, str, str]:
    requester, approver = _independent_accounts(work)
    requester_person = work.bound_person_by_account[requester]
    department = work.bound_engagement_by_account[requester].dept_path
    app_id = "application_test_jit"
    role = s.RoleV5(
        role_id=role_id,
        app_id=app_id,
        privilege_tier="standard",
        capabilities=capabilities,
        is_jit_capable_target=True,
        evidence_id="ev_test_jit_role",
    )
    admin_role = s.RoleV5(
        role_id="role_test_jit_admin",
        app_id=app_id,
        privilege_tier="admin",
        capabilities=("cap_test_admin",),
        is_jit_capable_target=True,
        evidence_id="ev_test_jit_admin_role",
    )
    eligible = s.GrantV5(
        grant_id="grant_test_jit_eligible",
        account_id=requester,
        role_id=role_id,
        grant_mode="eligible",
        scope_path=department,
        sod_prune_priority=1,
        evidence_id="ev_test_jit_eligible",
    )
    admin = s.GrantV5(
        grant_id="grant_test_jit_admin",
        account_id=approver,
        role_id=admin_role.role_id,
        grant_mode="standing",
        scope_path=department,
        sod_prune_priority=1,
        evidence_id="ev_test_jit_admin",
    )
    jit_requests = {
        request_id: s.JITRequestV5(
            request_id=request_id,
            eval_seq=eval_seq,
            requester_account_id=requester,
            requested_role_id=role_id,
            requested_scope_path=department,
            target_resource=app_id,
            request_channel=channel,  # type: ignore[arg-type]
            change_ticket_id=ticket_id,
            proposed_approver_account_id=approver,
            requested_ttl_s=900,
            etag=f"{eval_seq:016x}",
            evidence_id=evidence_id,
        )
        for request_id, eval_seq, ticket_id, evidence_id in request_specs
    }
    tickets = {
        ticket_id: s.ChangeTicketV5(
            ticket_id=ticket_id,
            external_system="JIRA_JSM",
            external_ticket_status="APPROVED_EXTERNAL",
            bound_requester_account_id=requester,
            approved_role_id=role_id,
            approved_scope_path=department,
            target_resource=app_id,
            single_use=True,
            approved_by_account_id=approver,
            approved_at_s=work.clock - 60,
            expires_at_s=work.clock + 300,
            etag=f"{index:016x}",
            evidence_id=f"ev_{ticket_id}",
        )
        for index, ticket_id in enumerate(
            sorted({ticket_id for _, _, ticket_id, _ in request_specs if ticket_id is not None}),
            start=1,
        )
    }
    accounts = dict(work.state.idp_accounts)
    accounts[requester] = accounts[requester].model_copy(update={"group_ids": ()})
    accounts[approver] = accounts[approver].model_copy(update={"group_ids": ()})
    work.ctx.state = work.state.model_copy(
        update={
            "roles": {**work.state.roles, role_id: role, admin_role.role_id: admin_role},
            "grants": {**work.state.grants, eligible.grant_id: eligible, admin.grant_id: admin},
            "idp_accounts": accounts,
            "jit_requests": jit_requests,
            "change_tickets": tickets,
        }
    )
    work.grants.update({eligible.grant_id: eligible, admin.grant_id: admin})
    for account_id in work.active_account_ids:
        if work.bound_person_by_account[account_id] == requester_person:
            work.standing[account_id] = set()
            work.eligible[account_id] = set()
    work.eligible[requester] = {eligible.grant_id}
    work.standing[approver] = {admin.grant_id}
    work.provisional_sessions = []
    work.ctx.queue = work.ctx.queue.model_copy(
        update={"target_jit_request_ids": tuple(sorted(jit_requests))}
    )
    return requester, approver, role_id, department, app_id


def _agent_setup(
    work: Any,
    *,
    role_id: str,
    capabilities: tuple[str, ...],
    actor_standing_roles: tuple[str, ...],
    with_session_only: bool = False,
) -> tuple[str, str, str, str]:
    actor_account = next(iter(sorted(work.active_account_ids)))
    actor = work.bound_person_by_account[actor_account]
    agent_id = next(iter(sorted(work.state.enterprise_agents)))
    original_agent = work.state.enterprise_agents[agent_id]
    sponsor = next(
        person_id
        for person_id, person in sorted(work.state.hr_persons.items())
        if any(
            engagement.eng_status == "ACTIVE"
            and engagement.start_s <= work.clock
            and (engagement.end_s is None or work.clock < engagement.end_s)
            for engagement in person.engagements
        )
    )
    department = work.bound_engagement_by_account[actor_account].dept_path
    app_id = "application_test_obo"
    role = s.RoleV5(
        role_id=role_id,
        app_id=app_id,
        privilege_tier="standard",
        capabilities=capabilities,
        is_jit_capable_target=True,
        evidence_id="ev_test_obo_role",
    )
    binding = s.AgentToolBindingV5(
        binding_id="atg_test_obo",
        tool_connector_id=next(iter(work.state.connector_schemas)),
        requested_mode="AUTONOMOUS_BOUNDED",
        privilege_tier="standard",
        capabilities=capabilities,
        scope_path=department,
        evidence_id="ev_test_obo_binding",
    )
    agent = original_agent.model_copy(update={"tool_bindings": (binding,)})
    old_outcome = work.ledger.agent_by_id[agent_id]
    outcome = old_outcome.model_copy(
        update={
            "p1_agent_action": "REGISTER_ACTIVE",
            "resolved_sponsor_person_id": sponsor,
            "active_autonomous_binding_ids": (binding.binding_id,),
            "active_hitl_binding_ids": (),
        }
    )
    new_ledger = work.ledger.model_copy(
        update={
            "agent_outcomes": tuple(
                sorted(
                    (
                        outcome if row.agent_id == agent_id else row
                        for row in work.ledger.agent_outcomes
                    ),
                    key=lambda row: row.agent_id,
                )
            )
        }
    )
    work.ctx.state = work.state.model_copy(
        update={
            "roles": {**work.state.roles, role_id: role},
            "enterprise_agents": {**work.state.enterprise_agents, agent_id: agent},
            "prior_phase_ledger": new_ledger,
        }
    )
    test_grants: dict[str, s.GrantV5] = {}
    work.standing[actor_account] = set()
    for index, standing_role_id in enumerate(actor_standing_roles):
        grant_id = f"grant_test_obo_{index}"
        grant = s.GrantV5(
            grant_id=grant_id,
            account_id=actor_account,
            role_id=standing_role_id,
            grant_mode="standing",
            scope_path=department,
            sod_prune_priority=index + 1,
            evidence_id=f"ev_{grant_id}",
        )
        test_grants[grant_id] = grant
        work.standing[actor_account].add(grant_id)
    work.grants.update(test_grants)
    work.ctx.state = work.state.model_copy(update={"grants": {**work.state.grants, **test_grants}})
    if with_session_only:
        work.standing[actor_account] = set()
        work.provisional_sessions.append(
            s.HistoricalSessionV5(
                session_id="sess_test_obo_only",
                account_id=actor_account,
                role_id=role_id,
                scope_path=department,
                session_kind="PERSONAL_JIT",
                start_s=work.clock - 10,
                scheduled_end_s=work.clock + 600,
                audit_events_count=1,
                last_audit_event_s=work.clock - 10,
                evidence_id="ev_test_jit_session",
            )
        )
    else:
        work.provisional_sessions = []
    request = s.AgentDelegationRequestV5(
        request_id="dlreq_test_01",
        eval_seq=1,
        workflow_id="wfl_test_01",
        agent_id=agent_id,
        delegator_account_id=actor_account,
        parent_delegation_request_id=None,
        delegation_hop=1,
        requested_role_id=role_id,
        requested_scope_path=department,
        target_resource=app_id,
        proposed_hitl_approver_account_id=None,
        requested_ttl_s=600,
        etag="0000000000000001",
        evidence_id="ev_dlreq_test_01",
    )
    work.ctx.state = work.state.model_copy(
        update={"agent_delegation_requests": {request.request_id: request}}
    )
    work.ctx.queue = work.ctx.queue.model_copy(
        update={"target_agent_delegation_ids": (request.request_id,)}
    )
    return actor_account, actor, agent_id, request.request_id


def test_jit_ticket_is_consumed_once_and_ttl_is_ticket_bounded() -> None:
    work, _ = _phase2_work()
    _, _, role_id, scope, _ = _set_jit_setup(
        work,
        (
            ("req_test_first", 1, "tkt_test_single", "ev_req_test_first"),
            ("req_test_second", 2, "tkt_test_single", "ev_req_test_second"),
        ),
    )
    mutations = evaluate_requests(work)
    first, second = mutations

    assert first.jit_decision == "APPROVE_JIT"
    assert first.approved_ttl_s == 300
    assert first.external_sync_status == "SYNC_GRANTED"
    assert second.jit_decision == "DENY"
    assert (
        work.ctx.trace[("JIT_DECISION_MUTATION", "req_test_second")].context["failing_predicate"]
        == "P2"
    )
    assert "JIT-V5-01.P2:ev_req_test_first" in second.cite
    assert "JIT-V5-01.P2:ev_tkt_test_single" in second.cite
    assert work.consumed_ticket_ids == {"tkt_test_single"}
    assert [session.session_id for session in work.provisional_sessions] == [
        "sess_prov_req_test_first"
    ]
    assert work.provisional_sessions[0].role_id == role_id
    assert work.provisional_sessions[0].scope_path == scope


def test_emergency_jit_requires_exact_ticket_binding_and_consumes_single_use() -> None:
    work, _ = _phase2_work()
    requester, _, _, _, _ = _set_jit_setup(
        work,
        (
            ("req_emergency_first", 1, "tkt_emergency", "ev_req_emergency_first"),
            ("req_emergency_replay", 2, "tkt_emergency", "ev_req_emergency_replay"),
        ),
        channel="EMERGENCY_BREAK_GLASS",
    )
    request = work.state.jit_requests["req_emergency_first"]
    ticket = work.state.change_tickets["tkt_emergency"]
    assert (
        ticket.bound_requester_account_id,
        ticket.target_resource,
        ticket.approved_role_id,
        ticket.approved_scope_path,
    ) == (
        requester,
        request.target_resource,
        request.requested_role_id,
        request.requested_scope_path,
    )

    first, replay = evaluate_requests(work)

    assert first.jit_decision == "APPROVE_JIT_COSIGN"
    assert first.approved_ttl_s <= 1_800
    assert replay.jit_decision == "DENY"
    assert (
        work.ctx.trace[("JIT_DECISION_MUTATION", "req_emergency_replay")].context[
            "failing_predicate"
        ]
        == "P2"
    )
    assert work.consumed_ticket_ids == {"tkt_emergency"}

    wrong_binding, _ = _phase2_work()
    _set_jit_setup(
        wrong_binding,
        (("req_emergency_wrong_ticket", 1, "tkt_wrong_binding", "ev_req_wrong_ticket"),),
        channel="EMERGENCY_BREAK_GLASS",
    )
    bad_ticket = wrong_binding.state.change_tickets["tkt_wrong_binding"].model_copy(
        update={"target_resource": "different-application"}
    )
    wrong_binding.ctx.state = wrong_binding.state.model_copy(
        update={"change_tickets": {bad_ticket.ticket_id: bad_ticket}}
    )
    rejected = evaluate_requests(wrong_binding)[0]
    assert rejected.jit_decision == "DENY"
    assert (
        wrong_binding.ctx.trace[("JIT_DECISION_MUTATION", "req_emergency_wrong_ticket")].context[
            "failing_predicate"
        ]
        == "P2"
    )
    assert not wrong_binding.consumed_ticket_ids


def test_jit_static_sod_includes_an_ongoing_session() -> None:
    work, ids = _phase2_work()
    requester, _, _, scope, app_id = _set_jit_setup(
        work,
        (("req_test_session", 1, None, "ev_req_test_session"),),
        capabilities=("wire",),
        channel="EMERGENCY_BREAK_GLASS",
    )
    pay_role = ids["role_names"]["pay"]
    work.provisional_sessions.append(
        s.HistoricalSessionV5(
            session_id="sess_active_conflict",
            account_id=requester,
            role_id=pay_role,
            scope_path=scope,
            session_kind="PERSONAL_JIT",
            start_s=work.clock - 30,
            scheduled_end_s=work.clock + 300,
            audit_events_count=1,
            last_audit_event_s=work.clock - 30,
            evidence_id="ev_active_conflict_session",
        )
    )
    request = work.state.jit_requests["req_test_session"].model_copy(
        update={"target_resource": app_id}
    )
    work.ctx.state = work.state.model_copy(update={"jit_requests": {request.request_id: request}})
    mutation = evaluate_requests(work)[0]

    assert mutation.jit_decision == "DENY"
    assert (
        work.ctx.trace[("JIT_DECISION_MUTATION", request.request_id)].context["failing_predicate"]
        == "P3"
    )
    assert "JIT-V5-01.P3:ev_active_conflict_session" in mutation.cite


def test_obo_d2_never_borrows_capability_from_a_jit_session() -> None:
    work, _ = _phase2_work()
    _, _, _, request_id = _agent_setup(
        work,
        role_id="role_test_obo_only",
        capabilities=("cap_request_test",),
        actor_standing_roles=(),
        with_session_only=True,
    )
    mutation = evaluate_requests(work)[0]

    assert mutation.delegation_decision == "DENY_DELEGATION"
    assert (
        work.ctx.trace[("AGENT_DELEGATION_MUTATION", request_id)].context["failing_predicate"]
        == "D2"
    )
    assert "AGENT-DEL-V5-01.D2:ev_test_obo_grant" not in mutation.cite
    assert work.approved_delegations == {}


def test_obo_d3_uses_actor_person_standing_footprint_for_sod() -> None:
    work, ids = _phase2_work()
    actor_account, actor, _, request_id = _agent_setup(
        work,
        role_id="role_test_obo_wire",
        capabilities=("wire",),
        actor_standing_roles=(
            "role_test_obo_wire",
            ids["role_names"]["pay"],
        ),
    )
    mutation = evaluate_requests(work)[0]

    assert mutation.delegation_decision == "DENY_DELEGATION"
    assert (
        work.ctx.trace[("AGENT_DELEGATION_MUTATION", request_id)].context["failing_predicate"]
        == "D3"
    )
    assert "AGENT-DEL-V5-01.D3:ev_grant_test_obo_1" in mutation.cite
    assert work.bound_person_by_account[actor_account] == actor


def test_obo_approval_updates_workflow_and_provisional_session() -> None:
    work, _ = _phase2_work()
    actor_account, actor, _, request_id = _agent_setup(
        work,
        role_id="role_test_obo_approved",
        capabilities=("cap_request_test",),
        actor_standing_roles=("role_test_obo_approved",),
    )
    mutation = evaluate_requests(work)[0]

    assert mutation.delegation_decision == "APPROVE_DELEGATION"
    assert mutation.hitl_cosigner_account_id is None
    assert request_id in work.approved_delegations
    assert work.workflow_footprints["wfl_test_01"]
    session = next(
        session
        for session in work.provisional_sessions
        if session.session_id == f"sess_prov_{request_id}"
    )
    assert session.account_id == actor_account
    assert work.bound_person_by_account[actor_account] == actor


def test_obo_child_cannot_expand_approved_parent_footprint() -> None:
    work, _ = _phase2_work()
    actor_account, _, agent_id, parent_id = _agent_setup(
        work,
        role_id="role_test_obo_child",
        capabilities=("cap_parent", "cap_child"),
        actor_standing_roles=("role_test_obo_child",),
    )
    child = work.state.agent_delegation_requests[parent_id]
    parent_role = s.RoleV5(
        role_id="role_test_obo_parent",
        app_id=child.target_resource,
        privilege_tier="standard",
        capabilities=("cap_parent",),
        is_jit_capable_target=True,
        evidence_id="ev_test_obo_parent_role",
    )
    parent = child.model_copy(
        update={"requested_role_id": parent_role.role_id, "delegation_hop": 1}
    )
    expanded_child = child.model_copy(
        update={
            "request_id": "dlreq_test_child",
            "eval_seq": 2,
            "parent_delegation_request_id": parent_id,
            "delegation_hop": 2,
        }
    )
    work.ctx.state = work.state.model_copy(
        update={
            "roles": {**work.state.roles, parent_role.role_id: parent_role},
            "agent_delegation_requests": {
                parent_id: parent,
                expanded_child.request_id: expanded_child,
            },
        }
    )
    work.ctx.queue = work.ctx.queue.model_copy(
        update={"target_agent_delegation_ids": (parent_id, expanded_child.request_id)}
    )

    parent_result, child_result = evaluate_requests(work)

    assert parent_result.delegation_decision == "APPROVE_DELEGATION"
    assert child_result.delegation_decision == "DENY_DELEGATION"
    assert (
        work.ctx.trace[("AGENT_DELEGATION_MUTATION", expanded_child.request_id)].context[
            "failing_predicate"
        ]
        == "D2"
    )
    assert parent_id in work.approved_delegations
    assert expanded_child.request_id not in work.approved_delegations
    assert agent_id in work.ledger.agent_by_id
    assert actor_account in work.active_account_ids


def test_obo_hitl_cannot_cosign_for_the_delegating_actor() -> None:
    work, _ = _phase2_work()
    actor_account, actor, agent_id, request_id = _agent_setup(
        work,
        role_id="role_test_obo_hitl",
        capabilities=("cap_test_hitl",),
        actor_standing_roles=("role_test_obo_hitl",),
    )
    request = work.state.agent_delegation_requests[request_id].model_copy(
        update={"proposed_hitl_approver_account_id": actor_account}
    )
    original_agent = work.state.enterprise_agents[agent_id]
    scope = request.requested_scope_path
    auto_binding = s.AgentToolBindingV5(
        binding_id="atg_test_obo_auto",
        tool_connector_id=next(iter(work.state.connector_schemas)),
        requested_mode="AUTONOMOUS_BOUNDED",
        privilege_tier="standard",
        capabilities=("cap_tool_only",),
        scope_path=scope,
        evidence_id="ev_test_obo_auto",
    )
    hitl_binding = s.AgentToolBindingV5(
        binding_id="atg_test_obo_hitl",
        tool_connector_id=auto_binding.tool_connector_id,
        requested_mode="HITL_REQUIRED",
        privilege_tier="standard",
        capabilities=("cap_test_hitl",),
        scope_path=scope,
        evidence_id="ev_test_obo_hitl",
    )
    agent = original_agent.model_copy(update={"tool_bindings": (auto_binding, hitl_binding)})
    outcome = work.ledger.agent_by_id[agent_id].model_copy(
        update={
            "active_autonomous_binding_ids": (auto_binding.binding_id,),
            "active_hitl_binding_ids": (hitl_binding.binding_id,),
        }
    )
    ledger = work.ledger.model_copy(
        update={
            "agent_outcomes": tuple(
                outcome if row.agent_id == agent_id else row for row in work.ledger.agent_outcomes
            )
        }
    )
    work.ctx.state = work.state.model_copy(
        update={
            "enterprise_agents": {**work.state.enterprise_agents, agent_id: agent},
            "prior_phase_ledger": ledger,
            "agent_delegation_requests": {request_id: request},
        }
    )

    mutation = evaluate_requests(work)[0]

    assert mutation.delegation_decision == "DENY_DELEGATION"
    assert (
        work.ctx.trace[("AGENT_DELEGATION_MUTATION", request_id)].context["failing_predicate"]
        == "D4"
    )
    assert f"AGENT-DEL-V5-01.D4:{work.state.hr_persons[actor].evidence_id}" in mutation.cite


def test_temporal_sod_cooldown_blocks_recent_session() -> None:
    work, _ = _phase2_work()
    requester, _, role_id, scope, _ = _set_jit_setup(
        work,
        (("req_test_temporal", 1, None, "ev_req_test_temporal"),),
        channel="EMERGENCY_BREAK_GLASS",
    )
    prior_role = s.RoleV5(
        role_id="role_test_temporal_prior",
        app_id="application_test_jit",
        privilege_tier="standard",
        capabilities=("cap_temporal_prior",),
        is_jit_capable_target=True,
        evidence_id="ev_test_temporal_prior_role",
    )
    rule = s.TemporalSoDRuleV5(
        tsod_id="tsod_test_request_cooldown",
        role_a_id=prior_role.role_id,
        role_b_id=role_id,
        cooldown_s=600,
        scope_mode="SAME_LEAF_SCOPE",
        evidence_id="ev_test_request_cooldown",
    )
    work.ctx.state = work.state.model_copy(
        update={
            "roles": {**work.state.roles, prior_role.role_id: prior_role},
            "overlay": work.state.overlay.model_copy(update={"temporal_sod_rules": (rule,)}),
        }
    )
    work.provisional_sessions = [
        s.HistoricalSessionV5(
            session_id="sess_test_temporal_prior",
            account_id=requester,
            role_id=prior_role.role_id,
            scope_path=scope,
            session_kind="PERSONAL_JIT",
            start_s=work.clock - 900,
            scheduled_end_s=work.clock - 300,
            audit_events_count=1,
            last_audit_event_s=work.clock - 350,
            evidence_id="ev_test_temporal_session",
        )
    ]

    mutation = evaluate_requests(work)[0]

    assert mutation.jit_decision == "DENY"
    assert (
        work.ctx.trace[("JIT_DECISION_MUTATION", "req_test_temporal")].context["failing_predicate"]
        == "P3"
    )
    assert "JIT-V5-01.P3:ev_test_temporal_session" in mutation.cite
    assert "JIT-V5-01.P3:ev_test_request_cooldown" in mutation.cite


def test_emergency_jit_respects_shared_break_glass_cooldown() -> None:
    work, _ = _phase2_work()
    requester, _, role_id, scope, _ = _set_jit_setup(
        work,
        (("req_test_break_glass_cooldown", 1, None, "ev_req_test_break_glass_cooldown"),),
        channel="EMERGENCY_BREAK_GLASS",
    )
    work.ctx.state = work.state.model_copy(
        update={"overlay": work.state.overlay.model_copy(update={"break_glass_cooldown_s": 600})}
    )
    work.provisional_sessions = [
        s.HistoricalSessionV5(
            session_id="sess_test_shared_break_glass_cooldown",
            account_id=requester,
            role_id=role_id,
            scope_path=scope,
            session_kind="SHARED_BREAK_GLASS",
            start_s=work.clock - 900,
            scheduled_end_s=work.clock - 300,
            audit_events_count=1,
            last_audit_event_s=work.clock - 350,
            evidence_id="ev_test_shared_break_glass_session",
        )
    ]

    mutation = evaluate_requests(work)[0]

    assert mutation.jit_decision == "DENY"
    assert (
        work.ctx.trace[("JIT_DECISION_MUTATION", "req_test_break_glass_cooldown")].context[
            "failing_predicate"
        ]
        == "P3"
    )
    assert "JIT-V5-01.P3:ev_test_shared_break_glass_session" in mutation.cite
    assert f"JIT-V5-01.P3:{work.state.roles[role_id].evidence_id}" in mutation.cite


def test_inactive_bound_person_remains_in_jit_preconditions() -> None:
    work, _ = _phase2_work()
    requester, _, _, _, _ = _set_jit_setup(
        work,
        (("req_test_inactive", 1, None, "ev_req_test_inactive"),),
        channel="EMERGENCY_BREAK_GLASS",
    )
    outcome = work.ledger.account_by_id[requester]
    assert outcome.bound_person_id is not None
    assert outcome.bound_engagement_id is not None
    person = work.state.hr_persons[outcome.bound_person_id]
    ended_engagements = tuple(
        engagement.model_copy(update={"end_s": work.clock})
        if engagement.eng_id == outcome.bound_engagement_id
        else engagement
        for engagement in person.engagements
    )
    ended_person = person.model_copy(update={"engagements": ended_engagements})
    work.ctx.state = work.state.model_copy(
        update={"hr_persons": {**work.state.hr_persons, person.person_id: ended_person}}
    )
    work.active_account_ids.discard(requester)

    mutation = evaluate_requests(work)[0]

    assert mutation.jit_decision == "DENY"
    assert f"{person.person_id}@{person.etag}:{person.lifecycle_gen}" in mutation.if_match
    assert f"JIT-V5-01.P1:{person.evidence_id}" in mutation.cite
