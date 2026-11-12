"""Sequential Phase-2 JIT and OBO request evaluation.

The evaluator consumes the P2.1/P2.2 working ledger and mutates only its
provisional session, consumed-ticket, approved-delegation, and workflow state.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from . import schemas as s
from .oracle import _mk
from .oracle_p2 import (
    _account_deny,
    _account_person,
    _account_standing_footprint,
    _effective_grant,
    _Phase2WorkingState,
    bound_person_hist,
    effective_authorization_footprint,
    session_end_s,
)
from .scope_algebra import (
    CapabilityFootprint,
    capability_footprint,
    engagement_is_live,
    group_closure,
    has_live_engagement,
    is_subordinate,
    resolve_scope,
    sod_witnesses,
)


def evaluate_requests(work: _Phase2WorkingState) -> list[Any]:
    """Evaluate queued JIT/OBO requests in their single shared sequence."""
    state = work.state
    queued: list[tuple[int, str, str, Any]] = []
    for request_id in work.ctx.queue.target_jit_request_ids:
        jit_request = state.jit_requests[request_id]
        queued.append((jit_request.eval_seq, jit_request.request_id, "JIT", jit_request))
    for request_id in work.ctx.queue.target_agent_delegation_ids:
        delegation_request = state.agent_delegation_requests[request_id]
        queued.append(
            (
                delegation_request.eval_seq,
                delegation_request.request_id,
                "OBO",
                delegation_request,
            )
        )
    sequences = [item[0] for item in queued]
    if len(sequences) != len(set(sequences)):
        raise ValueError("P2.3 JIT/OBO eval_seq values must be unique")

    mutations: list[Any] = []
    consumed_ticket_evidence: dict[str, str] = {}
    for _, _, kind, request in sorted(queued, key=lambda row: (row[0], row[1])):
        if kind == "JIT":
            jit_request = request
            jit_mutation = _evaluate_jit(work, jit_request, consumed_ticket_evidence)
            if jit_mutation.jit_decision != "DENY":
                ticket_id = jit_request.change_ticket_id
                ticket = work.state.change_tickets.get(ticket_id) if ticket_id is not None else None
                if ticket is not None and ticket.single_use:
                    work.consumed_ticket_ids.add(ticket.ticket_id)
                    consumed_ticket_evidence[ticket.ticket_id] = jit_request.evidence_id
                work.provisional_sessions.append(
                    s.HistoricalSessionV5(
                        session_id=f"sess_prov_{jit_request.request_id}",
                        account_id=jit_request.requester_account_id,
                        role_id=jit_request.requested_role_id,
                        scope_path=jit_request.requested_scope_path,
                        session_kind=(
                            "SHARED_BREAK_GLASS"
                            if jit_request.request_channel == "EMERGENCY_BREAK_GLASS"
                            else "PERSONAL_JIT"
                        ),
                        start_s=work.clock,
                        scheduled_end_s=work.clock + jit_mutation.approved_ttl_s,
                        revoked_at_s=None,
                        audit_events_count=1,
                        last_audit_event_s=work.clock,
                        evidence_id=jit_request.evidence_id,
                    )
                )
            mutations.append(jit_mutation)
        else:
            delegation_request = request
            delegation_mutation = _evaluate_delegation(work, delegation_request)
            if delegation_mutation.delegation_decision != "DENY_DELEGATION":
                work.approved_delegations[delegation_request.request_id] = (
                    delegation_request,
                    delegation_mutation,
                )
                role = work.state.roles[delegation_request.requested_role_id]
                footprint = capability_footprint(
                    work.ctx.catalog,
                    role.capabilities,
                    (delegation_request.requested_scope_path,),
                )
                work.workflow_footprints.setdefault(delegation_request.workflow_id, set()).update(
                    footprint
                )
                work.provisional_sessions.append(
                    s.HistoricalSessionV5(
                        session_id=f"sess_prov_{delegation_request.request_id}",
                        account_id=delegation_request.delegator_account_id,
                        role_id=delegation_request.requested_role_id,
                        scope_path=delegation_request.requested_scope_path,
                        session_kind="AGENT_OBO",
                        start_s=work.clock,
                        scheduled_end_s=work.clock + delegation_mutation.approved_ttl_s,
                        revoked_at_s=None,
                        audit_events_count=1,
                        last_audit_event_s=work.clock,
                        evidence_id=delegation_request.evidence_id,
                    )
                )
            mutations.append(delegation_mutation)
    return mutations


def _request_footprint(
    work: _Phase2WorkingState, role_id: str, scope_path: str
) -> CapabilityFootprint:
    role = work.state.roles[role_id]
    return capability_footprint(work.ctx.catalog, role.capabilities, (scope_path,))


def _evidence(rule: str, evidence_id: str) -> str:
    return f"{rule}:{evidence_id}"


def _bound_person_inactive(work: _Phase2WorkingState, account_id: str) -> str | None:
    outcome = work.ledger.account_by_id.get(account_id)
    if outcome is None or outcome.bound_person_id is None or outcome.bound_engagement_id is None:
        return None
    person = work.state.hr_persons.get(outcome.bound_person_id)
    if person is None:
        return None
    engagement = person.engagements_by_id.get(outcome.bound_engagement_id)
    if engagement is not None and not engagement_is_live(engagement, work.clock):
        return person.person_id
    return None


def _group_allow_conflict_evidence(
    work: _Phase2WorkingState,
    account_id: str,
    requested: frozenset[tuple[str, str]],
    rule_prefix: str,
) -> set[str]:
    account = work.state.idp_accounts.get(account_id)
    if account is None:
        return set()
    deny = _account_deny(work, account_id)
    citations: set[str] = set()
    for group_id in group_closure(account, work.state.groups):
        group = work.state.groups[group_id]
        if group.effect != "ALLOW":
            continue
        capabilities = set(group.capabilities)
        for role_id in group.granted_role_ids:
            capabilities.update(work.state.roles[role_id].capabilities)
        footprint = capability_footprint(work.ctx.catalog, capabilities, (group.scope_path,)) - deny
        if sod_witnesses(requested, footprint, work.ctx.sod_rules):
            citations.add(_evidence(rule_prefix, group.evidence_id))
    return citations


def _standing_conflict_evidence(
    work: _Phase2WorkingState,
    person_id: str,
    requested: frozenset[tuple[str, str]],
    rule_prefix: str,
) -> set[str]:
    citations: set[str] = set()
    for account_id in sorted(work.active_account_ids):
        if work.bound_person_by_account.get(account_id) != person_id:
            continue
        citations.update(_group_allow_conflict_evidence(work, account_id, requested, rule_prefix))
        for grant_id in sorted(work.standing.get(account_id, set())):
            if grant_id not in work.grants:
                continue
            if sod_witnesses(
                requested, _effective_grant(work, account_id, grant_id), work.ctx.sod_rules
            ):
                citations.add(_evidence(rule_prefix, work.grants[grant_id].evidence_id))
    return citations


def _active_session_conflict_evidence(
    work: _Phase2WorkingState,
    person_id: str,
    requested: frozenset[tuple[str, str]],
    rule_prefix: str,
) -> set[str]:
    citations: set[str] = set()
    for session in work.provisional_sessions:
        if bound_person_hist(
            work, session.account_id
        ) != person_id or not session.start_s <= work.clock < session_end_s(session):
            continue
        role = work.state.roles[session.role_id]
        footprint = capability_footprint(work.ctx.catalog, role.capabilities, (session.scope_path,))
        if sod_witnesses(requested, footprint, work.ctx.sod_rules):
            citations.add(_evidence(rule_prefix, session.evidence_id))
    return citations


def _static_sod_citations(
    work: _Phase2WorkingState,
    person_id: str,
    requested: frozenset[tuple[str, str]],
    rule_prefix: str,
) -> set[str]:
    return _standing_conflict_evidence(work, person_id, requested, rule_prefix) | (
        _active_session_conflict_evidence(work, person_id, requested, rule_prefix)
    )


def _temporal_blocks(
    work: _Phase2WorkingState,
    person_id: str,
    role_id: str,
    scope_path: str,
    *,
    break_glass: bool = False,
) -> tuple[set[str], set[str], set[str]]:
    """Return temporal blockers, rule evidence, and break-glass cooldown sessions."""
    requested_leaves = resolve_scope(work.ctx.catalog, scope_path)
    session_evidence: set[str] = set()
    rule_evidence: set[str] = set()
    break_glass_evidence: set[str] = set()
    for session in work.provisional_sessions:
        if bound_person_hist(work, session.account_id) != person_id or session.start_s > work.clock:
            continue
        session_end = session_end_s(session)
        role_pair = {session.role_id, role_id}
        if session.role_id != role_id:
            for rule in work.ctx.overlay.temporal_sod_rules:
                if {rule.role_a_id, rule.role_b_id} != role_pair:
                    continue
                if rule.scope_mode != "GLOBAL" and not (
                    resolve_scope(work.ctx.catalog, session.scope_path) & requested_leaves
                ):
                    continue
                if session.audit_events_count > 0 and work.clock < session_end + rule.cooldown_s:
                    session_evidence.add(session.evidence_id)
                    rule_evidence.add(rule.evidence_id)
        if (
            break_glass
            and session.session_kind == "SHARED_BREAK_GLASS"
            and work.clock < session_end + work.ctx.overlay.break_glass_cooldown_s
        ):
            session_evidence.add(session.evidence_id)
            break_glass_evidence.add(session.evidence_id)
    return session_evidence, rule_evidence, break_glass_evidence


def _admin_leaf_scopes(work: _Phase2WorkingState, account_id: str, app_id: str) -> frozenset[str]:
    account = work.state.idp_accounts[account_id]
    leaves: set[str] = set()
    for grant_id in work.standing.get(account_id, set()):
        grant = work.grants.get(grant_id)
        if grant is None:
            continue
        role = work.state.roles[grant.role_id]
        if (
            role.app_id == app_id
            and role.privilege_tier in {"admin", "critical"}
            and _effective_grant(work, account_id, grant_id)
        ):
            leaves.update(resolve_scope(work.ctx.catalog, grant.scope_path))
    deny = _account_deny(work, account_id)
    for group_id in group_closure(account, work.state.groups):
        group = work.state.groups[group_id]
        if group.effect != "ALLOW":
            continue
        for role_id in group.granted_role_ids:
            role = work.state.roles[role_id]
            if role.app_id != app_id or role.privilege_tier not in {"admin", "critical"}:
                continue
            effective = (
                capability_footprint(work.ctx.catalog, role.capabilities, (group.scope_path,))
                - deny
            )
            if effective:
                leaves.update(resolve_scope(work.ctx.catalog, group.scope_path))
    return frozenset(leaves)


def _ticket_p2_failure(
    work: _Phase2WorkingState,
    request: s.JITRequestV5,
    consumed_ticket_evidence: Mapping[str, str],
) -> tuple[bool, set[str]]:
    ticket_id = request.change_ticket_id
    if ticket_id is None:
        return (request.request_channel == "STANDARD_TICKET", set())
    ticket = work.state.change_tickets.get(ticket_id)
    if ticket is None:
        return True, set()
    failed_single_use = ticket.single_use and ticket.ticket_id in work.consumed_ticket_ids
    approved = (
        ticket.external_ticket_status == "APPROVED_EXTERNAL"
        and ticket.approved_at_s <= work.clock < ticket.expires_at_s
        and ticket.ticket_id not in work.retro_tainted_ids
        and (
            ticket.bound_requester_account_id,
            ticket.target_resource,
            ticket.approved_role_id,
            ticket.approved_scope_path,
        )
        == (
            request.requester_account_id,
            request.target_resource,
            request.requested_role_id,
            request.requested_scope_path,
        )
        and not failed_single_use
    )
    support = {ticket.evidence_id}
    if failed_single_use and ticket.ticket_id in consumed_ticket_evidence:
        support.add(consumed_ticket_evidence[ticket.ticket_id])
    if ticket.ticket_id in work.retro_tainted_ids:
        approval = work.ledger.account_by_id.get(ticket.approved_by_account_id)
        if approval is not None and approval.bound_person_id in work.state.hr_persons:
            support.add(work.state.hr_persons[approval.bound_person_id].evidence_id)
    return not approved, support


def _evaluate_jit(
    work: _Phase2WorkingState,
    request: s.JITRequestV5,
    consumed_ticket_evidence: Mapping[str, str],
) -> s.JITDecisionMutationV5:
    state = work.state
    role = state.roles[request.requested_role_id]
    requested = _request_footprint(work, role.role_id, request.requested_scope_path)
    requester = request.requester_account_id
    requester_person = _account_person(work, requester)
    bound_requester_person = bound_person_hist(work, requester)
    deny = _account_deny(work, requester)
    eligible = any(
        grant_id in work.grants
        and work.grants[grant_id].role_id == role.role_id
        and resolve_scope(work.ctx.catalog, request.requested_scope_path)
        <= resolve_scope(work.ctx.catalog, work.grants[grant_id].scope_path)
        for grant_id in work.eligible.get(requester, set())
    )
    p1_ok = (
        requester_person is not None
        and role.app_id == request.target_resource
        and role.is_jit_capable_target
        and not (requested & deny)
        and (request.request_channel == "EMERGENCY_BREAK_GLASS" or eligible)
    )
    p2_failed, p2_support = _ticket_p2_failure(work, request, consumed_ticket_evidence)
    p2_ok = not p2_failed

    p3a_witnesses = (
        sod_witnesses(
            requested,
            effective_authorization_footprint(work, requester_person),
            work.ctx.sod_rules,
        )
        if requester_person is not None
        else frozenset()
    )
    p3b_sessions: set[str] = set()
    p3b_rules: set[str] = set()
    if requester_person is not None:
        p3b_sessions, p3b_rules, p3b_break_glass_sessions = _temporal_blocks(
            work,
            requester_person,
            role.role_id,
            request.requested_scope_path,
            break_glass=request.request_channel == "EMERGENCY_BREAK_GLASS",
        )
    p3_ok = not p3a_witnesses and not p3b_sessions

    approver = request.proposed_approver_account_id
    approver_person = _account_person(work, approver)
    p4_ok = (
        requester_person is not None
        and approver_person is not None
        and approver_person != requester_person
        and not is_subordinate(approver_person, requester_person, work.clock, state.hr_persons)
        and resolve_scope(work.ctx.catalog, request.requested_scope_path)
        <= _admin_leaf_scopes(work, approver, request.target_resource)
    )

    failing: str | None
    if not p1_ok:
        failing = "P1"
    elif not p2_ok:
        failing = "P2"
    elif not p3_ok:
        failing = "P3"
    elif not p4_ok:
        failing = "P4"
    else:
        failing = None

    citations: set[str] = set()
    pre_ids: list[str] = [request.request_id, requester]
    if bound_requester_person is not None:
        pre_ids.append(bound_requester_person)
    if failing is None:
        ticket = (
            state.change_tickets.get(request.change_ticket_id) if request.change_ticket_id else None
        )
        if ticket is not None:
            pre_ids.append(ticket.ticket_id)
        if approver_person is None:
            raise AssertionError("successful JIT P4 evaluation requires an active approver person")
        pre_ids.extend((approver, approver_person))
        citations.update(
            {
                _evidence("JIT-V5-01.P1", state.idp_accounts[requester].evidence_id),
                _evidence(
                    "JIT-V5-01.P2",
                    ticket.evidence_id if ticket is not None else request.evidence_id,
                ),
                _evidence("JIT-V5-01.P3", request.evidence_id),
                _evidence("JIT-V5-01.P4", state.idp_accounts[approver].evidence_id),
            }
        )
        needs_cosign = (
            request.request_channel == "EMERGENCY_BREAK_GLASS"
            or role.privilege_tier in work.ctx.overlay.critical_jit_tiers
        )
        ttl = min(
            request.requested_ttl_s,
            dict(work.ctx.overlay.jit_ttl_cap_by_tier)[role.privilege_tier],
            1800 if request.request_channel == "EMERGENCY_BREAK_GLASS" else 10**9,
            state.change_tickets[request.change_ticket_id].expires_at_s - work.clock
            if request.change_ticket_id is not None
            else 10**9,
        )
        decision = "APPROVE_JIT_COSIGN" if needs_cosign else "APPROVE_JIT"
        mutation = cast(
            s.JITDecisionMutationV5,
            _mk(
                work.ctx,
                s.JITDecisionMutationV5,
                request.request_id,
                {
                    "mutation_kind": "JIT_DECISION_MUTATION",
                    "jit_decision": decision,
                    "approver_account_id": approver,
                    "approved_ttl_s": ttl,
                    "external_sync_status": (
                        "NO_EXTERNAL_TICKET"
                        if request.change_ticket_id is None
                        else "SYNC_GRANTED_COSIGN"
                        if needs_cosign
                        else "SYNC_GRANTED"
                    ),
                },
                pre_ids,
                citations,
                {
                    "failing_predicate": None,
                    "predicate_results": {"P1": p1_ok, "P2": p2_ok, "P3": p3_ok, "P4": p4_ok},
                },
            ),
        )
        return mutation

    citations.add(_evidence("JIT-V5-01.P" + failing[1], request.evidence_id))
    if failing == "P1":
        citations.add(_evidence("JIT-V5-01.P1", state.idp_accounts[requester].evidence_id))
        if role.app_id != request.target_resource or not role.is_jit_capable_target:
            citations.add(_evidence("JIT-V5-01.P1", role.evidence_id))
        inactive_person = _bound_person_inactive(work, requester)
        if inactive_person is not None:
            citations.add(_evidence("JIT-V5-01.P1", state.hr_persons[inactive_person].evidence_id))
        for group_id in group_closure(state.idp_accounts[requester], state.groups):
            group = state.groups[group_id]
            if group.effect != "DENY":
                continue
            capabilities = set(group.capabilities)
            for role_id in group.granted_role_ids:
                capabilities.update(state.roles[role_id].capabilities)
            denied = capability_footprint(work.ctx.catalog, capabilities, (group.scope_path,))
            if denied & requested:
                citations.add(_evidence("JIT-V5-01.P1", group.evidence_id))
    elif failing == "P2":
        citations.update(_evidence("JIT-V5-01.P2", item) for item in p2_support)
    elif failing == "P3":
        if p3a_witnesses and requester_person is not None:
            citations.update(
                _static_sod_citations(work, requester_person, requested, "JIT-V5-01.P3")
            )
        else:
            citations.update(_evidence("JIT-V5-01.P3", item) for item in p3b_sessions)
            citations.update(_evidence("JIT-V5-01.P3", item) for item in p3b_rules)
            for session in work.provisional_sessions:
                if session.evidence_id not in p3b_sessions:
                    continue
                if session.evidence_id in p3b_break_glass_sessions:
                    citations.add(
                        _evidence("JIT-V5-01.P3", state.roles[session.role_id].evidence_id)
                    )
    else:  # P4
        if approver in state.idp_accounts:
            citations.add(_evidence("JIT-V5-01.P4", state.idp_accounts[approver].evidence_id))
        if (
            approver_person is not None
            and requester_person is not None
            and is_subordinate(approver_person, requester_person, work.clock, state.hr_persons)
        ):
            citations.add(_evidence("JIT-V5-01.P4", state.hr_persons[approver_person].evidence_id))
        inactive_person = _bound_person_inactive(work, approver)
        if inactive_person is not None:
            citations.add(_evidence("JIT-V5-01.P4", state.hr_persons[inactive_person].evidence_id))

    return cast(
        s.JITDecisionMutationV5,
        _mk(
            work.ctx,
            s.JITDecisionMutationV5,
            request.request_id,
            {
                "mutation_kind": "JIT_DECISION_MUTATION",
                "jit_decision": "DENY",
                "approver_account_id": None,
                "approved_ttl_s": 0,
                "external_sync_status": (
                    "NO_EXTERNAL_TICKET"
                    if request.change_ticket_id is None
                    else "SYNC_REJECTED_POLICY"
                ),
            },
            pre_ids,
            citations,
            {
                "failing_predicate": failing,
                "predicate_results": {"P1": p1_ok, "P2": p2_ok, "P3": p3_ok, "P4": p4_ok},
            },
        ),
    )


def _obo_workflow_citations(
    work: _Phase2WorkingState,
    request: s.AgentDelegationRequestV5,
    requested: frozenset[tuple[str, str]],
) -> set[str]:
    citations = _static_sod_citations(
        work,
        work.bound_person_by_account[request.delegator_account_id],
        requested,
        "AGENT-DEL-V5-01.D3",
    )
    for prior_request, _ in work.approved_delegations.values():
        if prior_request.workflow_id != request.workflow_id:
            continue
        prior_fp = _request_footprint(
            work, prior_request.requested_role_id, prior_request.requested_scope_path
        )
        if sod_witnesses(requested, prior_fp, work.ctx.sod_rules):
            citations.add(_evidence("AGENT-DEL-V5-01.D3", prior_request.evidence_id))
    return citations


def _evaluate_delegation(
    work: _Phase2WorkingState,
    request: s.AgentDelegationRequestV5,
) -> s.AgentDelegationMutationV5:
    state = work.state
    agent = state.enterprise_agents.get(request.agent_id)
    if agent is None:
        raise ValueError(
            f"P2.3 delegation {request.request_id} is missing required agent row {request.agent_id}"
        )
    agent_outcome = work.ledger.agent_by_id.get(request.agent_id)
    sponsor = agent_outcome.resolved_sponsor_person_id if agent_outcome is not None else None
    actor_account = request.delegator_account_id
    actor = _account_person(work, actor_account)
    bound_actor = bound_person_hist(work, actor_account)
    role = state.roles[request.requested_role_id]
    requested = _request_footprint(work, role.role_id, request.requested_scope_path)

    p1_ok = (
        agent_outcome is not None
        and agent_outcome.p1_agent_action == "REGISTER_ACTIVE"
        and sponsor is not None
        and sponsor in state.hr_persons
        and has_live_engagement(state.hr_persons[sponsor], work.clock)
        and actor is not None
        and role.app_id == request.target_resource
        and role.is_jit_capable_target
    )

    active_auto: set[str] = set(
        agent_outcome.active_autonomous_binding_ids if agent_outcome is not None else ()
    )
    active_hitl: set[str] = set(
        agent_outcome.active_hitl_binding_ids if agent_outcome is not None else ()
    )
    bindings = {binding.binding_id: binding for binding in agent.tool_bindings}
    f_auto = frozenset().union(
        *(
            capability_footprint(
                work.ctx.catalog,
                bindings[binding_id].capabilities,
                (bindings[binding_id].scope_path,),
            )
            for binding_id in active_auto
            if binding_id in bindings
        )
    )
    f_ceil = f_auto | frozenset().union(
        *(
            capability_footprint(
                work.ctx.catalog,
                bindings[binding_id].capabilities,
                (bindings[binding_id].scope_path,),
            )
            for binding_id in active_hitl
            if binding_id in bindings
        )
    )
    parent = (
        state.agent_delegation_requests.get(request.parent_delegation_request_id)
        if request.parent_delegation_request_id
        else None
    )
    approved_parent = (
        work.approved_delegations.get(request.parent_delegation_request_id)
        if request.parent_delegation_request_id
        else None
    )
    hop_ok = request.delegation_hop <= work.ctx.overlay.max_agent_delegation_hops
    if request.parent_delegation_request_id is not None:
        hop_ok = bool(
            hop_ok
            and parent is not None
            and approved_parent is not None
            and request.delegation_hop == parent.delegation_hop + 1
            and _account_person(work, parent.delegator_account_id) == actor
            and requested
            <= _request_footprint(work, parent.requested_role_id, parent.requested_scope_path)
        )
    p2_ok = (
        requested <= f_ceil
        and requested <= _account_standing_footprint(work, actor_account)
        and hop_ok
    )

    d3a_witnesses = (
        sod_witnesses(
            requested,
            effective_authorization_footprint(work, actor)
            | frozenset(work.workflow_footprints.get(request.workflow_id, set())),
            work.ctx.sod_rules,
        )
        if actor is not None
        else frozenset()
    )
    d3b_sessions: set[str] = set()
    d3b_rules: set[str] = set()
    if actor is not None:
        d3b_sessions, d3b_rules, _ = _temporal_blocks(
            work,
            actor,
            role.role_id,
            request.requested_scope_path,
        )
    p3_ok = not d3a_witnesses and not d3b_sessions

    needs_hitl = (
        not (requested <= f_auto) or role.privilege_tier in work.ctx.overlay.critical_jit_tiers
    )
    hitl = request.proposed_hitl_approver_account_id
    hitl_person = _account_person(work, hitl) if hitl is not None else None
    p4_ok = not needs_hitl or bool(
        hitl is not None
        and hitl_person is not None
        and actor is not None
        and sponsor is not None
        and hitl_person not in {actor, sponsor}
        and not is_subordinate(hitl_person, actor, work.clock, state.hr_persons)
        and not is_subordinate(hitl_person, sponsor, work.clock, state.hr_persons)
        and resolve_scope(work.ctx.catalog, request.requested_scope_path)
        <= _admin_leaf_scopes(work, hitl, request.target_resource)
    )

    failing = (
        "D1"
        if not p1_ok
        else "D2"
        if not p2_ok
        else "D3"
        if not p3_ok
        else "D4"
        if not p4_ok
        else None
    )
    pre_ids: list[str] = [request.request_id, request.agent_id, actor_account]
    if sponsor is not None:
        pre_ids.append(sponsor)
    if bound_actor is not None:
        pre_ids.append(bound_actor)
    if parent is not None:
        pre_ids.append(parent.request_id)
    citations: set[str] = set()
    if failing is None:
        needs_cosign = needs_hitl
        ttl = min(
            request.requested_ttl_s,
            dict(work.ctx.overlay.jit_ttl_cap_by_tier)[role.privilege_tier],
            approved_parent[1].approved_ttl_s if approved_parent is not None else 10**9,
        )
        decision = "APPROVE_DELEGATION_COSIGN" if needs_cosign else "APPROVE_DELEGATION"
        cosigner = hitl if needs_cosign else None
        if cosigner is not None and hitl_person is not None:
            pre_ids.extend((cosigner, hitl_person))
            citations.add(_evidence("AGENT-DEL-V5-01.D4", state.idp_accounts[cosigner].evidence_id))
        citations.update(
            {
                _evidence("AGENT-DEL-V5-01.D1", agent.evidence_id),
                _evidence("AGENT-DEL-V5-01.D2", state.idp_accounts[actor_account].evidence_id),
                _evidence("AGENT-DEL-V5-01.D3", request.evidence_id),
            }
        )
        if parent is not None:
            citations.add(_evidence("AGENT-DEL-V5-01.D2", parent.evidence_id))
        return cast(
            s.AgentDelegationMutationV5,
            _mk(
                work.ctx,
                s.AgentDelegationMutationV5,
                request.request_id,
                {
                    "mutation_kind": "AGENT_DELEGATION_MUTATION",
                    "delegation_decision": decision,
                    "hitl_cosigner_account_id": cosigner,
                    "approved_ttl_s": ttl,
                },
                pre_ids,
                citations,
                {
                    "failing_predicate": None,
                    "predicate_results": {
                        "D1": p1_ok,
                        "D2": p2_ok,
                        "D3": p3_ok,
                        "D4": p4_ok,
                    },
                    "needs_hitl": needs_hitl,
                },
            ),
        )

    citations.add(_evidence("AGENT-DEL-V5-01." + failing, request.evidence_id))
    if failing == "D1":
        citations.add(_evidence("AGENT-DEL-V5-01.D1", agent.evidence_id))
        citations.add(
            _evidence("AGENT-DEL-V5-01.D1", state.idp_accounts[actor_account].evidence_id)
        )
        if (
            sponsor is not None
            and sponsor in state.hr_persons
            and not has_live_engagement(state.hr_persons[sponsor], work.clock)
        ):
            citations.add(_evidence("AGENT-DEL-V5-01.D1", state.hr_persons[sponsor].evidence_id))
        inactive_actor = _bound_person_inactive(work, actor_account)
        if inactive_actor is not None:
            citations.add(
                _evidence("AGENT-DEL-V5-01.D1", state.hr_persons[inactive_actor].evidence_id)
            )
        if role.app_id != request.target_resource or not role.is_jit_capable_target:
            citations.add(_evidence("AGENT-DEL-V5-01.D1", role.evidence_id))
    elif failing == "D2":
        citations.add(_evidence("AGENT-DEL-V5-01.D2", agent.evidence_id))
        citations.add(
            _evidence("AGENT-DEL-V5-01.D2", state.idp_accounts[actor_account].evidence_id)
        )
        if parent is not None:
            citations.add(_evidence("AGENT-DEL-V5-01.D2", parent.evidence_id))
    elif failing == "D3":
        if d3a_witnesses and actor is not None:
            citations.update(_obo_workflow_citations(work, request, requested))
        else:
            citations.update(_evidence("AGENT-DEL-V5-01.D3", item) for item in d3b_sessions)
            citations.update(_evidence("AGENT-DEL-V5-01.D3", item) for item in d3b_rules)
    else:  # D4
        if hitl in state.idp_accounts:
            citations.add(_evidence("AGENT-DEL-V5-01.D4", state.idp_accounts[hitl].evidence_id))
        if hitl_person is not None and (
            hitl_person in {actor, sponsor}
            or (
                actor is not None
                and is_subordinate(hitl_person, actor, work.clock, state.hr_persons)
            )
            or (
                sponsor is not None
                and is_subordinate(hitl_person, sponsor, work.clock, state.hr_persons)
            )
        ):
            citations.add(
                _evidence("AGENT-DEL-V5-01.D4", state.hr_persons[hitl_person].evidence_id)
            )
        if hitl is not None:
            inactive_hitl = _bound_person_inactive(work, hitl)
            if inactive_hitl is not None:
                citations.add(
                    _evidence("AGENT-DEL-V5-01.D4", state.hr_persons[inactive_hitl].evidence_id)
                )
    return cast(
        s.AgentDelegationMutationV5,
        _mk(
            work.ctx,
            s.AgentDelegationMutationV5,
            request.request_id,
            {
                "mutation_kind": "AGENT_DELEGATION_MUTATION",
                "delegation_decision": "DENY_DELEGATION",
                "hitl_cosigner_account_id": None,
                "approved_ttl_s": 0,
            },
            pre_ids,
            citations,
            {
                "failing_predicate": failing,
                "predicate_results": {
                    "D1": p1_ok,
                    "D2": p2_ok,
                    "D3": p3_ok,
                    "D4": p4_ok,
                },
                "needs_hitl": needs_hitl,
            },
        ),
    )
