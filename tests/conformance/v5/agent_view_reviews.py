"""Independent agent-view reference for v5 UAR and phase-2 optimization.

This module is intentionally limited to the rendered card and delivered rows.
It does not import the production oracle, verifier, generator, baselines, or
runtime.  When a required fact is not recoverable from the visible ledger and
selected rows, the result records the exact field that remains underdefined.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

MutationKind = Literal["UAR_CAMPAIGN_MUTATION", "OPTIMIZATION_MUTATION"]


class DeliveredPhase(Protocol):
    """Small structural view of the already-resolved delivered phase."""

    episode_id: str
    phase_index: int
    manifest: dict[str, Any]
    policy_card: dict[str, Any]
    slices: dict[str, Any]
    q1_slice_ids: tuple[str, ...]
    q2_slice_ids: tuple[str, ...]
    selected_slice_ids: tuple[str, ...]
    tables: dict[str, tuple[dict[str, Any], ...]]
    table_rows: dict[str, dict[str, dict[str, Any]]]
    row_ranks: dict[str, dict[str, tuple[int, int, str]]]
    payload_hashes: tuple[dict[str, str], ...]


def derive_review_targets(
    phase: DeliveredPhase,
    mutation_kind: MutationKind,
    target_ids: Sequence[str],
    *,
    visible_prior_ledger: Mapping[str, Any],
    preceding_p2_mutations: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Derive data-only UAR or P2 reviewer-optimization rows from public inputs.

    The card fixes the version-token suffix for campaign and optimization task
    rows at ``:1``.  Governed account tokens use the visible P1 ledger when the
    account is still present in the delivered P2 view.  All result tuples are
    converted to ordinary JSON-compatible lists.
    """
    if mutation_kind == "UAR_CAMPAIGN_MUTATION":
        return _derive_uar(phase, target_ids, visible_prior_ledger, preceding_p2_mutations)
    if mutation_kind == "OPTIMIZATION_MUTATION":
        return _derive_p2_optimization(
            phase, target_ids, visible_prior_ledger, preceding_p2_mutations
        )
    raise ValueError(f"unsupported mutation kind: {mutation_kind}")


