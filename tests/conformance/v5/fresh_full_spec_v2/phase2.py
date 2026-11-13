"""Independent Phase-2 reducer for SPEC-v5 §3.2 and §1.5."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .common import PublicInputError, PublicWorld, all_rows, precond
from .phase1 import _mutation
from .policy import (
    Footprint,
    active_session_end,
    effective_department,
    effective_grant_footprint,
    engagement_live,
    group_closure,
    group_footprint,
    leaves,
    person_engagement,
    person_has_live_engagement,
    raw_group_capabilities,
    role_footprint,
    scope_overlap,
    scoped_sod_witnesses,
    session_active,
    subordinate,
)


class PhaseTwo:
    def __init__(self, world: PublicWorld) -> None:
        if world.phase != 2 or world.ledger is None:
            raise ValueError("PhaseTwo requires phase-2 public input and a visible phase ledger")
        self.w = world
        self.ledger = world.ledger
        self.hr = world.rows.get("hr_persons", {})
        self.roles = world.rows.get("roles", {})
        self.grants = world.rows.get("grants", {})
        self.accounts = world.rows.get("idp_accounts", {})
        self.groups = world.rows.get("groups", {})
        self.sessions = all_rows(world, "historical_sessions")
        self.tickets = world.rows.get("change_tickets", {})
        self.bridges = world.rows.get("transfer_bridges", {})
        self.incidents = world.rows.get("cae_incidents", {})
        self.campaigns = world.rows.get("uar_campaigns", {})
        self.requests = world.rows.get("jit_requests", {})
        self.delegations = world.rows.get("agent_delegation_requests", {})
        self.enterprise_agents = world.rows.get("enterprise_agents", {})
        self.baseline_nhis = world.rows.get("baseline_nhis", {})
        self.target_nhis = world.rows.get("target_nhis", {})
        self.baseline_agents = world.rows.get("baseline_agents", {})
        self.ledger_accounts = {x["account_id"]: x for x in self.ledger["account_outcomes"]}
        self.ledger_nhis = {x["nhi_id"]: x for x in self.ledger["nhi_outcomes"]}
        self.ledger_agents = {x["agent_id"]: x for x in self.ledger["agent_outcomes"]}
        self.retro_tainted: set[str] = set()
        self.retro_rows: dict[str, dict[str, Any]] = {}
        self.cae_mutations: dict[str, dict[str, Any]] = {}
        self.inactive_due_engagement: set[str] = set()
        self.active_accounts: set[str] = set()
        self.standing_work: dict[str, set[str]] = defaultdict(set)
        self.eligible_work: dict[str, set[str]] = defaultdict(set)
        self.postshock_stripped: dict[tuple[str, str], tuple[dict[str, Any], ...]] = {}
        self.uar_mutations: dict[str, dict[str, Any]] = {}
        self.uar_stage_context: dict[str, dict[str, Any]] = {}
        self.working_sessions: list[dict[str, Any]] = []
        self.consumed_ticket_ids: set[str] = set()
        self.consumed_ticket_by: dict[str, str] = {}
        self.approved_dlreqs: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self.workflow_fp: dict[str, set[tuple[str, str]]] = defaultdict(set)
        self.jit_mutations: dict[str, dict[str, Any]] = {}
        self.jit_failure_by_id: dict[str, str | None] = {}
        self.delegation_mutations: dict[str, dict[str, Any]] = {}
        self.delegation_failure_by_id: dict[str, str | None] = {}
        self.opt_mutations: dict[str, dict[str, Any]] = {}
        self.optimization_feasible_by_target: dict[str, set[tuple[str, ...]]] = {}
        self.optimization_safety_by_target: dict[str, dict[tuple[str, ...], tuple[bool, bool]]] = {}
        self.gaps: list[dict[str, str]] = []

    def derive(self) -> list[dict[str, Any]]:
        self._derive_cae()
        self._initialize_working_state()
        self._derive_uar()
        self._derive_jit_and_delegation()
        try:
            self._derive_optimization()
        except PublicInputError as exc:
            if exc.code != "UNDERDEFINED_PUBLIC_INPUT":
                raise
            self.gaps.append(
                {
                    "code": exc.code,
                    "operation": self.w.case["operation"],
                    "case_id": self.w.case["case_id"],
                    "path": exc.path,
                    "detail": exc.detail,
                }
            )
        mutations = (
            list(self.cae_mutations.values())
            + list(self.uar_mutations.values())
            + list(self.jit_mutations.values())
            + list(self.delegation_mutations.values())
            + list(self.opt_mutations.values())
        )
        mutations.sort(key=lambda m: (m["mutation_kind"], m["target_entity_id"]))
        return mutations

    def _queue(self, field: str) -> tuple[str, ...]:
        return tuple(self.w.queue[field])

    def _person_from_ledger(self, account_id: str | None) -> str | None:
        outcome = self.ledger_accounts.get(account_id or "")
        return None if outcome is None else outcome["bound_person_id"]

    def _engagement_is_current(self, account_id: str) -> bool:
        outcome = self.ledger_accounts.get(account_id)
        if (
            outcome is None
            or outcome["p1_action"] != "KEEP_ACTIVE"
            or outcome["bound_person_id"] not in self.hr
        ):
            return False
        eng = person_engagement(self.w, outcome["bound_person_id"], outcome["bound_engagement_id"])
        return engagement_live(eng, self.w.clock)

    def _retro_for_person(self, person_id: str) -> int | None:
        person = self.hr.get(person_id)
        if person is None:
            return None
        times = [
            e["retro_terminated_effective_s"]
            for e in person["engagements"]
            if e["retro_terminated_effective_s"] is not None
        ]
        return min(times) if times else None

    def _retro_invalidates(self, approved_by: str, approved_at: int, incident_person: str) -> bool:
        outcome = self.ledger_accounts.get(approved_by)
        retro = self._retro_for_person(incident_person)
        return bool(
            outcome is not None
            and outcome["bound_person_id"] == incident_person
            and retro is not None
            and approved_at >= retro
        )

    def _derive_cae(self) -> None:
        self.cae_mutations = {}
        self.retro_tainted = set()
        self.retro_rows = {}
        for incident_id in sorted(self._queue("target_cae_incident_ids")):
            incident = self.incidents.get(incident_id)
            if incident is None:
                raise PublicInputError(
                    "UNMATERIALIZABLE_INPUT",
                    "/public_episode/phase2_slices",
                    f"target CAE incident missing from delivered view: {incident_id}",
                )
            person_id = incident["person_id"]
            person = self.hr.get(person_id)
            if person is None:
                raise PublicInputError(
                    "UNMATERIALIZABLE_INPUT",
                    "/public_episode/phase2_slices",
                    "CAE incident person is absent from current delivered view",
                )
            retro: set[str] = set()
            for ticket in self.tickets.values():
                if self._retro_invalidates(
                    ticket["approved_by_account_id"], ticket["approved_at_s"], person_id
                ):
                    retro.add(ticket["ticket_id"])
                    self.retro_rows[ticket["ticket_id"]] = ticket
            for bridge in self.bridges.values():
                if self._retro_invalidates(
                    bridge["approved_by_account_id"], bridge["approved_at_s"], person_id
                ):
                    retro.add(bridge["bridge_id"])
                    self.retro_rows[bridge["bridge_id"]] = bridge
            self.retro_tainted.update(retro)
            if incident["trigger_kind"] == "BENIGN_SESSION_RENEWAL":
                revoke, exposure, breached = False, 0, False
            else:
                revoke = True
                tokens = {t["token_id"]: t for t in incident["tokens"]}
                pending = set(tokens)
                idp_cutoff: dict[str, int] = {}
                effective_revoke: dict[str, int | None] = {}
                ordered: list[dict[str, Any]] = []
                while pending:
                    ready = sorted(
                        tid
                        for tid in pending
                        if tokens[tid]["parent_token_id"] is None
                        or tokens[tid]["parent_token_id"] in idp_cutoff
                    )
                    if not ready:
                        raise PublicInputError(
                            "UNDERDEFINED_PUBLIC_INPUT",
                            "/public_episode/phase2_slices/cae_incidents/tokens",
                            "refresh-token parent graph is cyclic or incomplete",
                        )
                    for tid in ready:
                        token = tokens[tid]
                        pending.remove(tid)
                        ordered.append(token)
                        own_candidates = [
                            token[x]
                            for x in ("revoke_ack_at_s", "token_exp_s")
                            if token[x] is not None
                        ]
                        own_cut = min(own_candidates)
                        parent_id = token["parent_token_id"]
                        idp_cutoff[tid] = (
                            own_cut if parent_id is None else min(own_cut, idp_cutoff[parent_id])
                        )
                        rev_candidates = [token["revoke_ack_at_s"]]
                        if parent_id is not None:
                            rev_candidates.append(effective_revoke[parent_id])
                        effective_revoke[tid] = min(
                            (x for x in rev_candidates if x is not None), default=None
                        )
                intervals: list[tuple[int, int]] = []
                for token in ordered:
                    if token["token_kind"] == "REFRESH":
                        continue
                    parent = token["parent_token_id"]
                    if parent is not None and token["issued_at_s"] >= idp_cutoff[parent]:
                        continue
                    if token["cae_capable_spoke"] and token["binding_kind"] == "BOUND_DPOP":
                        candidates = [
                            token["token_exp_s"],
                            token["next_spoke_poll_sync_at_s"],
                        ]
                        if effective_revoke[token["token_id"]] is not None:
                            candidates.append(effective_revoke[token["token_id"]])
                    elif token["cae_capable_spoke"] and token["binding_kind"] == "BEARER_UNBOUND":
                        candidates = [
                            token["token_exp_s"],
                            token["next_spoke_poll_sync_at_s"],
                        ]
                        if token["revoke_ack_at_s"] is not None:
                            candidates.append(token["revoke_ack_at_s"])
                    else:
                        candidates = [
                            token["token_exp_s"],
                            token["next_spoke_poll_sync_at_s"],
                        ]
                    start = max(incident["event_ts_s"], token["issued_at_s"])
                    stop = min(candidates)
                    if stop > start:
                        intervals.append((start, stop))
                intervals.sort()
                exposure = 0
                if intervals:
                    left, right = intervals[0]
                    for start, stop in intervals[1:]:
                        if start <= right:
                            right = max(right, stop)
                        else:
                            exposure += right - left
                            left, right = start, stop
                    exposure += right - left
                breached = exposure > self.w.overlay["cae_sla_s"]
            cites = {
                f"CAE-V5-01:{incident['evidence_id']}",
                f"CAE-V5-01:{person['evidence_id']}",
            }
            cites.update(f"CAE-V5-01:{token['evidence_id']}" for token in incident["tokens"])
            for entity_id in retro:
                source_row = self.retro_rows[entity_id]
                cites.add(f"SHOCK-V5-01:{source_row['evidence_id']}")
            self.cae_mutations[incident_id] = _mutation(
                "CAE_CONTAINMENT_MUTATION",
                incident_id,
                precond(self.w, [incident_id, person_id]),
                tuple(sorted(cites)),
                cae_revoke_sessions=revoke,
                cae_computed_exposure_s=exposure,
                cae_sla_breached=breached,
                retro_invalidated_ids=sorted(retro),
            )

    def _initialize_working_state(self) -> None:
        self.active_accounts = set()
        self.inactive_due_engagement = set()
        self.standing_work = defaultdict(set)
        self.eligible_work = defaultdict(set)
        self.postshock_stripped = {}
        self.working_sessions = list(self.sessions)
        for account_id, outcome in self.ledger_accounts.items():
            still_active = self._engagement_is_current(account_id)
            if still_active:
                self.active_accounts.add(account_id)
                self.standing_work[account_id] = set(outcome["effective_standing_grant_ids"])
                self.eligible_work[account_id] = set(outcome["effective_eligible_grant_ids"])
            else:
                self.standing_work[account_id] = set()
                self.eligible_work[account_id] = set()
                if outcome["p1_action"] == "KEEP_ACTIVE" and outcome["bound_person_id"] in self.hr:
                    eng = person_engagement(
                        self.w,
                        outcome["bound_person_id"],
                        outcome["bound_engagement_id"],
                    )
                    if eng is not None and not engagement_live(eng, self.w.clock):
                        self.inactive_due_engagement.add(account_id)
        for account_id in sorted(self.active_accounts):
            outcome = self.ledger_accounts[account_id]
            person_id = outcome["bound_person_id"]
            eng = person_engagement(self.w, person_id, outcome["bound_engagement_id"])
            if eng is None:
                continue
            department = effective_department(eng, self.w.clock)
            for grant_id in sorted(self.standing_work[account_id] | self.eligible_work[account_id]):
                grant = self.grants.get(grant_id)
                if grant is None:
                    continue
                if grant["scope_path"] == "/*" or leaves(self.w, grant["scope_path"]) <= leaves(
                    self.w, department
                ):
                    continue
                role = self.roles.get(grant["role_id"])
                valid_bridges = tuple(
                    bridge
                    for bridge in self.bridges.values()
                    if bridge["account_id"] == account_id
                    and bridge["grant_id"] == grant_id
                    and bridge["bridge_id"] not in self.retro_tainted
                    and eng["prior_dept_path"] is not None
                    and leaves(self.w, grant["scope_path"])
                    <= leaves(self.w, bridge["from_dept_path"])
                    and bridge["from_dept_path"] == eng["prior_dept_path"]
                    and bridge["to_dept_path"] == department
                    and role is not None
                    and bridge["target_resource"] == role["app_id"]
                    and bridge["approved_at_s"] <= self.w.clock < bridge["expires_at_s"]
                )
                if valid_bridges:
                    continue
                self.standing_work[account_id].discard(grant_id)
                self.eligible_work[account_id].discard(grant_id)
                tainted = tuple(
                    bridge
                    for bridge in self.bridges.values()
                    if bridge["account_id"] == account_id
                    and bridge["grant_id"] == grant_id
                    and bridge["bridge_id"] in self.retro_tainted
                    and eng["prior_dept_path"] is not None
                    and leaves(self.w, grant["scope_path"])
                    <= leaves(self.w, bridge["from_dept_path"])
                    and bridge["from_dept_path"] == eng["prior_dept_path"]
                    and bridge["to_dept_path"] == department
                    and role is not None
                    and bridge["target_resource"] == role["app_id"]
                    and bridge["approved_at_s"] <= self.w.clock < bridge["expires_at_s"]
                )
                self.postshock_stripped[(account_id, grant_id)] = tainted

    def _active_sessions(self, person_id: str | None) -> list[dict[str, Any]]:
        if person_id is None:
            return []
        return [
            session
            for session in self.working_sessions
            if self._bound_person(session["account_id"]) == person_id
            and session_active(session, self.w.clock)
        ]

    def _temporal_blockers(
        self, person_id: str | None, role_id: str, scope_path: str, emergency: bool
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        temporal: list[tuple[dict[str, Any], dict[str, Any]]] = []
        shared: list[dict[str, Any]] = []
        for session in self.working_sessions:
            if self._bound_person(session["account_id"]) != person_id:
                continue
            end = active_session_end(session)
            for rule in self.w.overlay["temporal_sod_rules"]:
                pair = {rule["role_a_id"], rule["role_b_id"]} == {
                    session["role_id"],
                    role_id,
                }
                different = session["role_id"] != role_id
                scope_ok = rule["scope_mode"] == "GLOBAL" or scope_overlap(
                    self.w, session["scope_path"], scope_path
                )
                if (
                    pair
                    and different
                    and scope_ok
                    and session["audit_events_count"] > 0
                    and session["start_s"] <= self.w.clock < end + rule["cooldown_s"]
                ):
                    temporal.append((session, rule))
            if (
                emergency
                and session["session_kind"] == "SHARED_BREAK_GLASS"
                and session["start_s"]
                <= self.w.clock
                < end + self.w.overlay["break_glass_cooldown_s"]
            ):
                shared.append(session)
        return ([x[0] for x in temporal], [x[1] for x in temporal], shared)

    def _standing_account_fp(self, account_id: str) -> Footprint:
        if not self._active(account_id):
            return frozenset()
        footprint = set(group_footprint(self.w, account_id))
        for gid in self.standing_work[account_id]:
            grant = self.grants.get(gid)
            if grant is not None:
                footprint.update(effective_grant_footprint(self.w, account_id, grant))
        return frozenset(footprint)

    def _active_conflict_sources(self, person_id: str | None, request_fp: Footprint) -> set[str]:
        cites: set[str] = set()
        if person_id is None:
            return cites
        for aid in sorted(self.active_accounts):
            if self._bound_person(aid) != person_id:
                continue
            for group in group_closure(self.w, aid):
                if group["effect"] != "ALLOW":
                    continue
                group_fp = {
                    (cap, leaf)
                    for cap in raw_group_capabilities(self.w, group)
                    for leaf in leaves(self.w, group["scope_path"])
                } - set(self._deny(aid))
                if scoped_sod_witnesses(self.w, request_fp, group_fp):
                    cites.add(group["evidence_id"])
            for gid in self.standing_work[aid]:
                grant = self.grants.get(gid)
                if grant is not None and scoped_sod_witnesses(
                    self.w, request_fp, effective_grant_footprint(self.w, aid, grant)
                ):
                    cites.add(grant["evidence_id"])
        for session in self._active_sessions(person_id):
            if scoped_sod_witnesses(
                self.w,
                request_fp,
                role_footprint(self.w, session["role_id"], session["scope_path"]),
            ):
                cites.add(session["evidence_id"])
        return cites

    def _derive_jit_and_delegation(self) -> None:
        self.jit_mutations = {}
        self.delegation_mutations = {}
        self.jit_failure_by_id = {}
        self.delegation_failure_by_id = {}
        events: list[tuple[int, str, dict[str, Any]]] = []
        events.extend((request["eval_seq"], "jit", request) for request in self.requests.values())
        events.extend(
            (request["eval_seq"], "delegation", request) for request in self.delegations.values()
        )
        sequences = [item[0] for item in events]
        if len(sequences) != len(set(sequences)):
            raise PublicInputError(
                "UNDERDEFINED_PUBLIC_INPUT",
                "/public_episode/phase2_slices/requests",
                "JIT and delegation eval_seq values are tied",
            )
        events.sort(key=lambda item: item[0])
        tier_caps = dict(self.w.overlay["jit_ttl_cap_by_tier"])
        for _, event_kind, request in events:
            if event_kind == "jit":
                self._derive_one_jit(request, tier_caps)
            else:
                self._derive_one_delegation(request, tier_caps)

    def _derive_one_jit(self, request: dict[str, Any], tier_caps: dict[str, int]) -> None:
        request_id = request["request_id"]
        requester = request["requester_account_id"]
        requester_person = self._bound_person(requester)
        role = self.roles.get(request["requested_role_id"])
        if role is None:
            raise PublicInputError(
                "UNMATERIALIZABLE_INPUT",
                "/public_episode/phase2_slices/jit_requests",
                "requested role is absent from delivered view",
            )
        request_fp = role_footprint(self.w, role["role_id"], request["requested_scope_path"])
        p1_fail = not (
            self._active(requester)
            and role["app_id"] == request["target_resource"]
            and role["is_jit_capable_target"]
            and not (request_fp & self._deny(requester))
            and (
                request["request_channel"] == "EMERGENCY_BREAK_GLASS"
                or any(
                    gid in self.eligible_work[requester]
                    and self.grants.get(gid, {}).get("role_id") == role["role_id"]
                    and leaves(self.w, request["requested_scope_path"])
                    <= leaves(self.w, self.grants[gid]["scope_path"])
                    for gid in self.eligible_work[requester]
                )
            )
        )
        ticket = (
            self.tickets.get(request["change_ticket_id"])
            if request["change_ticket_id"] is not None
            else None
        )
        ticket_missing = request["change_ticket_id"] is not None and ticket is None
        ticket_failure = (
            request["request_channel"] == "STANDARD_TICKET" and request["change_ticket_id"] is None
        )
        ticket_replay = False
        ticket_retro = False
        ticket_person: str | None = None
        if request["change_ticket_id"] is not None:
            if ticket is None:
                ticket_failure = True
            else:
                ticket_person = self._person_from_ledger(ticket["approved_by_account_id"])
                ticket_retro = ticket["ticket_id"] in self.retro_tainted
                ticket_replay = (
                    ticket["single_use"] and ticket["ticket_id"] in self.consumed_ticket_ids
                )
                binding_ok = (
                    ticket["bound_requester_account_id"],
                    ticket["target_resource"],
                    ticket["approved_role_id"],
                    ticket["approved_scope_path"],
                ) == (
                    requester,
                    request["target_resource"],
                    request["requested_role_id"],
                    request["requested_scope_path"],
                )
                ticket_failure = not (
                    ticket["external_ticket_status"] == "APPROVED_EXTERNAL"
                    and ticket["approved_at_s"] <= self.w.clock < ticket["expires_at_s"]
                    and not ticket_retro
                    and binding_ok
                    and not ticket_replay
                )
        p2_fail = ticket_failure or ticket_missing
        p3a_conflict = bool(
            scoped_sod_witnesses(self.w, request_fp, self._k_eff_auth(requester_person))
        )
        temporal_block, temporal_rules, shared_block = self._temporal_blockers(
            requester_person,
            role["role_id"],
            request["requested_scope_path"],
            request["request_channel"] == "EMERGENCY_BREAK_GLASS",
        )
        p3b_fail = bool(temporal_block or shared_block)
        approver = request["proposed_approver_account_id"]
        approver_person = self._bound_person(approver)
        p4_fail = not (
            self._active(approver)
            and approver_person is not None
            and approver_person != requester_person
            and not subordinate(self.w, approver_person, requester_person, self.w.clock)
            and leaves(self.w, request["requested_scope_path"])
            <= self._admin_scope(approver, request["target_resource"])
        )
        failed = next(
            (
                gate
                for gate, fail in (
                    ("P1", p1_fail),
                    ("P2", p2_fail),
                    ("P3", p3a_conflict or p3b_fail),
                    ("P4", p4_fail),
                )
                if fail
            ),
            None,
        )
        self.jit_failure_by_id[request_id] = failed
        if failed is not None:
            decision, approver_out, ttl = "DENY", None, 0
            external = (
                "NO_EXTERNAL_TICKET"
                if request["change_ticket_id"] is None
                else "SYNC_REJECTED_POLICY"
            )
        else:
            needs_cosign = (
                request["request_channel"] == "EMERGENCY_BREAK_GLASS"
                or role["privilege_tier"] in self.w.overlay["critical_jit_tiers"]
            )
            decision = "APPROVE_JIT_COSIGN" if needs_cosign else "APPROVE_JIT"
            approver_out = approver
            ttl = min(
                request["requested_ttl_s"],
                tier_caps[role["privilege_tier"]],
                1800
                if request["request_channel"] == "EMERGENCY_BREAK_GLASS"
                else tier_caps[role["privilege_tier"]],
                ticket["expires_at_s"] - self.w.clock if ticket is not None else 10**9,
            )
            external = (
                "NO_EXTERNAL_TICKET"
                if ticket is None
                else ("SYNC_GRANTED_COSIGN" if needs_cosign else "SYNC_GRANTED")
            )
        citations: set[str] = set()
        if failed is None:
            requester_row = self.accounts.get(requester)
            if requester_row is not None:
                citations.add(f"JIT-V5-01.P1:{requester_row['evidence_id']}")
            if ticket is not None:
                citations.add(f"JIT-V5-01.P2:{ticket['evidence_id']}")
            else:
                citations.add(f"JIT-V5-01.P2:{request['evidence_id']}")
            citations.add(f"JIT-V5-01.P3:{request['evidence_id']}")
            approver_row = self.accounts.get(approver or "")
            if approver_row is not None:
                citations.add(f"JIT-V5-01.P4:{approver_row['evidence_id']}")
        elif failed == "P1":
            citations.add(f"JIT-V5-01.P1:{request['evidence_id']}")
            requester_row = self.accounts.get(requester)
            if requester_row is not None:
                citations.add(f"JIT-V5-01.P1:{requester_row['evidence_id']}")
            if role["app_id"] != request["target_resource"] or not role["is_jit_capable_target"]:
                citations.add(f"JIT-V5-01.P1:{role['evidence_id']}")
            if requester in self.inactive_due_engagement and requester_person in self.hr:
                citations.add(f"JIT-V5-01.P1:{self.hr[requester_person]['evidence_id']}")
            if request_fp & self._deny(requester):
                from .policy import cited_group_denies

                citations.update(
                    f"JIT-V5-01.P1:{ev}" for ev in cited_group_denies(self.w, requester, request_fp)
                )
        elif failed == "P2":
            citations.add(f"JIT-V5-01.P2:{request['evidence_id']}")
            if ticket is not None:
                citations.add(f"JIT-V5-01.P2:{ticket['evidence_id']}")
            if ticket_replay and ticket is not None:
                previous = self.consumed_ticket_by.get(ticket["ticket_id"])
                if previous is not None:
                    citations.add(f"JIT-V5-01.P2:{previous}")
            if ticket_retro and ticket_person in self.hr:
                citations.add(f"JIT-V5-01.P2:{self.hr[ticket_person]['evidence_id']}")
        elif failed == "P3":
            citations.add(f"JIT-V5-01.P3:{request['evidence_id']}")
            if p3a_conflict:
                citations.update(
                    f"JIT-V5-01.P3:{e}"
                    for e in self._active_conflict_sources(requester_person, request_fp)
                )
            else:
                for session, rule in zip(temporal_block, temporal_rules, strict=True):
                    citations.add(f"JIT-V5-01.P3:{session['evidence_id']}")
                    citations.add(f"JIT-V5-01.P3:{rule['evidence_id']}")
                for session in shared_block:
                    citations.add(f"JIT-V5-01.P3:{session['evidence_id']}")
                    session_role = self.roles.get(session["role_id"])
                    if session_role is not None:
                        citations.add(f"JIT-V5-01.P3:{session_role['evidence_id']}")
        else:
            citations.add(f"JIT-V5-01.P4:{request['evidence_id']}")
            approver_row = self.accounts.get(approver or "")
            if approver_row is not None:
                citations.add(f"JIT-V5-01.P4:{approver_row['evidence_id']}")
            if approver in self.inactive_due_engagement and approver_person in self.hr:
                citations.add(f"JIT-V5-01.P4:{self.hr[approver_person]['evidence_id']}")
            if (
                approver_person is not None
                and requester_person is not None
                and subordinate(self.w, approver_person, requester_person, self.w.clock)
                and approver_person in self.hr
            ):
                citations.add(f"JIT-V5-01.P4:{self.hr[approver_person]['evidence_id']}")
        match_ids: list[str | None] = [request_id, requester, requester_person]
        if failed is None:
            if request["change_ticket_id"] is not None:
                match_ids.append(request["change_ticket_id"])
            match_ids.extend((approver, approver_person))
        self.jit_mutations[request_id] = _mutation(
            "JIT_DECISION_MUTATION",
            request_id,
            precond(self.w, match_ids),
            tuple(sorted(citations)),
            jit_decision=decision,
            approver_account_id=approver_out,
            approved_ttl_s=ttl,
            external_sync_status=external,
        )
        if failed is None:
            if ticket is not None and ticket["single_use"]:
                self.consumed_ticket_ids.add(ticket["ticket_id"])
                self.consumed_ticket_by[ticket["ticket_id"]] = request["evidence_id"]
            self.working_sessions.append(
                {
                    "session_id": f"sess_prov_{request_id}",
                    "account_id": requester,
                    "role_id": role["role_id"],
                    "scope_path": request["requested_scope_path"],
                    "session_kind": "SHARED_BREAK_GLASS"
                    if request["request_channel"] == "EMERGENCY_BREAK_GLASS"
                    else "PERSONAL_JIT",
                    "start_s": self.w.clock,
                    "scheduled_end_s": self.w.clock + ttl,
                    "revoked_at_s": None,
                    "audit_events_count": 1,
                    "last_audit_event_s": self.w.clock,
                    "evidence_id": request["evidence_id"],
                }
            )

    def _derive_optimization(self) -> None:
        self.opt_mutations = {}
        for opt_id in sorted(self._queue("target_opt_task_ids")):
            opt = self.w.rows.get("optimization_tasks", {}).get(opt_id)
            if opt is None:
                raise PublicInputError(
                    "UNMATERIALIZABLE_INPUT",
                    "/public_episode/phase2_slices",
                    f"phase-2 optimization task missing from delivered view: {opt_id}",
                )
            if opt["phase_kind"] != "P2_REVIEWER_ASSIGNMENT":
                raise PublicInputError(
                    "UNDERDEFINED_PUBLIC_INPUT",
                    "/public_episode/phase2_slices/optimization_tasks",
                    "phase-2 queue contains a phase-1 optimization task",
                )
            candidates = sorted(opt["candidates"], key=lambda c: c["candidate_id"])
            by_candidate = {c["candidate_id"]: c for c in candidates}

            def candidate_footprint(candidate: dict[str, Any]) -> Footprint:
                role_fp = set()
                for scope in candidate["covered_scopes"]:
                    role_fp.update(role_footprint(self.w, candidate["role_id"], scope))
                return frozenset(role_fp)

            def feasible(
                selected_ids: tuple[str, ...],
                by_candidate: dict[str, dict[str, Any]] = by_candidate,
                opt: dict[str, Any] = opt,
            ) -> bool:
                selected = [by_candidate[cid] for cid in selected_ids]
                by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
                covered: set[str] = set()
                for candidate in selected:
                    aid = candidate["candidate_account_id"]
                    person_id = self._bound_person(aid)
                    if not self._active(aid) or person_id is None:
                        return False
                    fp = candidate_footprint(candidate)
                    if fp & self._deny(aid):
                        return False
                    if person_id in opt["excluded_conflict_person_ids"]:
                        return False
                    if any(
                        subordinate(self.w, person_id, conflict, self.w.clock)
                        for conflict in opt["excluded_conflict_person_ids"]
                    ):
                        return False
                    by_person[person_id].append(candidate)
                    covered.update(candidate["covered_scopes"])
                if not set(opt["required_review_scopes"]) <= covered:
                    return False
                for person_id, person_candidates in by_person.items():
                    if len(person_candidates) > opt["max_assignments_per_person"]:
                        return False
                    selected_fp = frozenset(
                        pair
                        for candidate in person_candidates
                        for pair in candidate_footprint(candidate)
                    )
                    if scoped_sod_witnesses(
                        self.w, selected_fp, self._k_eff_auth(person_id) | selected_fp
                    ):
                        return False
                return True

            def safety_flags(
                selected_ids: tuple[str, ...],
                by_candidate: dict[str, dict[str, Any]] = by_candidate,
                opt: dict[str, Any] = opt,
            ) -> tuple[bool, bool]:
                selected = [by_candidate[cid] for cid in selected_ids]
                by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
                covered: set[str] = set()
                overgrant = False
                for candidate in selected:
                    aid = candidate["candidate_account_id"]
                    person_id = self._bound_person(aid)
                    if not self._active(aid) or person_id is None:
                        overgrant = True
                        continue
                    fp = candidate_footprint(candidate)
                    overgrant |= bool(fp & self._deny(aid))
                    overgrant |= person_id in opt["excluded_conflict_person_ids"]
                    overgrant |= any(
                        subordinate(self.w, person_id, conflict, self.w.clock)
                        for conflict in opt["excluded_conflict_person_ids"]
                    )
                    by_person[person_id].append(candidate)
                    covered.update(candidate["covered_scopes"])
                availability = not set(opt["required_review_scopes"]) <= covered
                for person_id, person_candidates in by_person.items():
                    overgrant |= len(person_candidates) > opt["max_assignments_per_person"]
                    selected_fp = frozenset(
                        pair
                        for candidate in person_candidates
                        for pair in candidate_footprint(candidate)
                    )
                    overgrant |= bool(
                        scoped_sod_witnesses(
                            self.w, selected_fp, self._k_eff_auth(person_id) | selected_fp
                        )
                    )
                return overgrant, availability

            candidate_ids = [candidate["candidate_id"] for candidate in candidates]
            feasible_sets: list[tuple[int, tuple[str, ...]]] = []
            safety_by_subset: dict[tuple[str, ...], tuple[bool, bool]] = {}
            for mask in range(1 << len(candidate_ids)):
                selected = tuple(
                    candidate_ids[index]
                    for index in range(len(candidate_ids))
                    if mask & (1 << index)
                )
                safety_by_subset[selected] = safety_flags(selected)
                if feasible(selected):
                    cost = sum(by_candidate[cid]["action_cost"] for cid in selected)
                    feasible_sets.append((cost, selected))
            self.optimization_feasible_by_target[opt_id] = {
                selected for _, selected in feasible_sets
            }
            self.optimization_safety_by_target[opt_id] = safety_by_subset
            if not feasible_sets:
                raise PublicInputError(
                    "UNDERDEFINED_PUBLIC_INPUT",
                    "/public_episode/phase2_slices/optimization_tasks",
                    "no reviewer assignment subset satisfies the normative constraints",
                )
            minimum = min(cost for cost, _ in feasible_sets)
            winners = [selected for cost, selected in feasible_sets if cost == minimum]
            if len(winners) != 1:
                raise PublicInputError(
                    "UNDERDEFINED_PUBLIC_INPUT",
                    "/public_episode/phase2_slices/optimization_tasks",
                    "multiple reviewer assignment sets share the minimum objective; "
                    "SPEC-v5 does not define an equal-cost tie-break",
                )
            selected_ids = winners[0]
            citations = {f"OPT-V5-02:{opt['evidence_id']}"}
            match_ids: list[str | None] = [opt_id]
            for candidate_id in selected_ids:
                candidate = by_candidate[candidate_id]
                aid = candidate["candidate_account_id"]
                person_id = self._bound_person(aid)
                account = self.accounts.get(aid)
                role = self.roles.get(candidate["role_id"])
                if account is None or person_id not in self.hr or role is None:
                    raise PublicInputError(
                        "UNMATERIALIZABLE_INPUT",
                        "/public_episode/phase2_slices/optimization_tasks",
                        "selected reviewer evidence is absent from the delivered view",
                    )
                citations.add(f"OPT-V5-02:{account['evidence_id']}")
                citations.add(f"OPT-V5-02:{self.hr[person_id]['evidence_id']}")
                citations.add(f"OPT-V5-02:{role['evidence_id']}")
                match_ids.extend((aid, person_id))
            self.opt_mutations[opt_id] = _mutation(
                "OPTIMIZATION_MUTATION",
                opt_id,
                precond(self.w, match_ids),
                tuple(sorted(citations)),
                selected_ids=list(selected_ids),
                objective_value=minimum,
            )

    def _derive_one_delegation(self, request: dict[str, Any], tier_caps: dict[str, int]) -> None:
        request_id = request["request_id"]
        agent_id = request["agent_id"]
        delegator = request["delegator_account_id"]
        actor_person = self._bound_person(delegator)
        agent = self.enterprise_agents.get(agent_id)
        agent_out = self.ledger_agents.get(agent_id)
        role = self.roles.get(request["requested_role_id"])
        if role is None:
            raise PublicInputError(
                "UNMATERIALIZABLE_INPUT",
                "/public_episode/phase2_slices/agent_delegation_requests",
                "delegation requested role is absent from delivered view",
            )
        request_fp = role_footprint(self.w, role["role_id"], request["requested_scope_path"])
        sponsor = None if agent_out is None else agent_out["resolved_sponsor_person_id"]
        p1_d1 = bool(
            agent is not None
            and agent_out is not None
            and agent_out["p1_agent_action"] == "REGISTER_ACTIVE"
            and sponsor in self.hr
            and person_has_live_engagement(self.w, sponsor, self.w.clock)
            and self._active(delegator)
            and role["app_id"] == request["target_resource"]
            and role["is_jit_capable_target"]
        )
        bindings = {} if agent is None else {b["binding_id"]: b for b in agent["tool_bindings"]}
        auto_ids = () if agent_out is None else agent_out["active_autonomous_binding_ids"]
        hitl_ids = () if agent_out is None else agent_out["active_hitl_binding_ids"]
        auto_fp = frozenset(
            (cap, leaf)
            for bid in auto_ids
            if bid in bindings
            for cap in bindings[bid]["capabilities"]
            for leaf in leaves(self.w, bindings[bid]["scope_path"])
        )
        ceiling_fp = set(auto_fp)
        for bid in hitl_ids:
            binding = bindings.get(bid)
            if binding is not None:
                ceiling_fp.update(
                    (cap, leaf)
                    for cap in binding["capabilities"]
                    for leaf in leaves(self.w, binding["scope_path"])
                )
        d2_fail = not (
            request_fp <= ceiling_fp
            and request_fp <= self._standing_account_fp(delegator)
            and request["delegation_hop"] <= self.w.overlay["max_agent_delegation_hops"]
        )
        parent_pair: tuple[dict[str, Any], dict[str, Any]] | None = None
        parent_id = request["parent_delegation_request_id"]
        if parent_id is not None:
            parent_pair = self.approved_dlreqs.get(parent_id)
            if parent_pair is None:
                d2_fail = True
            else:
                parent_req, _ = parent_pair
                parent_role = self.roles.get(parent_req["requested_role_id"])
                parent_fp = (
                    frozenset()
                    if parent_role is None
                    else role_footprint(
                        self.w,
                        parent_role["role_id"],
                        parent_req["requested_scope_path"],
                    )
                )
                d2_fail = d2_fail or not (
                    request["delegation_hop"] == parent_req["delegation_hop"] + 1
                    and self._bound_person(parent_req["delegator_account_id"]) == actor_person
                    and request_fp <= parent_fp
                )
        actor_effective = set(self._k_eff_auth(actor_person)) | set(
            self.workflow_fp[request["workflow_id"]]
        )
        d3a_fail = bool(scoped_sod_witnesses(self.w, request_fp, actor_effective))
        temporal_block, temporal_rules, _ = self._temporal_blockers(
            actor_person, role["role_id"], request["requested_scope_path"], False
        )
        d3b_fail = bool(temporal_block)
        need_hitl = (
            not (request_fp <= auto_fp)
            or role["privilege_tier"] in self.w.overlay["critical_jit_tiers"]
        )
        hitl = request["proposed_hitl_approver_account_id"]
        hitl_person = self._bound_person(hitl)
        sponsor_or_actor = {p for p in (actor_person, sponsor) if p is not None}
        d4_fail = False
        if need_hitl:
            d4_fail = not (
                hitl is not None
                and self._active(hitl)
                and hitl_person is not None
                and hitl_person not in sponsor_or_actor
                and not subordinate(self.w, hitl_person, actor_person, self.w.clock)
                and not subordinate(self.w, hitl_person, sponsor, self.w.clock)
                and leaves(self.w, request["requested_scope_path"])
                <= self._admin_scope(hitl, request["target_resource"])
            )
        failed = next(
            (
                gate
                for gate, fail in (
                    ("D1", not p1_d1),
                    ("D2", d2_fail),
                    ("D3", d3a_fail or d3b_fail),
                    ("D4", d4_fail),
                )
                if fail
            ),
            None,
        )
        self.delegation_failure_by_id[request_id] = failed
        if failed is not None:
            decision, hitl_out, ttl = "DENY_DELEGATION", None, 0
        else:
            decision = "APPROVE_DELEGATION_COSIGN" if need_hitl else "APPROVE_DELEGATION"
            hitl_out = hitl if need_hitl else None
            parent_ttl = parent_pair[1]["approved_ttl_s"] if parent_pair is not None else 10**9
            ttl = min(
                request["requested_ttl_s"],
                tier_caps[role["privilege_tier"]],
                parent_ttl,
            )
        citations: set[str] = set()
        if failed is None:
            citations.add(f"AGENT-DEL-V5-01.D1:{agent['evidence_id']}")
            delegator_row = self.accounts.get(delegator)
            if delegator_row is not None:
                citations.add(f"AGENT-DEL-V5-01.D2:{delegator_row['evidence_id']}")
            if parent_id is not None and parent_id in self.delegations:
                citations.add(f"AGENT-DEL-V5-01.D2:{self.delegations[parent_id]['evidence_id']}")
            citations.add(f"AGENT-DEL-V5-01.D3:{request['evidence_id']}")
            if need_hitl and hitl in self.accounts:
                citations.add(f"AGENT-DEL-V5-01.D4:{self.accounts[hitl]['evidence_id']}")
        elif failed == "D1":
            citations.add(f"AGENT-DEL-V5-01.D1:{request['evidence_id']}")
            if agent is not None:
                citations.add(f"AGENT-DEL-V5-01.D1:{agent['evidence_id']}")
            delegator_row = self.accounts.get(delegator)
            if delegator_row is not None:
                citations.add(f"AGENT-DEL-V5-01.D1:{delegator_row['evidence_id']}")
            if sponsor in self.hr and not person_has_live_engagement(self.w, sponsor, self.w.clock):
                citations.add(f"AGENT-DEL-V5-01.D1:{self.hr[sponsor]['evidence_id']}")
            if delegator in self.inactive_due_engagement and actor_person in self.hr:
                citations.add(f"AGENT-DEL-V5-01.D1:{self.hr[actor_person]['evidence_id']}")
            if role["app_id"] != request["target_resource"] or not role["is_jit_capable_target"]:
                citations.add(f"AGENT-DEL-V5-01.D1:{role['evidence_id']}")
        elif failed == "D2":
            citations.add(f"AGENT-DEL-V5-01.D2:{request['evidence_id']}")
            if agent is not None:
                citations.add(f"AGENT-DEL-V5-01.D2:{agent['evidence_id']}")
            delegator_row = self.accounts.get(delegator)
            if delegator_row is not None:
                citations.add(f"AGENT-DEL-V5-01.D2:{delegator_row['evidence_id']}")
            if parent_id is not None and parent_id in self.delegations:
                citations.add(f"AGENT-DEL-V5-01.D2:{self.delegations[parent_id]['evidence_id']}")
        elif failed == "D3":
            citations.add(f"AGENT-DEL-V5-01.D3:{request['evidence_id']}")
            if d3a_fail:
                citations.update(
                    f"AGENT-DEL-V5-01.D3:{ev}"
                    for ev in self._active_conflict_sources(actor_person, request_fp)
                )
                for prior_req, _ in self.approved_dlreqs.values():
                    if prior_req["workflow_id"] == request["workflow_id"]:
                        prior_role = self.roles.get(prior_req["requested_role_id"])
                        prior_fp = (
                            frozenset()
                            if prior_role is None
                            else role_footprint(
                                self.w,
                                prior_role["role_id"],
                                prior_req["requested_scope_path"],
                            )
                        )
                        if scoped_sod_witnesses(self.w, request_fp, prior_fp):
                            citations.add(f"AGENT-DEL-V5-01.D3:{prior_req['evidence_id']}")
                for session in self._active_sessions(actor_person):
                    if scoped_sod_witnesses(
                        self.w,
                        request_fp,
                        role_footprint(self.w, session["role_id"], session["scope_path"]),
                    ):
                        citations.add(f"AGENT-DEL-V5-01.D3:{session['evidence_id']}")
            else:
                for session, rule in zip(temporal_block, temporal_rules, strict=True):
                    citations.add(f"AGENT-DEL-V5-01.D3:{session['evidence_id']}")
                    citations.add(f"AGENT-DEL-V5-01.D3:{rule['evidence_id']}")
        else:
            citations.add(f"AGENT-DEL-V5-01.D4:{request['evidence_id']}")
            if hitl in self.accounts:
                citations.add(f"AGENT-DEL-V5-01.D4:{self.accounts[hitl]['evidence_id']}")
            if hitl_person in self.hr and (
                hitl in self.inactive_due_engagement
                or hitl_person in sponsor_or_actor
                or subordinate(self.w, hitl_person, actor_person, self.w.clock)
                or subordinate(self.w, hitl_person, sponsor, self.w.clock)
            ):
                citations.add(f"AGENT-DEL-V5-01.D4:{self.hr[hitl_person]['evidence_id']}")
        match_ids: list[str | None] = [
            request_id,
            agent_id,
            delegator,
            sponsor,
            actor_person,
        ]
        if parent_id is not None and parent_id in self.delegations:
            match_ids.append(parent_id)
        if decision == "APPROVE_DELEGATION_COSIGN":
            match_ids.extend((hitl, hitl_person))
        mutation = _mutation(
            "AGENT_DELEGATION_MUTATION",
            request_id,
            precond(self.w, match_ids),
            tuple(sorted(citations)),
            delegation_decision=decision,
            hitl_cosigner_account_id=hitl_out,
            approved_ttl_s=ttl,
        )
        self.delegation_mutations[request_id] = mutation
        if failed is None:
            self.approved_dlreqs[request_id] = (request, mutation)
            self.workflow_fp[request["workflow_id"]].update(request_fp)
            self.working_sessions.append(
                {
                    "session_id": f"sess_prov_{request_id}",
                    "account_id": delegator,
                    "role_id": role["role_id"],
                    "scope_path": request["requested_scope_path"],
                    "session_kind": "AGENT_OBO",
                    "start_s": self.w.clock,
                    "scheduled_end_s": self.w.clock + ttl,
                    "revoked_at_s": None,
                    "audit_events_count": 1,
                    "last_audit_event_s": self.w.clock,
                    "evidence_id": request["evidence_id"],
                }
            )

    def _active(self, account_id: str | None) -> bool:
        return account_id is not None and account_id in self.active_accounts

    def _bound_person(self, account_id: str | None) -> str | None:
        if account_id is None:
            return None
        outcome = self.ledger_accounts.get(account_id)
        return None if outcome is None else outcome["bound_person_id"]

    def _k_work(self, person_id: str | None) -> Footprint:
        if person_id is None:
            return frozenset()
        footprint: set[tuple[str, str]] = set()
        for aid in sorted(self.active_accounts):
            if self._bound_person(aid) != person_id:
                continue
            footprint.update(group_footprint(self.w, aid))
            for gid in self.standing_work[aid]:
                grant = self.grants.get(gid)
                if grant is not None:
                    footprint.update(effective_grant_footprint(self.w, aid, grant))
        return frozenset(footprint)

    def _k_eff_auth(self, person_id: str | None) -> Footprint:
        footprint = set(self._k_work(person_id))
        if person_id is None:
            return frozenset()
        for session in self.working_sessions:
            if self._bound_person(session["account_id"]) == person_id and session_active(
                session, self.w.clock
            ):
                footprint.update(role_footprint(self.w, session["role_id"], session["scope_path"]))
        return frozenset(footprint)

    def _person_nhi_fp(self, person_id: str | None) -> Footprint:
        if person_id is None:
            return frozenset()
        footprint: set[tuple[str, str]] = set()
        for nhi in self.baseline_nhis.values():
            if nhi["owner_person_id"] == person_id and nhi["status"] == "ACTIVE":
                footprint.update(
                    (cap, leaf)
                    for cap in nhi["capabilities"]
                    for leaf in leaves(self.w, nhi["scope_path"])
                )
        for nhi_id, outcome in self.ledger_nhis.items():
            target = self.target_nhis.get(nhi_id)
            if (
                target is not None
                and outcome["p1_nhi_action"] == "ATTRIBUTE_OWNER"
                and outcome["resolved_owner_person_id"] == person_id
            ):
                footprint.update(
                    (cap, leaf)
                    for cap in target["capabilities"]
                    for leaf in leaves(self.w, target["scope_path"])
                )
        return frozenset(footprint)

    def _person_sponsor_auto_fp(self, person_id: str | None) -> Footprint:
        if person_id is None:
            return frozenset()
        footprint: set[tuple[str, str]] = set()
        for agent in self.baseline_agents.values():
            if agent["sponsor_person_id"] == person_id and agent["status"] == "ACTIVE":
                footprint.update(
                    (cap, leaf)
                    for cap in agent["autonomous_capabilities"]
                    for leaf in leaves(self.w, agent["scope_path"])
                )
        for agent_id, outcome in self.ledger_agents.items():
            agent = self.enterprise_agents.get(agent_id)
            if (
                agent is None
                or outcome["p1_agent_action"] != "REGISTER_ACTIVE"
                or outcome["resolved_sponsor_person_id"] != person_id
            ):
                continue
            bindings = {b["binding_id"]: b for b in agent["tool_bindings"]}
            for binding_id in outcome["active_autonomous_binding_ids"]:
                binding = bindings.get(binding_id)
                if binding is not None:
                    footprint.update(
                        (cap, leaf)
                        for cap in binding["capabilities"]
                        for leaf in leaves(self.w, binding["scope_path"])
                    )
        return frozenset(footprint)

    def _admin_scope(self, account_id: str | None, app_id: str) -> frozenset[str]:
        if not self._active(account_id):
            return frozenset()
        leaves_out: set[str] = set()
        for gid in self.standing_work[account_id]:
            grant = self.grants.get(gid)
            role = None if grant is None else self.roles.get(grant["role_id"])
            if (
                role is None
                or role["app_id"] != app_id
                or role["privilege_tier"] not in {"admin", "critical"}
            ):
                continue
            if effective_grant_footprint(self.w, account_id, grant):
                leaves_out.update(leaves(self.w, grant["scope_path"]))
        denied = self._deny(account_id)
        for group in group_closure(self.w, account_id):
            if group["effect"] != "ALLOW":
                continue
            for role_id in group["granted_role_ids"]:
                role = self.roles.get(role_id)
                if (
                    role is None
                    or role["app_id"] != app_id
                    or role["privilege_tier"] not in {"admin", "critical"}
                ):
                    continue
                fp = role_footprint(self.w, role_id, group["scope_path"]) - denied
                if fp:
                    leaves_out.update(leaves(self.w, group["scope_path"]))
        return frozenset(leaves_out)

    def _deny(self, account_id: str) -> Footprint:
        from .policy import deny_footprint

        return deny_footprint(self.w, account_id)

    def _covers_campaign(self, account_id: str, grant_id: str, campaign: dict[str, Any]) -> bool:
        grant = self.grants.get(grant_id)
        role = None if grant is None else self.roles.get(grant["role_id"])
        admin_role = self.roles.get(campaign["admin_role_id"])
        if grant is None or role is None or admin_role is None:
            return False
        if role["app_id"] != campaign["app_id"] or role["privilege_tier"] not in {
            "admin",
            "critical",
        }:
            return False
        required = role_footprint(self.w, campaign["admin_role_id"], campaign["scope_path"])
        return required <= effective_grant_footprint(self.w, account_id, grant)

    def _item_group_lock(self, item: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        grant = self.grants.get(item["grant_id"])
        if grant is None or grant["grant_mode"] != "standing" or item["used_within_lookback"]:
            return ()
        role_fp = role_footprint(self.w, grant["role_id"], grant["scope_path"])
        denied = self._deny(item["account_id"])
        effective = role_fp - denied
        locks = []
        for group in group_closure(self.w, item["account_id"]):
            if group["effect"] != "ALLOW":
                continue
            raw = {
                (cap, leaf)
                for cap in raw_group_capabilities(self.w, group)
                for leaf in leaves(self.w, group["scope_path"])
            }
            if (effective & raw) - denied:
                locks.append(group)
        return tuple(locks)

    def _derive_uar(self) -> None:
        self.uar_mutations = {}
        for campaign_id in sorted(self._queue("target_uar_campaign_ids")):
            campaign = self.campaigns.get(campaign_id)
            if campaign is None:
                raise PublicInputError(
                    "UNMATERIALIZABLE_INPUT",
                    "/public_episode/phase2_slices",
                    f"target UAR campaign missing from delivered view: {campaign_id}",
                )
            items = sorted(campaign["items"], key=lambda item: item["item_id"])
            all_item_grants = {item["grant_id"] for item in items}
            survivors: list[dict[str, Any]] = []
            inactive_items: list[dict[str, Any]] = []
            stripped_items: list[tuple[dict[str, Any], tuple[dict[str, Any], ...]]] = []
            for item in items:
                account_id = item["account_id"]
                grant_id = item["grant_id"]
                if not self._active(account_id):
                    if account_id in self.inactive_due_engagement:
                        inactive_items.append(item)
                    tainted = self.postshock_stripped.get((account_id, grant_id), ())
                    if tainted:
                        stripped_items.append((item, tainted))
                    continue
                if grant_id not in self.standing_work[account_id] | self.eligible_work[account_id]:
                    tainted = self.postshock_stripped.get((account_id, grant_id), ())
                    if tainted:
                        stripped_items.append((item, tainted))
                    continue
                survivors.append(item)
            overrides: list[tuple[str, str]] = []
            decisions: dict[str, str] = {
                item["item_id"]: "REVOKE" for item in items if item not in survivors
            }
            assigned_or_override: dict[str, str | None] = {}
            reviewable: list[dict[str, Any]] = []
            for item in survivors:
                person_id = self._bound_person(item["account_id"])
                assigned = item["assigned_reviewer_account_id"]
                if self._valid_reviewer(assigned, person_id):
                    reviewer = assigned
                else:
                    reviewer = min(
                        (
                            aid
                            for aid in campaign["backup_reviewer_account_ids"]
                            if aid in self.accounts and self._valid_reviewer(aid, person_id)
                        ),
                        default=None,
                    )
                    if reviewer is not None:
                        overrides.append((item["item_id"], reviewer))
                assigned_or_override[item["item_id"]] = reviewer
                if reviewer is None:
                    decisions[item["item_id"]] = "ESCALATE"
                else:
                    reviewable.append(item)
            locked: dict[str, tuple[dict[str, Any], ...]] = {}
            for item in reviewable:
                group_locks = self._item_group_lock(item)
                if group_locks:
                    locked[item["item_id"]] = group_locks
                    decisions[item["item_id"]] = "ESCALATE"
            reviewable_unlocked = [item for item in reviewable if item["item_id"] not in locked]
            valid_reviewers: dict[str, list[str]] = {}
            for item in items:
                item_id = item["item_id"]
                if item not in survivors:
                    valid_reviewers[item_id] = []
                    continue
                reviewed_person = self._bound_person(item["account_id"])
                assigned = item["assigned_reviewer_account_id"]
                if self._valid_reviewer(assigned, reviewed_person):
                    valid_reviewers[item_id] = [assigned]
                else:
                    valid_reviewers[item_id] = sorted(
                        aid
                        for aid in campaign["backup_reviewer_account_ids"]
                        if aid in self.accounts and self._valid_reviewer(aid, reviewed_person)
                    )
            self.uar_stage_context[campaign_id] = {
                "survivor_ids": {item["item_id"] for item in survivors},
                "locked_ids": set(locked),
                "valid_reviewers": valid_reviewers,
                "reviewable_ids": {item["item_id"] for item in reviewable},
            }
            unreviewed_standing: set[str] = set()
            unreviewed_eligible: set[str] = set()
            for account_id in self.active_accounts:
                for gid in self.standing_work[account_id] - all_item_grants:
                    if self._covers_campaign(account_id, gid, campaign):
                        unreviewed_standing.add(self._bound_person(account_id))
                for gid in self.eligible_work[account_id] - all_item_grants:
                    if self._covers_campaign(account_id, gid, campaign):
                        unreviewed_eligible.add(self._bound_person(account_id))
            preserve_floor = (
                self.w.overlay["uar_preserve_standing_floor_default"]
                if campaign["preserve_standing_floor_override"] == "USE_OVERLAY_DEFAULT"
                else campaign["preserve_standing_floor_override"] == "TRUE"
            )
            standing_floor = campaign["m_standing"] if preserve_floor else 0
            standing_people = {p for p in unreviewed_standing if p is not None}
            standing_items = [
                item
                for item in reviewable_unlocked
                if self.grants[item["grant_id"]]["grant_mode"] == "standing"
            ]
            for item in standing_items:
                gid = item["grant_id"]
                grant = self.grants[gid]
                role = self.roles[grant["role_id"]]
                if item["used_within_lookback"]:
                    decisions[item["item_id"]] = "CERTIFY"
                    if self._covers_campaign(item["account_id"], gid, campaign):
                        person_id = self._bound_person(item["account_id"])
                        if person_id is not None:
                            standing_people.add(person_id)
                else:
                    decisions[item["item_id"]] = (
                        "CONVERT_TO_JIT" if role["is_jit_capable_target"] else "REVOKE"
                    )
            if len(standing_people) < standing_floor:
                for item in standing_items:
                    if item["used_within_lookback"] or not self._covers_campaign(
                        item["account_id"], item["grant_id"], campaign
                    ):
                        continue
                    person_id = self._bound_person(item["account_id"])
                    if person_id is not None and person_id not in standing_people:
                        decisions[item["item_id"]] = "CERTIFY"
                        standing_people.add(person_id)
                        if len(standing_people) >= standing_floor:
                            break
            emergency: str | None = None
            if len(standing_people) < standing_floor:
                required = role_footprint(self.w, campaign["admin_role_id"], campaign["scope_path"])
                candidates = []
                for aid in campaign["candidate_custodian_account_ids"]:
                    if not self._active(aid):
                        continue
                    person_id = self._bound_person(aid)
                    if person_id is None or person_id in standing_people:
                        continue
                    if not required <= (required - self._deny(aid)):
                        continue
                    if scoped_sod_witnesses(self.w, required, self._k_work(person_id)):
                        continue
                    if self.w.overlay["enforce_cross_nhi_owner_sod"] and scoped_sod_witnesses(
                        self.w, required, self._person_nhi_fp(person_id)
                    ):
                        continue
                    if self.w.overlay["enforce_sponsor_agent_sod"] and scoped_sod_witnesses(
                        self.w, required, self._person_sponsor_auto_fp(person_id)
                    ):
                        continue
                    candidates.append(aid)
                emergency = min(candidates, default=None)
                if emergency is not None:
                    person_id = self._bound_person(emergency)
                    if person_id is not None:
                        standing_people.add(person_id)

            eligible_people = {p for p in unreviewed_eligible if p is not None}
            eligible_people.update(
                self._bound_person(item["account_id"])
                for item in standing_items
                if decisions.get(item["item_id"]) == "CONVERT_TO_JIT"
                and self._covers_campaign(item["account_id"], item["grant_id"], campaign)
                and self._bound_person(item["account_id"]) is not None
            )
            eligible_items = [
                item
                for item in reviewable_unlocked
                if self.grants[item["grant_id"]]["grant_mode"] == "eligible"
            ]
            for item in eligible_items:
                gid = item["grant_id"]
                if item["used_within_lookback"]:
                    decisions[item["item_id"]] = "CERTIFY"
                    if self._covers_campaign(item["account_id"], gid, campaign):
                        person_id = self._bound_person(item["account_id"])
                        if person_id is not None:
                            eligible_people.add(person_id)
                else:
                    decisions[item["item_id"]] = "REVOKE"
            if len(eligible_people) < campaign["m_eligible"]:
                for item in eligible_items:
                    if item["used_within_lookback"] or not self._covers_campaign(
                        item["account_id"], item["grant_id"], campaign
                    ):
                        continue
                    person_id = self._bound_person(item["account_id"])
                    if person_id is not None and person_id not in eligible_people:
                        decisions[item["item_id"]] = "CERTIFY"
                        eligible_people.add(person_id)
                        if len(eligible_people) >= campaign["m_eligible"]:
                            break
            # Apply decisions immediately for the next campaign and P2.3.
            for item in items:
                decision = decisions.get(item["item_id"], "ESCALATE")
                aid, gid = item["account_id"], item["grant_id"]
                if decision in {"REVOKE", "ESCALATE"}:
                    self.standing_work[aid].discard(gid)
                    self.eligible_work[aid].discard(gid)
                elif decision == "CONVERT_TO_JIT":
                    self.standing_work[aid].discard(gid)
                    self.eligible_work[aid].add(gid)
                    if gid in self.grants:
                        self.grants[gid] = {
                            **self.grants[gid],
                            "grant_mode": "eligible",
                        }
            if emergency is not None:
                custodian_grant_id = f"cust__{campaign_id}__{emergency}"
                synthetic = {
                    "grant_id": custodian_grant_id,
                    "account_id": emergency,
                    "role_id": campaign["admin_role_id"],
                    "grant_mode": "standing",
                    "scope_path": campaign["scope_path"],
                    "sod_prune_priority": 1,
                    "evidence_id": campaign["evidence_id"],
                }
                self.grants[custodian_grant_id] = synthetic
                self.standing_work[emergency].add(custodian_grant_id)
            citations = {f"UAR-V5-01:{campaign['evidence_id']}"}
            citations.update(f"UAR-V5-01:{item['evidence_id']}" for item in items)
            match_ids: list[str | None] = [campaign_id]
            for item in inactive_items:
                person_id = self._person_from_ledger(item["account_id"])
                if person_id in self.hr:
                    citations.add(f"UAR-V5-01:{self.hr[person_id]['evidence_id']}")
                    match_ids.append(person_id)
            for item, tainted_bridges in stripped_items:
                person_id = self._person_from_ledger(item["account_id"])
                if person_id in self.hr:
                    citations.add(f"UAR-V5-01:{self.hr[person_id]['evidence_id']}")
                    match_ids.append(person_id)
                for bridge in tainted_bridges:
                    citations.add(f"UAR-V5-01:{bridge['evidence_id']}")
                    match_ids.append(bridge["bridge_id"])
            for _, account_id in overrides:
                account = self.accounts.get(account_id)
                if account is not None:
                    citations.add(f"UAR-V5-01:{account['evidence_id']}")
                    match_ids.append(account_id)
                person_id = self._bound_person(account_id)
                if person_id in self.hr:
                    citations.add(f"UAR-V5-01:{self.hr[person_id]['evidence_id']}")
            for groups in locked.values():
                citations.update(f"UAR-V5-01:{group['evidence_id']}" for group in groups)
            if emergency is not None:
                account = self.accounts[emergency]
                person_id = self._bound_person(emergency)
                citations.add(f"UAR-V5-01:{account['evidence_id']}")
                match_ids.append(emergency)
                if person_id in self.hr:
                    citations.add(f"UAR-V5-01:{self.hr[person_id]['evidence_id']}")
                    match_ids.append(person_id)
                role = self.roles.get(campaign["admin_role_id"])
                if role is not None:
                    citations.add(f"UAR-V5-01:{role['evidence_id']}")
            self.uar_mutations[campaign_id] = _mutation(
                "UAR_CAMPAIGN_MUTATION",
                campaign_id,
                precond(self.w, match_ids),
                tuple(sorted(citations)),
                uar_decisions=[
                    [item["item_id"], decisions.get(item["item_id"], "ESCALATE")] for item in items
                ],
                reviewer_overrides=[
                    [item_id, account_id] for item_id, account_id in sorted(overrides)
                ],
                emergency_custodian_account_id=emergency,
            )

    def _valid_reviewer(
        self, reviewer_account_id: str | None, reviewed_person_id: str | None
    ) -> bool:
        if (
            reviewer_account_id is None
            or reviewer_account_id not in self.accounts
            or not self._active(reviewer_account_id)
        ):
            return False
        reviewer_person = self._bound_person(reviewer_account_id)
        return bool(
            reviewer_person is not None
            and reviewer_person != reviewed_person_id
            and not subordinate(self.w, reviewer_person, reviewed_person_id, self.w.clock)
        )
