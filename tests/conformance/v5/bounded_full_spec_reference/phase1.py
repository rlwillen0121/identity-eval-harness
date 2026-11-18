"""Independent P1 derivation from the delivered P1 view and normative §§1–3."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

from identitybench.v5.scope_algebra import (
    capability_footprint,
    effective_grant_footprint,
    group_closure,
    group_footprints,
    has_live_engagement,
    is_subordinate,
    sod_witnesses,
    syntactically_mitigates,
    unmitigated_witnesses,
)

from .common import PhaseContext, live_engagement, person_for_account, sorted_unique


def _ver(ctx: PhaseContext, ids: Any) -> tuple[str, ...]:
    if isinstance(ids, str):
        ids = (ids,)
    return sorted_unique(
        tok
        for entity_id in ids
        if entity_id is not None
        for tok in (ctx.live_version(entity_id),)
        if tok is not None
    )


def _cite(ctx: PhaseContext, rule: str, ids: Any) -> tuple[str, ...]:
    if isinstance(ids, str):
        ids = (ids,)
    out = set()
    for entity_id in ids:
        if entity_id is None:
            continue
        evidence_id = ctx.view.evidence_for(entity_id)
        if evidence_id is not None:
            out.add(f"{rule}:{evidence_id}")
    return tuple(sorted(out))


def _evidence(rule: str, evidence_ids: Any) -> tuple[str, ...]:
    if isinstance(evidence_ids, str):
        evidence_ids = (evidence_ids,)
    return tuple(sorted({f"{rule}:{value}" for value in evidence_ids if value is not None}))


def _person_engagement(person: Any, engagement_id: str | None) -> Any | None:
    if person is None or engagement_id is None:
        return None
    return person.engagements_by_id.get(engagement_id)


def _pbind_ids(account: Any, people: dict[str, Any], clock: int) -> tuple[str, ...]:
    if account.immutable_person_ref is not None:
        ids = {p.person_id for p in people.values() if p.person_id == account.immutable_person_ref}
        ids.update(
            p.person_id
            for p in people.values()
            if p.primary_email == account.login_email and has_live_engagement(p, clock)
        )
        return tuple(sorted(ids))
    return tuple(
        sorted(
            p.person_id
            for p in people.values()
            if account.login_email == p.primary_email or account.login_email in p.historical_emails
        )
    )


def _bind_account(ctx: PhaseContext, account: Any) -> dict[str, Any]:
    v = ctx.phase1_view
    people = v.rows("hr_persons")
    roles = v.rows("roles")
    grants = v.rows("grants")
    bridges = v.rows("transfer_bridges")
    pbind = _pbind_ids(account, people, ctx.clock)
    base = {
        "account_action": "ESCALATE_BINDING_CONFLICT",
        "bound_person_id": None,
        "bound_engagement_id": None,
        "retained_grant_ids": tuple(sorted(account.direct_grant_ids)),
        "pre_person_ids": pbind,
        "pre_action": "ESCALATE_BINDING_CONFLICT",
        "pre_engagement": None,
        "converted_pruned": set(),
        "mover_pruned": set(),
        "mover_survivors": set(),
        "bridge_by_pruned": defaultdict(set),
    }
    person = None
    if account.immutable_person_ref is not None:
        person = people.get(account.immutable_person_ref)
        cross_person_email = any(
            p.person_id != account.immutable_person_ref
            and p.primary_email == account.login_email
            and has_live_engagement(p, ctx.clock)
            for p in people.values()
        )
        if person is None or cross_person_email:
            return base
    else:
        if len(pbind) != 1:
            return base
        person = people[pbind[0]]
    pinned = _person_engagement(person, account.bound_engagement_ref)
    if pinned is None:
        live = [e for e in person.engagements if live_engagement(e, ctx.clock)]
        if len(live) > 1:
            return base
        if len(live) == 1:
            pinned = live[0]
        else:
            pinned = max(person.engagements, key=lambda e: (e.start_s, e.eng_id))
    if not live_engagement(pinned, ctx.clock):
        base.update(
            account_action="DISABLE_ACCOUNT",
            bound_person_id=person.person_id,
            bound_engagement_id=pinned.eng_id,
            retained_grant_ids=(),
            pre_action="DISABLE_ACCOUNT",
            pre_engagement=pinned,
        )
        return base
    keep = set(account.direct_grant_ids)
    if (
        pinned.eng_status == "CONTRACTOR_TO_FTE_PENDING"
        and pinned.conversion_start_s is not None
        and pinned.conversion_grace_deadline_s is not None
        and pinned.conversion_start_s <= ctx.clock < pinned.conversion_grace_deadline_s
    ):
        for gid in tuple(keep):
            grant = grants.get(gid)
            role = None if grant is None else roles.get(grant.role_id)
            if (
                grant is not None
                and role is not None
                and grant.grant_mode == "standing"
                and role.privilege_tier in {"admin", "critical"}
            ):
                keep.remove(gid)
                base["converted_pruned"].add(gid)
    dept = (
        pinned.prior_dept_path
        if (
            pinned.dept_transfer_effective_at_s is not None
            and pinned.prior_dept_path is not None
            and ctx.clock < pinned.dept_transfer_effective_at_s
        )
        else pinned.dept_path
    )
    for gid in tuple(keep):
        grant = grants.get(gid)
        role = None if grant is None else roles.get(grant.role_id)
        if grant is None or role is None:
            continue
        if grant.scope_path == "/*" or ctx.scope.leaves(grant.scope_path) <= ctx.scope.leaves(dept):
            continue
        matching = [
            bridge
            for bridge in bridges.values()
            if bridge.account_id == account.account_id
            and bridge.grant_id == gid
            and pinned.prior_dept_path is not None
            and ctx.scope.leaves(grant.scope_path) <= ctx.scope.leaves(bridge.from_dept_path)
            and bridge.from_dept_path == pinned.prior_dept_path
            and bridge.to_dept_path == dept
            and bridge.target_resource == role.app_id
            and bridge.approved_at_s <= ctx.clock < bridge.expires_at_s
        ]
        if not matching:
            keep.remove(gid)
            base["mover_pruned"].add(gid)
        else:
            for bridge in matching:
                base["bridge_by_pruned"][gid].add(bridge.bridge_id)
    base.update(
        account_action="KEEP_ACTIVE",
        bound_person_id=person.person_id,
        bound_engagement_id=pinned.eng_id,
        retained_grant_ids=tuple(sorted(keep)),
        mover_survivors=set(keep),
        pre_action="KEEP_ACTIVE",
        pre_engagement=pinned,
    )
    return base


def _account_group_footprint(
    ctx: PhaseContext, account: Any
) -> tuple[frozenset[Any], frozenset[Any]]:
    v = ctx.phase1_view
    return group_footprints(account, v.rows("groups"), v.rows("roles"), ctx.policy.scope_catalog)


def _grant_footprint(ctx: PhaseContext, account: Any, grant_id: str) -> frozenset[Any]:
    v = ctx.phase1_view
    grant = v.get("grants", grant_id)
    if grant is None:
        return frozenset()
    _, deny = _account_group_footprint(ctx, account)
    return effective_grant_footprint(grant, v.rows("roles"), deny, ctx.policy.scope_catalog)


def _all_group_footprints(ctx: PhaseContext, account: Any) -> tuple[frozenset[Any], frozenset[Any]]:
    return _account_group_footprint(ctx, account)


@dataclass
class P1State:
    ctx: PhaseContext
    connector: dict[str, dict[str, Any]] = field(default_factory=dict)
    hr: dict[str, dict[str, Any]] = field(default_factory=dict)
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    live_controls: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    kept_by_account: dict[str, set[str]] = field(default_factory=dict)
    kstanding_by_account: dict[str, frozenset[Any]] = field(default_factory=dict)
    kstanding_by_person: dict[str, frozenset[Any]] = field(default_factory=dict)
    fnhi_by_person: dict[str, frozenset[Any]] = field(default_factory=dict)
    kspons_by_person: dict[str, frozenset[Any]] = field(default_factory=dict)
    nhi_actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    agent_actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    sod_before: dict[str, dict[tuple[str, str], frozenset[Any]]] = field(default_factory=dict)
    d1_pairs: dict[str, set[tuple[str, str]]] = field(default_factory=dict)
    kept_pairs_order: dict[str, list[tuple[str, str]]] = field(default_factory=dict)


def _safe_halt_ids(conn: Any) -> tuple[str, ...]:
    if conn.sync_mode == "DELTA_CURSOR" or conn.transport_kind == "FLAT_FILE_SFTP":
        return tuple(sorted(conn.baseline_reconciled_account_ids))
    return ()


def _validate_connector_row(conn: Any, raw_row: Any) -> dict[str, Any] | None:
    if not isinstance(raw_row, (tuple, list)):
        return None
    if any(
        not isinstance(pair, (tuple, list)) or len(pair) != 2 or not isinstance(pair[0], str)
        for pair in raw_row
    ):
        return None
    keys = [pair[0] for pair in raw_row]
    if len(keys) != len(set(keys)):
        return None
    record = dict(raw_row)
    if any(
        not isinstance(record.get(field), str) or not record[field].strip()
        for field in conn.required_fields
    ):
        return None
    aid = record.get("account_id")
    if not isinstance(aid, str) or re.fullmatch(r"^acct_[a-z0-9_]+$", aid) is None:
        return None
    status = record.get("status")
    if not isinstance(status, str) or status not in {"ACTIVE", "DISABLED", "TOMBSTONE"}:
        return None
    raw_entitlements = record.get("raw_entitlements", ())
    if not isinstance(raw_entitlements, (tuple, list)) or any(
        not isinstance(ent, str) or re.fullmatch(r"^[a-zA-Z0-9_./:-]{1,64}$", ent) is None
        for ent in raw_entitlements
    ):
        return None
    version = record.get("version")
    if version is not None and (not isinstance(version, int) or isinstance(version, bool)):
        return None
    return {"account_id": aid, "status": status, "ents": set(raw_entitlements), "version": version}


def _sync_connector(ctx: PhaseContext, conn: Any) -> dict[str, Any]:
    v = ctx.phase1_view
    pages = v.connector_pages_for(conn.connector_id)
    safe_ids = _safe_halt_ids(conn)
    attempts_read: list[Any] = []

    def halt(reason: str) -> dict[str, Any]:
        return {
            "halted_fail_closed": True,
            "halt_reason": reason,
            "reconciled_account_ids": safe_ids,
            "remediated_spoke_drift": (),
            "itsm_ticket_actions": (),
            "inspected_pages": tuple(attempts_read),
        }

    if conn.auth_state != "AUTH_OK":
        return halt("HALT_AUTH_OR_TLS")
    if (
        conn.transport_kind == "JDBC_SQL"
        and conn.sync_mode == "DELTA_CURSOR"
        and not conn.has_delete_audit_view
    ):
        return halt("HALT_SCHEMA_OR_TRAILER")
    cursor = "cursor_0"
    visited: set[str] = set()
    total_rows = 0
    row_state: dict[str, dict[str, Any]] = {}
    while True:
        if cursor in visited:
            return halt("HALT_CURSOR_OR_HTTP")
        visited.add(cursor)
        choices = sorted(
            (page for page in pages if page.cursor_in == cursor),
            key=lambda page: page.attempt_index,
        )
        chosen = None
        for page in choices:
            attempts_read.append(page)
            if page.http_status == 429:
                continue
            if page.http_status == 500:
                return halt("HALT_CURSOR_OR_HTTP")
            if page.http_status == 200:
                chosen = page
                break
        if chosen is None:
            return halt("HALT_CURSOR_OR_HTTP")
        if (
            cursor == "cursor_0"
            and conn.sync_mode == "DELTA_CURSOR"
            and chosen.base_checkpoint_etag != conn.local_checkpoint_etag
        ):
            return halt("HALT_CHECKPOINT_DRIFT")
        for raw_row in chosen.records:
            total_rows += 1
            record = _validate_connector_row(conn, raw_row)
            if record is None:
                return halt("HALT_SCHEMA_OR_TRAILER")
            aid = record["account_id"]
            if conn.transport_kind == "REST_WEB_SERVICE":
                old = row_state.get(aid)
                if old is None:
                    row_state[aid] = record
                elif (record["version"] is None) != (old["version"] is None):
                    return halt("HALT_SCHEMA_OR_TRAILER")
                elif record["version"] is None or record["version"] >= old["version"]:
                    row_state[aid] = record
            elif conn.transport_kind == "JDBC_SQL":
                old = row_state.get(aid)
                if old is None:
                    row_state[aid] = record
                elif (record["version"] is None) != (old["version"] is None) or (
                    record["version"] is not None and record["version"] < old["version"]
                ):
                    return halt("HALT_SCHEMA_OR_TRAILER")
                elif record["version"] is not None and record["version"] > old["version"]:
                    row_state[aid] = record
                else:
                    if record["status"] != old["status"]:
                        return halt("HALT_SCHEMA_OR_TRAILER")
                    old["ents"].update(record["ents"])
            else:
                if aid in row_state:
                    return halt("HALT_SCHEMA_OR_TRAILER")
                row_state[aid] = record
        if chosen.next_cursor is None:
            return halt("HALT_CURSOR_OR_HTTP")
        if chosen.next_cursor == "cursor_end":
            break
        cursor = chosen.next_cursor
    if conn.transport_kind == "FLAT_FILE_SFTP" and total_rows != conn.expected_file_row_count:
        return halt("HALT_SCHEMA_OR_TRAILER")
    baseline = set(conn.baseline_reconciled_account_ids)
    if conn.sync_mode == "FULL_SNAPSHOT":
        candidate = {aid for aid, row in row_state.items() if row["status"] != "TOMBSTONE"}
    else:
        candidate = baseline - {
            aid for aid, row in row_state.items() if row["status"] == "TOMBSTONE"
        }
        candidate |= {aid for aid, row in row_state.items() if row["status"] != "TOMBSTONE"}
    if (
        conn.transport_kind == "FLAT_FILE_SFTP"
        and conn.sync_mode == "FULL_SNAPSHOT"
        and baseline
        and len(baseline - candidate) * 10000 > conn.max_delete_threshold_bps * len(baseline)
    ):
        return halt("HALT_MASS_DELETE_GUARD")
    policy = (
        ctx.overlay.connector_unmapped_policy_default
        if conn.unmapped_policy_override == "USE_OVERLAY_DEFAULT"
        else conn.unmapped_policy_override
    )
    mapping = dict(conn.mapping_rules)
    remediated: set[str] = set()
    accounts = v.rows("idp_accounts")
    roles = v.rows("roles")
    grants = v.rows("grants")
    for aid in sorted(candidate):
        row = row_state.get(aid)
        if row is None or row["status"] == "TOMBSTONE":
            continue
        for raw_ent in sorted(row["ents"]):
            if raw_ent not in mapping:
                if policy == "HALT_CONNECTOR":
                    return halt("HALT_UNMAPPED_STRICT")
                remediated.add(f"{aid}:unmapped:{raw_ent}")
                continue
            role_id = mapping[raw_ent]
            account = accounts.get(aid)
            authorized = False
            if account is not None and account.status == "ACTIVE":
                _allow_fp, deny_fp = group_footprints(
                    account, v.rows("groups"), roles, ctx.policy.scope_catalog
                )
                for gid in account.direct_grant_ids:
                    grant = grants.get(gid)
                    if grant is None or grant.role_id != role_id or grant.grant_mode != "standing":
                        continue
                    fp = effective_grant_footprint(grant, roles, deny_fp, ctx.policy.scope_catalog)
                    if fp:
                        authorized = True
                        break
                if not authorized:
                    closure = group_closure(account, v.rows("groups"))
                    role = roles.get(role_id)
                    if role is not None:
                        for group_id in closure:
                            group = v.get("groups", group_id)
                            if (
                                group is None
                                or group.effect != "ALLOW"
                                or role_id not in group.granted_role_ids
                            ):
                                continue
                            raw = capability_footprint(
                                ctx.policy.scope_catalog, role.capabilities, (group.scope_path,)
                            )
                            if raw - deny_fp:
                                authorized = True
                                break
            if not authorized:
                remediated.add(f"{aid}:{role_id}")
    return {
        "halted_fail_closed": False,
        "halt_reason": "NONE",
        "reconciled_account_ids": tuple(sorted(candidate)),
        "remediated_spoke_drift": tuple(sorted(remediated)),
        "itsm_ticket_actions": (),
        "inspected_pages": tuple(attempts_read),
    }


def _audit_sink_healthy(ctx: PhaseContext, connector_id: str) -> bool:
    conn = ctx.phase1_view.get("connector_schemas", connector_id)
    # All policy-referenced connectors are in the governed P1 queue. If not, the
    # public packet does not expose a computed sync result and health is unknown.
    result = getattr(ctx, "_p1_state", None)
    outcome = None if result is None else result.connector.get(connector_id)
    return bool(
        conn is not None
        and outcome is not None
        and not outcome["halted_fail_closed"]
        and 0 <= ctx.clock - conn.audit_watermark_s <= ctx.overlay.freshness_sla_s
    )


def _derive_hr_mapping(ctx: PhaseContext, task: Any, state: P1State) -> dict[str, Any]:
    v = ctx.phase1_view
    people = v.rows("hr_persons")
    emp_index = {p.emp_immutable_id: p.person_id for p in people.values()}
    accepted: list[Any] = []
    rejected: list[Any] = []
    for feed in task.feeds:
        healthy = _audit_sink_healthy(ctx, feed.connector_id)
        if (
            feed.source_kind != "TARGET_APP_SPOKE"
            and healthy
            and not (
                ctx.overlay.require_immutable_hr_correlation
                and feed.correlation_strategy == "MUTABLE_EMAIL_ONLY"
            )
        ):
            accepted.append(feed)
        else:
            rejected.append(feed)
    row_results: list[tuple[Any, Any]] = []
    row_invalid: set[str] = set()
    for feed in accepted:
        status_map = dict(feed.status_code_map)
        for assertion in feed.assertions:
            key = f"{feed.feed_id}:{assertion.person_id}"
            valid = (
                assertion.person_id in people
                and assertion.affiliation in feed.authorized_affiliations
                and assertion.raw_status_code in status_map
            )
            if valid and assertion.emp_immutable_id is not None:
                valid = (
                    bool(assertion.emp_immutable_id.strip())
                    and feed.correlation_strategy == "IMMUTABLE_EMP_ID_THEN_EMAIL"
                    and emp_index.get(assertion.emp_immutable_id) == assertion.person_id
                )
            elif valid:
                valid = (
                    not ctx.overlay.require_immutable_hr_correlation
                    and assertion.email == people[assertion.person_id].primary_email
                    and not any(
                        p.person_id != assertion.person_id
                        and assertion.email in ({p.primary_email} | set(p.historical_emails))
                        for p in people.values()
                    )
                )
            if not valid:
                row_invalid.add(key)
            else:
                row_results.append((feed, assertion))
    collision_keys: set[str] = set()
    for (feed_a, a), (feed_b, b) in combinations(row_results, 2):
        if a.person_id != b.person_id and (
            a.email == b.email
            or (a.emp_immutable_id is not None and a.emp_immutable_id == b.emp_immutable_id)
        ):
            collision_keys.add(f"{feed_a.feed_id}:{a.person_id}")
            collision_keys.add(f"{feed_b.feed_id}:{b.person_id}")
    quarantine = row_invalid | collision_keys
    winners: dict[str, tuple[Any, Any]] = {}
    for feed, assertion in row_results:
        key = f"{feed.feed_id}:{assertion.person_id}"
        if key in quarantine:
            continue
        prior = winners.get(assertion.person_id)
        if prior is None or (feed.precedence_rank, feed.feed_id) < (
            prior[0].precedence_rank,
            prior[0].feed_id,
        ):
            winners[assertion.person_id] = (feed, assertion)
    statuses = tuple(
        sorted(
            (person_id, dict(feed.status_code_map)[assertion.raw_status_code])
            for person_id, (feed, assertion) in winners.items()
        )
    )
    consulted: set[str] = set()
    for feed in accepted:
        for assertion in feed.assertions:
            if assertion.person_id in people:
                consulted.add(assertion.person_id)
            if assertion.emp_immutable_id is not None:
                consulted.update(
                    p.person_id
                    for p in people.values()
                    if p.emp_immutable_id == assertion.emp_immutable_id
                )
            if assertion.email:
                consulted.update(
                    p.person_id
                    for p in people.values()
                    if assertion.email == p.primary_email or assertion.email in p.historical_emails
                )
    state.hr[task.mapping_task_id] = {
        "accepted": tuple(sorted(feed.feed_id for feed in accepted)),
        "rejected": tuple(sorted(feed.feed_id for feed in rejected)),
        "resolved": statuses,
        "quarantine": tuple(sorted(quarantine)),
        "consulted": tuple(sorted(consulted)),
        "feeds": tuple(task.feeds),
    }
    return state.hr[task.mapping_task_id]


def _classify_base_accounts(ctx: PhaseContext, state: P1State) -> None:
    accounts = ctx.phase1_view.rows("idp_accounts")
    for account_id in ctx.q.get("target_human_account_ids", ()):
        account = accounts.get(account_id)
        if account is not None:
            state.accounts[account_id] = _bind_account(ctx, account)


def _person_for_pre_account(state: P1State, account_id: str) -> str | None:
    return state.accounts.get(account_id, {}).get("bound_person_id")


def _active_kept_account_ids(state: P1State, person_id: str) -> list[str]:
    return sorted(
        aid
        for aid, outcome in state.accounts.items()
        if outcome["pre_action"] == "KEEP_ACTIVE" and outcome["bound_person_id"] == person_id
    )


def _compute_sod_and_controls(ctx: PhaseContext, state: P1State) -> None:
    v = ctx.phase1_view
    people = v.rows("hr_persons")
    accounts = v.rows("idp_accounts")
    grants = v.rows("grants")
    controls = tuple(v.rows("compensating_controls").values())
    initial_states: dict[str, tuple[Any, frozenset[Any], frozenset[Any], set[str]]] = {}
    for account_id, outcome in state.accounts.items():
        if outcome["pre_action"] != "KEEP_ACTIVE":
            state.kept_by_account[account_id] = set()
            state.kstanding_by_account[account_id] = frozenset()
            continue
        account = accounts[account_id]
        fgrp, fdeny = _account_group_footprint(ctx, account)
        kept = set(outcome["retained_grant_ids"])
        initial_states[account_id] = (account, fgrp, fdeny, kept)
        state.kept_by_account[account_id] = set(kept)
    people_with_accounts = sorted(
        {
            o["bound_person_id"]
            for o in state.accounts.values()
            if o["pre_action"] == "KEEP_ACTIVE" and o["bound_person_id"] is not None
        }
    )
    visiting: set[str] = set()
    done: set[str] = set()

    def compute_person(person_id: str) -> None:
        if person_id in done:
            return
        if person_id in visiting:
            raise ValueError("monitor dependency cycle is not resolvable from public records")
        visiting.add(person_id)
        own_controls = [cc for cc in controls if cc.monitored_person_id == person_id]
        for cc in own_controls:
            monitor_person = _person_for_pre_account(state, cc.monitor_account_id)
            if monitor_person and monitor_person != person_id:
                compute_person(monitor_person)
        fgrp_by_account: dict[str, frozenset[Any]] = {}
        fdeny_by_account: dict[str, frozenset[Any]] = {}
        p_accounts = _active_kept_account_ids(state, person_id)
        for aid in p_accounts:
            if aid in initial_states:
                _, fgrp_by_account[aid], fdeny_by_account[aid], _ = initial_states[aid]
        kgrp = frozenset().union(*fgrp_by_account.values()) if fgrp_by_account else frozenset()
        live: list[Any] = []
        for cc in own_controls:
            if not _audit_sink_healthy(ctx, cc.audit_connector_id):
                continue
            mon_out = state.accounts.get(cc.monitor_account_id)
            monitor_person = _person_for_pre_account(state, cc.monitor_account_id)
            if mon_out is None or mon_out["pre_action"] != "KEEP_ACTIVE" or monitor_person is None:
                continue
            if monitor_person == person_id or is_subordinate(
                monitor_person, person_id, ctx.clock, people
            ):
                continue
            mon_fp = state.kstanding_by_account.get(cc.monitor_account_id, frozenset())
            need = capability_footprint(
                ctx.policy.scope_catalog,
                (cc.required_monitor_capability,),
                (cc.covered_scope_path,),
            )
            if not need <= mon_fp:
                continue
            live.append(cc)
        state.live_controls[person_id] = tuple(live)
        pairs: list[tuple[str, str]] = []
        for aid in p_accounts:
            for gid in state.accounts[aid]["retained_grant_ids"]:
                grant = grants.get(gid)
                if grant is None:
                    continue
                fp = effective_grant_footprint(
                    grant,
                    v.rows("roles"),
                    fdeny_by_account.get(aid, frozenset()),
                    ctx.policy.scope_catalog,
                )
                if not unmitigated_witnesses(
                    fp,
                    kgrp,
                    person_id,
                    live,
                    ctx.overlay.forbidden_sod_rules,
                    ctx.policy.scope_catalog,
                ):
                    pairs.append((aid, gid))
        pairs.sort(key=lambda pair: (grants[pair[1]].sod_prune_priority, pair[1], pair[0]))
        state.d1_pairs[person_id] = set(pairs)
        kept_total: set[Any] = set(kgrp)
        kept_ids_by_account: dict[str, set[str]] = {aid: set() for aid in p_accounts}
        kept_order: list[tuple[str, str]] = []
        before: dict[tuple[str, str], frozenset[Any]] = {}
        for aid, gid in pairs:
            grant = grants[gid]
            fp = effective_grant_footprint(
                grant,
                v.rows("roles"),
                fdeny_by_account.get(aid, frozenset()),
                ctx.policy.scope_catalog,
            )
            before[(aid, gid)] = frozenset(kept_total)
            if not unmitigated_witnesses(
                fp,
                frozenset(kept_total),
                person_id,
                live,
                ctx.overlay.forbidden_sod_rules,
                ctx.policy.scope_catalog,
            ):
                kept_ids_by_account[aid].add(gid)
                kept_order.append((aid, gid))
                kept_total.update(fp)
        state.sod_before[person_id] = before
        state.kept_pairs_order[person_id] = kept_order
        state.kstanding_by_person[person_id] = frozenset(kept_total)
        for aid in p_accounts:
            keep_ids = kept_ids_by_account.get(aid, set())
            state.kept_by_account[aid] = keep_ids
            _, fgrp, fdeny, _ = initial_states[aid]
            account_standing = set(fgrp)
            for gid in keep_ids:
                grant = grants[gid]
                if grant.grant_mode == "standing":
                    account_standing.update(
                        effective_grant_footprint(
                            grant, v.rows("roles"), fdeny, ctx.policy.scope_catalog
                        )
                    )
            state.kstanding_by_account[aid] = frozenset(account_standing)
        visiting.remove(person_id)
        done.add(person_id)

    for person_id in people_with_accounts:
        compute_person(person_id)
    # Keep the exact post-SoD grant partition on each account outcome.
    for aid, outcome in state.accounts.items():
        if outcome["pre_action"] == "KEEP_ACTIVE":
            outcome["retained_grant_ids"] = tuple(sorted(state.kept_by_account.get(aid, set())))


def cc_sensitive_grants(ctx: PhaseContext, state: P1State, account_id: str) -> set[str]:
    """Return grants whose retention changes when only live controls are used."""

    outcome = state.accounts.get(account_id)
    if outcome is None or outcome["pre_action"] != "KEEP_ACTIVE":
        return set()
    person_id = outcome["bound_person_id"]
    if person_id is None:
        return set()
    v = ctx.phase1_view
    accounts = v.rows("idp_accounts")
    grants = v.rows("grants")
    all_controls = tuple(v.rows("compensating_controls").values())
    active_account_ids = _active_kept_account_ids(state, person_id)
    group_parts = [
        _account_group_footprint(ctx, accounts[aid])[0]
        for aid in active_account_ids
        if aid in accounts
    ]
    kgrp = frozenset().union(*group_parts) if group_parts else frozenset()
    candidates: list[tuple[str, str]] = []
    footprints: dict[tuple[str, str], frozenset[Any]] = {}
    for aid in active_account_ids:
        account_outcome = state.accounts[aid]
        for grant_id in account_outcome.get("mover_survivors", ()):
            grant = grants.get(grant_id)
            account = accounts.get(aid)
            if grant is None or account is None:
                continue
            key = (aid, grant_id)
            fp = _grant_footprint(ctx, account, grant_id)
            if not unmitigated_witnesses(
                fp,
                kgrp,
                person_id,
                all_controls,
                ctx.overlay.forbidden_sod_rules,
                ctx.policy.scope_catalog,
            ):
                candidates.append(key)
                footprints[key] = fp
    candidates.sort(key=lambda pair: (grants[pair[1]].sod_prune_priority, pair[1], pair[0]))
    kept_counterfactual: dict[str, set[str]] = {aid: set() for aid in active_account_ids}
    kept_total: set[Any] = set(kgrp)
    for aid, grant_id in candidates:
        fp = footprints[(aid, grant_id)]
        if not unmitigated_witnesses(
            fp,
            frozenset(kept_total),
            person_id,
            all_controls,
            ctx.overlay.forbidden_sod_rules,
            ctx.policy.scope_catalog,
        ):
            kept_counterfactual[aid].add(grant_id)
            kept_total.update(fp)
    current = state.kept_by_account.get(account_id, set())
    counterfactual = kept_counterfactual.get(account_id, set())
    return current ^ counterfactual


def _preperson(ctx: PhaseContext, account_id: str | None) -> str | None:
    account = ctx.phase1_view.get("idp_accounts", account_id)
    return None if account is None else person_for_account(ctx, account_id, phase1=True)


def _fulfiller_qualifies(
    ctx: PhaseContext, state: P1State, account_id: str | None, subject_person_id: str | None
) -> bool:
    if account_id is None or account_id not in state.accounts:
        return False
    outcome = state.accounts[account_id]
    if outcome["pre_action"] != "KEEP_ACTIVE":
        return False
    bound_person = outcome["bound_person_id"]
    if bound_person is None or bound_person == subject_person_id:
        return False
    person = ctx.phase1_view.get("hr_persons", bound_person)
    engagement = _person_engagement(person, outcome["bound_engagement_id"])
    return engagement is not None and live_engagement(engagement, ctx.clock)


def _attach_itsm(ctx: PhaseContext, state: P1State) -> None:
    v = ctx.phase1_view
    for connector_id, result in state.connector.items():
        conn = v.get("connector_schemas", connector_id)
        if (
            conn is None
            or result["halted_fail_closed"]
            or conn.provisioning_mode == "AUTOMATED_WRITE"
        ):
            result["itsm_ticket_actions"] = ()
            result["consulted_ticket_fulfiller_account_ids"] = ()
            continue
        actions: list[dict[str, Any]] = []
        consulted_ticket_fulfillers: set[str] = set()
        for drift_token in result["remediated_spoke_drift"]:
            aid_subj = drift_token.split(":", 1)[0]
            if (
                aid_subj in state.accounts
                and state.accounts[aid_subj]["bound_person_id"] is not None
            ):
                subject = state.accounts[aid_subj]["bound_person_id"]
            else:
                subject = _preperson(ctx, aid_subj)
            primary_ok = _fulfiller_qualifies(
                ctx, state, conn.itsm_primary_fulfiller_account_id, subject
            )
            backup_ok = _fulfiller_qualifies(
                ctx, state, conn.itsm_backup_fulfiller_account_id, subject
            )
            best = (
                conn.itsm_primary_fulfiller_account_id
                if primary_ok
                else (conn.itsm_backup_fulfiller_account_id if backup_ok else None)
            )
            ticket = next((t for t in conn.open_itsm_tickets if t.drift_token == drift_token), None)
            if (
                ticket is not None
                and ticket.ticket_status == "CLOSED_COMPLETE"
                and conn.audit_watermark_s <= ticket.closed_at_s
            ):
                action, assignee = "AWAIT_NEXT_AGGREGATION", None
            elif not conn.itsm_bridge_healthy or best is None:
                action, assignee = "ESCALATE_ITSM_UNAVAILABLE", None
            elif ticket is None:
                action, assignee = "DISPATCH_NEW_TICKET", best
            elif ticket.ticket_status == "CLOSED_COMPLETE":
                action, assignee = "REOPEN_PHANTOM_CLOSURE", best
            elif ctx.clock >= ticket.sla_due_s:
                action, assignee = "ESCALATE_SLA_OR_COI", best
            else:
                assigned_to = ticket.assigned_to_account_id
                if assigned_to is not None:
                    consulted_ticket_fulfillers.add(assigned_to)
                if not _fulfiller_qualifies(ctx, state, assigned_to, subject):
                    action, assignee = "ESCALATE_SLA_OR_COI", best
                else:
                    action, assignee = "KEEP_PENDING_IN_SLA", assigned_to
            actions.append(
                {
                    "drift_token": drift_token,
                    "ticket_action": action,
                    "assignee_account_id": assignee,
                }
            )
        result["itsm_ticket_actions"] = tuple(actions)
        result["consulted_ticket_fulfiller_account_ids"] = tuple(
            sorted(consulted_ticket_fulfillers)
        )


def _fresh(ctx: PhaseContext, signal: Any) -> bool:
    age = ctx.clock - signal.synced_at_s
    return 0 <= age <= ctx.overlay.freshness_sla_s


def _raw_signal_person(ctx: PhaseContext, state: P1State, signal: Any) -> tuple[str | None, bool]:
    v = ctx.phase1_view
    source = signal.source
    if source in {"iac_state_attestation", "cicd_deploy_provenance", "cmdb_service_owner"}:
        pid = (
            signal.candidate_person_id
            if signal.candidate_person_id in v.rows("hr_persons")
            else None
        )
        return pid, pid is not None
    if source == "vault_rotator_principal":
        account_id = signal.rotator_account_id
        outcome = state.accounts.get(account_id or "")
        if (
            account_id in v.rows("idp_accounts")
            and outcome is not None
            and outcome["bound_person_id"] is not None
        ):
            return outcome["bound_person_id"], outcome["pre_action"] == "KEEP_ACTIVE"
        return _preperson(ctx, account_id), False
    if source == "parent_nhi_custody":
        parent = v.get("baseline_nhis", signal.parent_nhi_id)
        if parent is None or parent.owner_person_id not in v.rows("hr_persons"):
            return None, False
        rot_fresh = 0 <= ctx.clock - parent.last_rotated_at_s <= ctx.overlay.rotation_sla_s
        return parent.owner_person_id, parent.status == "ACTIVE" and rot_fresh
    return None, False


def _nhi_owner_eligibility(
    ctx: PhaseContext,
    state: P1State,
    target: Any,
    person_id: str,
    source_signal: Any,
    source_custody: bool,
) -> bool:
    v = ctx.phase1_view
    person = v.get("hr_persons", person_id)
    if person is None or not _fresh(ctx, source_signal) or not source_custody:
        return False
    eligible_engs = [
        e
        for e in person.engagements
        if live_engagement(e, ctx.clock)
        and (
            e.affiliation == "FTE"
            or (ctx.overlay.allow_contractor_nhi_owners and e.affiliation == "CONTRACTOR")
        )
        and not any(
            (
                e.prior_dept_path
                if e.dept_transfer_effective_at_s is not None
                and e.prior_dept_path is not None
                and ctx.clock < e.dept_transfer_effective_at_s
                else e.dept_path
            ).startswith(prefix)
            for prefix in ctx.overlay.owner_excluded_dept_prefixes
        )
    ]
    if not eligible_engs:
        return False
    target_fp = capability_footprint(
        ctx.policy.scope_catalog, target.capabilities, (target.scope_path,)
    )
    kstanding = state.kstanding_by_person.get(person_id, frozenset())
    if sod_witnesses(target_fp, kstanding, ctx.overlay.forbidden_sod_rules):
        return False
    if ctx.overlay.enforce_cross_nhi_owner_sod and sod_witnesses(
        target_fp, state.fnhi_by_person.get(person_id, frozenset()), ctx.overlay.forbidden_sod_rules
    ):
        return False
    prod_count = state.nhi_prod_count.get(person_id, 0) if hasattr(state, "nhi_prod_count") else 0
    return not (target.environment == "prod" and prod_count >= ctx.overlay.max_prod_nhis_per_owner)


def _derive_nhi(ctx: PhaseContext, state: P1State, target: Any) -> dict[str, Any]:
    v = ctx.phase1_view
    baseline = v.rows("baseline_nhis")
    target_fp = capability_footprint(
        ctx.policy.scope_catalog, target.capabilities, (target.scope_path,)
    )
    prod_counts: dict[str, int] = defaultdict(int)
    fnhi: dict[str, set[Any]] = defaultdict(set)
    for row in baseline.values():
        if row.status == "ACTIVE":
            fnhi[row.owner_person_id].update(
                capability_footprint(ctx.policy.scope_catalog, row.capabilities, (row.scope_path,))
            )
            if row.environment == "prod":
                prod_counts[row.owner_person_id] += 1
    for target_id in sorted(state.nhi_actions):
        prior = state.nhi_actions[target_id]
        owner = prior.get("resolved_owner_person_id")
        nhi = v.get("target_nhis", target_id)
        if (
            nhi is not None
            and nhi.environment == "prod"
            and prior.get("nhi_action") == "ATTRIBUTE_OWNER"
            and owner
        ):
            prod_counts[owner] += 1
        if nhi is not None and prior.get("nhi_action") == "ATTRIBUTE_OWNER" and owner:
            fnhi[owner].update(
                capability_footprint(ctx.policy.scope_catalog, nhi.capabilities, (nhi.scope_path,))
            )
    state.fnhi_by_person = {p: frozenset(fp) for p, fp in fnhi.items()}
    state.nhi_prod_count = dict(prod_counts)
    signal_cache: list[tuple[Any, str | None, bool]] = []
    ranks = {
        "iac_state_attestation": 1,
        "vault_rotator_principal": 2,
        "parent_nhi_custody": 3,
        "cicd_deploy_provenance": 4,
        "cmdb_service_owner": 5,
    }
    for signal in target.signals:
        raw_person, custody = _raw_signal_person(ctx, state, signal)
        signal_cache.append((signal, raw_person if custody else None, custody))
    if target.subkind == "shared_break_glass":
        custodians = [
            signal
            for signal, pid, custody in signal_cache
            if signal.source == "vault_rotator_principal"
            and _fresh(ctx, signal)
            and signal.rotator_account_id in v.rows("idp_accounts")
            and state.accounts.get(signal.rotator_account_id, {}).get("pre_action") == "KEEP_ACTIVE"
        ]
        if custodians:
            result = {
                "nhi_action": "FLAG_SHARED_EXCEPTION",
                "resolved_owner_person_id": None,
                "owner_resolution_source": None,
                "signals": tuple(target.signals),
            }
        else:
            result = {
                "nhi_action": "DISABLE_ORPHAN",
                "resolved_owner_person_id": None,
                "owner_resolution_source": None,
                "signals": tuple(target.signals),
            }
        state.nhi_actions[target.nhi_id] = result
        return result
    eligible_by_rank: dict[int, list[tuple[str, Any]]] = defaultdict(list)
    for signal, candidate_person, custody in signal_cache:
        rank = ranks.get(signal.source)
        if rank is None or candidate_person is None:
            continue
        if _nhi_owner_eligibility(ctx, state, target, candidate_person, signal, custody):
            eligible_by_rank[rank].append((candidate_person, signal))
    winner_rank = next((rank for rank in sorted(eligible_by_rank) if eligible_by_rank[rank]), None)
    if winner_rank is None:
        result = {
            "nhi_action": "DISABLE_ORPHAN",
            "resolved_owner_person_id": None,
            "owner_resolution_source": None,
            "signals": tuple(target.signals),
        }
    else:
        candidates = {pid for pid, _ in eligible_by_rank[winner_rank]}
        if len(candidates) != 1:
            result = {
                "nhi_action": "ESCALATE",
                "resolved_owner_person_id": None,
                "owner_resolution_source": None,
                "signals": tuple(target.signals),
                "eligible_rank_signals": tuple(
                    signal for _, signal in eligible_by_rank[winner_rank]
                ),
            }
        else:
            pid = next(iter(candidates))
            source = next(
                signal.source
                for person_id, signal in eligible_by_rank[winner_rank]
                if person_id == pid
            )
            result = {
                "nhi_action": "ATTRIBUTE_OWNER",
                "resolved_owner_person_id": pid,
                "owner_resolution_source": source,
                "signals": tuple(target.signals),
                "eligible_rank_signals": tuple(
                    signal
                    for person_id, signal in eligible_by_rank[winner_rank]
                    if person_id == pid
                ),
            }
    state.nhi_actions[target.nhi_id] = result
    if result["nhi_action"] == "ATTRIBUTE_OWNER" and result["resolved_owner_person_id"]:
        pid = result["resolved_owner_person_id"]
        fnhi.setdefault(pid, set()).update(target_fp)
        if target.environment == "prod":
            prod_counts[pid] = prod_counts.get(pid, 0) + 1
        state.fnhi_by_person = {p: frozenset(fp) for p, fp in fnhi.items()}
        state.nhi_prod_count = dict(prod_counts)
    return result


def _agent_sponsor_eligible(ctx: PhaseContext, state: P1State, person_id: str, target: Any) -> bool:
    v = ctx.phase1_view
    person = v.get("hr_persons", person_id)
    if person is None or not _active_kept_account_ids(state, person_id):
        return False
    live = [
        e
        for e in person.engagements
        if live_engagement(e, ctx.clock)
        and (
            e.affiliation == "FTE"
            or (ctx.overlay.allow_contractor_agent_sponsors and e.affiliation == "CONTRACTOR")
        )
        and not any(
            (
                e.prior_dept_path
                if e.dept_transfer_effective_at_s is not None
                and e.prior_dept_path is not None
                and ctx.clock < e.dept_transfer_effective_at_s
                else e.dept_path
            ).startswith(prefix)
            for prefix in ctx.overlay.owner_excluded_dept_prefixes
        )
    ]
    if not live:
        return False
    baseline_count = sum(
        1
        for row in v.rows("baseline_agents").values()
        if row.sponsor_person_id == person_id
        and row.environment == "prod"
        and row.status == "ACTIVE"
    )
    added_count = sum(
        1
        for agent_id, row in state.agent_actions.items()
        if row["agent_action"] == "REGISTER_ACTIVE"
        and row["resolved_sponsor_person_id"] == person_id
        and v.get("enterprise_agents", agent_id).environment == "prod"
    )
    return (
        target.environment != "prod"
        or baseline_count + added_count < ctx.overlay.max_prod_agents_per_sponsor
    )


def _derive_agent(ctx: PhaseContext, state: P1State, agent: Any) -> dict[str, Any]:
    v = ctx.phase1_view
    if agent.manifest_digest_etag != agent.approved_registry_digest_etag or not (
        0 <= ctx.clock - agent.attestation_synced_at_s <= ctx.overlay.freshness_sla_s
    ):
        result = {
            "agent_action": "SUSPEND_UNATTESTED_OR_STALE",
            "resolved_sponsor_person_id": None,
            "active_autonomous_binding_ids": (),
            "active_hitl_binding_ids": (),
        }
        state.agent_actions[agent.agent_id] = result
        return result
    sponsor = agent.candidate_sponsor_person_id
    if not _agent_sponsor_eligible(ctx, state, sponsor, agent):
        result = {
            "agent_action": "SUSPEND_SPONSOR_INVALID",
            "resolved_sponsor_person_id": None,
            "active_autonomous_binding_ids": (),
            "active_hitl_binding_ids": (),
        }
        state.agent_actions[agent.agent_id] = result
        return result
    auto: set[str] = set()
    hitl: set[str] = set()
    for binding in agent.tool_bindings:
        connector = v.get("connector_schemas", binding.tool_connector_id)
        sync = state.connector.get(binding.tool_connector_id)
        if connector is None or sync is None or sync["halted_fail_closed"]:
            continue
        if binding.requested_mode == "HITL_REQUIRED":
            hitl.add(binding.binding_id)
        elif binding.privilege_tier not in ctx.overlay.critical_jit_tiers:
            auto.add(binding.binding_id)
        elif ctx.overlay.permit_hitl_step_up_for_autonomous:
            hitl.add(binding.binding_id)
    if not auto and not hitl:
        result = {
            "agent_action": "SUSPEND_NO_ACTIVE_TOOLS",
            "resolved_sponsor_person_id": sponsor,
            "active_autonomous_binding_ids": (),
            "active_hitl_binding_ids": (),
        }
        state.agent_actions[agent.agent_id] = result
        return result
    auto_fp: set[Any] = set()
    all_fp: set[Any] = set()
    binding_by_id = {b.binding_id: b for b in agent.tool_bindings}
    for binding_id in auto:
        b = binding_by_id[binding_id]
        fp = capability_footprint(ctx.policy.scope_catalog, b.capabilities, (b.scope_path,))
        auto_fp.update(fp)
        all_fp.update(fp)
    for binding_id in hitl:
        b = binding_by_id[binding_id]
        all_fp.update(
            capability_footprint(ctx.policy.scope_catalog, b.capabilities, (b.scope_path,))
        )
    self_conflict = bool(
        sod_witnesses(frozenset(all_fp), frozenset(all_fp), ctx.overlay.forbidden_sod_rules)
    )
    sponsor_conflict = ctx.overlay.enforce_sponsor_agent_sod and bool(
        sod_witnesses(
            frozenset(auto_fp),
            state.kstanding_by_person.get(sponsor, frozenset())
            | state.fnhi_by_person.get(sponsor, frozenset())
            | state.kspons_by_person.get(sponsor, frozenset()),
            ctx.overlay.forbidden_sod_rules,
        )
    )
    if self_conflict or sponsor_conflict:
        result = {
            "agent_action": "QUARANTINE_SOD_VIOLATION",
            "resolved_sponsor_person_id": sponsor,
            "active_autonomous_binding_ids": (),
            "active_hitl_binding_ids": (),
            "sponsor_conflict_auto_fp": frozenset(auto_fp),
        }
    else:
        result = {
            "agent_action": "REGISTER_ACTIVE",
            "resolved_sponsor_person_id": sponsor,
            "active_autonomous_binding_ids": tuple(sorted(auto)),
            "active_hitl_binding_ids": tuple(sorted(hitl)),
        }
        state.kspons_by_person[sponsor] = state.kspons_by_person.get(
            sponsor, frozenset()
        ) | frozenset(auto_fp)
    state.agent_actions[agent.agent_id] = result
    return result


def _agent_sponsor_conflict_evidence(
    ctx: PhaseContext, state: P1State, sponsor_id: str, current_agent: Any
) -> set[str]:
    """Return public evidence for sponsor-side SoD sources used by quarantine."""

    v = ctx.phase1_view
    result = state.agent_actions.get(current_agent.agent_id, {})
    auto_fp = result.get("sponsor_conflict_auto_fp", frozenset())
    if not auto_fp:
        return set()
    citations: set[str] = set()
    accounts = v.rows("idp_accounts")
    groups = v.rows("groups")
    roles = v.rows("roles")

    for account_id in _active_kept_account_ids(state, sponsor_id):
        account = accounts.get(account_id)
        if account is None:
            continue
        closure = group_closure(account, groups)
        deny_fp: set[Any] = set()
        for group_id in closure:
            group = groups[group_id]
            if group.effect != "DENY":
                continue
            capabilities = set(group.capabilities)
            for role_id in group.granted_role_ids:
                role = roles.get(role_id)
                if role is not None:
                    capabilities.update(role.capabilities)
            deny_fp.update(
                capability_footprint(ctx.policy.scope_catalog, capabilities, (group.scope_path,))
            )
        for group_id in closure:
            group = groups[group_id]
            if group.effect != "ALLOW":
                continue
            capabilities = set(group.capabilities)
            for role_id in group.granted_role_ids:
                role = roles.get(role_id)
                if role is not None:
                    capabilities.update(role.capabilities)
            footprint = (
                capability_footprint(ctx.policy.scope_catalog, capabilities, (group.scope_path,))
                - deny_fp
            )
            if sod_witnesses(auto_fp, footprint, ctx.overlay.forbidden_sod_rules):
                citations.add(group.evidence_id)
        for grant_id in state.kept_by_account.get(account_id, set()):
            grant = v.get("grants", grant_id)
            if grant is None or grant.grant_mode != "standing":
                continue
            footprint = _grant_footprint(ctx, account, grant_id)
            if sod_witnesses(auto_fp, footprint, ctx.overlay.forbidden_sod_rules):
                citations.add(grant.evidence_id)

    for baseline in v.rows("baseline_nhis").values():
        if baseline.status != "ACTIVE" or baseline.owner_person_id != sponsor_id:
            continue
        footprint = capability_footprint(
            ctx.policy.scope_catalog, baseline.capabilities, (baseline.scope_path,)
        )
        if sod_witnesses(auto_fp, footprint, ctx.overlay.forbidden_sod_rules):
            citations.add(baseline.evidence_id)
    for nhi_id, outcome in state.nhi_actions.items():
        target = v.get("target_nhis", nhi_id)
        if (
            target is None
            or outcome.get("nhi_action") != "ATTRIBUTE_OWNER"
            or outcome.get("resolved_owner_person_id") != sponsor_id
        ):
            continue
        footprint = capability_footprint(
            ctx.policy.scope_catalog, target.capabilities, (target.scope_path,)
        )
        if sod_witnesses(auto_fp, footprint, ctx.overlay.forbidden_sod_rules):
            citations.add(target.evidence_id)

    for baseline in v.rows("baseline_agents").values():
        if baseline.status != "ACTIVE" or baseline.sponsor_person_id != sponsor_id:
            continue
        footprint = capability_footprint(
            ctx.policy.scope_catalog,
            baseline.autonomous_capabilities,
            (baseline.scope_path,),
        )
        if sod_witnesses(auto_fp, footprint, ctx.overlay.forbidden_sod_rules):
            citations.add(baseline.evidence_id)
    for prior_id, outcome in state.agent_actions.items():
        if prior_id >= current_agent.agent_id or (
            outcome.get("agent_action") != "REGISTER_ACTIVE"
            or outcome.get("resolved_sponsor_person_id") != sponsor_id
        ):
            continue
        prior = v.get("enterprise_agents", prior_id)
        if prior is None:
            continue
        binding_by_id = {binding.binding_id: binding for binding in prior.tool_bindings}
        prior_fp: set[Any] = set()
        for binding_id in outcome.get("active_autonomous_binding_ids", ()):
            binding = binding_by_id.get(binding_id)
            if binding is not None:
                prior_fp.update(
                    capability_footprint(
                        ctx.policy.scope_catalog, binding.capabilities, (binding.scope_path,)
                    )
                )
        if sod_witnesses(auto_fp, prior_fp, ctx.overlay.forbidden_sod_rules):
            citations.add(prior.evidence_id)
    return citations


def _principal_live(state: P1State, principal_id: str) -> bool:
    if principal_id.startswith("acct_"):
        return state.accounts.get(principal_id, {}).get("pre_action") == "KEEP_ACTIVE"
    if principal_id.startswith("nhi_"):
        return state.nhi_actions.get(principal_id, {}).get("nhi_action") in {
            "ATTRIBUTE_OWNER",
            "FLAG_SHARED_EXCEPTION",
        }
    if principal_id.startswith("agt_"):
        return state.agent_actions.get(principal_id, {}).get("agent_action") == "REGISTER_ACTIVE"
    return False


def _ciem_finding(ctx: PhaseContext, state: P1State, task: Any, principal: Any) -> dict[str, Any]:
    provider = principal.cloud_provider
    escalation_edges: list[tuple[str, set[str]]] = []
    if provider == "AWS":
        resource = set(principal.aws_resource_policy_allow)
        identity = set(principal.aws_identity_allow)
        raw = identity | resource if principal.aws_same_account_resource else identity & resource
        boundary = (
            set(principal.aws_permission_boundary_allow)
            if principal.aws_permission_boundary_allow is not None
            else raw
        )
        bounded = (raw & boundary & set(principal.aws_scp_allow)) - set(principal.aws_explicit_deny)
        passrole = {"iam:PassRole", "compute:RunWorkload"} <= bounded and bool(
            principal.aws_passrole_target_actions
        )
        assume = (
            "sts:AssumeRole" in bounded
            and principal.aws_trust_policy_valid
            and bool(principal.aws_assumerole_chain_actions)
        )
        if passrole:
            escalation_edges.append(
                ("AWS_PASSROLE_EXEC", set(principal.aws_passrole_target_actions))
            )
        if assume:
            escalation_edges.append(
                ("AWS_ASSUMEROLE_CHAIN", set(principal.aws_assumerole_chain_actions))
            )
    elif provider == "AZURE":
        rbac = set().union(
            *(set(allow) - set(not_actions) for allow, not_actions in principal.az_role_assignments)
        )
        bounded = (
            rbac
            if principal.az_exempt_from_deny_assignment
            else rbac - set(principal.az_deny_assignment_actions)
        )
        role_write = (
            "Microsoft.Authorization/roleAssignments/write" in bounded
            and not principal.az_abac_delegation_constrained
            and bool(principal.az_unconstrained_role_assign_write_target_actions)
        )
        app_cred = "Application.ReadWrite.All" in bounded and bool(
            principal.az_entra_app_cred_target_actions
        )
        if role_write:
            escalation_edges.append(
                (
                    "AZURE_ROLE_ASSIGN_WRITE",
                    set(principal.az_unconstrained_role_assign_write_target_actions),
                )
            )
        if app_cred:
            escalation_edges.append(
                ("AZURE_ENTRA_APP_CRED", set(principal.az_entra_app_cred_target_actions))
            )
    else:
        allowed = set().union(
            *(
                set(actions)
                for tag, actions in principal.gcp_allow_bindings
                if tag in {"*", principal.gcp_resource_env_tag}
            )
        )
        bounded = allowed - set(principal.gcp_v2_deny_actions)
        sa_edge = bool(
            {"iam.serviceAccounts.actAs", "iam.serviceAccounts.getAccessToken"} & bounded
        ) and bool(principal.gcp_sa_impersonation_target_actions)
        wif_edge = principal.gcp_wif_unscoped_repo_claim
        if sa_edge:
            escalation_edges.append(
                ("GCP_SA_IMPERSONATION", set(principal.gcp_sa_impersonation_target_actions))
            )
        if wif_edge:
            escalation_edges.append(("GCP_WIF_UNSCOPED_CLAIM", set()))
    effective = set(bounded)
    for _, actions in escalation_edges:
        effective.update(actions)
    escalation_order = (
        "AWS_PASSROLE_EXEC",
        "AWS_ASSUMEROLE_CHAIN",
        "AZURE_ROLE_ASSIGN_WRITE",
        "AZURE_ENTRA_APP_CRED",
        "GCP_WIF_UNSCOPED_CLAIM",
        "GCP_SA_IMPERSONATION",
    )
    available = {name for name, _ in escalation_edges}
    escalation = next((name for name in escalation_order if name in available), "NONE")
    forbidden = tuple(sorted(effective & set(task.forbidden_standing_cloud_actions)))
    if not _principal_live(state, principal.linked_principal_id):
        remedy = "REVOKE_ORPHAN_CLOUD_PRINCIPAL"
    elif escalation == "GCP_WIF_UNSCOPED_CLAIM":
        remedy = "LOCK_WIF_ATTRIBUTE_CONDITION"
    elif escalation != "NONE":
        remedy = "STRIP_ESCALATION_EDGE"
    elif forbidden:
        remedy = "ATTACH_BOUNDARY_OR_DENY"
    else:
        remedy = "KEEP_COMPLIANT"
    return {
        "cprin_id": principal.cprin_id,
        "effective_forbidden_actions": forbidden,
        "escalation_vector": escalation,
        "remediation_action": remedy,
    }


def _birthright_partition(
    ctx: PhaseContext, workflow: Any
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    v = ctx.phase1_view
    roles = v.rows("roles")
    rules = workflow.birthright_catalog
    tier_quarantine: set[str] = set()
    for rule in rules:
        role = roles[rule.role_id]
        if (
            (
                "VENDOR" in rule.allowed_affiliations
                and role.privilege_tier in {"elevated", "admin", "critical"}
            )
            or ("CONTRACTOR" in rule.allowed_affiliations and role.privilege_tier == "critical")
            or (
                rule.grant_mode == "standing"
                and role.privilege_tier in ctx.overlay.critical_jit_tiers
                and not role.is_jit_capable_target
            )
        ):
            tier_quarantine.add(rule.rule_id)
    conflict: set[str] = set(tier_quarantine)
    for first, second in combinations(rules, 2):
        if first.rule_id in tier_quarantine or second.rule_id in tier_quarantine:
            continue
        overlap_population = bool(
            set(first.allowed_affiliations) & set(second.allowed_affiliations)
        ) and bool(ctx.scope.leaves(first.dept_prefix) & ctx.scope.leaves(second.dept_prefix))
        if not overlap_population:
            continue
        fp_first = capability_footprint(
            ctx.policy.scope_catalog, roles[first.role_id].capabilities, (first.scope_path,)
        )
        fp_second = capability_footprint(
            ctx.policy.scope_catalog, roles[second.role_id].capabilities, (second.scope_path,)
        )
        if sod_witnesses(fp_first, fp_second, ctx.overlay.forbidden_sod_rules):
            conflict.update((first.rule_id, second.rule_id))
    quarantined = tuple(sorted(conflict))
    converted: set[str] = set()
    active: set[str] = set()
    for rule in rules:
        if rule.rule_id in conflict:
            continue
        role = roles[rule.role_id]
        if (
            rule.grant_mode == "standing"
            and role.privilege_tier in ctx.overlay.critical_jit_tiers
            and role.is_jit_capable_target
        ):
            converted.add(rule.rule_id)
        else:
            active.add(rule.rule_id)
    return tuple(sorted(active)), tuple(sorted(converted)), quarantined


def _workflow_spec_is_determinable(
    ctx: PhaseContext, workflow: Any
) -> tuple[dict[str, Any] | None, str | None]:
    """Derive the task-parameterized workflow contract from public task modes."""

    order_joiner = (
        "VERIFY_HR_AUTH_AND_EFFECTIVE_DATE",
        "CORRELATE_IMMUTABLE_ID_OR_CREATE_ACCOUNT",
        "PROVISION_BIRTHRIGHT_ACCESS",
        "BIND_TELEMETRY_AND_MFA_POLICY",
        "EMIT_JOINER_AUDIT_RECEIPT",
    )
    order_mover = (
        "SNAPSHOT_PRIOR_GRANTS_AND_BRIDGES",
        "REVOKE_OUT_OF_SCOPE_NON_BRIDGED_GRANTS",
        "PROVISION_NEW_DEPT_BIRTHRIGHTS",
        "ENFORCE_POST_MOVER_SOD_AND_REBIND_OWNERSHIP",
        "TRIGGER_TARGETED_MOVER_CERTIFICATION",
    )
    order_leaver = (
        "DISABLE_IDP_AND_SSO_SESSIONS",
        "REVOKE_REFRESH_AND_OBO_TREES",
        "TRANSFER_OWNED_NHI_AND_AGENT_CUSTODY",
        "DEPROVISION_SPOKE_ENTITLEMENTS_OR_DISPATCH_ITSM",
        "APPLY_MAILBOX_ARCHIVE_OR_LEGAL_HOLD_RETENTION",
    )
    return (
        {
            "joiner_steps": order_joiner,
            "mover_steps": order_mover,
            "leaver_steps": order_leaver,
            "joiner_contractor_sponsor_policy": (
                "REQUIRE_ACTIVE_FTE_SPONSOR_PRE_PROVISION"
                if workflow.contractor_sponsor_check_mode == "STRICT_PRE_PROVISION"
                else "ALLOW_INTERNAL_CONVERSION_CARRYOVER_SPONSOR"
            ),
            "no_show_disposition": "FULL_REVOKE_AND_DISABLE_NEVER_STARTED",
            "legal_hold_disposition": (
                "SUSPEND_AUTH_REVOKE_ENTITLEMENTS_PRESERVE_HOLD_DATA"
                if workflow.legal_hold_scope_mode == "SUSPEND_PRESERVE_ARCHIVE"
                else "SUSPEND_AUTH_ISOLATE_FORENSIC_SNAPSHOT"
            ),
            "orphan_handoff_policy": (
                "REASSIGN_TO_ACTIVE_MANAGER_OR_ESCROW"
                if workflow.orphan_custody_mode == "HANDOFF_TO_MANAGER_ELSE_SUSPEND"
                else "IMMEDIATE_SUSPEND_ALL_OWNED_ENTITIES"
            ),
            "spoke_error_policy": "FAIL_CLOSED_RETRY_QUEUE_AND_ALERT",
            "disconnected_target_policy": (
                "DISPATCH_ITSM_AND_VERIFY_POST_CLOSE_AGGREGATION"
                if workflow.has_disconnected_spokes
                else "NOT_APPLICABLE_AUTOMATED_ONLY"
            ),
        },
        None,
    )


def derive_phase1(context: PhaseContext) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if context.phase_index != 1:
        raise ValueError("derive_phase1 requires phase_index=1")
    v = context.phase1_view
    state = P1State(ctx=context)
    context._p1_state = state
    missing: list[dict[str, str]] = []
    for connector_id in sorted(context.q.get("target_connector_ids", ())):
        connector = v.get("connector_schemas", connector_id)
        if connector is None:
            missing.append(
                {
                    "path": "/phase1_manifest/work_queue/target_connector_ids",
                    "detail": f"missing delivered connector {connector_id}",
                }
            )
            continue
        state.connector[connector_id] = _sync_connector(context, connector)
    for task_id in sorted(context.q.get("target_hr_mapping_ids", ())):
        task = v.get("hr_source_mappings", task_id)
        if task is None:
            missing.append(
                {
                    "path": "/phase1_manifest/work_queue/target_hr_mapping_ids",
                    "detail": f"missing delivered HR mapping task {task_id}",
                }
            )
            continue
        _derive_hr_mapping(context, task, state)
    _classify_base_accounts(context, state)
    try:
        _compute_sod_and_controls(context, state)
    except (KeyError, ValueError) as exc:
        missing.append(
            {
                "path": "/phase1_slices",
                "detail": (
                    f"P1 account/control graph cannot be fully resolved from public rows: {exc}"
                ),
            }
        )
    _attach_itsm(context, state)

    for baseline in v.rows("baseline_agents").values():
        if baseline.status == "ACTIVE":
            state.kspons_by_person[baseline.sponsor_person_id] = state.kspons_by_person.get(
                baseline.sponsor_person_id, frozenset()
            ) | capability_footprint(
                context.policy.scope_catalog,
                baseline.autonomous_capabilities,
                (baseline.scope_path,),
            )

    # Source-specific NHI owner resolution is ordered so that earlier target
    # attributions consume the finite prod cap and add their capability footprint.
    for nhi_id in sorted(context.q.get("target_nhi_ids", ())):
        target = v.get("target_nhis", nhi_id)
        if target is None:
            missing.append(
                {
                    "path": "/phase1_manifest/work_queue/target_nhi_ids",
                    "detail": f"missing delivered NHI {nhi_id}",
                }
            )
            continue
        _derive_nhi(context, state, target)

    for agent_id in sorted(context.q.get("target_agent_ids", ())):
        agent = v.get("enterprise_agents", agent_id)
        if agent is None:
            missing.append(
                {
                    "path": "/phase1_manifest/work_queue/target_agent_ids",
                    "detail": f"missing delivered enterprise agent {agent_id}",
                }
            )
            continue
        _derive_agent(context, state, agent)

    plans: list[dict[str, Any]] = []
    plans.extend(_connector_mutations(context, state))
    plans.extend(_hr_mutations(context, state))
    plans.extend(_account_mutations(context, state))
    plans.extend(_nhi_mutations(context, state))
    plans.extend(_agent_mutations(context, state))
    plans.extend(_ciem_mutations(context, state))
    plans.extend(_workflow_mutations(context, state, missing))
    plans.extend(_optimization_mutations(context, state, missing))
    plans.sort(key=lambda m: (m["mutation_kind"], m["target_entity_id"]))
    return plans, missing


def _connector_mutations(ctx: PhaseContext, state: P1State) -> list[dict[str, Any]]:
    out = []
    v = ctx.phase1_view
    for connector_id in sorted(ctx.q.get("target_connector_ids", ())):
        conn = v.get("connector_schemas", connector_id)
        result = state.connector.get(connector_id)
        if conn is None or result is None:
            continue
        page_evidence = [page.evidence_id for page in result["inspected_pages"]]
        cites = {f"CONN-V5-01:{conn.evidence_id}"}
        cites.update(_evidence("CONN-V5-01", page_evidence))
        for drift in result["remediated_spoke_drift"]:
            aid = drift.split(":", 1)[0]
            account = v.get("idp_accounts", aid)
            if account is not None and not result["halted_fail_closed"]:
                cites.add(f"CONN-V5-01:{account.evidence_id}")
        if not result["halted_fail_closed"] and result["itsm_ticket_actions"]:
            cites.add(f"ITSM-V5-01:{conn.evidence_id}")
            for drift in result["remediated_spoke_drift"]:
                ticket = next((t for t in conn.open_itsm_tickets if t.drift_token == drift), None)
                if ticket is not None:
                    cites.add(f"ITSM-V5-01:{ticket.evidence_id}")
            # P1.4b evaluates the primary and backup fulfiller candidates before
            # selecting the qualified best account.
            for aid in (
                conn.itsm_primary_fulfiller_account_id,
                conn.itsm_backup_fulfiller_account_id,
            ):
                account = v.get("idp_accounts", aid)
                if account is not None:
                    cites.add(f"ITSM-V5-01:{account.evidence_id}")
                    p = state.accounts.get(aid, {}).get("bound_person_id")
                    person = v.get("hr_persons", p)
                    if person is not None:
                        cites.add(f"ITSM-V5-01:{person.evidence_id}")
            for aid in result.get("consulted_ticket_fulfiller_account_ids", ()):
                account = v.get("idp_accounts", aid)
                if account is None:
                    continue
                cites.add(f"ITSM-V5-01:{account.evidence_id}")
                person_id = state.accounts.get(aid, {}).get("bound_person_id") or _preperson(
                    ctx, aid
                )
                person = v.get("hr_persons", person_id)
                if person is not None:
                    cites.add(f"ITSM-V5-01:{person.evidence_id}")
        pre_ids: set[str] = {connector_id}
        if (
            not result["halted_fail_closed"]
            and conn.provisioning_mode == "DISCONNECTED_ITSM_TICKET"
        ):
            for aid in (
                conn.itsm_primary_fulfiller_account_id,
                conn.itsm_backup_fulfiller_account_id,
            ):
                if aid is None:
                    continue
                acct = v.get("idp_accounts", aid)
                if acct is None:
                    continue
                person = state.accounts.get(aid, {}).get("bound_person_id") or _preperson(ctx, aid)
                if person is not None:
                    pre_ids.add(aid)
                    pre_ids.add(person)
            for ticket in conn.open_itsm_tickets:
                aid = ticket.assigned_to_account_id
                if aid is not None:
                    person = state.accounts.get(aid, {}).get("bound_person_id")
                    if person is not None:
                        pre_ids.add(aid)
                        pre_ids.add(person)
        raw = {
            "mutation_kind": "CONNECTOR_SYNC_MUTATION",
            "target_entity_id": connector_id,
            "if_match": _ver(ctx, pre_ids),
            "cite": tuple(sorted(cites)),
            "halted_fail_closed": result["halted_fail_closed"],
            "halt_reason": result["halt_reason"],
            "reconciled_account_ids": result["reconciled_account_ids"],
            "remediated_spoke_drift": result["remediated_spoke_drift"],
            "itsm_ticket_actions": result["itsm_ticket_actions"],
        }
        out.append(raw)
    return out


def _hr_mutations(ctx: PhaseContext, state: P1State) -> list[dict[str, Any]]:
    out = []
    v = ctx.phase1_view
    for task_id in sorted(ctx.q.get("target_hr_mapping_ids", ())):
        task = v.get("hr_source_mappings", task_id)
        result = state.hr.get(task_id)
        if task is None or result is None:
            continue
        ids = {task_id}
        evidence = {task.evidence_id}
        for feed in task.feeds:
            ids.add(feed.connector_id)
            evidence.add(feed.evidence_id)
            evidence.update((v.get("connector_schemas", feed.connector_id).evidence_id,))
        for person_id in result["consulted"]:
            ids.add(person_id)
            person = v.get("hr_persons", person_id)
            if person is not None:
                evidence.add(person.evidence_id)
        cites = _evidence("HR-AUTH-V5-01", evidence)
        out.append(
            {
                "mutation_kind": "HR_SOURCE_MAPPING_MUTATION",
                "target_entity_id": task_id,
                "if_match": _ver(ctx, ids),
                "cite": cites,
                "accepted_feed_ids": result["accepted"],
                "rejected_feed_ids": result["rejected"],
                "resolved_person_statuses": result["resolved"],
                "quarantined_assertion_keys": result["quarantine"],
            }
        )
    return out


def _account_mutations(ctx: PhaseContext, state: P1State) -> list[dict[str, Any]]:
    out = []
    v = ctx.phase1_view
    people = v.rows("hr_persons")
    accounts = v.rows("idp_accounts")
    grants = v.rows("grants")
    controls = tuple(v.rows("compensating_controls").values())
    groups = v.rows("groups")
    roles = v.rows("roles")
    for account_id in sorted(ctx.q.get("target_human_account_ids", ())):
        account = accounts.get(account_id)
        result = state.accounts.get(account_id)
        if account is None or result is None:
            continue
        pre_ids: set[str] = {account_id, *result["pre_person_ids"]}
        citations: set[str] = set()
        binding_rule = "BIND-V5-01a" if account.immutable_person_ref is not None else "BIND-V5-01b"
        citations.add(f"{binding_rule}:{account.evidence_id}")
        for person_id in result["pre_person_ids"]:
            p = people.get(person_id)
            if p is not None:
                citations.add(f"{binding_rule}:{p.evidence_id}")
        person_id = result["bound_person_id"]
        engagement = result["pre_engagement"]
        if (
            person_id is not None
            and engagement is not None
            and engagement.eng_status == "CONTRACTOR_TO_FTE_PENDING"
        ):
            person = people.get(person_id)
            if person is not None:
                citations.add(f"CONV-V5-02:{person.evidence_id}")
            for gid in result["converted_pruned"]:
                grant = grants.get(gid)
                if grant is not None:
                    citations.add(f"CONV-V5-02:{grant.evidence_id}")
        if result["pre_action"] == "KEEP_ACTIVE":
            mover_examined = set(result["mover_pruned"]) | set(result.get("bridge_by_pruned", {}))
            for gid in mover_examined:
                grant = grants.get(gid)
                if grant is not None:
                    citations.add(f"MOVER-V5-03:{grant.evidence_id}")
                for bridge in v.rows("transfer_bridges").values():
                    if bridge.account_id == account_id and bridge.grant_id == gid:
                        citations.add(f"MOVER-V5-03:{bridge.evidence_id}")
        if person_id is not None and result["pre_action"] == "KEEP_ACTIVE":
            own_controls = state.live_controls.get(person_id, ())
            active_account_ids = _active_kept_account_ids(state, person_id)
            kgrp_parts = [
                _account_group_footprint(ctx, accounts[aid])[0]
                for aid in active_account_ids
                if aid in accounts
            ]
            kgrp = frozenset().union(*kgrp_parts) if kgrp_parts else frozenset()
            before_map = state.sod_before.get(person_id, {})
            d1_pairs = state.d1_pairs.get(person_id, set())
            for gid in result["retained_grant_ids"]:
                grant = grants.get(gid)
                if grant is None:
                    continue
                key = (account_id, gid)
                fp = _grant_footprint(ctx, account, gid)
                ktrace = before_map.get(key, kgrp)
                raw_witnesses = sod_witnesses(fp, ktrace, ctx.overlay.forbidden_sod_rules)
                for control in controls:
                    control_live = control in own_controls
                    matches = any(
                        syntactically_mitigates(
                            control, person_id, witness, rule, ctx.policy.scope_catalog
                        )
                        for witness in raw_witnesses
                        for rule in ctx.overlay.forbidden_sod_rules
                        if rule.rule_code == witness[0]
                    )
                    if control_live and matches:
                        citations.add(f"CC-V5-01:{control.evidence_id}")
                        conn = v.get("connector_schemas", control.audit_connector_id)
                        monitor_acct = v.get("idp_accounts", control.monitor_account_id)
                        if conn is not None:
                            citations.add(f"CC-V5-01:{conn.evidence_id}")
                        if monitor_acct is not None:
                            citations.add(f"CC-V5-01:{monitor_acct.evidence_id}")
                        pre_ids.add(control.cc_id)
                role = roles.get(grant.role_id)
                if role is not None:
                    raw_grant = capability_footprint(
                        ctx.policy.scope_catalog, role.capabilities, (grant.scope_path,)
                    )
                    for group_id in group_closure(account, groups):
                        group = groups[group_id]
                        if group.effect != "DENY":
                            continue
                        raw_group = set(group.capabilities)
                        for role_id in group.granted_role_ids:
                            raw_group.update(roles[role_id].capabilities)
                        deny_fp = capability_footprint(
                            ctx.policy.scope_catalog, raw_group, (group.scope_path,)
                        )
                        if sod_witnesses(
                            raw_grant & deny_fp, ktrace, ctx.overlay.forbidden_sod_rules
                        ):
                            citations.add(f"SOD-V5-01:{group.evidence_id}")
            pruned_after_mover = set(result.get("mover_survivors", set())) - set(
                result["retained_grant_ids"]
            )
            for gid in sorted(pruned_after_mover):
                grant = grants.get(gid)
                if grant is None:
                    continue
                key = (account_id, gid)
                ktrace = before_map.get(key, kgrp)
                fp = _grant_footprint(ctx, account, gid)
                witnesses = sod_witnesses(fp, ktrace, ctx.overlay.forbidden_sod_rules)
                if not witnesses:
                    continue
                citations.add(f"SOD-V5-01:{grant.evidence_id}")
                if key not in d1_pairs:
                    for group_id in group_closure(account, groups):
                        group = groups[group_id]
                        if group.effect != "ALLOW":
                            continue
                        raw_group = set(group.capabilities)
                        for role_id in group.granted_role_ids:
                            raw_group.update(roles[role_id].capabilities)
                        group_fp = capability_footprint(
                            ctx.policy.scope_catalog, raw_group, (group.scope_path,)
                        )
                        if sod_witnesses(fp, group_fp, ctx.overlay.forbidden_sod_rules):
                            citations.add(f"SOD-V5-01:{group.evidence_id}")
                else:
                    for earlier_aid, earlier_gid in state.kept_pairs_order.get(person_id, ()):
                        earlier_grant = grants.get(earlier_gid)
                        earlier_account = accounts.get(earlier_aid)
                        if earlier_grant is None or earlier_account is None:
                            continue
                        if sod_witnesses(
                            fp,
                            _grant_footprint(ctx, earlier_account, earlier_gid),
                            ctx.overlay.forbidden_sod_rules,
                        ):
                            citations.add(f"SOD-V5-01:{earlier_grant.evidence_id}")
                for control in controls:
                    if control in own_controls:
                        continue
                    if any(
                        syntactically_mitigates(
                            control, person_id, witness, rule, ctx.policy.scope_catalog
                        )
                        for witness in witnesses
                        for rule in ctx.overlay.forbidden_sod_rules
                        if rule.rule_code == witness[0]
                    ):
                        citations.add(f"CC-V5-01:{control.evidence_id}")
        out.append(
            {
                "mutation_kind": "ACCOUNT_RECON_MUTATION",
                "target_entity_id": account_id,
                "if_match": _ver(ctx, pre_ids),
                "cite": tuple(sorted(citations)),
                "account_action": result["account_action"],
                "bound_person_id": result["bound_person_id"],
                "bound_engagement_id": result["bound_engagement_id"],
                "retained_grant_ids": result["retained_grant_ids"],
            }
        )
    return out


def _nhi_mutations(ctx: PhaseContext, state: P1State) -> list[dict[str, Any]]:
    out = []
    v = ctx.phase1_view
    for nhi_id in sorted(ctx.q.get("target_nhi_ids", ())):
        target = v.get("target_nhis", nhi_id)
        result = state.nhi_actions.get(nhi_id)
        if target is None or result is None:
            continue
        pre_ids: set[str] = {nhi_id}
        cite_evidence: set[str] = {target.evidence_id}
        selected_signals = []
        if result["nhi_action"] == "FLAG_SHARED_EXCEPTION":
            selected_signals = [
                s
                for s in target.signals
                if s.source == "vault_rotator_principal"
                and _fresh(ctx, s)
                and s.rotator_account_id in v.rows("idp_accounts")
                and state.accounts.get(s.rotator_account_id, {}).get("pre_action") == "KEEP_ACTIVE"
            ]
        elif result["nhi_action"] == "ATTRIBUTE_OWNER" or result["nhi_action"] == "ESCALATE":
            selected_signals = list(result.get("eligible_rank_signals", ()))
        elif result["nhi_action"] == "DISABLE_ORPHAN":
            selected_signals = list(target.signals)
        for signal in selected_signals:
            cite_evidence.add(signal.evidence_id)
            if signal.rotator_account_id is not None:
                account = v.get("idp_accounts", signal.rotator_account_id)
                if account is not None and result["nhi_action"] in {
                    "FLAG_SHARED_EXCEPTION",
                    "ATTRIBUTE_OWNER",
                    "ESCALATE",
                }:
                    pre_ids.add(signal.rotator_account_id)
                    cite_evidence.add(account.evidence_id)
            if signal.parent_nhi_id is not None and result["nhi_action"] in {
                "ATTRIBUTE_OWNER",
                "ESCALATE",
            }:
                parent = v.get("baseline_nhis", signal.parent_nhi_id)
                if parent is not None:
                    pre_ids.add(signal.parent_nhi_id)
                    cite_evidence.add(parent.evidence_id)
        owner = result["resolved_owner_person_id"]
        if owner is not None:
            pre_ids.add(owner)
            person = v.get("hr_persons", owner)
            if person is not None:
                cite_evidence.add(person.evidence_id)
        if result["nhi_action"] == "ATTRIBUTE_OWNER" and target.environment == "prod":
            owner = result["resolved_owner_person_id"]
            for baseline in v.rows("baseline_nhis").values():
                if (
                    baseline.owner_person_id == owner
                    and baseline.environment == "prod"
                    and baseline.status == "ACTIVE"
                ):
                    cite_evidence.add(baseline.evidence_id)
            for prev_id in sorted(state.nhi_actions):
                if prev_id == nhi_id:
                    break
                prev = state.nhi_actions[prev_id]
                if (
                    prev.get("nhi_action") == "ATTRIBUTE_OWNER"
                    and prev.get("resolved_owner_person_id") == owner
                ):
                    row = v.get("target_nhis", prev_id)
                    if row is not None and row.environment == "prod":
                        cite_evidence.add(row.evidence_id)
        raw = {
            "mutation_kind": "NHI_GOVERNANCE_MUTATION",
            "target_entity_id": nhi_id,
            "if_match": _ver(ctx, pre_ids),
            "cite": _evidence("NHI-V5-01", cite_evidence),
            "nhi_action": result["nhi_action"],
            "resolved_owner_person_id": result["resolved_owner_person_id"],
            "owner_resolution_source": result["owner_resolution_source"],
        }
        out.append(raw)
    return out


def _agent_mutations(ctx: PhaseContext, state: P1State) -> list[dict[str, Any]]:
    out = []
    v = ctx.phase1_view
    for agent_id in sorted(ctx.q.get("target_agent_ids", ())):
        agent = v.get("enterprise_agents", agent_id)
        result = state.agent_actions.get(agent_id)
        if agent is None or result is None:
            continue
        pre_ids = {agent_id}
        sponsor = agent.candidate_sponsor_person_id
        if sponsor in v.rows("hr_persons"):
            pre_ids.add(sponsor)
        cite_evidence = {agent.evidence_id}
        if result["agent_action"] != "SUSPEND_UNATTESTED_OR_STALE" and sponsor in v.rows(
            "hr_persons"
        ):
            person = v.get("hr_persons", sponsor)
            cite_evidence.add(person.evidence_id)
            if agent.environment == "prod":
                for base in v.rows("baseline_agents").values():
                    if (
                        base.sponsor_person_id == sponsor
                        and base.environment == "prod"
                        and base.status == "ACTIVE"
                    ):
                        cite_evidence.add(base.evidence_id)
                for previous_id in sorted(state.agent_actions):
                    if previous_id == agent_id:
                        break
                    previous = state.agent_actions[previous_id]
                    row = v.get("enterprise_agents", previous_id)
                    if (
                        previous["agent_action"] == "REGISTER_ACTIVE"
                        and previous["resolved_sponsor_person_id"] == sponsor
                        and row is not None
                        and row.environment == "prod"
                    ):
                        cite_evidence.add(row.evidence_id)
            if result["agent_action"] in {
                "REGISTER_ACTIVE",
                "SUSPEND_NO_ACTIVE_TOOLS",
                "QUARANTINE_SOD_VIOLATION",
            }:
                pre_ids.update(_active_kept_account_ids(state, sponsor))
                for binding in agent.tool_bindings:
                    conn = v.get("connector_schemas", binding.tool_connector_id)
                    if conn is not None:
                        cite_evidence.add(conn.evidence_id)
                    cite_evidence.add(binding.evidence_id)
                    pre_ids.add(binding.tool_connector_id)
                if result["agent_action"] == "QUARANTINE_SOD_VIOLATION":
                    cite_evidence.update(
                        _agent_sponsor_conflict_evidence(ctx, state, sponsor, agent)
                    )
        raw = {
            "mutation_kind": "AGENT_REGISTRY_MUTATION",
            "target_entity_id": agent_id,
            "if_match": _ver(ctx, pre_ids),
            "cite": _evidence("AGENT-REG-V5-01", cite_evidence),
            "agent_action": result["agent_action"],
            "resolved_sponsor_person_id": result["resolved_sponsor_person_id"],
            "active_autonomous_binding_ids": result["active_autonomous_binding_ids"],
            "active_hitl_binding_ids": result["active_hitl_binding_ids"],
        }
        out.append(raw)
    return out


def _ciem_mutations(ctx: PhaseContext, state: P1State) -> list[dict[str, Any]]:
    out = []
    v = ctx.phase1_view
    for task_id in sorted(ctx.q.get("target_ciem_task_ids", ())):
        task = v.get("cloud_ciem_tasks", task_id)
        if task is None:
            continue
        findings = tuple(
            _ciem_finding(ctx, state, task, p)
            for p in sorted(task.principals, key=lambda p: p.cprin_id)
        )
        pre_ids = {task_id} | {p.linked_principal_id for p in task.principals}
        cite_ids = {task_id}
        for principal, finding in zip(
            sorted(task.principals, key=lambda p: p.cprin_id), findings, strict=True
        ):
            if finding["remediation_action"] == "REVOKE_ORPHAN_CLOUD_PRINCIPAL":
                cite_ids.add(principal.linked_principal_id)
        out.append(
            {
                "mutation_kind": "CLOUD_CIEM_MUTATION",
                "target_entity_id": task_id,
                "if_match": _ver(ctx, pre_ids),
                "cite": _cite(ctx, "CIEM-V5-01", cite_ids),
                "findings": findings,
            }
        )
    return out


def _workflow_mutations(
    ctx: PhaseContext, state: P1State, missing: list[dict[str, str]]
) -> list[dict[str, Any]]:
    out = []
    v = ctx.phase1_view
    for task_id in sorted(ctx.q.get("target_jml_workflow_ids", ())):
        workflow = v.get("jml_workflow_tasks", task_id)
        if workflow is None:
            continue
        active, converted, quarantined = _birthright_partition(ctx, workflow)
        spec, reason = _workflow_spec_is_determinable(ctx, workflow)
        if reason is not None:
            missing.append({"path": f"/phase1_slices/{task_id}/draft_spec", "detail": reason})
            # There is no principled way to substitute a schema-valid authored
            # spec for the simulation over the absent sealed event set.
            continue
        out.append(
            {
                "mutation_kind": "JML_WORKFLOW_MUTATION",
                "target_entity_id": task_id,
                "if_match": _ver(ctx, (task_id,)),
                "cite": _evidence(
                    "JML-WF-V5-01",
                    [workflow.evidence_id]
                    + [b.evidence_id for b in workflow.birthright_catalog]
                    + [v.get("roles", b.role_id).evidence_id for b in workflow.birthright_catalog]
                    + [event.evidence_id for event in workflow.visible_trace_events],
                ),
                "active_birthright_rule_ids": active,
                "converted_eligible_brule_ids": converted,
                "quarantined_birthright_rule_ids": quarantined,
                "authored_spec": spec,
            }
        )
    return out


def _optimization_mutations(
    ctx: PhaseContext, state: P1State, missing: list[dict[str, str]]
) -> list[dict[str, Any]]:
    # The exact P1 optimizer is a finite, at-most-10 candidate search. Its
    # feasibility is state-coupled; defer only when a candidate's base state or
    # live-control graph could not be reconstructed from public P1 rows.
    out = []
    for task_id in sorted(ctx.q.get("target_opt_task_ids", ())):
        task = ctx.phase1_view.get("optimization_tasks", task_id)
        if task is None:
            continue
        if task.phase_kind != "P1_ZSP_REFACTOR":
            continue
        selected, objective, relevant_control_ids, touched_person_ids = _solve_p1_optimization(
            ctx, state, task
        )
        if selected is None:
            missing.append(
                {
                    "path": f"/phase1_slices/{task_id}/candidates",
                    "detail": (
                        "P1 optimization feasibility or unique optimum is not fully derivable "
                        "from the supplied public view"
                    ),
                }
            )
            continue
        pre_ids = {task_id}
        for person_id in touched_person_ids:
            pre_ids.update(
                aid
                for aid, outcome in state.accounts.items()
                if outcome["bound_person_id"] == person_id
                and outcome["pre_action"] == "KEEP_ACTIVE"
            )
        pre_ids.update(relevant_control_ids)
        out.append(
            {
                "mutation_kind": "OPTIMIZATION_MUTATION",
                "target_entity_id": task_id,
                "if_match": _ver(ctx, pre_ids),
                "cite": _cite(ctx, "OPT-V5-01", {task_id, *relevant_control_ids}),
                "selected_ids": selected,
                "objective_value": objective,
            }
        )
    return out


def _solve_p1_optimization(
    ctx: PhaseContext, state: P1State, task: Any
) -> tuple[tuple[str, ...] | None, int | None, set[str], set[str]]:
    """Exhaustive normative minimization for the bounded P1 candidate set."""

    v = ctx.phase1_view
    roles = v.rows("roles")
    grants = v.rows("grants")
    accounts = v.rows("idp_accounts")
    controls = tuple(v.rows("compensating_controls").values())
    candidates = tuple(task.candidates)
    people_opt = {person_id for person_id, _, _ in task.required_person_caps}
    people_opt.update(c.target_person_id for c in candidates if c.target_person_id is not None)
    people_touched = set(people_opt)
    accounts_for_person: dict[str, list[str]] = defaultdict(list)
    for aid, outcome in state.accounts.items():
        if outcome["pre_action"] == "KEEP_ACTIVE" and outcome["bound_person_id"]:
            accounts_for_person[outcome["bound_person_id"]].append(aid)
    people_for_state = people_touched | set(accounts_for_person)
    for p in people_for_state:
        accounts_for_person[p].sort()
    pmin = {p: aids[0] for p, aids in accounts_for_person.items() if aids}
    base_groups: dict[str, frozenset[Any]] = {}
    base_nhi: dict[str, frozenset[Any]] = {}
    base_sponsor: dict[str, frozenset[Any]] = {}
    for p in people_for_state:
        fparts = []
        for aid in accounts_for_person.get(p, []):
            account = accounts.get(aid)
            if account is not None:
                fparts.append(
                    group_footprints(account, v.rows("groups"), roles, ctx.policy.scope_catalog)[0]
                )
        base_groups[p] = frozenset().union(*fparts) if fparts else frozenset()
        base_nhi[p] = state.fnhi_by_person.get(p, frozenset())
        base_sponsor[p] = state.kspons_by_person.get(p, frozenset())
    live_control_ids = {
        cc.cc_id for live_controls in state.live_controls.values() for cc in live_controls
    }
    preserve_control_ids = {
        cc.cc_id for cc in controls if cc.preserve_if_live and cc.cc_id in live_control_ids
    }
    feasible_subsets: dict[tuple[str, ...], bool] = {}
    safety_subsets: dict[tuple[str, ...], tuple[bool, bool]] = {}
    post_state_by_subset: dict[tuple[str, ...], tuple[dict[str, set[Any]], set[str]]] = {}
    best_key: tuple[Any, ...] | None = None
    best: list[tuple[tuple[str, ...], int]] = []
    # Known fixed standing footprints by person before selected refactors.
    base_standing: dict[str, set[Any]] = {}
    base_grant_ids: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for p in people_for_state:
        standing = set(base_groups[p])
        for aid in accounts_for_person.get(p, []):
            for gid in state.kept_by_account.get(aid, set()):
                grant = grants.get(gid)
                if grant is not None and grant.grant_mode == "standing":
                    standing.update(_grant_footprint(ctx, accounts[aid], gid))
                    base_grant_ids[p].append((aid, gid))
        base_standing[p] = standing
    for mask in range(1 << len(candidates)):
        chosen = tuple(candidates[i] for i in range(len(candidates)) if mask & (1 << i))
        selected_ids = tuple(sorted(c.candidate_id for c in chosen))
        converts: dict[str, set[str]] = defaultdict(set)
        adds: dict[str, list[Any]] = defaultdict(list)
        feasible = True
        overgrant = False
        availability_breach = False
        for candidate in chosen:
            if candidate.action_kind == "CONVERT_GRANT_TO_ELIGIBLE":
                grant = grants.get(candidate.target_grant_id)
                if (
                    grant is None
                    or candidate.target_grant_id
                    not in state.kept_by_account.get(
                        pmin.get(candidate.target_person_id, ""), set()
                    )
                    or grant.grant_mode != "standing"
                    or state.accounts.get(pmin.get(candidate.target_person_id, ""), {}).get(
                        "bound_person_id"
                    )
                    != candidate.target_person_id
                    or not roles[grant.role_id].is_jit_capable_target
                ):
                    feasible = False
                    break
                converts[candidate.target_person_id].add(candidate.target_grant_id)
            elif candidate.action_kind == "ASSIGN_ROLE_STANDING":
                if candidate.target_person_id not in pmin or candidate.role_id not in roles:
                    feasible = False
                    break
                adds[candidate.target_person_id].append(candidate)
            else:
                feasible = False
                break
        if not feasible:
            feasible_subsets[selected_ids] = False
            safety_subsets[selected_ids] = (overgrant, availability_breach)
            continue
        post: dict[str, set[Any]] = {}
        for p in people_touched:
            fp = set(base_groups[p])
            for _aid, gid in base_grant_ids.get(p, []):
                if gid in converts[p]:
                    continue
                fp.update(_grant_footprint(ctx, accounts[aid], gid))
            min_acct = accounts.get(pmin.get(p, ""))
            deny = (
                group_footprints(min_acct, v.rows("groups"), roles, ctx.policy.scope_catalog)[1]
                if min_acct is not None
                else frozenset()
            )
            for candidate in adds[p]:
                fp.update(
                    capability_footprint(
                        ctx.policy.scope_catalog,
                        roles[candidate.role_id].capabilities,
                        (candidate.scope_path,),
                    )
                    - deny
                )
            post[p] = fp
        post_control_live = _p1_post_control_live(
            ctx, controls, live_control_ids, post, base_standing
        )
        if not preserve_control_ids <= post_control_live:
            overgrant = True
            feasible_subsets[selected_ids] = False
            safety_subsets[selected_ids] = (overgrant, availability_breach)
            continue
        for person_id in people_touched:
            if any(
                p == person_id
                and not (
                    {cap} <= {c for c, _ in post[p]}
                    and ctx.scope.leaves(scope) <= {leaf for _, leaf in post[p]}
                )
                for p, cap, scope in task.required_person_caps
            ):
                availability_breach = True
                feasible = False
                break
            account = accounts.get(pmin.get(person_id, ""))
            deny = (
                group_footprints(account, v.rows("groups"), roles, ctx.policy.scope_catalog)[1]
                if account is not None
                else frozenset()
            )
            live_controls = [cc for cc in controls if cc.cc_id in post_control_live]
            if unmitigated_witnesses(
                frozenset(post[person_id]),
                frozenset(post[person_id]),
                person_id,
                live_controls,
                ctx.overlay.forbidden_sod_rules,
                ctx.policy.scope_catalog,
            ):
                overgrant = True
                feasible = False
                break
            if ctx.overlay.enforce_cross_nhi_owner_sod and sod_witnesses(
                frozenset(post[person_id]), base_nhi[person_id], ctx.overlay.forbidden_sod_rules
            ):
                overgrant = True
                feasible = False
                break
            if ctx.overlay.enforce_sponsor_agent_sod and sod_witnesses(
                frozenset(post[person_id]), base_sponsor[person_id], ctx.overlay.forbidden_sod_rules
            ):
                overgrant = True
                feasible = False
                break
        if not feasible:
            feasible_subsets[selected_ids] = False
            safety_subsets[selected_ids] = (overgrant, availability_breach)
            continue
        feasible_subsets[selected_ids] = True
        safety_subsets[selected_ids] = (overgrant, availability_breach)
        post_state_by_subset[selected_ids] = (post, post_control_live)
        weight = {"read": 1, "standard": 2, "elevated": 4, "admin": 8, "critical": 16}
        standing_charge = 0
        for p in people_touched:
            for _aid, gid in base_grant_ids.get(p, []):
                if gid in converts[p]:
                    continue
                grant = grants[gid]
                if grant.grant_mode == "standing":
                    standing_charge += weight[roles[grant.role_id].privilege_tier]
            for candidate in adds[p]:
                standing_charge += weight[roles[candidate.role_id].privilege_tier]
        objective = standing_charge + sum(c.action_cost for c in chosen)
        key = (objective, selected_ids)
        if best_key is None or objective < best_key[0]:
            best_key = key
            best = [(selected_ids, objective)]
        elif objective == best_key[0]:
            best.append((selected_ids, objective))
    ctx._p1_optimization_feasible = getattr(ctx, "_p1_optimization_feasible", {})
    ctx._p1_optimization_feasible[task.task_id] = feasible_subsets
    ctx._p1_optimization_safety = getattr(ctx, "_p1_optimization_safety", {})
    ctx._p1_optimization_safety[task.task_id] = safety_subsets
    if not best:
        return None, None, set(), people_touched
    if len({x[0] for x in best}) != 1:
        return None, None, set(), people_touched
    selected_ids, objective = best[0]
    selected_post, selected_post_live = post_state_by_subset[selected_ids]
    relevant_controls = _p1_relevant_controls(
        ctx,
        controls,
        live_control_ids,
        people_touched,
        selected_post,
        selected_post_live,
        base_standing,
    )
    return selected_ids, objective, relevant_controls, people_touched


def _p1_relevant_controls(
    ctx: PhaseContext,
    controls: tuple[Any, ...],
    live_control_ids: set[str],
    people_touched: set[str],
    post: dict[str, set[Any]],
    post_control_live: set[str],
    base_standing: dict[str, set[Any]],
) -> set[str]:
    relevant: set[str] = set()
    for control in controls:
        monitor_person = person_for_account(ctx, control.monitor_account_id, phase1=True)
        touched = control.monitored_person_id in people_touched or monitor_person in people_touched
        if control.cc_id not in live_control_ids or not touched:
            continue
        if control.preserve_if_live:
            relevant.add(control.cc_id)
            continue
        if control.cc_id not in post_control_live:
            continue
        person_id = control.monitored_person_id
        post_footprint = post.get(person_id, base_standing.get(person_id))
        if post_footprint is None:
            continue
        post_fp = frozenset(post_footprint)
        for witness in sod_witnesses(post_fp, post_fp, ctx.overlay.forbidden_sod_rules):
            rule = next(
                (row for row in ctx.overlay.forbidden_sod_rules if row.rule_code == witness[0]),
                None,
            )
            if rule is not None and syntactically_mitigates(
                control, person_id, witness, rule, ctx.policy.scope_catalog
            ):
                relevant.add(control.cc_id)
                break
    return relevant


def _p1_post_control_live(
    ctx: PhaseContext,
    controls: tuple[Any, ...],
    live_control_ids: set[str],
    post: dict[str, set[Any]],
    base_standing: dict[str, set[Any]],
) -> set[str]:
    """Recompute monitor liveness, retaining fixed base footprints outside P_touched."""

    post_live: set[str] = set()
    for control in controls:
        if control.cc_id not in live_control_ids:
            continue
        monitor_person = person_for_account(ctx, control.monitor_account_id, phase1=True)
        monitor_fp = post.get(monitor_person, set(base_standing.get(monitor_person, set())))
        need = capability_footprint(
            ctx.policy.scope_catalog,
            (control.required_monitor_capability,),
            (control.covered_scope_path,),
        )
        if need <= monitor_fp:
            post_live.add(control.cc_id)
    return post_live