def _get(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if isinstance(value, Mapping) else default


def _as_rows(value: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        values = value.values()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = value
    else:
        return []
    return [dict(row) for row in values if isinstance(row, Mapping) and key in row]


def _ledger_rows(ledger: Mapping[str, Any], collection: str, key: str) -> dict[str, dict[str, Any]]:
    return {str(row[key]): row for row in _as_rows(ledger.get(collection), key)}


def _table(phase: DeliveredPhase, name: str) -> dict[str, dict[str, Any]]:
    rows = phase.table_rows.get(name, {})
    return {str(k): dict(v) for k, v in rows.items() if isinstance(v, Mapping)}


def _row(phase: DeliveredPhase, table: str, key: str) -> dict[str, Any] | None:
    row = _table(phase, table).get(key)
    return dict(row) if row is not None else None


def _undefined(target: str, field: str, reason: str) -> dict[str, str]:
    return {"target_entity_id": target, "field": field, "reason": reason}


def _token(entity_id: str, row: Mapping[str, Any] | None, generation: Any) -> str | None:
    if row is None or not isinstance(row.get("etag"), str) or generation is None:
        return None
    return f"{entity_id}@{row['etag']}:{generation}"


def _fixed_token(entity_id: str, row: Mapping[str, Any] | None) -> str | None:
    # The rendered card's LV table explicitly assigns :1 to camp/opt rows.
    return _token(entity_id, row, 1)


def _account_token(
    phase: DeliveredPhase,
    account_id: str,
    ledger_accounts: Mapping[str, Mapping[str, Any]],
) -> str | None:
    current = _row(phase, "idp_accounts", account_id)
    prior = ledger_accounts.get(account_id)
    if current is None:
        return None
    if prior is not None:
        return _token(
            account_id,
            {"etag": prior.get("current_etag")},
            prior.get("current_account_gen"),
        )
    return _token(account_id, current, current.get("account_gen"))


def _person_token(phase: DeliveredPhase, person_id: str) -> str | None:
    return _token(
        person_id,
        _row(phase, "hr_persons", person_id),
        (_row(phase, "hr_persons", person_id) or {}).get("lifecycle_gen"),
    )


def _cite(rule: str, row: Mapping[str, Any] | None) -> str | None:
    evidence_id = row.get("evidence_id") if row is not None else None
    if not isinstance(evidence_id, str) or not evidence_id:
        return None
    return f"{rule}:{evidence_id}"


def _scope_leaves(phase: DeliveredPhase, path: str) -> frozenset[str] | None:
    catalog = _get(phase.policy_card, "scope_catalog", {})
    leaves = _get(catalog, "leaf_scopes", ())
    if not isinstance(leaves, Sequence) or isinstance(leaves, (str, bytes)):
        return None
    leaf_set = {x for x in leaves if isinstance(x, str)}
    if path == "/*":
        return frozenset(leaf_set)
    found = {leaf for leaf in leaf_set if leaf == path or leaf.startswith(path.rstrip("/") + "/")}
    return frozenset(found) if found else None


def _engagement_live(row: Mapping[str, Any], now: int) -> bool | None:
    status = row.get("eng_status")
    retro = row.get("retro_terminated_effective_s")
    if retro is not None and (not isinstance(retro, int) or now >= retro):
        return False
    if status == "ACTIVE":
        start, end = row.get("start_s"), row.get("end_s")
        if not isinstance(start, int):
            return None
        return start <= now and (end is None or (isinstance(end, int) and now < end))
    if status == "CONTRACTOR_TO_FTE_PENDING":
        start, deadline = row.get("conversion_start_s"), row.get("conversion_grace_deadline_s")
        if not isinstance(start, int) or not isinstance(deadline, int):
            return None
        # The rendered card's eng(p,t) predicate intentionally has no lower
        # bound for a pending conversion engagement.
        return now < deadline
    if status in {"TERMINATED", "FUTURE_HIRE"}:
        return False
    return None


def _person_engagements(phase: DeliveredPhase, person_id: str) -> list[dict[str, Any]] | None:
    person = _row(phase, "hr_persons", person_id)
    if person is None:
        return None
    engagements = person.get("engagements")
    if not isinstance(engagements, Sequence) or isinstance(engagements, (str, bytes)):
        return None
    return [dict(e) for e in engagements if isinstance(e, Mapping)]


def _bound_person(account_id: str, ledger_accounts: Mapping[str, Mapping[str, Any]]) -> str | None:
    outcome = ledger_accounts.get(account_id)
    value = outcome.get("bound_person_id") if outcome is not None else None
    return value if isinstance(value, str) else None


def _account_live(
    phase: DeliveredPhase,
    account_id: str,
    ledger_accounts: Mapping[str, Mapping[str, Any]],
) -> bool | None:
    outcome = ledger_accounts.get(account_id)
    row = _row(phase, "idp_accounts", account_id)
    if outcome is None or row is None:
        return None
    if outcome.get("p1_action") != "KEEP_ACTIVE" or row.get("status") != "ACTIVE":
        return False
    person_id = outcome.get("bound_person_id")
    engagement_id = outcome.get("bound_engagement_id")
    if not isinstance(person_id, str) or not isinstance(engagement_id, str):
        return None
    engagements = _person_engagements(phase, person_id)
    if engagements is None:
        return None
    match = next((e for e in engagements if e.get("eng_id") == engagement_id), None)
    if match is None:
        return None
    now = _get(phase.manifest, "clock_epoch_s")
    return _engagement_live(match, now) if isinstance(now, int) else None


def _manager_edges(phase: DeliveredPhase, person_id: str) -> set[str] | None:
    engagements = _person_engagements(phase, person_id)
    now = _get(phase.manifest, "clock_epoch_s")
    if engagements is None or not isinstance(now, int):
        return None
    managers: set[str] = set()
    for engagement in engagements:
        live = _engagement_live(engagement, now)
        if live is None:
            return None
        manager = engagement.get("manager_person_id")
        if live and isinstance(manager, str):
            managers.add(manager)
    return managers


def _is_subordinate(phase: DeliveredPhase, candidate: str, subject: str) -> bool | None:
    """Card definition: candidate reaches subject through live manager edges."""
    todo = [candidate]
    seen: set[str] = set()
    while todo:
        current = todo.pop()
        if current in seen:
            continue
        seen.add(current)
        managers = _manager_edges(phase, current)
        if managers is None:
            return None
        if subject in managers:
            return True
        todo.extend(sorted(managers - seen, reverse=True))
    return False


def _grant_state(
    ledger_accounts: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    standing: dict[str, set[str]] = {}
    eligible: dict[str, set[str]] = {}
    for account_id, outcome in ledger_accounts.items():
        standing[account_id] = set(outcome.get("effective_standing_grant_ids") or ())
        eligible[account_id] = set(outcome.get("effective_eligible_grant_ids") or ())
    return standing, eligible


def _apply_prior_uar(
    phase: DeliveredPhase,
    preceding: Sequence[Mapping[str, Any]],
    standing: dict[str, set[str]],
    eligible: dict[str, set[str]],
) -> None:
    campaigns = _table(phase, "uar_campaigns")
    for mutation in preceding:
        if mutation.get("mutation_kind") != "UAR_CAMPAIGN_MUTATION":
            continue
        campaign = campaigns.get(str(mutation.get("target_entity_id", "")))
        if campaign is None:
            continue
        item_accounts = {
            str(item.get("item_id")): str(item.get("account_id"))
            for item in campaign.get("items", ())
            if isinstance(item, Mapping)
        }
        for pair in mutation.get("uar_decisions", ()):
            if not isinstance(pair, Sequence) or len(pair) != 2:
                continue
            item_id, decision = str(pair[0]), pair[1]
            account_id = item_accounts.get(item_id)
            if account_id is None:
                continue
            item = next(
                (
                    x
                    for x in campaign.get("items", ())
                    if isinstance(x, Mapping) and x.get("item_id") == item_id
                ),
                None,
            )
            grant_id = str(item.get("grant_id")) if item else ""
            if decision in {"REVOKE", "ESCALATE"}:
                standing.setdefault(account_id, set()).discard(grant_id)
                eligible.setdefault(account_id, set()).discard(grant_id)
            elif decision == "CONVERT_TO_JIT":
                standing.setdefault(account_id, set()).discard(grant_id)
                eligible.setdefault(account_id, set()).add(grant_id)
        custodian_id = mutation.get("emergency_custodian_account_id")
        if isinstance(custodian_id, str):
            standing.setdefault(custodian_id, set()).add(
                f"cust__{mutation.get('target_entity_id')}__{custodian_id}"
            )


def _synthetic_custodian_grants(
    phase: DeliveredPhase,
    preceding: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    campaigns = _table(phase, "uar_campaigns")
    result: dict[str, dict[str, Any]] = {}
    for mutation in preceding:
        if mutation.get("mutation_kind") != "UAR_CAMPAIGN_MUTATION":
            continue
        campaign_id = str(mutation.get("target_entity_id", ""))
        account_id = mutation.get("emergency_custodian_account_id")
        if not isinstance(account_id, str):
            continue
        campaign = campaigns.get(campaign_id)
        if campaign is None:
            continue
        grant_id = f"cust__{campaign_id}__{account_id}"
        result[grant_id] = {
            "grant_id": grant_id,
            "account_id": account_id,
            "role_id": campaign.get("admin_role_id"),
            "grant_mode": "standing",
            "scope_path": campaign.get("scope_path"),
        }
    return result


def _shock_stripped_grants(
    phase: DeliveredPhase,
    ledger_accounts: Mapping[str, Mapping[str, Any]],
    standing: Mapping[str, set[str]],
    eligible: Mapping[str, set[str]],
    preceding: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[tuple[str, str], str]]:
    """Apply the public SHOCK-V5-01 grant-continuity rule to P1 grants."""
    stripped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    unresolved: dict[tuple[str, str], str] = {}
    work_queue = _get(phase.manifest, "work_queue", {})
    incident_ids = _get(work_queue, "target_cae_incident_ids", ())
    if not incident_ids:
        return stripped, unresolved
    incidents = _table(phase, "cae_incidents")
    all_grants_by_account = {
        account_id: {
            grant_id
            for grant_id in (
                set(standing.get(account_id, set())) | set(eligible.get(account_id, set()))
            )
            if not grant_id.startswith("cust__")
        }
        for account_id in ledger_accounts
    }
    if "cae_incidents" not in phase.tables and "cae_incidents" not in phase.table_rows:
        for account_id, grant_ids in all_grants_by_account.items():
            for grant_id in grant_ids:
                unresolved[(account_id, grant_id)] = (
                    "P2 incident rows needed for SHOCK-V5-01 are not selected."
                )
        return stripped, unresolved
    affected_people: set[str] = set()
    for incident_id in incident_ids:
        incident = incidents.get(str(incident_id))
        if incident is None or not isinstance(incident.get("person_id"), str):
            for account_id, grant_ids in all_grants_by_account.items():
                for grant_id in grant_ids:
                    unresolved[(account_id, grant_id)] = (
                        "A targeted incident row is missing its P1-bound person."
                    )
            continue
        affected_people.add(str(incident["person_id"]))
    if not affected_people:
        return stripped, unresolved

    now = _get(phase.manifest, "clock_epoch_s")
    if not isinstance(now, int):
        for account_id, outcome in ledger_accounts.items():
            if outcome.get("bound_person_id") in affected_people:
                for grant_id in all_grants_by_account.get(account_id, set()):
                    unresolved[(account_id, grant_id)] = (
                        "Phase clock is absent for shock bridge validity."
                    )
        return stripped, unresolved
    tainted_ids = {
        str(entity_id)
        for mutation in preceding
        if mutation.get("mutation_kind") == "CAE_CONTAINMENT_MUTATION"
        for entity_id in mutation.get("retro_invalidated_ids", ())
    }
    bridges = _table(phase, "transfer_bridges")
    bridges_available = "transfer_bridges" in phase.tables or "transfer_bridges" in phase.table_rows
    grants = _table(phase, "grants")
    roles = _table(phase, "roles")

    for account_id, outcome in ledger_accounts.items():
        person_id = outcome.get("bound_person_id")
        if person_id not in affected_people:
            continue
        grant_ids = all_grants_by_account.get(account_id, set())
        person = _row(phase, "hr_persons", str(person_id))
        if person is None:
            for grant_id in grant_ids:
                unresolved[(account_id, grant_id)] = "Incident person's current HR row is missing."
            continue
        bound_engagement_id = outcome.get("bound_engagement_id")
        engagements = person.get("engagements", ())
        bound = (
            next(
                (
                    e
                    for e in engagements
                    if isinstance(e, Mapping) and e.get("eng_id") == bound_engagement_id
                ),
                None,
            )
            if isinstance(engagements, Sequence)
            else None
        )
        if bound is None:
            for grant_id in grant_ids:
                unresolved[(account_id, grant_id)] = (
                    "Exact P1-bound engagement continuity is missing."
                )
            continue
        live = _account_live(phase, account_id, ledger_accounts)
        if live is None:
            for grant_id in grant_ids:
                unresolved[(account_id, grant_id)] = (
                    "Exact P1-bound engagement liveness is not fully visible."
                )
            continue
        if not live:
            continue
        retro_values = [
            e.get("retro_terminated_effective_s")
            for e in engagements
            if isinstance(e, Mapping) and e.get("retro_terminated_effective_s") is not None
        ]
        if any(not isinstance(x, int) for x in retro_values):
            for grant_id in grant_ids:
                unresolved[(account_id, grant_id)] = (
                    "An engagement retro-termination time is malformed."
                )
            continue
        t_retro = min(retro_values) if retro_values else None
        prior_dept = bound.get("prior_dept_path")
        transfer_at = bound.get("dept_transfer_effective_at_s")
        current_dept = bound.get("dept_path")
        if not isinstance(current_dept, str):
            for grant_id in grant_ids:
                unresolved[(account_id, grant_id)] = "Bound engagement department is missing."
            continue
        effective_dept = (
            prior_dept
            if isinstance(prior_dept, str) and isinstance(transfer_at, int) and now < transfer_at
            else current_dept
        )
        dept_leaves = _scope_leaves(phase, effective_dept)
        if dept_leaves is None:
            for grant_id in grant_ids:
                unresolved[(account_id, grant_id)] = (
                    "Effective department scope does not resolve in the delivered catalog."
                )
            continue

        for grant_id in grant_ids:
            grant = grants.get(grant_id)
            role = roles.get(str(grant.get("role_id", ""))) if grant else None
            grant_leaves = _scope_leaves(phase, str(grant.get("scope_path", ""))) if grant else None
            if grant is None or role is None or grant_leaves is None:
                unresolved[(account_id, grant_id)] = (
                    "P1 grant, role, or scope needed for shock continuity is missing."
                )
                continue
            if grant_leaves <= dept_leaves:
                continue
            if not bridges_available:
                unresolved[(account_id, grant_id)] = (
                    "Matching transfer-bridge rows are not selected for an out-of-department grant."
                )
                continue
            from_dept = bound.get("prior_dept_path")
            to_dept = bound.get("dept_path")
            if not isinstance(from_dept, str) or not isinstance(to_dept, str):
                stripped[(account_id, grant_id)] = []
                continue
            matching = [
                row
                for row in bridges.values()
                if row.get("account_id") == account_id
                and row.get("grant_id") == grant_id
                and row.get("from_dept_path") == from_dept
                and row.get("to_dept_path") == to_dept
                and row.get("target_resource") == role.get("app_id")
            ]
            if len(matching) > 1:
                unresolved[(account_id, grant_id)] = (
                    "Multiple exact bridge rows match this out-of-department grant."
                )
                continue
            bridge = matching[0] if matching else None
            if bridge is None:
                stripped[(account_id, grant_id)] = []
                continue
            approved_at, expires_at = bridge.get("approved_at_s"), bridge.get("expires_at_s")
            approved_by = bridge.get("approved_by_account_id")
            if (
                not isinstance(approved_at, int)
                or not isinstance(expires_at, int)
                or not isinstance(approved_by, str)
            ):
                unresolved[(account_id, grant_id)] = (
                    "Matching bridge approval or expiry fields are incomplete."
                )
                continue
            tainted = str(bridge.get("bridge_id", "")) in tainted_ids
            if t_retro is not None and approved_at >= t_retro:
                approver = ledger_accounts.get(approved_by)
                if approver is None:
                    unresolved[(account_id, grant_id)] = (
                        "Approver P1 binding needed for bridge taint is missing."
                    )
                    continue
                approver_live = _account_live(phase, approved_by, ledger_accounts)
                if approver_live is None:
                    unresolved[(account_id, grant_id)] = (
                        "Approver liveness needed for bridge taint is incomplete."
                    )
                    continue
                tainted = tainted or (
                    approver_live and approver.get("bound_person_id") == person_id
                )
            if not tainted and approved_at <= now < expires_at:
                continue
            stripped[(account_id, grant_id)] = [bridge]
    return stripped, unresolved


def _derive_uar(
    phase: DeliveredPhase,
    target_ids: Sequence[str],
    visible_prior_ledger: Mapping[str, Any],
    preceding: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mutations: list[dict[str, Any]] = []
    undefined: list[dict[str, Any]] = []
    ledger_accounts = _ledger_rows(visible_prior_ledger, "account_outcomes", "account_id")
    standing, eligible = _grant_state(ledger_accounts)
    shock_stripped, shock_unknown = _shock_stripped_grants(
        phase, ledger_accounts, standing, eligible, preceding
    )
    for account_id, grant_id in shock_stripped:
        standing.setdefault(account_id, set()).discard(grant_id)
        eligible.setdefault(account_id, set()).discard(grant_id)
    _apply_prior_uar(phase, preceding, standing, eligible)
    campaigns = _table(phase, "uar_campaigns")
    state_uncertain = False

    if phase.phase_index != 2:
        return [], [_undefined("*", "phase_index", "UAR is defined here only for Phase 2.")]

    for campaign_id in sorted(set(target_ids)):
        if state_uncertain:
            undefined.append(
                _undefined(
                    campaign_id,
                    "uar_decisions",
                    "An earlier campaign's unresolved outcome prevents applying this "
                    "campaign's sequential prefix.",
                )
            )
            continue
        campaign = campaigns.get(campaign_id)
        if campaign is None:
            undefined.append(
                _undefined(
                    campaign_id,
                    "uar_decisions",
                    "Campaign row is not in selected delivered tables.",
                )
            )
            state_uncertain = True
            continue
        for (account_id, grant_id), reason in shock_unknown.items():
            account_live = _account_live(phase, account_id, ledger_accounts)
            if account_live is False:
                continue
            if account_live is None:
                undefined.append(_undefined(campaign_id, "uar_decisions", reason))
                state_uncertain = True
                break
            grant = _row(phase, "grants", grant_id)
            role = _row(phase, "roles", str(grant.get("role_id", ""))) if grant else None
            if grant is None or role is None:
                undefined.append(_undefined(campaign_id, "uar_decisions", reason))
                state_uncertain = True
                break
            covered = _covered_for_campaign(phase, campaign, account_id, grant, role)
            if covered is None or covered:
                undefined.append(_undefined(campaign_id, "uar_decisions", reason))
                state_uncertain = True
                break
        if state_uncertain:
            continue
        campaign_token = _fixed_token(campaign_id, campaign)
        item_decisions: list[tuple[str, str]] = []
        reviewer_overrides: list[tuple[str, str]] = []
        cite_values: set[str] = set()
        pre_values: set[str] = set()
        local_undefined: list[dict[str, str]] = []
        if campaign_token is None:
            local_undefined.append(
                _undefined(campaign_id, "if_match", "Visible campaign etag is missing.")
            )
        campaign_cite = _cite("UAR-V5-01", campaign)
        if campaign_cite is None:
            local_undefined.append(
                _undefined(campaign_id, "cite", "Campaign evidence_id is absent.")
            )
        else:
            cite_values.add(campaign_cite)
        items = [dict(item) for item in campaign.get("items", ()) if isinstance(item, Mapping)]
        items.sort(key=lambda item: str(item.get("item_id", "")))
        if not items:
            local_undefined.append(
                _undefined(campaign_id, "uar_decisions", "The campaign has no visible item rows.")
            )

        # The item step follows the card's stated item_id order and the already
        # derived P2 prefix.  Only facts needed by a branch are consulted.
        for item in items:
            item_id = str(item.get("item_id", ""))
            account_id = str(item.get("account_id", ""))
            grant_id = str(item.get("grant_id", ""))
            item_cite = _cite("UAR-V5-01", item)
            if item_cite is None:
                local_undefined.append(
                    _undefined(
                        campaign_id, f"cite[{item_id}]", "Campaign item evidence_id is absent."
                    )
                )
            else:
                cite_values.add(item_cite)
            outcome = ledger_accounts.get(account_id)
            account_row = _row(phase, "idp_accounts", account_id)
            if outcome is None or account_row is None:
                local_undefined.append(
                    _undefined(
                        campaign_id,
                        f"uar_decisions[{item_id}]",
                        "P1 account outcome or visible account row is missing.",
                    )
                )
                continue
            person_id = _bound_person(account_id, ledger_accounts)
            if person_id is None:
                local_undefined.append(
                    _undefined(
                        campaign_id,
                        f"uar_decisions[{item_id}]",
                        "P1-bound person is missing for this campaign item.",
                    )
                )
                continue
            if (account_id, grant_id) in shock_unknown:
                local_undefined.append(
                    _undefined(
                        campaign_id,
                        f"uar_decisions[{item_id}]",
                        shock_unknown[(account_id, grant_id)],
                    )
                )
                continue
            for bridge in shock_stripped.get((account_id, grant_id), ()):
                bridge_id = str(bridge.get("bridge_id", ""))
                token = _fixed_token(bridge_id, bridge)
                if token:
                    pre_values.add(token)
                else:
                    local_undefined.append(
                        _undefined(
                            campaign_id,
                            f"if_match[{bridge_id}]",
                            "Shock-stripped bridge lacks a visible etag.",
                        )
                    )
                bridge_cite = _cite("UAR-V5-01", bridge)
                if bridge_cite:
                    cite_values.add(bridge_cite)
                else:
                    local_undefined.append(
                        _undefined(
                            campaign_id,
                            f"cite[{bridge_id}]",
                            "Shock-stripped bridge lacks evidence_id.",
                        )
                    )
            live = _account_live(phase, account_id, ledger_accounts)
            in_standing = grant_id in standing.get(account_id, set())
            in_eligible = grant_id in eligible.get(account_id, set())
            if live is None:
                local_undefined.append(
                    _undefined(
                        campaign_id,
                        f"uar_decisions[{item_id}]",
                        "The bound engagement needed for acctLive is not fully visible.",
                    )
                )
                continue
            if not live or not (in_standing or in_eligible):
                item_decisions.append((item_id, "REVOKE"))
                token = _person_token(phase, person_id)
                if token:
                    pre_values.add(token)
                else:
                    local_undefined.append(
                        _undefined(
                            campaign_id,
                            f"if_match[{person_id}]",
                            "Step-1 inactive bound person has no visible lifecycle version.",
                        )
                    )
                person_cite = _cite("UAR-V5-01", _row(phase, "hr_persons", person_id))
                if person_cite:
                    cite_values.add(person_cite)
                continue

            reviewer_id = item.get("assigned_reviewer_account_id")
            if not isinstance(reviewer_id, str):
                local_undefined.append(
                    _undefined(
                        campaign_id,
                        f"reviewer_overrides[{item_id}]",
                        "Assigned reviewer account is absent.",
                    )
                )
                continue
            subject = _bound_person(account_id, ledger_accounts)
            reviewer_person = _bound_person(reviewer_id, ledger_accounts)
            reviewer_live = _account_live(phase, reviewer_id, ledger_accounts)
            if subject is None or reviewer_live is None:
                local_undefined.append(
                    _undefined(
                        campaign_id,
                        f"reviewer_overrides[{item_id}]",
                        "Reviewer or subject P1 live binding is not fully visible.",
                    )
                )
                continue
            reviewer_valid = False
            if reviewer_live:
                if reviewer_person is None:
                    local_undefined.append(
                        _undefined(
                            campaign_id,
                            f"reviewer_overrides[{item_id}]",
                            "Active reviewer has no visible bound person.",
                        )
                    )
                    continue
                subordinate = _is_subordinate(phase, reviewer_person, subject)
                if subordinate is None:
                    local_undefined.append(
                        _undefined(
                            campaign_id,
                            f"reviewer_overrides[{item_id}]",
                            "Live manager edges needed for reviewer COI are not fully visible.",
                        )
                    )
                    continue
                reviewer_valid = reviewer_person != subject and not subordinate
            if not reviewer_valid:
                valid_backups: list[str] = []
                uncertain_backup = False
                for backup_id in campaign.get("backup_reviewer_account_ids", ()):
                    if not isinstance(backup_id, str):
                        continue
                    backup_person = _bound_person(backup_id, ledger_accounts)
                    backup_live = _account_live(phase, backup_id, ledger_accounts)
                    if backup_live is None:
                        uncertain_backup = True
                        continue
                    if not backup_live:
                        continue
                    if backup_person is None:
                        uncertain_backup = True
                        continue
                    backup_sub = _is_subordinate(phase, backup_person, subject)
                    if backup_sub is None:
                        uncertain_backup = True
                    elif backup_live and backup_person != subject and not backup_sub:
                        valid_backups.append(backup_id)
                if uncertain_backup:
                    local_undefined.append(
                        _undefined(
                            campaign_id,
                            f"reviewer_overrides[{item_id}]",
                            "A listed backup reviewer has unresolved active-binding "
                            "or COI evidence.",
                        )
                    )
                    continue
                if valid_backups:
                    chosen = min(valid_backups)
                    reviewer_overrides.append((item_id, chosen))
                    reviewer_valid = True
                    chosen_row = _row(phase, "idp_accounts", chosen)
                    chosen_person = _bound_person(chosen, ledger_accounts)
                    token = _account_token(phase, chosen, ledger_accounts)
                    if token:
                        pre_values.add(token)
                    else:
                        local_undefined.append(
                            _undefined(
                                campaign_id,
                                f"if_match[{chosen}]",
                                "Override reviewer lacks a visible LV token.",
                            )
                        )
                    if chosen_person:
                        person_token = _person_token(phase, chosen_person)
                        if person_token:
                            pre_values.add(person_token)
                        else:
                            local_undefined.append(
                                _undefined(
                                    campaign_id,
                                    f"if_match[{chosen_person}]",
                                    "Override reviewer person lacks a visible LV token.",
                                )
                            )
                    chosen_cite = _cite("UAR-V5-01", chosen_row)
                    if chosen_cite:
                        cite_values.add(chosen_cite)
                    if chosen_person:
                        person_cite = _cite("UAR-V5-01", _row(phase, "hr_persons", chosen_person))
                        if person_cite:
                            cite_values.add(person_cite)
                else:
                    item_decisions.append((item_id, "ESCALATE"))
                    continue

            # Standing lock is only relevant for unused standing items.
            if item.get("used_within_lookback") is True:
                item_decisions.append((item_id, "CERTIFY"))
                continue
            if in_standing:
                grant = _row(phase, "grants", grant_id)
                role = _row(phase, "roles", str(grant.get("role_id", ""))) if grant else None
                if grant is None or role is None:
                    local_undefined.append(
                        _undefined(
                            campaign_id,
                            f"uar_decisions[{item_id}]",
                            "Unused standing-item role/scope needed by the lock and "
                            "JIT-capability checks is missing.",
                        )
                    )
                    continue
                if not isinstance(role.get("is_jit_capable_target"), bool):
                    local_undefined.append(
                        _undefined(
                            campaign_id,
                            f"uar_decisions[{item_id}]",
                            "Role JIT-capability is not visible.",
                        )
                    )
                    continue
                locked = _standing_item_locked(phase, account_id, grant, role)
                if locked is None:
                    local_undefined.append(
                        _undefined(
                            campaign_id,
                            f"uar_decisions[{item_id}]",
                            "Raw ALLOW group and DENY footprints needed for the "
                            "standing lock are not fully visible.",
                        )
                    )
                    continue
                if locked:
                    account_groups = _visible_group_rows(phase, account_id)
                    if account_groups is None:
                        local_undefined.append(
                            _undefined(
                                campaign_id,
                                f"cite[{item_id}]",
                                "Locking-group evidence rows are not fully visible.",
                            )
                        )
                        continue
                    cite_values.update(
                        cite
                        for cite in (_cite("UAR-V5-01", group) for group in account_groups)
                        if cite is not None
                    )
                    item_decisions.append((item_id, "ESCALATE"))
                else:
                    item_decisions.append(
                        (item_id, "CONVERT_TO_JIT" if role["is_jit_capable_target"] else "REVOKE")
                    )
            elif in_eligible:
                item_decisions.append((item_id, "REVOKE"))
            else:
                local_undefined.append(
                    _undefined(
                        campaign_id, f"uar_decisions[{item_id}]", "Live grant mode is unresolved."
                    )
                )

        # Do not invent a floor promotion or emergency custodian when the
        # visible item outcomes do not already prove both requested floors.
        # This gate only asks for coverage of the visible, certified items; a
        # complete campaign-wide count is below when it can be established.
        emergency_custodian: str | None = None
        if not local_undefined:
            floor_state = _visible_campaign_floors(
                phase,
                campaign,
                items,
                [*preceding, *mutations],
                item_decisions,
                ledger_accounts,
                standing,
                eligible,
            )
            (
                floors_ok,
                floor_reason,
                needs_standing,
                needs_eligible,
                stand_people,
                eligible_people,
            ) = floor_state
            if floors_ok is None:
                local_undefined.append(
                    _undefined(
                        campaign_id,
                        "uar_decisions",
                        floor_reason or "Campaign floor cannot be evaluated from visible data.",
                    )
                )
            else:
                decisions_by_item = dict(item_decisions)
                # Floor-preserving promotions are part of UAR-V5-01.  They
                # apply only to unused, active, covered items that were
                # otherwise on the ordinary convert/revoke path.
                if needs_standing:
                    for item in items:
                        item_id = str(item.get("item_id", ""))
                        account_id = str(item.get("account_id", ""))
                        grant_id = str(item.get("grant_id", ""))
                        person_id = _bound_person(account_id, ledger_accounts)
                        if (
                            person_id is None
                            or _account_live(phase, account_id, ledger_accounts) is not True
                            or item.get("used_within_lookback") is True
                            or decisions_by_item.get(item_id) not in {"CONVERT_TO_JIT", "REVOKE"}
                            or grant_id not in standing.get(account_id, set())
                            or person_id in stand_people
                        ):
                            continue
                        grant = _row(phase, "grants", grant_id)
                        role = (
                            _row(phase, "roles", str(grant.get("role_id", ""))) if grant else None
                        )
                        if grant is None or role is None:
                            local_undefined.append(
                                _undefined(
                                    campaign_id,
                                    f"uar_decisions[{item_id}]",
                                    "Standing-floor promotion needs the visible grant and role.",
                                )
                            )
                            continue
                        covered = _covered_for_campaign(phase, campaign, account_id, grant, role)
                        if covered is None:
                            local_undefined.append(
                                _undefined(
                                    campaign_id,
                                    f"uar_decisions[{item_id}]",
                                    "Standing-floor promotion coverage is not fully visible.",
                                )
                            )
                            continue
                        if covered:
                            decisions_by_item[item_id] = "CERTIFY"
                            stand_people.add(person_id)
                            needs_standing -= 1
                            if not needs_standing:
                                break
                item_decisions = sorted(decisions_by_item.items())
                floor_state = _visible_campaign_floors(
                    phase,
                    campaign,
                    items,
                    [*preceding, *mutations],
                    item_decisions,
                    ledger_accounts,
                    standing,
                    eligible,
                )
                (
                    floors_ok,
                    floor_reason,
                    needs_standing,
                    needs_eligible,
                    stand_people,
                    eligible_people,
                ) = floor_state
                if floors_ok is None:
                    local_undefined.append(
                        _undefined(
                            campaign_id,
                            "uar_decisions",
                            floor_reason
                            or "Campaign floor cannot be evaluated after standing promotions.",
                        )
                    )
                if needs_eligible:
                    for item in items:
                        item_id = str(item.get("item_id", ""))
                        account_id = str(item.get("account_id", ""))
                        grant_id = str(item.get("grant_id", ""))
                        person_id = _bound_person(account_id, ledger_accounts)
                        if (
                            person_id is None
                            or _account_live(phase, account_id, ledger_accounts) is not True
                            or item.get("used_within_lookback") is True
                            or decisions_by_item.get(item_id) != "REVOKE"
                            or grant_id not in eligible.get(account_id, set())
                            or person_id in eligible_people
                        ):
                            continue
                        grant = _row(phase, "grants", grant_id)
                        role = (
                            _row(phase, "roles", str(grant.get("role_id", ""))) if grant else None
                        )
                        if grant is None or role is None:
                            local_undefined.append(
                                _undefined(
                                    campaign_id,
                                    f"uar_decisions[{item_id}]",
                                    "Eligible-floor promotion needs the visible grant and role.",
                                )
                            )
                            continue
                        covered = _covered_for_campaign(phase, campaign, account_id, grant, role)
                        if covered is None:
                            local_undefined.append(
                                _undefined(
                                    campaign_id,
                                    f"uar_decisions[{item_id}]",
                                    "Eligible-floor promotion coverage is not fully visible.",
                                )
                            )
                            continue
                        if covered:
                            decisions_by_item[item_id] = "CERTIFY"
                            eligible_people.add(person_id)
                            needs_eligible -= 1
                            if not needs_eligible:
                                break
                item_decisions = sorted(decisions_by_item.items())
                floor_state = _visible_campaign_floors(
                    phase,
                    campaign,
                    items,
                    [*preceding, *mutations],
                    item_decisions,
                    ledger_accounts,
                    standing,
                    eligible,
                )
                (
                    floors_ok,
                    floor_reason,
                    needs_standing,
                    needs_eligible,
                    stand_people,
                    eligible_people,
                ) = floor_state
                if floors_ok is None:
                    local_undefined.append(
                        _undefined(
                            campaign_id,
                            "uar_decisions",
                            floor_reason
                            or "Campaign floor cannot be evaluated after eligible promotions.",
                        )
                    )
                elif needs_eligible:
                    local_undefined.append(
                        _undefined(
                            campaign_id,
                            "uar_decisions",
                            "The required eligible floor remains short after visible "
                            "covered eligible-item promotions.",
                        )
                    )
                elif needs_standing:
                    if needs_standing > 1:
                        local_undefined.append(
                            _undefined(
                                campaign_id,
                                "emergency_custodian_account_id",
                                "The visible standing-floor deficit exceeds the single "
                                "custodian field supported by the mutation schema.",
                            )
                        )
                    else:
                        emergency_custodian, custodian_pre, custodian_cite, reason = (
                            _select_custodian(
                                phase,
                                campaign,
                                stand_people,
                                ledger_accounts,
                                standing,
                                item_decisions,
                                items,
                                [*preceding, *mutations],
                                visible_prior_ledger,
                            )
                        )
                        if emergency_custodian is None:
                            local_undefined.append(
                                _undefined(
                                    campaign_id,
                                    "emergency_custodian_account_id",
                                    reason or "No uniquely eligible custodian is derivable.",
                                )
                            )
                        else:
                            pre_values.update(custodian_pre)
                            cite_values.update(custodian_cite)

        if campaign_token is not None:
            pre_values.add(campaign_token)
        if local_undefined:
            undefined.extend(local_undefined)
            state_uncertain = True
            continue
        mutations.append(
            {
                "target_entity_id": campaign_id,
                "mutation_kind": "UAR_CAMPAIGN_MUTATION",
                "if_match": sorted(pre_values),
                "cite": sorted(cite_values),
                "uar_decisions": [list(pair) for pair in sorted(item_decisions)],
                "reviewer_overrides": [list(pair) for pair in sorted(reviewer_overrides)],
                "emergency_custodian_account_id": emergency_custodian,
            }
        )
        # Apply this campaign immediately, as required by UAR-V5-01.
        _apply_prior_uar(phase, [mutations[-1]], standing, eligible)
    if not undefined:
        undefined.extend(
            _post_uar_self_sod(
                phase,
                ledger_accounts,
                standing,
                [*preceding, *mutations],
                visible_prior_ledger,
                sorted(set(target_ids))[-1] if target_ids else "*",
            )
        )
    return mutations, undefined


def _visible_campaign_floors(
    phase: DeliveredPhase,
    campaign: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
    preceding: Sequence[Mapping[str, Any]],
    decisions: Sequence[tuple[str, str]],
    ledger_accounts: Mapping[str, Mapping[str, Any]],
    standing: Mapping[str, set[str]],
    eligible: Mapping[str, set[str]],
) -> tuple[bool | None, str | None, int, int, set[str], set[str]]:
    """Count unreviewed and campaign-item people for each visible floor."""
    decision_by_item = dict(decisions)
    reviewed_grants = {str(item.get("grant_id", "")) for item in items}
    stand_people, eligible_people, base_known = _unreviewed_covered_people(
        phase,
        campaign,
        reviewed_grants,
        ledger_accounts,
        standing,
        eligible,
        _synthetic_custodian_grants(phase, preceding),
    )
    if not base_known:
        return (
            None,
            "Unreviewed active covered grants are not fully recoverable from selected rows.",
            0,
            0,
            stand_people,
            eligible_people,
        )
    for item in items:
        item_id = str(item.get("item_id", ""))
        account_id = str(item.get("account_id", ""))
        grant_id = str(item.get("grant_id", ""))
        action = decision_by_item.get(item_id)
        if action not in {"CERTIFY", "CONVERT_TO_JIT"}:
            continue
        outcome = ledger_accounts.get(account_id)
        person_id = _bound_person(account_id, ledger_accounts)
        if outcome is None or person_id is None:
            return (
                None,
                "Campaign-floor bound person is missing from the visible ledger.",
                0,
                0,
                stand_people,
                eligible_people,
            )
        grant = _row(phase, "grants", grant_id) or _synthetic_custodian_grants(
            phase, preceding
        ).get(grant_id)
        role = _row(phase, "roles", str(grant.get("role_id", ""))) if grant else None
        if grant is None or role is None:
            return (
                None,
                "Campaign-floor grant or role row is not selected.",
                0,
                0,
                stand_people,
                eligible_people,
            )
        covered = _covered_for_campaign(phase, campaign, account_id, grant, role)
        if covered is None:
            return (
                None,
                "Group rows needed to establish item coverage are not fully visible.",
                0,
                0,
                stand_people,
                eligible_people,
            )
        if not covered:
            continue
        if action == "CERTIFY":
            if grant_id in standing.get(account_id, set()):
                stand_people.add(person_id)
            elif grant_id in eligible.get(account_id, set()):
                eligible_people.add(person_id)
        elif action == "CONVERT_TO_JIT":
            eligible_people.add(person_id)
    m_standing = campaign.get("m_standing")
    m_eligible = campaign.get("m_eligible")
    if not isinstance(m_standing, int) or not isinstance(m_eligible, int):
        return None, "Campaign floor counts are absent.", 0, 0, stand_people, eligible_people
    override = campaign.get("preserve_standing_floor_override")
    if override == "USE_OVERLAY_DEFAULT":
        default = _get(
            _get(phase.policy_card, "overlay", {}), "uar_preserve_standing_floor_default"
        )
        if not isinstance(default, bool):
            return (
                None,
                "Overlay standing-floor default is absent.",
                0,
                0,
                stand_people,
                eligible_people,
            )
        preserve_standing = default
    elif override in {True, "TRUE"}:
        preserve_standing = True
    elif override in {False, "FALSE"}:
        preserve_standing = False
    else:
        return (
            None,
            "Standing-floor override is not a visible card value.",
            0,
            0,
            stand_people,
            eligible_people,
        )
    if not preserve_standing:
        m_standing = 0
    needs_standing = max(m_standing - len(stand_people), 0)
    needs_eligible = max(m_eligible - len(eligible_people), 0)
    return (
        not (needs_standing or needs_eligible),
        None,
        needs_standing,
        needs_eligible,
        stand_people,
        eligible_people,
    )


def _unreviewed_covered_people(
    phase: DeliveredPhase,
    campaign: Mapping[str, Any],
    reviewed_grants: set[str],
    ledger_accounts: Mapping[str, Mapping[str, Any]],
    standing: Mapping[str, set[str]],
    eligible: Mapping[str, set[str]],
    synthetic_grants: Mapping[str, Mapping[str, Any]],
) -> tuple[set[str], set[str], bool]:
    stand_people: set[str] = set()
    eligible_people: set[str] = set()
    grants = _table(phase, "grants")
    for account_id, outcome in ledger_accounts.items():
        if outcome.get("p1_action") != "KEEP_ACTIVE":
            continue
        live = _account_live(phase, account_id, ledger_accounts)
        person_id = _bound_person(account_id, ledger_accounts)
        if live is None or person_id is None:
            return stand_people, eligible_people, False
        if not live:
            continue
        for grant_id in sorted(standing.get(account_id, set()) | eligible.get(account_id, set())):
            if grant_id in reviewed_grants:
                continue
            grant = grants.get(grant_id) or synthetic_grants.get(grant_id)
            role = _row(phase, "roles", str(grant.get("role_id", ""))) if grant else None
            if grant is None or role is None:
                return stand_people, eligible_people, False
            covered = _covered_for_campaign(phase, campaign, account_id, grant, role)
            if covered is None:
                return stand_people, eligible_people, False
            if covered:
                if grant_id in standing.get(account_id, set()):
                    stand_people.add(person_id)
                else:
                    eligible_people.add(person_id)
    return stand_people, eligible_people, True


def _covered_for_campaign(
    phase: DeliveredPhase,
    campaign: Mapping[str, Any],
    account_id: str,
    grant: Mapping[str, Any],
    role: Mapping[str, Any],
) -> bool | None:
    if role.get("app_id") != campaign.get("app_id") or role.get("privilege_tier") not in {
        "admin",
        "critical",
    }:
        return False
    admin_role = _row(phase, "roles", str(campaign.get("admin_role_id", "")))
    if admin_role is None:
        return None
    campaign_caps = admin_role.get("capabilities", ())
    grant_caps = role.get("capabilities", ())
    camp_leaves = _scope_leaves(phase, str(campaign.get("scope_path", "")))
    grant_leaves = _scope_leaves(phase, str(grant.get("scope_path", "")))
    if camp_leaves is None or grant_leaves is None:
        return None
    if _visible_group_rows(phase, account_id) is None:
        return None
    required = {(cap, leaf) for cap in campaign_caps for leaf in camp_leaves}
    effective = {(cap, leaf) for cap in grant_caps for leaf in grant_leaves}
    denied = _group_effects(phase, account_id)[1]
    return required <= (effective - denied)


def _select_custodian(
    phase: DeliveredPhase,
    campaign: Mapping[str, Any],
    standing_people: set[str],
    ledger_accounts: Mapping[str, Mapping[str, Any]],
    standing: Mapping[str, set[str]],
    decisions: Sequence[tuple[str, str]],
    items: Sequence[Mapping[str, Any]],
    preceding: Sequence[Mapping[str, Any]],
    visible_prior_ledger: Mapping[str, Any],
) -> tuple[str | None, set[str], set[str], str | None]:
    """Select the minimum visible custodian satisfying the UAR card gates."""
    candidates = campaign.get("candidate_custodian_account_ids")
    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes)):
        return None, set(), set(), "Custodian candidate list is missing."
    admin_role = _row(phase, "roles", str(campaign.get("admin_role_id", "")))
    campaign_leaves = _scope_leaves(phase, str(campaign.get("scope_path", "")))
    if admin_role is None or campaign_leaves is None:
        return None, set(), set(), "Custodian admin role or campaign scope is not visible."
    freq = {(cap, leaf) for cap in admin_role.get("capabilities", ()) for leaf in campaign_leaves}
    overlay = _get(phase.policy_card, "overlay", {})
    check_nhi = _get(overlay, "enforce_cross_nhi_owner_sod") is True
    check_sponsor = _get(overlay, "enforce_sponsor_agent_sod") is True
    planned_standing = {key: set(value) for key, value in standing.items()}
    for pair in decisions:
        if len(pair) != 2:
            continue
        item_id, action = pair
        item = next((x for x in items if str(x.get("item_id", "")) == item_id), None)
        if item is None:
            continue
        account_id = str(item.get("account_id", ""))
        grant_id = str(item.get("grant_id", ""))
        if action in {"REVOKE", "ESCALATE"} or action == "CONVERT_TO_JIT":
            planned_standing.setdefault(account_id, set()).discard(grant_id)

    valid: list[tuple[str, str]] = []
    uncertain: list[str] = []
    for account_id in sorted({x for x in candidates if isinstance(x, str)}):
        outcome = ledger_accounts.get(account_id)
        person_id = _bound_person(account_id, ledger_accounts)
        live = _account_live(phase, account_id, ledger_accounts)
        account = _row(phase, "idp_accounts", account_id)
        if outcome is None or account is None or live is None:
            uncertain.append(account_id)
            continue
        if not live or person_id is None or person_id in standing_people:
            continue
        if _visible_group_rows(phase, account_id) is None:
            uncertain.append(account_id)
            continue
        _, denied = _group_effects(phase, account_id)
        if freq & denied:
            continue
        work, work_known = _person_work_footprint(
            phase, person_id, ledger_accounts, planned_standing, preceding
        )
        if not work_known:
            uncertain.append(account_id)
            continue
        if _has_sod_conflict(phase, freq, work):
            continue
        if check_nhi:
            nhi_footprint, nhi_known = _nhi_owner_footprint(phase, person_id, visible_prior_ledger)
            if not nhi_known:
                uncertain.append(account_id)
                continue
            if _has_sod_conflict(phase, freq, nhi_footprint):
                continue
        if check_sponsor:
            sponsor_footprint, sponsor_known = _sponsor_agent_footprint(
                phase, person_id, visible_prior_ledger
            )
            if not sponsor_known:
                uncertain.append(account_id)
                continue
            if _has_sod_conflict(phase, freq, sponsor_footprint):
                continue
        valid.append((account_id, person_id))

    if valid:
        chosen_id, chosen_person = min(valid, key=lambda x: x[0])
        if any(unknown <= chosen_id for unknown in uncertain):
            return (
                None,
                set(),
                set(),
                "An unresolved lower-sorted candidate could change the minimum custodian.",
            )
        account_token = _account_token(phase, chosen_id, ledger_accounts)
        person_token = _person_token(phase, chosen_person)
        if account_token is None or person_token is None:
            return (
                None,
                set(),
                set(),
                "Selected custodian account or person lacks a visible LV token.",
            )
        account_cite = _cite("UAR-V5-01", _row(phase, "idp_accounts", chosen_id))
        person_cite = _cite("UAR-V5-01", _row(phase, "hr_persons", chosen_person))
        role_cite = _cite("UAR-V5-01", admin_role)
        if not account_cite or not person_cite or not role_cite:
            return (
                None,
                set(),
                set(),
                "Selected custodian account, person, or admin role lacks citation evidence.",
            )
        return (
            chosen_id,
            {account_token, person_token},
            {account_cite, person_cite, role_cite},
            None,
        )
    if uncertain:
        return (
            None,
            set(),
            set(),
            "Custodian eligibility depends on unresolved visible inputs: "
            + ", ".join(sorted(uncertain)),
        )
    return None, set(), set(), "No listed custodian satisfies the visible UAR eligibility gates."


def _person_work_footprint(
    phase: DeliveredPhase,
    person_id: str,
    ledger_accounts: Mapping[str, Mapping[str, Any]],
    standing: Mapping[str, set[str]],
    preceding: Sequence[Mapping[str, Any]],
) -> tuple[set[tuple[str, str]], bool]:
    footprint: set[tuple[str, str]] = set()
    grants = _table(phase, "grants")
    synthetic_grants = _synthetic_custodian_grants(phase, preceding)
    roles = _table(phase, "roles")
    for account_id, outcome in ledger_accounts.items():
        if outcome.get("p1_action") != "KEEP_ACTIVE" or outcome.get("bound_person_id") != person_id:
            continue
        live = _account_live(phase, account_id, ledger_accounts)
        account = _row(phase, "idp_accounts", account_id)
        if live is None or account is None or _visible_group_rows(phase, account_id) is None:
            return set(), False
        if not live:
            continue
        allow, denied = _group_effects(phase, account_id)
        footprint.update(allow - denied)
        for grant_id in standing.get(account_id, set()):
            grant = grants.get(grant_id) or synthetic_grants.get(grant_id)
            role = roles.get(str(grant.get("role_id", ""))) if grant else None
            leaves = _scope_leaves(phase, str(grant.get("scope_path", ""))) if grant else None
            if grant is None or role is None or leaves is None:
                return set(), False
            footprint.update(
                {(cap, leaf) for cap in role.get("capabilities", ()) for leaf in leaves} - denied
            )
    return footprint, True


def _nhi_owner_footprint(
    phase: DeliveredPhase,
    person_id: str,
    ledger: Mapping[str, Any],
) -> tuple[set[tuple[str, str]], bool]:
    footprint: set[tuple[str, str]] = set()
    if "baseline_nhis" not in phase.tables and "baseline_nhis" not in phase.table_rows:
        return footprint, False
    if "target_nhis" not in phase.tables and "target_nhis" not in phase.table_rows:
        return footprint, False
    for row in _table(phase, "baseline_nhis").values():
        if row.get("status") != "ACTIVE" or row.get("owner_person_id") != person_id:
            continue
        leaves = _scope_leaves(phase, str(row.get("scope_path", "/*")))
        if leaves is None:
            return set(), False
        footprint.update((cap, leaf) for cap in row.get("capabilities", ()) for leaf in leaves)
    for outcome in _as_rows(ledger.get("nhi_outcomes"), "nhi_id"):
        if (
            outcome.get("p1_nhi_action") != "ATTRIBUTE_OWNER"
            or outcome.get("resolved_owner_person_id") != person_id
        ):
            continue
        row = _row(phase, "target_nhis", str(outcome.get("nhi_id", "")))
        if row is None:
            return set(), False
        leaves = _scope_leaves(phase, str(row.get("scope_path", "")))
        if leaves is None:
            return set(), False
        footprint.update((cap, leaf) for cap in row.get("capabilities", ()) for leaf in leaves)
    return footprint, True


def _sponsor_agent_footprint(
    phase: DeliveredPhase,
    person_id: str,
    ledger: Mapping[str, Any],
) -> tuple[set[tuple[str, str]], bool]:
    footprint: set[tuple[str, str]] = set()
    if "baseline_agents" not in phase.tables and "baseline_agents" not in phase.table_rows:
        return footprint, False
    if "enterprise_agents" not in phase.tables and "enterprise_agents" not in phase.table_rows:
        return footprint, False
    for row in _table(phase, "baseline_agents").values():
        if row.get("status") != "ACTIVE" or row.get("sponsor_person_id") != person_id:
            continue
        leaves = _scope_leaves(phase, str(row.get("scope_path", "/*")))
        if leaves is None:
            return set(), False
        footprint.update(
            (cap, leaf) for cap in row.get("autonomous_capabilities", ()) for leaf in leaves
        )
    agents = _table(phase, "enterprise_agents")
    for outcome in _as_rows(ledger.get("agent_outcomes"), "agent_id"):
        if (
            outcome.get("p1_agent_action") != "REGISTER_ACTIVE"
            or outcome.get("resolved_sponsor_person_id") != person_id
        ):
            continue
        row = agents.get(str(outcome.get("agent_id", "")))
        if row is None:
            return set(), False
        active = set(outcome.get("active_autonomous_binding_ids") or ())
        for binding in row.get("tool_bindings", ()):
            if not isinstance(binding, Mapping) or binding.get("binding_id") not in active:
                continue
            leaves = _scope_leaves(phase, str(binding.get("scope_path", "")))
            if leaves is None:
                return set(), False
            footprint.update(
                (cap, leaf) for cap in binding.get("capabilities", ()) for leaf in leaves
            )
    return footprint, True


def _visible_group_rows(phase: DeliveredPhase, account_id: str) -> list[dict[str, Any]] | None:
    account = _row(phase, "idp_accounts", account_id)
    groups = _table(phase, "groups")
    roles = _table(phase, "roles")
    if account is None:
        return None
    queue = [str(x) for x in account.get("group_ids", ())]
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    while queue:
        group_id = queue.pop(0)
        if group_id in seen:
            continue
        seen.add(group_id)
        group = groups.get(group_id)
        if group is None:
            return None
        result.append(group)
        for role_id in group.get("granted_role_ids", ()):
            if str(role_id) not in roles:
                return None
        queue.extend(str(x) for x in group.get("member_of_group_ids", ()))
    return result


def _post_uar_self_sod(
    phase: DeliveredPhase,
    ledger_accounts: Mapping[str, Mapping[str, Any]],
    standing: Mapping[str, set[str]],
    prefix: Sequence[Mapping[str, Any]],
    visible_prior_ledger: Mapping[str, Any],
    target_id: str,
) -> list[dict[str, str]]:
    """Recompute live controls and the card's final zero-self-W invariant."""
    active_people: set[str] = set()
    for account_id, outcome in ledger_accounts.items():
        if outcome.get("p1_action") != "KEEP_ACTIVE":
            continue
        person_id = outcome.get("bound_person_id")
        live = _account_live(phase, account_id, ledger_accounts)
        if live is None:
            return [
                _undefined(
                    target_id,
                    f"post_uar_self_w[{account_id}]",
                    "P1-bound engagement needed to determine active post-UAR "
                    "accounts is incomplete.",
                )
            ]
        if live:
            if not isinstance(person_id, str):
                return [
                    _undefined(
                        target_id,
                        f"post_uar_self_w[{account_id}]",
                        "A live P1 account has no visible bound person.",
                    )
                ]
            active_people.add(person_id)

    undefined: list[dict[str, str]] = []
    for person_id in sorted(active_people):
        footprint, known = _person_work_footprint(
            phase, person_id, ledger_accounts, standing, prefix
        )
        if not known:
            undefined.append(
                _undefined(
                    target_id,
                    f"post_uar_self_w[{person_id}]",
                    "Post-UAR Kstand for this active person is not fully recoverable "
                    "from delivered rows.",
                )
            )
            continue
        witnesses = _self_sod_witnesses(phase, footprint)
        if not witnesses:
            continue
        mitigated, control_unknown = _mitigated_self_sod_witnesses(
            phase,
            person_id,
            witnesses,
            ledger_accounts,
            standing,
            visible_prior_ledger,
            prefix,
        )
        if control_unknown:
            undefined.append(
                _undefined(
                    target_id,
                    f"post_uar_self_w[{person_id}]",
                    "A potentially mitigating compensating control lacks enough "
                    "delivered data to recompute C_live.",
                )
            )
        elif not mitigated:
            undefined.append(
                _undefined(
                    target_id,
                    f"post_uar_self_w[{person_id}]",
                    "The uniquely derived post-UAR footprint has an unmitigated "
                    "self-W witness; the card gives no post-check repair step.",
                )
            )
    return undefined


def _self_sod_witnesses(
    phase: DeliveredPhase, footprint: set[tuple[str, str]]
) -> list[tuple[str, str, str, str, str]]:
    """Return sorted (cap_a, cap_b, mode, leaf_a, leaf_b) self-W witnesses."""
    overlay = _get(phase.policy_card, "overlay", {})
    rules = _get(overlay, "forbidden_sod_rules", ())
    witnesses: set[tuple[str, str, str, str, str]] = set()
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        cap_a, cap_b, mode = (
            rule.get("cap_a"),
            rule.get("cap_b"),
            rule.get("scope_mode"),
        )
        if (
            not isinstance(cap_a, str)
            or not isinstance(cap_b, str)
            or cap_a == cap_b
            or mode not in {"GLOBAL", "SAME_LEAF_SCOPE"}
        ):
            continue
        leaves_a = {leaf for cap, leaf in footprint if cap == cap_a}
        leaves_b = {leaf for cap, leaf in footprint if cap == cap_b}
        if mode == "GLOBAL":
            witnesses.update(
                (cap_a, cap_b, mode, leaf_a, leaf_b) for leaf_a in leaves_a for leaf_b in leaves_b
            )
        else:
            witnesses.update((cap_a, cap_b, mode, leaf, leaf) for leaf in leaves_a & leaves_b)
    return sorted(witnesses)


def _mitigated_self_sod_witnesses(
    phase: DeliveredPhase,
    person_id: str,
    witnesses: Sequence[tuple[str, str, str, str, str]],
    ledger_accounts: Mapping[str, Mapping[str, Any]],
    standing: Mapping[str, set[str]],
    visible_prior_ledger: Mapping[str, Any],
    prefix: Sequence[Mapping[str, Any]],
) -> tuple[bool, bool]:
    """Evaluate card-defined C_live mitigation for final self-W witnesses."""
    controls_present = (
        "compensating_controls" in phase.tables or "compensating_controls" in phase.table_rows
    )
    if not controls_present:
        return False, True

    controls = list(_table(phase, "compensating_controls").values())
    any_unknown = False
    for cap_a, cap_b, mode, leaf_a, leaf_b in witnesses:
        matched_live = False
        witness_unknown = False
        for control in controls:
            pair = {control.get("cap_a"), control.get("cap_b")}
            if control.get("monitored_person_id") != person_id or pair != {cap_a, cap_b}:
                continue
            covered_path = control.get("covered_scope_path")
            if not isinstance(covered_path, str):
                witness_unknown = True
                continue
            covered = _scope_leaves(phase, covered_path)
            if covered is None:
                witness_unknown = True
                continue
            if mode == "GLOBAL":
                all_leaves = _scope_leaves(phase, "/*")
                if covered_path != "/*" or all_leaves is None or covered != all_leaves:
                    continue
            elif leaf_a not in covered or leaf_b not in covered:
                continue
            live = _control_live(
                phase,
                control,
                person_id,
                ledger_accounts,
                standing,
                visible_prior_ledger,
                prefix,
            )
            if live is True:
                matched_live = True
                break
            if live is None:
                witness_unknown = True
        if not matched_live:
            if witness_unknown:
                any_unknown = True
            else:
                return False, any_unknown
    return not any_unknown, any_unknown


def _control_live(
    phase: DeliveredPhase,
    control: Mapping[str, Any],
    subject_person_id: str,
    ledger_accounts: Mapping[str, Mapping[str, Any]],
    standing: Mapping[str, set[str]],
    visible_prior_ledger: Mapping[str, Any],
    prefix: Sequence[Mapping[str, Any]],
) -> bool | None:
    monitor_account_id = control.get("monitor_account_id")
    audit_connector_id = control.get("audit_connector_id")
    required_capability = control.get("required_monitor_capability")
    covered_path = control.get("covered_scope_path")
    if not all(
        isinstance(value, str)
        for value in (
            monitor_account_id,
            audit_connector_id,
            required_capability,
            covered_path,
        )
    ):
        return None
    now = _get(phase.manifest, "clock_epoch_s")
    overlay = _get(phase.policy_card, "overlay", {})
    sla = _get(overlay, "freshness_sla_s")
    schema = _row(phase, "connector_schemas", audit_connector_id)
    outcomes = _ledger_rows(visible_prior_ledger, "connector_outcomes", "connector_id")
    if schema is None or not isinstance(now, int) or not isinstance(sla, int):
        return None
    watermark = schema.get("audit_watermark_s")
    connector_outcome = outcomes.get(audit_connector_id)
    if not isinstance(watermark, int) or connector_outcome is None:
        return None
    healthy_audit = (
        not connector_outcome.get("halted_fail_closed", True) and 0 <= now - watermark <= sla
    )
    if not healthy_audit:
        return False

    monitor_live = _account_live(phase, monitor_account_id, ledger_accounts)
    monitor_person = _bound_person(monitor_account_id, ledger_accounts)
    if monitor_live is None or monitor_person is None:
        return None
    if not monitor_live or monitor_person == subject_person_id:
        return False
    subordinate = _is_subordinate(phase, monitor_person, subject_person_id)
    if subordinate is None:
        return None
    if subordinate:
        return False
    required_leaves = _scope_leaves(phase, covered_path)
    monitor_footprint, known = _account_standing_footprint(
        phase, monitor_account_id, ledger_accounts, standing, prefix
    )
    if required_leaves is None or not known:
        return None
    required = {(required_capability, leaf) for leaf in required_leaves}
    return required <= monitor_footprint


def _account_standing_footprint(
    phase: DeliveredPhase,
    account_id: str,
    ledger_accounts: Mapping[str, Mapping[str, Any]],
    standing: Mapping[str, set[str]],
    prefix: Sequence[Mapping[str, Any]],
) -> tuple[set[tuple[str, str]], bool]:
    account = _row(phase, "idp_accounts", account_id)
    if account is None or _visible_group_rows(phase, account_id) is None:
        return set(), False
    allow, denied = _group_effects(phase, account_id)
    footprint = allow - denied
    grants = _table(phase, "grants")
    synthetic = _synthetic_custodian_grants(phase, prefix)
    roles = _table(phase, "roles")
    for grant_id in standing.get(account_id, set()):
        grant = grants.get(grant_id) or synthetic.get(grant_id)
        role = roles.get(str(grant.get("role_id", ""))) if grant else None
        leaves = _scope_leaves(phase, str(grant.get("scope_path", ""))) if grant else None
        if grant is None or role is None or leaves is None:
            return set(), False
        footprint.update(
            {(cap, leaf) for cap in role.get("capabilities", ()) for leaf in leaves} - denied
        )
    return footprint, True


def _standing_item_locked(
    phase: DeliveredPhase,
    account_id: str,
    grant: Mapping[str, Any],
    role: Mapping[str, Any],
) -> bool | None:
    visible_groups = _visible_group_rows(phase, account_id)
    if visible_groups is None:
        return None
    grant_leaves = _scope_leaves(phase, str(grant.get("scope_path", "")))
    if grant_leaves is None:
        return None
    raw_allow, denied = _group_effects(phase, account_id)
    grant_footprint = {(cap, leaf) for cap in role.get("capabilities", ()) for leaf in grant_leaves}
    return bool(grant_footprint & (raw_allow - denied))


def _group_effects(
    phase: DeliveredPhase, account_id: str
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """Return raw ALLOW and DENY footprints from visible group memberships."""
    account = _row(phase, "idp_accounts", account_id)
    groups = _table(phase, "groups")
    roles = _table(phase, "roles")
    leaves = _get(_get(phase.policy_card, "scope_catalog", {}), "leaf_scopes", ())
    if account is None or not isinstance(leaves, Sequence):
        return set(), set()
    queue = list(account.get("group_ids", ()))
    seen: set[str] = set()
    allow: set[tuple[str, str]] = set()
    deny: set[tuple[str, str]] = set()
    while queue:
        group_id = str(queue.pop(0))
        if group_id in seen:
            continue
        seen.add(group_id)
        group = groups.get(group_id)
        if group is None:
            continue
        queue.extend(str(x) for x in group.get("member_of_group_ids", ()))
        scope = _scope_leaves(phase, str(group.get("scope_path", "/*")))
        if scope is None:
            continue
        caps = set(group.get("capabilities", ()))
        for role_id in group.get("granted_role_ids", ()):
            role = roles.get(str(role_id))
            if role is not None:
                caps.update(role.get("capabilities", ()))
        footprint = {(cap, leaf) for cap in caps for leaf in scope}
        (deny if group.get("effect", "ALLOW") == "DENY" else allow).update(footprint)
    return allow, deny


def _derive_p2_optimization(
    phase: DeliveredPhase,
    target_ids: Sequence[str],
    visible_prior_ledger: Mapping[str, Any],
    preceding: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mutations: list[dict[str, Any]] = []
    undefined: list[dict[str, Any]] = []
    if phase.phase_index != 2:
        return [], [
            _undefined("*", "phase_index", "This reference covers OPT-V5-02 in Phase 2 only.")
        ]
    ledger_accounts = _ledger_rows(visible_prior_ledger, "account_outcomes", "account_id")
    tasks = _table(phase, "optimization_tasks")
    roles = _table(phase, "roles")
    for task_id in sorted(set(target_ids)):
        task = tasks.get(task_id)
        if task is None:
            undefined.append(
                _undefined(
                    task_id,
                    "selected_ids",
                    "Optimization task row is not in selected delivered tables.",
                )
            )
            continue
        if task.get("phase_kind") != "P2_REVIEWER_ASSIGNMENT":
            undefined.append(
                _undefined(
                    task_id, "selected_ids", "Visible task is not a P2 reviewer-assignment task."
                )
            )
            continue
        candidates = [dict(c) for c in task.get("candidates", ()) if isinstance(c, Mapping)]
        required_scopes = task.get("required_review_scopes", ())
        max_per_person = task.get("max_assignments_per_person")
        if not isinstance(max_per_person, int) or not isinstance(required_scopes, Sequence):
            undefined.append(
                _undefined(task_id, "selected_ids", "Reviewer task constraints are incomplete.")
            )
            continue

        feasible: list[tuple[dict[str, Any], str, str, set[tuple[str, str]], int]] = []
        uncertain: list[str] = []
        for candidate in candidates:
            if candidate.get("action_kind") != "SELECT_REVIEWER_ACCOUNT":
                uncertain.append(str(candidate.get("candidate_id", "?")))
                continue
            candidate_id = str(candidate.get("candidate_id", ""))
            account_id = candidate.get("candidate_account_id")
            role_id = candidate.get("role_id")
            if not isinstance(account_id, str) or not isinstance(role_id, str):
                uncertain.append(candidate_id)
                continue
            outcome = ledger_accounts.get(account_id)
            account = _row(phase, "idp_accounts", account_id)
            role = roles.get(role_id)
            if (
                outcome is None
                or account is None
                or role is None
                or _visible_group_rows(phase, account_id) is None
            ):
                uncertain.append(candidate_id)
                continue
            person_id = _bound_person(account_id, ledger_accounts)
            live = _account_live(phase, account_id, ledger_accounts)
            if person_id is None or live is None:
                uncertain.append(candidate_id)
                continue
            if not live or outcome.get("p1_action") != "KEEP_ACTIVE":
                continue
            if person_id in set(task.get("excluded_conflict_person_ids", ())):
                continue
            excluded_rel = False
            relation_unknown = False
            for excluded in task.get("excluded_conflict_person_ids", ()):
                if not isinstance(excluded, str):
                    continue
                subordinate = _is_subordinate(phase, person_id, excluded)
                if subordinate is None:
                    relation_unknown = True
                    break
                excluded_rel = excluded_rel or subordinate
            if relation_unknown:
                uncertain.append(candidate_id)
                continue
            if excluded_rel:
                continue
            covered_scopes = candidate.get("covered_scopes", ())
            role_caps = role.get("capabilities", ())
            if not isinstance(covered_scopes, Sequence):
                uncertain.append(candidate_id)
                continue
            candidate_leaves: set[str] = set()
            for path in covered_scopes:
                if not isinstance(path, str):
                    uncertain.append(candidate_id)
                    break
                expanded = _scope_leaves(phase, path)
                if expanded is None:
                    uncertain.append(candidate_id)
                    break
                candidate_leaves.update(expanded)
            else:
                footprint = {(cap, leaf) for cap in role_caps for leaf in candidate_leaves}
                _, denied = _group_effects(phase, account_id)
                if footprint & denied:
                    continue
                cost = candidate.get("action_cost")
                if not isinstance(cost, int) or cost < 0:
                    uncertain.append(candidate_id)
                    continue
                feasible.append((candidate, person_id, role_id, footprint, cost))

        if uncertain:
            undefined.append(
                _undefined(
                    task_id,
                    "selected_ids",
                    "Feasibility of candidate(s) "
                    + ", ".join(sorted(set(uncertain)))
                    + " is unresolved from selected rows; a lower-cost candidate "
                    "could change the optimum.",
                )
            )
            continue

        required_leaves: set[str] = set()
        missing_scope = False
        for path in required_scopes:
            if not isinstance(path, str):
                missing_scope = True
                break
            expanded = _scope_leaves(phase, path)
            if expanded is None:
                missing_scope = True
                break
            required_leaves.update(expanded)
        if missing_scope:
            undefined.append(
                _undefined(
                    task_id,
                    "selected_ids",
                    "A required review scope does not resolve in the visible scope catalog.",
                )
            )
            continue

        # Enumerate only the delivered candidate set.  Positive action costs
        # make any feasible superset strictly worse, but all subsets are tested
        # so coverage, per-person limits, and SoD are applied together.
        best_cost: int | None = None
        best_options: dict[
            tuple[str, ...],
            list[tuple[dict[str, Any], str, str, set[tuple[str, str]], int]],
        ] = {}
        count = len(feasible)
        if count > 18:
            undefined.append(
                _undefined(
                    task_id,
                    "selected_ids",
                    "Candidate set is too large for an exhaustive independent subset check.",
                )
            )
            continue
        for mask in range(1 << count):
            selected = [feasible[i] for i in range(count) if mask & (1 << i)]
            assigned: dict[str, int] = {}
            scope_union: set[str] = set()
            valid = True
            for _candidate, person_id, _role_id, footprint, _cost in selected:
                assigned[person_id] = assigned.get(person_id, 0) + 1
                if assigned[person_id] > max_per_person:
                    valid = False
                    break
                scope_union.update(leaf for _cap, leaf in footprint)
            if not valid or not required_leaves <= scope_union:
                continue
            by_person: dict[str, set[tuple[str, str]]] = {}
            for _candidate, person_id, _role_id, footprint, _cost in selected:
                by_person.setdefault(person_id, set()).update(footprint)
            for person_id, added in by_person.items():
                auth, auth_known = _person_auth_footprint(
                    phase,
                    person_id,
                    ledger_accounts,
                    preceding,
                )
                if not auth_known:
                    valid = False
                    break
                if _has_sod_conflict(phase, added, auth | added):
                    valid = False
                    break
            if not valid:
                continue
            ids = tuple(sorted(str(item[0].get("candidate_id")) for item in selected))
            objective = sum(item[4] for item in selected)
            if best_cost is None or objective < best_cost:
                best_cost = objective
                best_options = {ids: selected}
            elif objective == best_cost:
                best_options[ids] = selected

        if best_cost is None:
            undefined.append(
                _undefined(
                    task_id,
                    "selected_ids",
                    "No visible candidate subset proves OPT-V5-02 feasibility; "
                    "the task's feasibility cannot be inferred.",
                )
            )
            continue
        if len(best_options) != 1:
            undefined.append(
                _undefined(
                    task_id,
                    "selected_ids",
                    "Multiple minimum-cost subsets satisfy the visible card; "
                    "OPT-V5-02 does not specify a counterfactual tie-break.",
                )
            )
            continue
        selected_ids, selected = next(iter(best_options.items()))
        objective = best_cost
        task_token = _fixed_token(task_id, task)
        cite_values: set[str] = set()
        task_cite = _cite("OPT-V5-02", task)
        if task_cite:
            cite_values.add(task_cite)
        pre_values: set[str] = set()
        if task_token:
            pre_values.add(task_token)
        else:
            undefined.append(
                _undefined(task_id, "if_match", "Visible optimization-task etag is missing.")
            )
            continue
        local_undefined: list[dict[str, str]] = []
        for candidate, person_id, role_id, _footprint, _cost in selected:
            account_id = str(candidate["candidate_account_id"])
            account_token = _account_token(phase, account_id, ledger_accounts)
            person_token = _person_token(phase, person_id)
            account_row = _row(phase, "idp_accounts", account_id)
            person_row = _row(phase, "hr_persons", person_id)
            role_row = roles.get(role_id)
            if account_token:
                pre_values.add(account_token)
            else:
                local_undefined.append(
                    _undefined(
                        task_id,
                        f"if_match[{account_id}]",
                        "Selected account has no visible LV token.",
                    )
                )
            if person_token:
                pre_values.add(person_token)
            else:
                local_undefined.append(
                    _undefined(
                        task_id,
                        f"if_match[{person_id}]",
                        "Selected bound person has no visible lifecycle token.",
                    )
                )
            for rule, row, label in (
                ("OPT-V5-02", account_row, f"cite[{account_id}]"),
                ("OPT-V5-02", person_row, f"cite[{person_id}]"),
                ("OPT-V5-02", role_row, f"cite[{role_id}]"),
            ):
                citation = _cite(rule, row)
                if citation:
                    cite_values.add(citation)
                else:
                    local_undefined.append(
                        _undefined(task_id, label, "Selected-row evidence_id is missing.")
                    )
        if local_undefined:
            undefined.extend(local_undefined)
            continue
        mutations.append(
            {
                "target_entity_id": task_id,
                "mutation_kind": "OPTIMIZATION_MUTATION",
                "if_match": sorted(pre_values),
                "cite": sorted(cite_values),
                "selected_ids": list(selected_ids),
                "objective_value": objective,
            }
        )
    return mutations, undefined


def _person_auth_footprint(
    phase: DeliveredPhase,
    person_id: str,
    ledger_accounts: Mapping[str, Mapping[str, Any]],
    preceding: Sequence[Mapping[str, Any]],
) -> tuple[set[tuple[str, str]], bool]:
    """Build the visible standing-plus-session footprint for one reviewer."""
    now = _get(phase.manifest, "clock_epoch_s")
    if not isinstance(now, int):
        return set(), False
    if "historical_sessions" not in phase.tables and "historical_sessions" not in phase.table_rows:
        return set(), False
    standing, eligible = _grant_state(ledger_accounts)
    _apply_prior_uar(phase, preceding, standing, eligible)
    synthetic_grants = _synthetic_custodian_grants(phase, preceding)
    accounts = [
        account_id
        for account_id, outcome in ledger_accounts.items()
        if outcome.get("p1_action") == "KEEP_ACTIVE" and outcome.get("bound_person_id") == person_id
    ]
    footprint: set[tuple[str, str]] = set()
    grants = _table(phase, "grants")
    roles = _table(phase, "roles")
    for account_id in accounts:
        live = _account_live(phase, account_id, ledger_accounts)
        account = _row(phase, "idp_accounts", account_id)
        if live is None or account is None:
            return set(), False
        if not live:
            continue
        if _visible_group_rows(phase, account_id) is None:
            return set(), False
        group_allow, group_deny = _group_effects(phase, account_id)
        footprint.update(group_allow - group_deny)
        for grant_id in standing.get(account_id, set()):
            grant = grants.get(grant_id) or synthetic_grants.get(grant_id)
            role = roles.get(str(grant.get("role_id", ""))) if grant else None
            if grant is None or role is None:
                return set(), False
            leaves = _scope_leaves(phase, str(grant.get("scope_path", "")))
            if leaves is None:
                return set(), False
            raw = {(cap, leaf) for cap in role.get("capabilities", ()) for leaf in leaves}
            footprint.update(raw - group_deny)

    # Historical H_prov is evaluated from the delivered session lifecycle
    # fields. CAE exposes revoked token trees, but the card does not connect
    # those token IDs to HistoricalSessionV5 rows or define a separate session
    # revocation timestamp derivation.
    for session in _table(phase, "historical_sessions").values():
        start, scheduled_end = session.get("start_s"), session.get("scheduled_end_s")
        revoked = session.get("revoked_at_s")
        if not isinstance(start, int) or not isinstance(scheduled_end, int):
            return set(), False
        if revoked is not None and not isinstance(revoked, int):
            return set(), False
        end = min(scheduled_end, revoked) if isinstance(revoked, int) else scheduled_end
        if not start <= now < end:
            continue
        account_id = str(session.get("account_id", ""))
        outcome = ledger_accounts.get(account_id)
        if outcome is None:
            return set(), False
        if outcome.get("bound_person_id") != person_id:
            continue
        role = roles.get(str(session.get("role_id", "")))
        leaves = _scope_leaves(phase, str(session.get("scope_path", "")))
        if role is None or leaves is None:
            return set(), False
        footprint.update((cap, leaf) for cap in role.get("capabilities", ()) for leaf in leaves)

    for mutation in preceding:
        kind = mutation.get("mutation_kind")
        if kind == "JIT_DECISION_MUTATION" and mutation.get("jit_decision") in {
            "APPROVE_JIT",
            "APPROVE_JIT_COSIGN",
        }:
            request = _row(phase, "jit_requests", str(mutation.get("target_entity_id", "")))
            if request is None:
                return set(), False
            account_id = request.get("requester_account_id")
            if not isinstance(account_id, str) or account_id not in ledger_accounts:
                return set(), False
            if _bound_person(account_id, ledger_accounts) != person_id:
                continue
            role = roles.get(str(request.get("requested_role_id", "")))
            leaves = _scope_leaves(phase, str(request.get("requested_scope_path", "")))
            ttl = mutation.get("approved_ttl_s")
            if role is None or leaves is None or not isinstance(ttl, int):
                return set(), False
            if ttl > 0:
                footprint.update(
                    (cap, leaf) for cap in role.get("capabilities", ()) for leaf in leaves
                )
        elif kind == "AGENT_DELEGATION_MUTATION" and mutation.get("delegation_decision") in {
            "APPROVE_DELEGATION",
            "APPROVE_DELEGATION_COSIGN",
        }:
            request = _row(
                phase, "agent_delegation_requests", str(mutation.get("target_entity_id", ""))
            )
            if request is None:
                return set(), False
            account_id = request.get("delegator_account_id")
            if not isinstance(account_id, str) or account_id not in ledger_accounts:
                return set(), False
            if _bound_person(account_id, ledger_accounts) != person_id:
                continue
            role = roles.get(str(request.get("requested_role_id", "")))
            leaves = _scope_leaves(phase, str(request.get("requested_scope_path", "")))
            ttl = mutation.get("approved_ttl_s")
            if role is None or leaves is None or not isinstance(ttl, int):
                return set(), False
            if ttl > 0:
                footprint.update(
                    (cap, leaf) for cap in role.get("capabilities", ()) for leaf in leaves
                )
    return footprint, True


def _has_sod_conflict(
    phase: DeliveredPhase,
    proposed: set[tuple[str, str]],
    combined: set[tuple[str, str]],
) -> bool:
    overlay = _get(phase.policy_card, "overlay", {})
    rules = _get(overlay, "forbidden_sod_rules", ())
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        a, b = rule.get("cap_a"), rule.get("cap_b")
        mode = rule.get("scope_mode")
        if not isinstance(a, str) or not isinstance(b, str):
            continue
        if a == b:
            continue
        left = {leaf for cap, leaf in proposed if cap == a}
        right = {leaf for cap, leaf in combined if cap == b}
        left |= {leaf for cap, leaf in proposed if cap == b}
        right |= {leaf for cap, leaf in combined if cap == a}
        if mode == "GLOBAL" and left and right:
            return True
        if mode == "SAME_LEAF_SCOPE" and left & right:
            return True
    return False
