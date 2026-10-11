"""Structural checks over a catalog or a generated world (SPEC §6.2, §9.2).

Each function returns a list of human-readable problems (empty when everything holds). The
generator raises if any check fails, so the guarantees the SPEC promises ("at most one control
per (identity, rule)", "birthright role sets are free of SoD conflicts") hold for every world;
dataset validation can rerun them on a loaded world.

The SPEC §9.2 completeness conditions are world invariants too, so every case slice inherits
them: every provisioned (account, entitlement) pair has a usage record, and every membership's
source matches its role's kind (``birthright`` memberships in birthright roles, ``request``
memberships in business roles). A view breaking either is incomplete and the oracle rejects it.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Protocol

from identitybench.policy.constants import PRIVILEGED_CAP_DAYS
from identitybench.world.eligibility import ineligibility
from identitybench.world.entities import (
    RISK_ORDER,
    AccountKind,
    AccountStatus,
    Assignment,
    AssignmentSource,
    EmploymentType,
    Entitlement,
    Environment,
    IdentityStatus,
    MembershipSource,
    PolicyPack,
    RiskTier,
    Role,
    RoleKind,
    SodRuleSides,
    TelemetryStatus,
    World,
)
from identitybench.world.ids import TICKET_PREFIXES
from identitybench.world.names import REGIONS
from identitybench.world.rolegraph import MAX_ROLE_DEPTH, RoleCycleError, RoleGraph, SodIndex
from identitybench.world.specs import SOD_DEFINITIONS

MAX_SOD_SIDE = 3


class AppLike(Protocol):
    @property
    def risk_tier(self) -> RiskTier: ...

    @property
    def environment(self) -> Environment: ...

    @property
    def region_restriction(self) -> str | None: ...


def entitlement_problems(
    entitlements: Mapping[str, Entitlement], apps: Mapping[str, AppLike]
) -> list[str]:
    """Every entitlement belongs to a known app and is at least as risky as its app."""
    problems: list[str] = []
    for ent_id, ent in entitlements.items():
        app = apps.get(ent.app_id)
        if app is None:
            problems.append(f"{ent_id}: unknown app {ent.app_id}")
        elif RISK_ORDER[ent.risk_tier] < RISK_ORDER[app.risk_tier]:
            problems.append(f"{ent_id}: risk {ent.risk_tier} below app risk {app.risk_tier}")
        if not ent.eligible_employment_types:
            problems.append(f"{ent_id}: no eligible employment types")
    return problems


def sod_problems(sod: Iterable[SodRuleSides], entitlements: Mapping[str, Entitlement]) -> list[str]:
    """SOD-001..006 each appear once with non-empty, small, sorted, disjoint, known sides."""
    problems: list[str] = []
    rules = list(sod)
    expected = [d.rule_id for d in SOD_DEFINITIONS]
    if [r.rule_id for r in rules] != expected:
        problems.append(f"SoD rules {[r.rule_id for r in rules]} != {expected}")
    for rule in rules:
        for name, side in (("A", rule.side_a), ("B", rule.side_b)):
            if not side:
                problems.append(f"{rule.rule_id}: side {name} is empty")
            if len(side) > MAX_SOD_SIDE:
                problems.append(f"{rule.rule_id}: side {name} has {len(side)} entitlements")
            if list(side) != sorted(side):
                problems.append(f"{rule.rule_id}: side {name} is not sorted")
            problems.extend(
                f"{rule.rule_id}: unknown entitlement {e}" for e in side if e not in entitlements
            )
        if set(rule.side_a) & set(rule.side_b):
            problems.append(f"{rule.rule_id}: sides overlap")
    return problems


def role_problems(roles: Mapping[str, Role], entitlements: Mapping[str, Entitlement]) -> list[str]:
    """References resolve, kinds match rules, nesting is acyclic and at most 3 levels deep."""
    problems: list[str] = []
    graph = RoleGraph(roles)
    for role_id, role in roles.items():
        if role_id != role.id:
            problems.append(f"role key {role_id} != id {role.id}")
        if not role.entitlements and not role.child_roles:
            problems.append(f"{role_id}: empty role")
        problems.extend(
            f"{role_id}: unknown entitlement {e}"
            for e in role.entitlements
            if e not in entitlements
        )
        problems.extend(
            f"{role_id}: unknown child role {c}" for c in role.child_roles if c not in roles
        )
        if (role.kind is RoleKind.birthright) != (role.attribute_rule is not None):
            problems.append(f"{role_id}: attribute rule must be set iff the role is birthright")
        try:
            depth = graph.depth(role_id)
        except RoleCycleError as exc:
            problems.append(f"role cycle: {exc}")
            continue
        if depth > MAX_ROLE_DEPTH:
            problems.append(f"{role_id}: nesting depth {depth} > {MAX_ROLE_DEPTH}")
    return problems


def birthright_problems(
    roles: Mapping[str, Role],
    entitlements: Mapping[str, Entitlement],
    apps: Mapping[str, AppLike],
    departments: Iterable[str],
    sod: tuple[SodRuleSides, ...],
) -> list[str]:
    """Birthright guarantees (SPEC §6.2):

    (a) for every (department, employment type, region), the union of the matching birthright
        roles' expanded entitlements contains no SoD conflict;
    (b) a birthright role never confers an entitlement that any identity it matches is
        ineligible for (ELIG-001/002/003/005).

    Callers run ``role_problems`` first; a cyclic role graph is reported there.
    """
    problems: list[str] = []
    graph = RoleGraph(roles)
    birthright = [(r.id, r.attribute_rule) for r in roles.values() if r.attribute_rule]
    try:
        expansions = {role_id: graph.expansion(role_id) for role_id, _ in birthright}
    except RoleCycleError:
        return problems
    all_types = tuple(EmploymentType)
    for role_id, rule in birthright:
        types = rule.employment_types or all_types
        regions = rule.regions or REGIONS
        for ent_id in sorted(expansions[role_id]):
            ent = entitlements.get(ent_id)
            app = apps.get(ent.app_id) if ent is not None else None
            if ent is None or app is None:
                continue
            reasons = {
                reason
                for t in types
                for region in regions
                if (reason := ineligibility(ent, app, t, region)) is not None
            }
            if reasons:
                problems.append(f"{role_id} confers {ent_id} ({', '.join(sorted(reasons))})")

    index = SodIndex(PolicyPack(org_name="", sod=sod))
    for dept in sorted(set(departments)):
        for etype in all_types:
            for region in REGIONS:
                held: set[str] = set()
                for role_id, rule in birthright:
                    if rule.matches_attributes(
                        department=dept, employment_type=etype, region=region
                    ):
                        held |= expansions[role_id]
                problems.extend(
                    f"birthright for ({dept}, {etype}, {region}) violates {rule_id}"
                    for rule_id in index.conflicts(held)
                )
    return problems


def world_problems(world: World) -> list[str]:
    """Every invariant of a generated world: integrity, uniqueness, catalog and SoD rules."""
    problems: list[str] = []
    collections: dict[str, Mapping[str, object]] = {
        "identities": world.identities,
        "applications": world.applications,
        "entitlements": world.entitlements,
        "roles": world.roles,
        "memberships": world.memberships,
        "accounts": world.accounts,
        "assignments": world.assignments,
        "usage": world.usage,
        "mitigating_controls": world.mitigating_controls,
        "tickets": world.tickets,
    }
    seen: Counter[str] = Counter()
    for name, items in collections.items():
        for key, item in items.items():
            seen[key] += 1
            if getattr(item, "id", None) != key:
                problems.append(f"{name}: key {key} != entity id")
    problems.extend(f"duplicate id across collections: {k}" for k, n in seen.items() if n > 1)

    problems.extend(_identity_problems(world))
    problems.extend(
        f"{a.id}: unknown owner {a.owner_id}"
        for a in world.applications.values()
        if a.owner_id not in world.identities
    )
    problems.extend(entitlement_problems(world.entitlements, world.applications))
    problems.extend(role_problems(world.roles, world.entitlements))
    departments = {i.department for i in world.identities.values()}
    problems.extend(
        birthright_problems(
            world.roles,
            world.entitlements,
            world.applications,
            departments,
            world.policy_pack.sod,
        )
    )
    problems.extend(sod_problems(world.policy_pack.sod, world.entitlements))
    problems.extend(_access_problems(world))
    problems.extend(_control_problems(world))
    problems.extend(_ticket_problems(world))
    return problems


def _identity_problems(world: World) -> list[str]:
    problems: list[str] = []
    for identity in world.identities.values():
        iid = identity.id
        if identity.manager_id is not None and identity.manager_id not in world.identities:
            problems.append(f"{iid}: unknown manager {identity.manager_id}")
        if identity.manager_id == iid:
            problems.append(f"{iid}: manages themself")
        if (identity.termination_date is None) != (identity.termination_type is None):
            problems.append(f"{iid}: termination date and type must be set together")
        if identity.status is IdentityStatus.terminated and identity.termination_date is None:
            problems.append(f"{iid}: terminated without a termination date")
        if (identity.contract_end_date is not None) != (
            identity.employment_type is EmploymentType.contractor
        ):
            problems.append(f"{iid}: contract end date must be set iff contractor")
        if (identity.move_date is None) != (identity.prior_department is None):
            problems.append(f"{iid}: move date and prior department must be set together")
    for identity in world.identities.values():
        chain: set[str] = set()
        current = identity.manager_id
        while current is not None and current in world.identities:
            if current in chain:
                problems.append(f"{identity.id}: manager chain has a cycle")
                break
            chain.add(current)
            current = world.identities[current].manager_id
    return problems


def _access_problems(world: World) -> list[str]:
    """Memberships, accounts, assignments and usage: integrity, the request rules a
    request-sourced assignment must satisfy, and SPEC §9.2 completeness."""
    problems: list[str] = []
    graph = RoleGraph(world.roles)
    for m in world.memberships.values():
        role = world.roles.get(m.role_id)
        if m.identity_id not in world.identities:
            problems.append(f"{m.id}: unknown identity {m.identity_id}")
        if role is None:
            problems.append(f"{m.id}: unknown role {m.role_id}")
        elif (m.source is MembershipSource.birthright) != (role.kind is RoleKind.birthright):
            problems.append(f"{m.id}: source {m.source} doesn't match role kind {role.kind}")
        if m.expires_on is not None and m.expires_on < m.granted_on:
            problems.append(f"{m.id}: expires before it is granted")

    human_pairs: Counter[tuple[str, str]] = Counter()
    for a in world.accounts.values():
        if a.app_id not in world.applications:
            problems.append(f"{a.id}: unknown app {a.app_id}")
        if a.identity_id is not None and a.identity_id not in world.identities:
            problems.append(f"{a.id}: unknown identity {a.identity_id}")
        if a.kind is AccountKind.human:
            if a.owner_id is not None:
                problems.append(f"{a.id}: human account with an owner")
            if a.identity_id is not None:
                human_pairs[(a.identity_id, a.app_id)] += 1
        else:
            if a.identity_id is not None:
                problems.append(f"{a.id}: service account with an identity")
            if a.owner_id is not None and a.owner_id not in world.identities:
                problems.append(f"{a.id}: unknown owner {a.owner_id}")
        if (a.status is AccountStatus.suspended) != (a.suspension_reason is not None):
            problems.append(f"{a.id}: suspension reason must be set iff suspended")
    problems.extend(
        f"identity {i} has {n} accounts in {app}" for (i, app), n in human_pairs.items() if n > 1
    )

    for s in world.assignments.values():
        account = world.accounts.get(s.account_id)
        ent = world.entitlements.get(s.entitlement_id)
        if account is None or ent is None:
            problems.append(f"{s.id}: unknown account or entitlement")
            continue
        if ent.app_id != account.app_id:
            problems.append(f"{s.id}: entitlement app {ent.app_id} != account app")
        if (s.source is AssignmentSource.request) != (s.request_id is not None):
            problems.append(f"{s.id}: request ID must be set iff source is request")
        if s.expires_on is not None and s.expires_on < s.granted_on:
            problems.append(f"{s.id}: expires before it is granted")
        if s.source is AssignmentSource.request:
            problems.extend(_request_problems(world, s, ent, account.identity_id))

    usage_pairs: Counter[tuple[str, str]] = Counter()
    for u in world.usage.values():
        account = world.accounts.get(u.account_id)
        ent = world.entitlements.get(u.entitlement_id)
        if account is None or ent is None:
            problems.append(f"{u.id}: unknown account or entitlement")
            continue
        if ent.app_id != account.app_id:
            problems.append(f"{u.id}: entitlement app != account app")
        if u.telemetry is TelemetryStatus.unavailable and u.last_used_on is not None:
            problems.append(f"{u.id}: unavailable telemetry with a last-used date")
        usage_pairs[(u.account_id, u.entitlement_id)] += 1
    problems.extend(f"duplicate usage for {pair}" for pair, n in usage_pairs.items() if n > 1)

    provisioned: set[tuple[str, str]] = set()
    account_of = {
        (a.identity_id, a.app_id): a.id
        for a in world.accounts.values()
        if a.kind is AccountKind.human and a.identity_id is not None
    }
    for m in world.memberships.values():
        if m.role_id not in world.roles:
            continue
        try:
            expansion = graph.expansion(m.role_id)
        except RoleCycleError:
            continue  # reported by role_problems
        for ent_id in expansion:
            ent = world.entitlements.get(ent_id)
            if ent is None:
                continue
            account_id = account_of.get((m.identity_id, ent.app_id))
            if account_id is None:
                problems.append(f"{m.id}: no account in {ent.app_id} for {ent_id}")
            else:
                provisioned.add((account_id, ent_id))
    provisioned |= {(s.account_id, s.entitlement_id) for s in world.assignments.values()}
    missing = sorted(provisioned - set(usage_pairs))
    problems.extend(f"no usage record for {pair}" for pair in missing[:20])
    if len(missing) > 20:
        problems.append(f"{len(missing) - 20} more provisioned pairs without a usage record")
    return problems


def _request_problems(
    world: World, s: Assignment, ent: Entitlement, holder_id: str | None
) -> list[str]:
    """A request ID claims an approved request, so the grant must be one a request could
    produce (SPEC §7.4): the entitlement is requestable (ELIG-004), and the grant ends within
    the entitlement's maximum duration (TEMP-002), within 7 days if it is privileged
    (TEMP-001), and on or before a contractor holder's contract end (LIFE-004)."""
    problems: list[str] = []
    if not ent.requestable:
        problems.append(f"{s.id}: request-sourced but {ent.id} isn't requestable")
    days = None if s.expires_on is None else (s.expires_on - s.granted_on).days + 1
    cap = ent.max_duration_days
    if cap is not None and (days is None or days > cap):
        problems.append(f"{s.id}: request-sourced grant longer than {cap} days")
    if ent.privileged and (days is None or days > PRIVILEGED_CAP_DAYS):
        problems.append(
            f"{s.id}: request-sourced privileged grant longer than {PRIVILEGED_CAP_DAYS} days"
        )
    holder = world.identities.get(holder_id) if holder_id is not None else None
    contract_end = holder.contract_end_date if holder is not None else None
    if contract_end is not None and (s.expires_on is None or s.expires_on > contract_end):
        problems.append(f"{s.id}: request-sourced grant runs past the contract end {contract_end}")
    return problems


