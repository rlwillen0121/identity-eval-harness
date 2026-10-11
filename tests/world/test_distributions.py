"""Distribution sanity for the midmarket world and a sweep of startup worlds (SPEC §6.1, §6.4).

Bounds are deliberately loose: they catch a broken knob (no terminated people, telemetry
everywhere, every grant dormant), not natural seed-to-seed variation. The startup sweep checks
that small orgs don't lose a feature (LIFE-003 material, security holds, telemetry gaps) by
chance.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

import pytest

from identitybench.world.apps_data import APP_SPECS
from identitybench.world.entities import (
    AccountKind,
    AccountStatus,
    EmploymentType,
    IdentityStatus,
    MembershipSource,
    SuspensionReason,
    TelemetryStatus,
    TicketStatus,
    TicketType,
    World,
)
from identitybench.world.generator import build_world
from identitybench.world.names import FIRST_NAMES
from identitybench.world.query import WorldIndex
from identitybench.world.roles_data import ROLE_SPECS
from identitybench.world.specs import EVERYONE


def _share(counter: Counter[str], key: str) -> float:
    return counter[key] / sum(counter.values())


def test_employment_types(midmarket_world: World) -> None:
    types = Counter(i.employment_type.value for i in midmarket_world.identities.values())
    assert abs(_share(types, "employee") - 0.80) < 0.02
    assert abs(_share(types, "contractor") - 0.12) < 0.02
    assert abs(_share(types, "intern") - 0.05) < 0.015
    assert abs(_share(types, "vendor") - 0.03) < 0.015


def test_statuses(midmarket_world: World) -> None:
    statuses = Counter(i.status.value for i in midmarket_world.identities.values())
    assert _share(statuses, "active") > 0.9
    assert abs(_share(statuses, "leave_of_absence") - 0.03) < 0.01
    assert abs(_share(statuses, "terminated") - 0.04) < 0.01


def test_hierarchy(midmarket_world: World, midmarket_index: WorldIndex) -> None:
    ceo = next(i for i in midmarket_world.identities.values() if i.manager_id is None)
    depths = Counter(len(midmarket_index.manager_chain(i)) for i in midmarket_world.identities)
    assert depths[0] == 1
    # CEO -> head -> director -> manager -> IC, one level deeper where a department head
    # reports to another head (Accounting and Procurement roll up to the CFO).
    assert max(depths) == 5
    assert depths[4] > depths[5]
    assert len(midmarket_index.direct_reports[ceo.id]) >= 10
    non_employees = {
        i.id
        for i in midmarket_world.identities.values()
        if i.employment_type is not EmploymentType.employee
    }
    assert not non_employees & set(midmarket_index.direct_reports)


def test_locations(midmarket_world: World) -> None:
    regions = Counter(i.location.region for i in midmarket_world.identities.values())
    assert {"NA", "EU", "APAC", "LATAM"} <= set(regions)
    assert _share(regions, "EU") > 0.1 and _share(regions, "APAC") > 0.05


def test_names_follow_country(midmarket_world: World) -> None:
    japan = [i for i in midmarket_world.identities.values() if i.location.country == "JP"]
    assert japan
    local = sum(1 for i in japan if i.display_name.split(" ")[0] in FIRST_NAMES["JP"])
    assert local / len(japan) > 0.75
    distinct = {i.display_name.split(" ")[0] for i in midmarket_world.identities.values()}
    assert len(distinct) > 400


def test_titles_are_noisy(midmarket_world: World) -> None:
    identities = list(midmarket_world.identities.values())
    noisy = sum(1 for i in identities if i.title != i.canonical_title)
    assert 0.4 < noisy / len(identities) < 0.95


def test_hire_dates_span_years(midmarket_world: World) -> None:
    anchor = midmarket_world.anchor_date
    ages = [(anchor - i.hire_date).days for i in midmarket_world.identities.values()]
    assert max(ages) > 8 * 365
    assert sum(1 for a in ages if a < 365) / len(ages) > 0.15


def test_lifecycle_messiness(midmarket_world: World) -> None:
    anchor = midmarket_world.anchor_date
    identities = list(midmarket_world.identities.values())
    lapsed = [
        i
        for i in identities
        if i.status is IdentityStatus.active
        and i.contract_end_date is not None
        and i.contract_end_date < anchor
    ]
    assert lapsed, "LIFE-003: some contracts lapsed while HR shows active"
    movers = [i for i in identities if i.move_date is not None]
    assert abs(len(movers) / len(identities) - 0.05) < 0.015
    terminated = [i for i in identities if i.status is IdentityStatus.terminated]
    assert all(
        i.termination_date and anchor - i.termination_date <= timedelta(days=120)
        for i in terminated
    )
    assert {i.termination_type.value for i in terminated if i.termination_type} == {
        "voluntary",
        "involuntary",
    }
    assert any(i.leave_returned_on for i in identities)
    assert any(
        i.status is IdentityStatus.active and i.termination_date and i.termination_date > anchor
        for i in identities
    ), "some people are serving notice"


def test_movers_keep_old_birthright_sometimes(midmarket_world: World) -> None:
    world = midmarket_world
    stale = 0
    for m in world.memberships.values():
        identity = world.identities[m.identity_id]
        rule = world.roles[m.role_id].attribute_rule
        if identity.move_date and rule is not None and not rule.matches(identity):
            stale += 1
    assert stale > 0


def test_deprovisioning_lag(midmarket_world: World) -> None:
    world = midmarket_world
    by_status: dict[str, Counter[AccountStatus]] = {}
    for a in world.accounts.values():
        if a.identity_id is None:
            continue
        status = world.identities[a.identity_id].status.value
        by_status.setdefault(status, Counter())[a.status] += 1
    terminated = by_status["terminated"]
    assert terminated[AccountStatus.disabled] > terminated[AccountStatus.active] > 0
    leave = by_status["leave_of_absence"]
    assert leave[AccountStatus.suspended] > leave[AccountStatus.active]
    reasons = Counter(a.suspension_reason for a in world.accounts.values() if a.suspension_reason)
    assert reasons[SuspensionReason.leave] > reasons[SuspensionReason.security_hold] > 0


def test_orphans_and_service_accounts(midmarket_world: World) -> None:
    world = midmarket_world
    human = [a for a in world.accounts.values() if a.kind is AccountKind.human]
    orphans = [a for a in human if a.identity_id is None]
    assert abs(len(orphans) / len(human) - 0.01) < 0.002
    with_assignments = {s.account_id for s in world.assignments.values()}
    assert all(a.id in with_assignments for a in orphans)
    service = [a for a in world.accounts.values() if a.kind is AccountKind.service]
    assert len(service) >= 40
    owners = [world.identities[a.owner_id] for a in service if a.owner_id]
    assert any(o.status is IdentityStatus.terminated for o in owners)
    assert any(a.owner_id is None for a in service)


def test_assignments(midmarket_world: World) -> None:
    world = midmarket_world
    anchor = world.anchor_date
    human_ids = {
        a.id for a in world.accounts.values() if a.kind is AccountKind.human and a.identity_id
    }
    personal = [s for s in world.assignments.values() if s.account_id in human_ids]
    assert 2 <= len(personal) / len(world.identities) <= 6
    sources = Counter(s.source.value for s in personal)
    assert sources["request"] > sources["direct"] > 0
    assert any(world.entitlements[s.entitlement_id].privileged for s in personal)
    timebound = [s for s in personal if s.expires_on is not None]
    expired = [s for s in timebound if s.expires_on and s.expires_on < anchor]
    assert timebound and expired and len(expired) < len(timebound)


def test_memberships(midmarket_world: World) -> None:
    world = midmarket_world
    sources = Counter(m.source.value for m in world.memberships.values())
    assert sources["birthright"] > sources["request"] > 0
    requested = [m for m in world.memberships.values() if m.source.value == "request"]
    assert any(m.expires_on and m.expires_on < world.anchor_date for m in requested)
    assert any(m.expires_on and m.expires_on >= world.anchor_date for m in requested)


def test_usage_shape(midmarket_world: World) -> None:
    world = midmarket_world
    anchor = world.anchor_date
    records = list(world.usage.values())
    unavailable = [u for u in records if u.telemetry is TelemetryStatus.unavailable]
    assert 0.02 < len(unavailable) / len(records) < 0.1
    available = [u for u in records if u.telemetry is TelemetryStatus.available]
    never = [u for u in available if u.last_used_on is None]
    dormant = [u for u in available if u.last_used_on and (anchor - u.last_used_on).days > 90]
    recent = [u for u in available if u.last_used_on and (anchor - u.last_used_on).days <= 30]
    assert 0.01 < len(never) / len(available) < 0.1
    assert 0.05 < len(dormant) / len(available) < 0.3
    assert len(recent) / len(available) > 0.5
    # Some applications report no telemetry at all.
    per_app: dict[str, set[TelemetryStatus]] = {}
    for u in records:
        per_app.setdefault(world.accounts[u.account_id].app_id, set()).add(u.telemetry)
    assert any(statuses == {TelemetryStatus.unavailable} for statuses in per_app.values())


def test_controls_have_date_spread(midmarket_world: World, midmarket_index: WorldIndex) -> None:
    world = midmarket_world
    anchor = world.anchor_date
    controls = list(world.mitigating_controls.values())
    assert len(controls) >= 10
    assert any(c.expires_on < anchor for c in controls)
    assert any(anchor <= c.expires_on <= anchor + timedelta(days=30) for c in controls)
    assert any(c.expires_on > anchor + timedelta(days=30) for c in controls)
    # MIT-002 material: live conflicts whose control is due for renewal within 30 days.
    sides = {r.rule_id: (set(r.side_a), set(r.side_b)) for r in world.policy_pack.sod}
    renewals = 0
    for c in controls:
        held = midmarket_index.held_entitlements(c.identity_id, anchor)
        side_a, side_b = sides[c.rule_id]
        live = bool(held & side_a and held & side_b)
        renewals += live and anchor <= c.expires_on <= anchor + timedelta(days=30)
    assert renewals >= 3


def _sod_entitlements(world: World) -> set[str]:
    return {e for r in world.policy_pack.sod for e in (*r.side_a, *r.side_b)}


def test_requested_roles_follow_department_history(midmarket_world: World) -> None:
    """Business roles come from the audience of the department the identity was in on the
    grant date: a mover's pre-move grants from the prior department, later ones from the
    new one. Roles touching an SoD side are skipped: conflict injection prefers the audience
    but falls back to any role reaching the side when none in the audience fits."""
    world = midmarket_world
    audience = {s.id: s.audience for s in ROLE_SPECS}
    sod_ents = _sod_entitlements(world)
    index = WorldIndex(world)
    prior_dept_grants = 0
    for m in world.memberships.values():
        if m.source is not MembershipSource.request:
            continue
        if index.graph.expansion(m.role_id) & sod_ents:
            continue
        identity = world.identities[m.identity_id]
        dept = identity.department
        if identity.move_date is not None and m.granted_on < identity.move_date:
            assert identity.prior_department is not None
            dept = identity.prior_department
            prior_dept_grants += 1
        assert EVERYONE in audience[m.role_id] or dept in audience[m.role_id], m.id
    assert prior_dept_grants > 0, "movers should carry business roles from their old team"


def test_assignments_follow_department_history(midmarket_world: World) -> None:
    """Personal assignments come from apps the identity's department (on the grant date)
    uses; SoD sides are skipped for the same reason as above."""
    world = midmarket_world
    user_depts = {f"app_{s.key}": s.user_depts for s in APP_SPECS}
    sod_ents = _sod_entitlements(world)
    for s in world.assignments.values():
        account = world.accounts[s.account_id]
        if account.identity_id is None or s.entitlement_id in sod_ents:
            continue
        identity = world.identities[account.identity_id]
        dept = identity.department
        if identity.move_date is not None and s.granted_on < identity.move_date:
            assert identity.prior_department is not None
            dept = identity.prior_department
        depts = user_depts[account.app_id]
        assert EVERYONE in depts or dept in depts, s.id


def test_tickets(midmarket_world: World) -> None:
    tickets = list(midmarket_world.tickets.values())
    assert len(tickets) == 400
    kinds = Counter(t.type for t in tickets)
    assert set(kinds) == set(TicketType)
    assert any(t.type is TicketType.incident and t.status is TicketStatus.open for t in tickets)
    assert any(t.status is TicketStatus.closed for t in tickets)
    assert any(len(t.related_app_ids) > 1 for t in tickets)
    assert all(t.summary for t in tickets)
    assert all(t.opened_on <= midmarket_world.anchor_date for t in tickets)


def test_application_owners_are_active_leaders(midmarket_world: World) -> None:
    for app in midmarket_world.applications.values():
        owner = midmarket_world.identities[app.owner_id]
        assert owner.status is IdentityStatus.active
        assert owner.employment_type is EmploymentType.employee


# ------------------------------------------------------------------------------ startup sweep

STARTUP_SEEDS = range(300, 316)


@pytest.fixture(scope="module")
def startup_worlds() -> list[World]:
    return [build_world("startup", seed) for seed in STARTUP_SEEDS]


def test_startup_telemetry_gaps(startup_worlds: list[World]) -> None:
    """A startup has no telemetry-less legacy apps, yet still has about 5% unknown usage."""
    for world in startup_worlds:
        records = list(world.usage.values())
        unavailable = sum(1 for u in records if u.telemetry is TelemetryStatus.unavailable)
        assert 0.03 < unavailable / len(records) < 0.08, world.seed


def test_startup_always_has_lifecycle_lookalikes(startup_worlds: list[World]) -> None:
    """Exact counts, not chance: every startup has an active contractor whose contract lapsed
    (LIFE-003) and an account on security hold (suspended, but not for leave)."""
    for world in startup_worlds:
        anchor = world.anchor_date
        assert any(
            i.status is IdentityStatus.active
            and i.contract_end_date is not None
            and i.contract_end_date < anchor
            for i in world.identities.values()
        ), world.seed
        assert any(
            a.suspension_reason is SuspensionReason.security_hold for a in world.accounts.values()
        ), world.seed


def test_startup_purchase_order_conflicts_land_in_finance(startup_worlds: list[World]) -> None:
    """A startup has no Procurement or Operations team, so SOD-006 conflicts fall back to
    Finance staff rather than to anyone at all (a sales rep raising and receiving POs)."""
    for world in startup_worlds:
        index = WorldIndex(world)
        rule = next(r for r in world.policy_pack.sod if r.rule_id == "SOD-006")
        holders = [
            i
            for i in world.identities.values()
            if (held := index.held_entitlements(i.id, world.anchor_date)) & set(rule.side_a)
            and held & set(rule.side_b)
        ]
        assert holders, world.seed
        assert {i.department for i in holders} == {"Finance"}, world.seed


def test_startup_control_approvers_vary(startup_worlds: list[World]) -> None:
    """Rule-specific approvers (Finance, IT, Engineering, HR leaders and the CEO), so a small
    org's exceptions aren't all signed off by the same person."""
    varied = sum(
        len({c.approver_id for c in world.mitigating_controls.values()}) >= 2
        for world in startup_worlds
    )
    assert varied >= len(startup_worlds) - 1