def _control_problems(world: World) -> list[str]:
    """Integrity, one control per (identity, rule), and dates that fit the people named: no
    control predates its holder's or approver's hire or postdates the holder's termination,
    and approvers are active (SPEC §6.4, no impossible history)."""
    problems: list[str] = []
    rule_ids = {r.rule_id for r in world.policy_pack.sod}
    pairs: Counter[tuple[str, str]] = Counter()
    for c in world.mitigating_controls.values():
        holder = world.identities.get(c.identity_id)
        approver = world.identities.get(c.approver_id)
        if holder is None:
            problems.append(f"{c.id}: unknown identity {c.identity_id}")
        else:
            if c.granted_on < holder.hire_date:
                problems.append(f"{c.id}: granted before the holder was hired")
            ended = holder.termination_date
            if holder.status is IdentityStatus.terminated and ended and c.granted_on > ended:
                problems.append(f"{c.id}: granted after the holder left")
        if approver is None:
            problems.append(f"{c.id}: unknown approver {c.approver_id}")
        else:
            if c.granted_on < approver.hire_date:
                problems.append(f"{c.id}: granted before the approver was hired")
            if approver.status is not IdentityStatus.active:
                problems.append(f"{c.id}: approver {approver.id} is not active")
        if c.approver_id == c.identity_id:
            problems.append(f"{c.id}: self-approved control")
        if c.rule_id not in rule_ids:
            problems.append(f"{c.id}: unknown SoD rule {c.rule_id}")
        if c.expires_on < c.granted_on:
            problems.append(f"{c.id}: expires before it is granted")
        pairs[(c.identity_id, c.rule_id)] += 1
    problems.extend(f"{n} controls for {pair}" for pair, n in pairs.items() if n > 1)
    return problems


def _ticket_problems(world: World) -> list[str]:
    problems: list[str] = []
    for t in world.tickets.values():
        if not t.id.startswith(TICKET_PREFIXES[t.type]):
            problems.append(f"{t.id}: prefix doesn't match type {t.type}")
        assignee = world.identities.get(t.assignee_id) if t.assignee_id is not None else None
        if t.assignee_id is not None and assignee is None:
            problems.append(f"{t.id}: unknown assignee {t.assignee_id}")
        if assignee is not None and assignee.hire_date > t.opened_on:
            problems.append(f"{t.id}: opened before its assignee was hired")
        problems.extend(
            f"{t.id}: unknown app {a}" for a in t.related_app_ids if a not in world.applications
        )
    return problems
