"""Tests for ``ViewBuilder``: completeness, closure, faithfulness, determinism, distractors, peer
stats and size.

Completeness is checked the way case builders rely on it: the oracle (``decide_request``,
``decide_review``, ``plan_jml``) must run on every view without ``OracleInputError``, over many
random focal identities and campaign items and every event type, on both worlds. The closure
checks restate SPEC §8.1-§8.3, §8.6 and §9.2 against the world and the entity schemas rather
than the builder's code: every ID a record in the view references resolves within the view
(fields are found by name, so a reference field added to an entity later is checked too), except
the plain-ID pointers the view builder documents; each focal identity's own records are all in;
and each task's additions are present.
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import statistics
import subprocess
import sys
from collections.abc import Collection, Iterable, Iterator, Sequence
from datetime import date, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

from identitybench.policy.access import SOURCE_FOR_ROLE_KIND, expand_role
from identitybench.policy.jml import plan_jml
from identitybench.policy.oracle import decide_request, decide_review
from identitybench.schemas.case import (
    AccessRequest,
    Campaign,
    CampaignScopeKind,
    CaseView,
    HrAttributes,
    HrEvent,
    PriorAccessRecord,
    ReviewItem,
    ReviewItemKind,
)
from identitybench.schemas.enums import Task
from identitybench.schemas.gold import JmlGold, RequestGold, ReviewGold
from identitybench.schemas.ops import OpType
from identitybench.tasks.common import IdAllocator
from identitybench.tasks.tiers import compute_tags, compute_tier
from identitybench.tasks.view_builder import (
    BIRTHRIGHT_MATCHING_EVENTS,
    CHARS_PER_TOKEN,
    FRESH_START_EVENTS,
    REQUEST_TOKEN_TARGET,
    REVIEW_TOKEN_CAP,
    DistractorShortage,
    DistractorSpec,
    ViewBuilder,
    ViewBuildError,
    approx_tokens,
)
from identitybench.world import query
from identitybench.world.entities import (
    Account,
    AccountKind,
    AccountStatus,
    EmploymentType,
    HrEventType,
    Identity,
    RoleKind,
    TelemetryStatus,
    UsageRecord,
    World,
)
from identitybench.world.query import WorldIndex, index_for, peer_share

REPO_ROOT = Path(__file__).resolve().parents[2]

COLLECTIONS = (
    "identities",
    "applications",
    "entitlements",
    "roles",
    "memberships",
    "accounts",
    "assignments",
    "usage",
    "mitigating_controls",
    "tickets",
)

COLLECTION_OF_PREFIX = {
    "u_": "identities",
    "app_": "applications",
    "ent_": "entitlements",
    "role_": "roles",
    "rm_": "memberships",
    "acct_": "accounts",
    "as_": "assignments",
    "use_": "usage",
    "mc_": "mitigating_controls",
    "INC-": "tickets",
    "CHG-": "tickets",
    "PRJ-": "tickets",
}
"""Record ID prefixes (``world/ids.py``). Other IDs, such as the ``req_…`` of a past grant's
request or a ``SOD-…`` rule, aren't records."""

LIST_REFERENCES = frozenset({"child_roles", "entitlements"})
"""Reference fields not named ``*_id`` or ``*_ids``: a role's child roles and entitlements."""

PLAIN_POINTERS = frozenset(
    {
        ("applications", "owner_id"),
        ("mitigating_controls", "approver_id"),
        ("tickets", "assignee_id"),
        ("tickets", "related_app_ids"),
    }
)
"""(collection, field) pairs the view builder documents as plain IDs: no rule follows them to a
record, so the record needn't be in the view."""

EVENT_TYPES = tuple(HrEventType)


@pytest.fixture(params=["startup", "midmarket"])
def world(request: pytest.FixtureRequest) -> World:
    """Each test that takes ``world`` runs on both profiles."""
    return request.getfixturevalue(f"{request.param}_world")


# --------------------------------------------------------------------------------------------
# Random headers
# --------------------------------------------------------------------------------------------


def attributes_of(identity: Identity) -> HrAttributes:
    return HrAttributes(
        department=identity.department,
        title=identity.title,
        employment_type=identity.employment_type,
        location=identity.location,
        manager_id=identity.manager_id,
        cost_center=identity.cost_center,
    )


def random_request(world: World, rng: random.Random, ids: IdAllocator) -> AccessRequest:
    people = sorted(world.identities)
    beneficiary = rng.choice(people)
    ticket_ref = rng.choice([None, rng.choice(sorted(world.tickets)), "INC-00001"])
    return AccessRequest(
        request_id=ids.request_id(),
        requester_id=rng.choice([beneficiary, beneficiary, rng.choice(people)]),
        beneficiary_id=beneficiary,
        entitlement_id=rng.choice(sorted(world.entitlements)),
        requested_duration_days=rng.choice([None, 1, 7, 30, 365]),
        is_break_glass=rng.random() < 0.15,
        ticket_ref=ticket_ref,
        justification="need it for the quarter close",
        submitted_on=world.anchor_date,
    )


def sources_of(world: World, identity_id: str) -> list[tuple[ReviewItemKind, str]]:
    index = index_for(world)
    found = [
        (ReviewItemKind.membership, m.id)
        for m in index.memberships_by_identity.get(identity_id, ())
    ]
    for account in index.accounts_by_identity.get(identity_id, ()):
        found += [
            (ReviewItemKind.assignment, a.id)
            for a in index.assignments_by_account.get(account.id, ())
        ]
    return found


def unowned_assignments(world: World) -> list[str]:
    """Assignments on orphaned and service accounts (HYG-002, HYG-003 items)."""
    return sorted(
        a.id for a in world.assignments.values() if world.accounts[a.account_id].identity_id is None
    )


def random_campaign(world: World, rng: random.Random, ids: IdAllocator) -> Campaign:
    """A manager-scoped campaign over 2-8 direct reports, or an application-scoped one, both
    salted with orphaned and service-account items."""
    index = index_for(world)
    if rng.random() < 0.6:
        managers = sorted(m for m, r in index.direct_reports.items() if 2 <= len(r) <= 8)
        manager = rng.choice(managers)
        refs = [ref for r in sorted(index.direct_reports[manager]) for ref in sources_of(world, r)]
        scope_kind, scope_ref, reviewer = CampaignScopeKind.manager, manager, manager
    else:
        app_id = rng.choice(sorted(world.applications))
        refs = [
            (ReviewItemKind.assignment, a.id)
            for a in world.assignments.values()
            if world.accounts[a.account_id].app_id == app_id
        ]
        refs = sorted(refs)[:12]
        scope_kind, scope_ref = CampaignScopeKind.application, app_id
        reviewer = world.applications[app_id].owner_id
    refs = sorted(refs)
    rng.shuffle(refs)
    refs = refs[:30] + [
        (ReviewItemKind.assignment, a) for a in rng.sample(unowned_assignments(world), 3)
    ]
    items = tuple(ReviewItem(item_id=ids.item_id(), kind=kind, ref=ref) for kind, ref in refs)
    return Campaign(
        campaign_id=ids.campaign_id(),
        reviewer_id=reviewer,
        scope_kind=scope_kind,
        scope_ref=scope_ref,
        items=items,
    )


def random_event(world: World, rng: random.Random, ids: IdAllocator, kind: HrEventType) -> HrEvent:
    """An event the oracle can plan from the world's facts as the view builder slices them."""
    people = sorted(world.identities.values(), key=lambda i: i.id)
    effective = world.anchor_date + timedelta(days=rng.randint(-10, 30))
    if kind is HrEventType.leaver:
        identity = rng.choice([i for i in people if i.termination_date is not None])
        assert identity.termination_date is not None
        effective = identity.termination_date
        before, after = attributes_of(identity), None
    elif kind is HrEventType.conversion:
        identity = rng.choice([i for i in people if i.employment_type is EmploymentType.contractor])
        before = attributes_of(identity)
        after = before.model_copy(update={"employment_type": EmploymentType.employee})
    elif kind is HrEventType.mover:
        identity = rng.choice(people)
        before = attributes_of(identity)
        departments = sorted({i.department for i in people} - {identity.department})
        after = before.model_copy(update={"department": rng.choice(departments)})
    elif kind in FRESH_START_EVENTS:
        identity = rng.choice(people)
        before, after = None, attributes_of(identity)
    else:
        identity = rng.choice(people)
        before = after = attributes_of(identity)
    return HrEvent(
        event_id=ids.event_id(),
        type=kind,
        identity_id=identity.id,
        effective_on=effective,
        before=before,
        after=after,
    )


def prior_stint(world: World, event: HrEvent, ids: IdAllocator) -> list[PriorAccessRecord]:
    """A rehire's history: the first two roles the subject holds in the world, as held in a
    prior stint (an edit a JML builder makes); nothing for other events."""
    if event.type is not HrEventType.rehire:
        return []
    memberships = index_for(world).memberships_by_identity.get(event.identity_id, ())
    return [
        PriorAccessRecord(
            id=ids.new("hist_"),
            identity_id=event.identity_id,
            role_id=membership.role_id,
            held_from=world.anchor_date - timedelta(days=900),
            held_until=world.anchor_date - timedelta(days=400),
        )
        for membership in sorted(memberships, key=lambda m: m.id)[:2]
    ]


def random_views(world: World, seed: int, n: int) -> Iterator[CaseView]:
    """A mix of request, campaign and event views (every event type within 21 views)."""
    rng = random.Random(seed)
    ids = IdAllocator(world, rng)
    builder = ViewBuilder(world)
    for k in range(n):
        if k % 3 == 0:
            yield builder.build_view(random_request(world, rng, ids))
        elif k % 3 == 1:
            yield builder.build_view(random_campaign(world, rng, ids))
        else:
            event = random_event(world, rng, ids, EVENT_TYPES[k % len(EVENT_TYPES)])
            yield builder.build_view(event, history=prior_stint(world, event, ids))


def holders(world: World, campaign: Campaign) -> list[str]:
    found: list[str] = []
    for item in campaign.items:
        if item.kind is ReviewItemKind.membership:
            found.append(world.memberships[item.ref].identity_id)
        else:
            holder = world.accounts[world.assignments[item.ref].account_id].identity_id
            if holder is not None:
                found.append(holder)
    return sorted(set(found))


# --------------------------------------------------------------------------------------------
# Independent closure and completeness checks
# --------------------------------------------------------------------------------------------


def collection_of(record_id: str) -> str | None:
    return next((c for p, c in COLLECTION_OF_PREFIX.items() if record_id.startswith(p)), None)


def references(record: BaseModel) -> Iterator[tuple[str, str]]:
    """(field, ID) for every reference field of a record: named ``*_id`` or ``*_ids``, or in
    ``LIST_REFERENCES``. A tuple field yields each element."""
    for name, value in record:
        if name == "id" or not (name.endswith(("_id", "_ids")) or name in LIST_REFERENCES):
            continue
        for item in value if isinstance(value, tuple) else (value,):
            if isinstance(item, str):
                yield name, item


def reference_problems(view: CaseView, people: Collection[str]) -> list[str]:
    """IDs a record in the view references that the view lacks.

    Every record counts except the identity records of people outside ``people`` (the focal
    identities and a joiner's or rehire's subject): everyone else is in the view as a record
    only, so their own pointers, such as their managers, needn't resolve.
    """
    problems: list[str] = []
    for name in COLLECTIONS:
        for record_id, record in getattr(view, name).items():
            if name == "identities" and record_id not in people:
                continue
            for field_name, ref in references(record):
                target = collection_of(ref)
                if target is None or (name, field_name) in PLAIN_POINTERS:
                    continue
                if ref not in getattr(view, target):
                    problems.append(f"{record_id}.{field_name} = {ref} is not in the view")
    return problems


def own_records(world: World, identity_id: str) -> dict[str, set[str]]:
    """The identity's own access records in the world, per view collection."""
    index = index_for(world)
    accounts = {a.id for a in index.accounts_by_identity.get(identity_id, ())}
    return {
        "memberships": {m.id for m in index.memberships_by_identity.get(identity_id, ())},
        "accounts": accounts,
        "assignments": {
            a.id for account in accounts for a in index.assignments_by_account.get(account, ())
        },
        "mitigating_controls": {c.id for c in index.controls_by_identity.get(identity_id, ())},
    }


def closure_problems(
    world: World, view: CaseView, focal: Sequence[str], hires: Sequence[str] = ()
) -> list[str]:
    """What SPEC §8.6 and §9.2 require of a view, stated against the world.

    ``focal`` identities must have every own record in the view; the subjects of joiner and
    rehire events (``hires``) must have none, owned service accounts and usage included.
    """
    problems = reference_problems(view, {*focal, *hires})
    if problems:
        return problems  # the checks below follow references, so they need them to resolve
    for identity_id in focal:
        manager_id = world.identities[identity_id].manager_id
        if manager_id is not None and manager_id not in view.identities:
            problems.append(f"manager of {identity_id} missing")
        for name, record_ids in own_records(world, identity_id).items():
            missing = record_ids - set(getattr(view, name))
            if missing:
                problems.append(f"{identity_id}: {name} missing {sorted(missing)}")
    for identity_id in hires:
        found = {
            name: record_ids & set(getattr(view, name))
            for name, record_ids in own_records(world, identity_id).items()
        }
        found["accounts"] |= {a.id for a in view.accounts.values() if a.owner_id == identity_id}
        found["usage"] = {u.id for u in view.usage.values() if u.account_id in found["accounts"]}
        problems += [
            f"{identity_id} joins without access, yet {name} {sorted(ids)} are in the view"
            for name, ids in found.items()
            if ids
        ]

    for m in view.memberships.values():
        role = view.roles.get(m.role_id)
        if role is not None and m.source is not SOURCE_FOR_ROLE_KIND[role.kind]:
            problems.append(f"{m.id}: source {m.source} in a {role.kind} role")

    # SPEC 0.4.1 §9.2: every provisioned (account, entitlement) pair has a usage record.
    pairs = {(u.account_id, u.entitlement_id) for u in view.usage.values()}
    provisioned = {(a.account_id, a.entitlement_id) for a in view.assignments.values()}
    for m in view.memberships.values():
        for entitlement_id in expand_role(view.roles, m.role_id):
            app_id = view.entitlements[entitlement_id].app_id
            accounts = [
                a.id
                for a in view.accounts.values()
                if a.identity_id == m.identity_id
                and a.app_id == app_id
                and a.kind is AccountKind.human
            ]
            if not accounts:
                problems.append(f"{m.id}: no account in {app_id} for {entitlement_id}")
            provisioned.update((account_id, entitlement_id) for account_id in accounts)
    problems.extend(f"no usage record for {pair}" for pair in sorted(provisioned - pairs))
    return problems


def role_closure(world: World, role_ids: Iterable[str]) -> set[str]:
    """The roles and their child roles at any depth."""
    found: set[str] = set()
    pending = list(role_ids)
    while pending:
        role_id = pending.pop()
        if role_id not in found:
            found.add(role_id)
            pending.extend(world.roles[role_id].child_roles)
    return found


def task_problems(world: World, view: CaseView) -> list[str]:
    """The records each task adds beyond the focal closures (the view builder's "Per task")."""
    problems: list[str] = []

    def need(collection: str, record_id: str, why: str) -> None:
        if record_id not in getattr(view, collection):
            problems.append(f"{why}: {record_id} not in {collection}")

    index = index_for(world)
    if (request := view.request) is not None:
        need("identities", request.requester_id, "requester")
        manager_id = world.identities[request.beneficiary_id].manager_id
        if manager_id is not None:
            need("identities", manager_id, "beneficiary's manager")
        entitlement = world.entitlements[request.entitlement_id]
        need("entitlements", entitlement.id, "requested entitlement")
        need("applications", entitlement.app_id, "requested entitlement's application")
        need("identities", world.applications[entitlement.app_id].owner_id, "application owner")
        if request.ticket_ref in world.tickets:
            need("tickets", request.ticket_ref, "ticket_ref")
    if (campaign := view.campaign) is not None:
        need("identities", campaign.reviewer_id, "reviewer")
        manager_scope = campaign.scope_kind is CampaignScopeKind.manager
        need("identities" if manager_scope else "applications", campaign.scope_ref, "scope")
        for item in campaign.items:
            if item.kind is ReviewItemKind.membership:
                need("memberships", item.ref, item.item_id)
                continue
            need("assignments", item.ref, item.item_id)
            owner_id = world.accounts[world.assignments[item.ref].account_id].owner_id
            if owner_id is not None:
                need("identities", owner_id, "service account owner")
    if (event := view.event) is not None:
        for manager_id in index.manager_chain(event.identity_id):
            need("identities", manager_id, "manager chain")
        for attributes in (event.before, event.after):
            if attributes is not None and attributes.manager_id is not None:
                need("identities", attributes.manager_id, "event's manager")
        birthright = {r.id for r in world.roles.values() if r.kind is RoleKind.birthright}
        if event.type in BIRTHRIGHT_MATCHING_EVENTS:
            for role_id in sorted(birthright):
                need("roles", role_id, "birthright role")
        else:
            # Leaver and leave plans read no role outside the subject's closure.
            held = [m.role_id for m in index.memberships_by_identity.get(event.identity_id, ())]
            named = [r.role_id for r in view.history.values() if r.role_id is not None]
            stray = (set(view.roles) & birthright) - role_closure(world, [*held, *named])
            if stray:
                problems.append(f"{event.type.value} view carries birthright roles {sorted(stray)}")
        if event.type not in FRESH_START_EVENTS:
            for account in index.service_accounts_owned_by.get(event.identity_id, ()):
                need("accounts", account.id, "owned service account")
                fallback = world.applications[account.app_id].owner_id
                need("identities", fallback, "transfer fallback owner")
    for record in view.history.values():
        if record.role_id is not None:
            need("roles", record.role_id, record.id)
        if record.entitlement_id is not None:
            need("entitlements", record.entitlement_id, record.id)
    return problems


def view_problems(world: World, view: CaseView, extra_focal: Sequence[str] = ()) -> list[str]:
    """Every closure and task check for a view, with the focal identities its header implies
    (plus ``extra_focal``)."""
    hires: list[str] = []
    if view.request is not None:
        focal = [view.request.beneficiary_id]
    elif view.campaign is not None:
        focal = holders(world, view.campaign)
    else:
        assert view.event is not None
        subject = [view.event.identity_id]
        focal, hires = ([], subject) if view.event.type in FRESH_START_EVENTS else (subject, [])
    return closure_problems(world, view, [*focal, *extra_focal], hires) + task_problems(world, view)


def faithfulness_problems(world: World, view: CaseView) -> list[str]:
    """Records that aren't byte-for-byte the world's record with the same ID."""
    problems: list[str] = []
    for name in COLLECTIONS:
        source = getattr(world, name)
        for record_id, record in getattr(view, name).items():
            if source.get(record_id) != record:
                problems.append(f"{name}.{record_id} differs from the world")
    return problems


def _without(view: CaseView, collection: str, drop: Collection[str]) -> CaseView:
    records = getattr(view, collection)
    return view.model_copy(update={collection: {k: v for k, v in records.items() if k not in drop}})


def test_closure_checks_catch_gaps(startup_world: World) -> None:
    """The checks above flag a missing usage record, a dangling reference, a focal identity's
    missing record, a missing task addition and a joiner's leaked access."""
    world = startup_world
    builder = ViewBuilder(world)
    beneficiary = world.identities[sorted(world.identities)[0]]
    entitlement = next(
        e
        for e in sorted(world.entitlements.values(), key=lambda e: e.id)
        if world.applications[e.app_id].owner_id != beneficiary.id
    )
    request = AccessRequest(
        request_id="req_00001",
        requester_id=beneficiary.id,
        beneficiary_id=beneficiary.id,
        entitlement_id=entitlement.id,
        justification="x",
        submitted_on=world.anchor_date,
    )
    view = builder.build_view(request)
    assert not view_problems(world, view)
    usage = view_problems(world, _without(view, "usage", [sorted(view.usage)[0]]))
    assert any(p.startswith("no usage record") for p in usage)
    role = next(r for r in view.roles.values() if r.entitlements)
    dangling = view_problems(world, _without(view, "entitlements", role.entitlements))
    assert any(p.startswith(f"{role.id}.entitlements") for p in dangling)
    membership = sorted(view.memberships)[0]
    dropped = view_problems(world, _without(view, "memberships", [membership]))
    assert any(membership in p and "memberships missing" in p for p in dropped)
    owner = world.applications[entitlement.app_id].owner_id
    no_owner = view_problems(world, _without(view, "identities", [owner]))
    assert any(p.startswith("application owner") for p in no_owner)

    joiner = HrEvent(
        event_id="evt_00001",
        type=HrEventType.joiner,
        identity_id=beneficiary.id,
        effective_on=world.anchor_date,
        after=attributes_of(beneficiary),
    )
    hire_view = builder.build_view(joiner)
    assert not view_problems(world, hire_view)
    held = next(
        m
        for m in index_for(world).memberships_by_identity[beneficiary.id]
        if m.role_id in hire_view.roles
    )
    leaked = hire_view.model_copy(update={"memberships": {held.id: held}})
    assert any("joins without access" in p for p in view_problems(world, leaked))


# --------------------------------------------------------------------------------------------
# Completeness: the oracle accepts every view
# --------------------------------------------------------------------------------------------


def test_request_views_are_complete(world: World) -> None:
    rng = random.Random(10)
    ids = IdAllocator(world, rng)
    builder = ViewBuilder(world)
    for _ in range(150):
        request = random_request(world, rng, ids)
        spec = DistractorSpec(peers=rng.randint(0, 3), tickets=rng.randint(0, 2))
        view = builder.build_view(request, distractors=spec, rng=rng)
        gold = decide_request(view)
        # Answerability: the serialized view gives the same answer (SPEC §9.2).
        assert decide_request(CaseView.model_validate_json(view.model_dump_json())) == gold
        assert not view_problems(world, view)


def test_review_views_are_complete(world: World) -> None:
    rng = random.Random(11)
    ids = IdAllocator(world, rng)
    builder = ViewBuilder(world)
    for _ in range(25):
        campaign = random_campaign(world, rng, ids)
        view = builder.build_view(campaign, distractors=DistractorSpec(peers=2), rng=rng)
        gold = decide_review(view)
        assert [item.item_id for item in gold.items] == [i.item_id for i in campaign.items]
        assert not view_problems(world, view)


@pytest.mark.parametrize("kind", EVENT_TYPES, ids=lambda k: k.value)
def test_jml_views_are_complete(world: World, kind: HrEventType) -> None:
    rng = random.Random(12)
    ids = IdAllocator(world, rng)
    builder = ViewBuilder(world)
    for _ in range(12):
        event = random_event(world, rng, ids, kind)
        history = prior_stint(world, event, ids)
        view = builder.build_view(event, history=history)
        plan_jml(view)
        assert not view_problems(world, view)
        assert set(view.history) == {record.id for record in history}


@pytest.mark.parametrize("kind", sorted(FRESH_START_EVENTS), ids=lambda k: k.value)
def test_joiner_and_rehire_views_plan_as_built(world: World, kind: HrEventType) -> None:
    """Every identity of both worlds can be the subject of a joiner or rehire case: the view
    leaves their access out, and ``plan_jml`` grants exactly the matching birthright roles."""
    ids = IdAllocator(world, random.Random(13))
    builder = ViewBuilder(world)
    for n, identity in enumerate(sorted(world.identities.values(), key=lambda i: i.id)):
        event = HrEvent(
            event_id=ids.event_id(),
            type=kind,
            identity_id=identity.id,
            effective_on=world.anchor_date + timedelta(days=n % 9 - 3),
            after=attributes_of(identity),
        )
        view = builder.build_view(event)
        gold = plan_jml(view)
        matching = {
            role.id
            for role in world.roles.values()
            if role.attribute_rule is not None and role.attribute_rule.matches(identity)
        }
        assert {op.op for op in gold.operations} <= {OpType.GRANT_ROLE}
        assert {op.role_id for op in gold.operations} == matching
        assert not view.memberships and not view.assignments and not view.usage
        assert not view.mitigating_controls and not view.accounts
        if n % 10 == 0:
            assert not view_problems(world, view)


def test_joiner_and_rehire_subjects_refuse_their_access(startup_world: World) -> None:
    world = startup_world
    index = index_for(world)
    builder = ViewBuilder(world)
    controlled = sorted(index.controls_by_identity)[0]
    owner = sorted(index.service_accounts_owned_by)[0]

    def hire(identity_id: str, kind: HrEventType = HrEventType.joiner) -> HrEvent:
        return HrEvent(
            event_id="evt_00001",
            type=kind,
            identity_id=identity_id,
            effective_on=world.anchor_date,
            after=attributes_of(world.identities[identity_id]),
        )

    human = index.accounts_by_identity[controlled][0]
    control = index.controls_by_identity[controlled][0]
    service = index.service_accounts_owned_by[owner][0]
    with pytest.raises(ViewBuildError, match="joiner or rehire"):
        builder.build_view(hire(controlled), focal=[controlled])
    with pytest.raises(ViewBuildError, match=f"account {human.id}"):
        builder.build_view(hire(controlled, HrEventType.rehire), extra=[human.id])
    with pytest.raises(ViewBuildError, match=f"mitigating control {control.id}"):
        builder.build_view(hire(controlled), extra=[control.id])
    with pytest.raises(ViewBuildError, match=f"service account {service.id}"):
        builder.build_view(hire(owner), extra=[service.id])
    # Everyone else's records still come in: another person's closure and accounts.
    other = next(i for i in sorted(index.accounts_by_identity) if i not in (controlled, owner))
    view = builder.build_view(hire(controlled), focal=[other])
    assert not view_problems(world, view, extra_focal=[other])
    plan_jml(view)


def test_distractors_never_add_a_hires_records(startup_world: World) -> None:
    """A joiner's or rehire's subject counts as focal for distractors: drawing every other
    expired control leaves the subject's own out, and one more is a shortage."""
    world = startup_world
    expired = [c for c in world.mitigating_controls.values() if c.expires_on < world.anchor_date]
    subject = sorted(c.identity_id for c in expired)[0]
    others = {c.id for c in expired if c.identity_id != subject}
    event = HrEvent(
        event_id="evt_00001",
        type=HrEventType.rehire,
        identity_id=subject,
        effective_on=world.anchor_date,
        after=attributes_of(world.identities[subject]),
    )
    builder = ViewBuilder(world)
    view = builder.build_view(
        event, distractors=DistractorSpec(expired_controls=len(others)), rng=random.Random(0)
    )
    assert set(view.mitigating_controls) == others
    with pytest.raises(DistractorShortage):
        builder.build_view(
            event,
            distractors=DistractorSpec(expired_controls=len(others) + 1),
            rng=random.Random(0),
        )


# --------------------------------------------------------------------------------------------
# Faithfulness, ordering and determinism
# --------------------------------------------------------------------------------------------


def test_views_are_faithful_ordered_slices(world: World) -> None:
    for view in random_views(world, seed=20, n=24):
        assert not faithfulness_problems(world, view)
        for name in (*COLLECTIONS, "history", "peer_stats"):
            keys = list(getattr(view, name))
            assert keys == sorted(keys), name
        assert view.org_name == world.org_name
        assert view.policy_pack == world.policy_pack
        assert view.as_of == world.anchor_date


def fingerprint(world: World) -> str:
    """SHA-256 over a fixed mix of views with their gold, tiers and tags: requests with
    distractors and peer stats, campaigns with distractors, and an event of every type (the
    rehire with history). Computed in fresh interpreters, it shows nothing depends on hash order.
    """
    digest = hashlib.sha256()
    rng = random.Random(90)
    ids = IdAllocator(world, rng)
    builder = ViewBuilder(world)

    def record(view: CaseView, gold: RequestGold | ReviewGold | JmlGold, task: Task) -> None:
        tags = compute_tags(view, gold, task, distractors=3)
        for part in (
            view.model_dump_json(),
            gold.model_dump_json(),
            compute_tier(view, gold, task),
        ):
            digest.update(part.encode())
        digest.update(",".join(tags).encode())

    for _ in range(6):
        request = random_request(world, rng, ids)
        view = builder.build_view(
            request,
            distractors=DistractorSpec(peers=2, tickets=1),
            peer_stats=[(request.beneficiary_id, request.entitlement_id)],
            rng=rng,
        )
        record(view, decide_request(view), Task.access_request)
    for _ in range(3):
        view = builder.build_view(
            random_campaign(world, rng, ids), distractors=DistractorSpec(peers=3), rng=rng
        )
        record(view, decide_review(view), Task.access_review)
    for kind in EVENT_TYPES:
        event = random_event(world, rng, ids, kind)
        view = builder.build_view(event, history=prior_stint(world, event, ids))
        record(view, plan_jml(view), Task.jml)
    return digest.hexdigest()


def test_builds_do_not_depend_on_hash_order(startup_world: World) -> None:
    """Same inputs, same bytes: within this process, and in fresh interpreters whose string
    hashing (so set iteration order) differs."""
    expected = fingerprint(startup_world)
    assert fingerprint(startup_world) == expected
    script = (
        "from identitybench.world.generator import build_world\n"
        "from tests.tasks.test_view_builder import fingerprint\n"
        f"print(fingerprint(build_world({startup_world.profile!r}, {startup_world.seed})))\n"
    )
    for hash_seed in ("0", "1", "4242"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO_ROOT,
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected, f"PYTHONHASHSEED={hash_seed}"


def test_input_order_does_not_matter(midmarket_world: World) -> None:
    world = midmarket_world
    rng = random.Random(31)
    ids = IdAllocator(world, rng)
    request = random_request(world, rng, ids)
    people = sorted(world.identities)[:5]
    extra = [sorted(world.tickets)[0], sorted(world.roles)[0], people[0]]
    builder = ViewBuilder(world)
    first = builder.build_view(request, focal=people, extra=extra)
    second = builder.build_view(request, focal=reversed(people), extra=reversed(extra))
    assert first.model_dump_json() == second.model_dump_json()
    assert not view_problems(world, first, extra_focal=people)


def test_as_of_override(startup_world: World) -> None:
    """A later as-of date is taken as given, peer stats included; an earlier one would put the
    world's records in the view's future and is refused."""
    world = startup_world
    as_of = world.anchor_date + timedelta(days=23)
    builder = ViewBuilder(world, as_of=as_of)
    request = random_request(world, random.Random(1), IdAllocator(world, random.Random(1)))
    pair = (request.beneficiary_id, request.entitlement_id)
    view = builder.build_view(request, peer_stats=[pair])
    assert view.as_of == as_of
    share = view.peer_stats[pair[0]][pair[1]]
    assert share == round(peer_share(world, *pair, as_of), 2)
    assert (
        ViewBuilder(world, as_of=world.anchor_date).build_view(request).as_of == world.anchor_date
    )
    with pytest.raises(ViewBuildError, match="before"):
        ViewBuilder(world, as_of=world.anchor_date - timedelta(days=1))


# --------------------------------------------------------------------------------------------
# What each task adds
# --------------------------------------------------------------------------------------------


def test_request_view_contents(midmarket_world: World) -> None:
    world = midmarket_world
    people = sorted(world.identities)
    entitlement = world.entitlements[sorted(world.entitlements)[3]]
    ticket = sorted(world.tickets)[0]
    request = AccessRequest(
        request_id="req_00001",
        requester_id=people[1],
        beneficiary_id=people[0],
        entitlement_id=entitlement.id,
        ticket_ref=ticket,
        justification="x",
        submitted_on=world.anchor_date,
    )
    view = ViewBuilder(world).build_view(request)
    owner = world.applications[entitlement.app_id].owner_id
    assert {people[0], people[1], owner} <= set(view.identities)
    assert entitlement.id in view.entitlements
    assert entitlement.app_id in view.applications
    assert set(view.tickets) == {ticket}
    unknown = request.model_copy(update={"ticket_ref": "INC-00002"})
    assert not ViewBuilder(world).build_view(unknown).tickets


def test_review_view_contents(midmarket_world: World) -> None:
    world = midmarket_world
    index = index_for(world)
    service = next(
        a
        for a in sorted(world.accounts.values(), key=lambda a: a.id)
        if a.kind is AccountKind.service
        and a.owner_id is not None
        and len(index.assignments_by_account.get(a.id, ())) >= 2
    )
    orphan = next(
        a
        for a in sorted(world.accounts.values(), key=lambda a: a.id)
        if a.kind is AccountKind.human
        and a.identity_id is None
        and index.assignments_by_account.get(a.id)
    )
    items = (
        ReviewItem(
            item_id="rv_00001",
            kind=ReviewItemKind.assignment,
            ref=index.assignments_by_account[service.id][0].id,
        ),
        ReviewItem(
            item_id="rv_00002",
            kind=ReviewItemKind.assignment,
            ref=index.assignments_by_account[orphan.id][0].id,
        ),
    )
    app_id = service.app_id
    reviewer = sorted(world.identities)[0]
    campaign = Campaign(
        campaign_id="cmp_00001",
        reviewer_id=reviewer,
        scope_kind=CampaignScopeKind.application,
        scope_ref=app_id,
        items=items,
    )
    view = ViewBuilder(world).build_view(campaign)
    assert service.owner_id in view.identities
    assert reviewer in view.identities
    assert app_id in view.applications
    for account in (service, orphan):
        assigned = {a.id for a in index.assignments_by_account[account.id]}
        assert assigned <= set(view.assignments)
    decide_review(view)


def test_jml_view_contents(midmarket_world: World) -> None:
    """A leave start brings the manager chain and the owned service accounts with their
    fallback owners, but no role outside the subject's closure; a move for the same person
    brings every birthright role of the organization with its expansion."""
    world = midmarket_world
    index = index_for(world)
    owner_id = next(o for o in sorted(index.service_accounts_owned_by) if index.manager_chain(o))
    identity = world.identities[owner_id]
    builder = ViewBuilder(world)
    leave = builder.build_view(
        HrEvent(
            event_id="evt_00001",
            type=HrEventType.leave_start,
            identity_id=owner_id,
            effective_on=world.anchor_date + timedelta(days=9),
            before=attributes_of(identity),
            after=attributes_of(identity),
        )
    )
    held = role_closure(world, [m.role_id for m in index.memberships_by_identity[owner_id]])
    birthright = {r.id for r in world.roles.values() if r.kind is RoleKind.birthright}
    assert set(leave.roles) == held
    assert birthright - held, "the subject holds every birthright role: nothing was left out"
    assert set(index.manager_chain(owner_id)) <= set(leave.identities)
    for account in index.service_accounts_owned_by[owner_id]:
        assert account.id in leave.accounts
        assert world.applications[account.app_id].owner_id in leave.identities

    departments = sorted({i.department for i in world.identities.values()} - {identity.department})
    move = builder.build_view(
        HrEvent(
            event_id="evt_00002",
            type=HrEventType.mover,
            identity_id=owner_id,
            effective_on=world.anchor_date + timedelta(days=9),
            before=attributes_of(identity),
            after=attributes_of(identity).model_copy(update={"department": departments[0]}),
        )
    )
    assert birthright <= set(move.roles)
    for role_id in birthright:
        assert index.graph.expansion(role_id) <= set(move.entitlements)
    plan_jml(leave)
    plan_jml(move)


def test_history_must_belong_to_someone_in_the_view(startup_world: World) -> None:
    world = startup_world
    identity = world.identities[sorted(world.identities)[0]]
    event = HrEvent(
        event_id="evt_00001",
        type=HrEventType.rehire,
        identity_id=identity.id,
        effective_on=world.anchor_date,
        after=attributes_of(identity),
    )
    builder = ViewBuilder(world)
    base = builder.build_view(event)
    stranger = next(i for i in sorted(world.identities) if i not in base.identities)
    record = PriorAccessRecord(
        id="hist_00001",
        identity_id=stranger,
        entitlement_id=sorted(world.entitlements)[0],
        held_from=date(2020, 1, 1),
        held_until=date(2021, 1, 1),
    )
    with pytest.raises(ViewBuildError, match="isn't in the view"):
        builder.build_view(event, history=[record])
    clash = record.model_copy(update={"identity_id": identity.id, "id": identity.id})
    with pytest.raises(ViewBuildError, match="clash"):
        builder.build_view(event, history=[clash])
    twice = record.model_copy(update={"identity_id": identity.id})
    with pytest.raises(ViewBuildError, match="repeat"):
        builder.build_view(event, history=[twice, twice])
    with pytest.raises(ViewBuildError, match="repeat"):
        builder.build_view(event, history=[twice.model_copy(update={"id": "evt_00001"})])
    view = builder.build_view(event, history=[twice])
    assert view.history == {"hist_00001": twice}
    assert sorted(world.entitlements)[0] in view.entitlements
    plan_jml(view)


# --------------------------------------------------------------------------------------------
# Input errors
# --------------------------------------------------------------------------------------------


def test_synthesized_ids_must_not_shadow_records(startup_world: World) -> None:
    world = startup_world
    builder = ViewBuilder(world)
    person = sorted(world.identities)[0]
    request = AccessRequest(
        request_id=person,
        requester_id=person,
        beneficiary_id=person,
        entitlement_id=sorted(world.entitlements)[0],
        justification="x",
        submitted_on=world.anchor_date,
    )
    with pytest.raises(ViewBuildError, match="clash"):
        builder.build_view(request)
    membership = next(m for m in world.memberships.values() if m.identity_id == person)
    campaign = Campaign(
        campaign_id="cmp_00001",
        reviewer_id=person,
        scope_kind=CampaignScopeKind.manager,
        scope_ref=person,
        items=(
            ReviewItem(item_id="rv_00001", kind=ReviewItemKind.membership, ref=membership.id),
            ReviewItem(item_id="rv_00001", kind=ReviewItemKind.membership, ref=membership.id),
        ),
    )
    with pytest.raises(ViewBuildError, match="repeat"):
        builder.build_view(campaign)


def test_unknown_records_are_rejected(startup_world: World) -> None:
    world = startup_world
    builder = ViewBuilder(world)
    person = sorted(world.identities)[0]
    request = AccessRequest(
        request_id="req_00001",
        requester_id=person,
        beneficiary_id="u_00000",
        entitlement_id=sorted(world.entitlements)[0],
        justification="x",
        submitted_on=world.anchor_date,
    )
    with pytest.raises(ViewBuildError, match="u_00000"):
        builder.build_view(request)
    request = request.model_copy(update={"beneficiary_id": person, "entitlement_id": "ent_nope"})
    with pytest.raises(ViewBuildError, match="ent_nope"):
        builder.build_view(request)
    membership = sorted(world.memberships)[0]
    wrong_kind = Campaign(
        campaign_id="cmp_00001",
        reviewer_id=person,
        scope_kind=CampaignScopeKind.manager,
        scope_ref=person,
        items=(ReviewItem(item_id="rv_00001", kind=ReviewItemKind.assignment, ref=membership),),
    )
    with pytest.raises(ViewBuildError, match="assignment"):
        builder.build_view(wrong_kind)
    joiner = HrEvent(
        event_id="evt_00001",
        type=HrEventType.joiner,
        identity_id="u_00000",
        effective_on=world.anchor_date,
        after=attributes_of(world.identities[person]),
    )
    with pytest.raises(ViewBuildError, match="u_00000"):
        builder.build_view(joiner)


def role_reached_account(world: World) -> tuple[str, Account, frozenset[str]]:
    """A member, their human account in an application one of their roles reaches, and the
    role-derived entitlements on that application (the first such account in ID order)."""
    index = index_for(world)
    for account in sorted(world.accounts.values(), key=lambda a: a.id):
        holder = account.identity_id
        if account.kind is not AccountKind.human or holder is None:
            continue
        reached = frozenset(
            e
            for m in index.memberships_by_identity.get(holder, ())
            for e in index.graph.expansion(m.role_id)
            if world.entitlements[e].app_id == account.app_id
        )
        if reached:
            return holder, account, reached
    raise AssertionError(f"no role reaches a human account in {world.world_id}")


def free_id(records: Collection[str], prefix: str) -> str:
    return next(f"{prefix}{n}" for n in range(99_999, 0, -1) if f"{prefix}{n}" not in records)


def test_role_usage_covers_every_human_account_in_the_application(startup_world: World) -> None:
    """An identity with two human accounts in an application its role reaches (a disabled old
    account beside the live one) needs a usage record through each: the oracle provisions the
    role's entitlements on every human account there (``RecordIndex.provisioned_pairs``). The
    copied world carries both accounts' records, so the view must too."""
    world = startup_world
    holder, account, reached = role_reached_account(world)
    second = account.model_copy(
        update={
            "id": free_id(world.accounts, "acct_"),
            "status": AccountStatus.disabled,
            "suspension_reason": None,
        }
    )
    usage = dict(world.usage)
    for entitlement_id in sorted(reached):
        usage_id = free_id(usage, "use_")
        usage[usage_id] = UsageRecord(
            id=usage_id,
            account_id=second.id,
            entitlement_id=entitlement_id,
            last_used_on=None,
            telemetry=TelemetryStatus.available,
        )
    copied = world.model_copy(
        update={"accounts": {**world.accounts, second.id: second}, "usage": usage}
    )
    request = AccessRequest(
        request_id="req_00001",
        requester_id=holder,
        beneficiary_id=holder,
        entitlement_id=sorted(world.entitlements)[0],
        justification="x",
        submitted_on=world.anchor_date,
    )
    view = ViewBuilder(copied).build_view(request)
    assert {account.id, second.id} <= set(view.accounts)
    for account_id in (account.id, second.id):
        covered = {u.entitlement_id for u in view.usage.values() if u.account_id == account_id}
        assert reached <= covered
    decide_request(view)
    assert not view_problems(copied, view)


def test_role_usage_needs_a_human_account(startup_world: World) -> None:
    """With the member's only account in the application gone, the role's entitlements there
    have nowhere to be used, and the builder refuses rather than yield a view without them."""
    world = startup_world
    holder, account, _ = role_reached_account(world)
    copied = world.model_copy(
        update={
            "accounts": {k: v for k, v in world.accounts.items() if k != account.id},
            "assignments": {
                k: v for k, v in world.assignments.items() if v.account_id != account.id
            },
            "usage": {k: v for k, v in world.usage.items() if v.account_id != account.id},
        }
    )
    request = AccessRequest(
        request_id="req_00001",
        requester_id=holder,
        beneficiary_id=holder,
        entitlement_id=sorted(world.entitlements)[0],
        justification="x",
        submitted_on=world.anchor_date,
    )
    with pytest.raises(ViewBuildError, match="no human account"):
        ViewBuilder(copied).build_view(request)


@pytest.mark.parametrize("collection", ["memberships", "assignments", "usage"])
def test_records_without_their_holder_are_refused_as_extras(
    startup_world: World, collection: str
) -> None:
    world = startup_world
    request = random_request(world, random.Random(2), IdAllocator(world, random.Random(2)))
    record_id = sorted(getattr(world, collection))[0]
    with pytest.raises(ViewBuildError):
        ViewBuilder(world).build_view(request, extra=[record_id])
    with pytest.raises(ViewBuildError, match="not a record"):
        ViewBuilder(world).build_view(request, extra=["xyz_123"])


def test_extras_bring_what_they_need(midmarket_world: World) -> None:
    world = midmarket_world
    index = index_for(world)
    request = random_request(world, random.Random(3), IdAllocator(world, random.Random(3)))
    nested = next(r for r in sorted(world.roles.values(), key=lambda r: r.id) if r.child_roles)
    control = sorted(world.mitigating_controls.values(), key=lambda c: c.id)[0]
    human = next(
        a
        for a in sorted(world.accounts.values(), key=lambda a: a.id)
        if a.identity_id is not None and a.identity_id != request.beneficiary_id
    )
    view = ViewBuilder(world).build_view(request, extra=[nested.id, control.id, human.id])
    assert set(nested.child_roles) <= set(view.roles)
    assert index.graph.expansion(nested.id) <= set(view.entitlements)
    assert control.identity_id in view.identities
    assert human.identity_id is not None
    assert not view_problems(world, view, extra_focal=[human.identity_id])
    decide_request(view)


# --------------------------------------------------------------------------------------------
# Distractors
# --------------------------------------------------------------------------------------------


def _request_with_baseline(world: World, seed: int) -> tuple[ViewBuilder, AccessRequest, CaseView]:
    rng = random.Random(seed)
    request = random_request(world, rng, IdAllocator(world, rng))
    builder = ViewBuilder(world)
    return builder, request, builder.build_view(request)


def test_peer_distractors(midmarket_world: World) -> None:
    builder, request, base = _request_with_baseline(midmarket_world, 40)
    view = builder.build_view(request, distractors=DistractorSpec(peers=4), rng=random.Random(0))
    added = set(view.identities) - set(base.identities)
    assert len(added) == 4
    department = midmarket_world.identities[request.beneficiary_id].department
    assert {midmarket_world.identities[i].department for i in added} == {department}
    assert set(view.memberships) == set(base.memberships)
    assert set(view.accounts) == set(base.accounts)


def test_peer_distractors_from_a_named_anchor(midmarket_world: World) -> None:
    """``peers_of`` picks the department the peers come from, instead of the beneficiary's."""
    world = midmarket_world
    builder, request, base = _request_with_baseline(world, 47)
    own = world.identities[request.beneficiary_id].department
    anchor = next(
        i for i in sorted(world.identities.values(), key=lambda i: i.id) if i.department != own
    )
    spec = DistractorSpec(peers=4, peers_of=anchor.id)
    view = builder.build_view(request, distractors=spec, rng=random.Random(0))
    added = set(view.identities) - set(base.identities)
    assert len(added) == 4
    assert {world.identities[i].department for i in added} == {anchor.department}
    with pytest.raises(ViewBuildError, match="u_00000"):
        builder.build_view(
            request, distractors=DistractorSpec(peers=1, peers_of="u_00000"), rng=random.Random(0)
        )


def test_peers_top_up_outside_a_small_department(startup_world: World) -> None:
    world = startup_world
    builder, request, base = _request_with_baseline(world, 41)
    department = world.identities[request.beneficiary_id].department
    free_peers = sum(
        1
        for i in world.identities.values()
        if i.department == department and i.id not in base.identities
    )
    spec = DistractorSpec(peers=free_peers + 3)
    view = builder.build_view(request, distractors=spec, rng=random.Random(0))
    added = set(view.identities) - set(base.identities)
    assert len(added) == free_peers + 3
    outsiders = [i for i in added if world.identities[i].department != department]
    assert len(outsiders) == 3


def test_expired_control_distractors(midmarket_world: World) -> None:
    world = midmarket_world
    builder, request, base = _request_with_baseline(world, 42)
    spec = DistractorSpec(expired_controls=3)
    view = builder.build_view(request, distractors=spec, rng=random.Random(0))
    added = set(view.mitigating_controls) - set(base.mitigating_controls)
    assert len(added) == 3
    for control_id in added:
        control = world.mitigating_controls[control_id]
        assert control.expires_on < world.anchor_date
        assert control.identity_id != request.beneficiary_id
        assert control.identity_id in view.identities


def test_ticket_and_entitlement_distractors(midmarket_world: World) -> None:
    world = midmarket_world
    sizes: dict[str, int] = {}
    for entitlement in world.entitlements.values():
        sizes[entitlement.app_id] = sizes.get(entitlement.app_id, 0) + 1
    largest = max(sorted(sizes), key=sizes.__getitem__)
    builder, request, _ = _request_with_baseline(world, 43)
    request = request.model_copy(
        update={
            "entitlement_id": min(e.id for e in world.entitlements.values() if e.app_id == largest),
            "ticket_ref": sorted(world.tickets)[0],
        }
    )
    base = builder.build_view(request)
    spec = DistractorSpec(tickets=3, entitlements=2)
    view = builder.build_view(request, distractors=spec, rng=random.Random(0))
    assert len(set(view.tickets) - set(base.tickets)) == 3
    assert request.ticket_ref not in set(view.tickets) - set(base.tickets)
    added = set(view.entitlements) - set(base.entitlements)
    assert len(added) == 2
    app_id = world.entitlements[request.entitlement_id].app_id
    assert {world.entitlements[e].app_id for e in added} == {app_id}


def free_expired_controls(world: World, view: CaseView) -> list[str]:
    """Expired controls of people whose access isn't in the view (distractor candidates)."""
    focal = {m.identity_id for m in view.memberships.values()}
    focal |= {a.identity_id for a in view.accounts.values() if a.identity_id is not None}
    return [
        c.id
        for c in world.mitigating_controls.values()
        if c.expires_on < view.as_of and c.identity_id not in focal
    ]


def free_entitlements(world: World, view: CaseView, request: AccessRequest) -> list[str]:
    app_id = world.entitlements[request.entitlement_id].app_id
    return [
        e.id
        for e in world.entitlements.values()
        if e.app_id == app_id and e.id not in view.entitlements
    ]


def test_distractors_never_change_gold(world: World) -> None:
    rng = random.Random(44)
    ids = IdAllocator(world, rng)
    builder = ViewBuilder(world)
    for _ in range(40):
        request = random_request(world, rng, ids)
        plain = builder.build_view(request)
        spec = DistractorSpec(
            peers=2,
            tickets=2,
            expired_controls=min(1, len(free_expired_controls(world, plain))),
            entitlements=min(1, len(free_entitlements(world, plain, request))),
        )
        noisy = builder.build_view(request, distractors=spec, rng=rng)
        assert decide_request(noisy) == decide_request(plain)
    for _ in range(4):
        campaign = random_campaign(world, rng, ids)
        plain = builder.build_view(campaign)
        campaign_spec = DistractorSpec(
            peers=2, tickets=1, expired_controls=min(1, len(free_expired_controls(world, plain)))
        )
        noisy = builder.build_view(campaign, distractors=campaign_spec, rng=rng)
        assert decide_review(noisy) == decide_review(plain)
    for kind in EVENT_TYPES:
        event = random_event(world, rng, ids, kind)
        event_spec = DistractorSpec(
            peers=2, tickets=1, entitlements=1, entitlements_of=sorted(world.applications)[0]
        )
        plain = builder.build_view(event)
        noisy = builder.build_view(event, distractors=event_spec, rng=rng)
        assert plan_jml(noisy) == plan_jml(plain)


def test_distractor_draws_are_seeded(startup_world: World) -> None:
    builder, request, _ = _request_with_baseline(startup_world, 45)
    spec = DistractorSpec(peers=3, tickets=2, entitlements=1)
    first = builder.build_view(request, distractors=spec, rng=random.Random(7))
    second = builder.build_view(request, distractors=spec, rng=random.Random(7))
    other = builder.build_view(request, distractors=spec, rng=random.Random(8))
    assert first.model_dump_json() == second.model_dump_json()
    assert first.model_dump_json() != other.model_dump_json()


def test_distractor_errors(startup_world: World) -> None:
    world = startup_world
    builder, request, _ = _request_with_baseline(world, 46)
    with pytest.raises(ValueError, match="rng"):
        builder.build_view(request, distractors=DistractorSpec(peers=1))
    with pytest.raises(DistractorShortage):
        builder.build_view(request, distractors=DistractorSpec(tickets=10_000), rng=random.Random())
    with pytest.raises(DistractorShortage):
        builder.build_view(
            request, distractors=DistractorSpec(expired_controls=500), rng=random.Random()
        )
    identity = world.identities[request.beneficiary_id]
    event = HrEvent(
        event_id="evt_00001",
        type=HrEventType.leave_start,
        identity_id=identity.id,
        effective_on=world.anchor_date,
        before=attributes_of(identity),
        after=attributes_of(identity),
    )
    with pytest.raises(ViewBuildError, match="entitlements_of"):
        builder.build_view(event, distractors=DistractorSpec(entitlements=1), rng=random.Random())
    with pytest.raises(ValueError):
        DistractorSpec(peers=-1)


def test_distractor_total() -> None:
    assert DistractorSpec().total == 0
    assert DistractorSpec(peers=2, tickets=1, expired_controls=3, entitlements=4).total == 10


# --------------------------------------------------------------------------------------------
# Peer stats
# --------------------------------------------------------------------------------------------


def test_peer_stats_match_the_world(world: World) -> None:
    rng = random.Random(50)
    ids = IdAllocator(world, rng)
    builder = ViewBuilder(world)
    for _ in range(30):
        request = random_request(world, rng, ids)
        held = sorted(
            e
            for m in index_for(world).memberships_by_identity.get(request.beneficiary_id, ())
            for e in index_for(world).graph.expansion(m.role_id)
        )
        pairs = [(request.beneficiary_id, request.entitlement_id)]
        if held:
            pairs.append((request.beneficiary_id, held[0]))
        view = builder.build_view(request, peer_stats=pairs)
        for identity_id, entitlement_id in pairs:
            expected = peer_share(world, identity_id, entitlement_id, world.anchor_date)
            assert view.peer_stats[identity_id][entitlement_id] == round(expected, 2)
            assert 0.0 <= view.peer_stats[identity_id][entitlement_id] <= 1.0
        assert decide_request(view) == decide_request(builder.build_view(request))


def test_peer_stats_use_the_builders_own_index(
    startup_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Peer stats come from the index the builder built at construction. ``index_for`` keeps
    only a few recent worlds, so a build that cycles through more would otherwise rebuild a
    whole world index (held access of everyone) for every view."""
    builder, request, _ = _request_with_baseline(startup_world, 53)

    def evicted(world: World) -> WorldIndex:
        raise AssertionError(f"the builder went back to index_for for {world.world_id}")

    monkeypatch.setattr(query, "index_for", evicted)
    pair = (request.beneficiary_id, request.entitlement_id)
    view = builder.build_view(request, peer_stats=[pair])
    assert request.entitlement_id in view.peer_stats[request.beneficiary_id]


def test_peer_stats_need_both_ends_in_the_view(startup_world: World) -> None:
    builder, request, base = _request_with_baseline(startup_world, 51)
    outsider = next(i for i in sorted(startup_world.identities) if i not in base.identities)
    with pytest.raises(ViewBuildError, match="peer stats"):
        builder.build_view(request, peer_stats=[(outsider, request.entitlement_id)])
    missing = next(e for e in sorted(startup_world.entitlements) if e not in base.entitlements)
    with pytest.raises(ViewBuildError, match="peer stats"):
        builder.build_view(request, peer_stats=[(request.beneficiary_id, missing)])


def test_peer_stats_need_a_freshly_generated_world(startup_world: World) -> None:
    reloaded = World.model_validate_json(startup_world.model_dump_json())
    _, request, _ = _request_with_baseline(startup_world, 52)
    builder = ViewBuilder(reloaded)
    builder.build_view(request)  # everything but peer stats works on a reloaded world
    with pytest.raises(ValueError):
        builder.build_view(request, peer_stats=[(request.beneficiary_id, request.entitlement_id)])


# --------------------------------------------------------------------------------------------
# Size
# --------------------------------------------------------------------------------------------


def test_approx_tokens_counts_compact_json_without_the_policy_pack(startup_world: World) -> None:
    """The policy pack reaches the model as policy text, so it isn't case data."""
    _, _, view = _request_with_baseline(startup_world, 60)
    compact = view.model_dump_json(exclude={"policy_pack"})
    assert approx_tokens(view) == math.ceil(len(compact) / CHARS_PER_TOKEN)
    other_pack = view.model_copy(
        update={"policy_pack": view.policy_pack.model_copy(update={"sod": ()})}
    )
    assert approx_tokens(other_pack) == approx_tokens(view)


def _request_sizes(world: World) -> list[int]:
    rng = random.Random(61)
    ids = IdAllocator(world, rng)
    builder = ViewBuilder(world)
    return [approx_tokens(builder.build_view(random_request(world, rng, ids))) for _ in range(80)]


def test_startup_request_views_meet_the_size_target(startup_world: World) -> None:
    # Measured median ~3.4k; the bounds leave room for the world generator to grow a little
    # without letting a view double.
    sizes = _request_sizes(startup_world)
    low, high = REQUEST_TOKEN_TARGET
    assert low <= statistics.median(sizes) <= 1.2 * high
    assert max(sizes) <= 2 * high


def test_midmarket_request_views_stay_compact(midmarket_world: World) -> None:
    # The mandatory closure of a midmarket employee is ~6k tokens on its own (measured median
    # view 6.7k, see the view_builder docstring); this guards against regressions such as
    # pulling in every application owner or other people's access.
    sizes = _request_sizes(midmarket_world)
    assert statistics.median(sizes) <= 2.5 * REQUEST_TOKEN_TARGET[1]
    assert max(sizes) <= 4 * REQUEST_TOKEN_TARGET[1]


def test_manager_campaigns_fit_the_review_cap(world: World) -> None:
    index = index_for(world)
    builder = ViewBuilder(world)
    rng = random.Random(62)
    ids = IdAllocator(world, rng)
    # Up to six holders: measured worst case ~28k on midmarket (eight holders reach ~36k).
    managers = sorted(m for m, r in index.direct_reports.items() if 2 <= len(r) <= 6)
    for manager in rng.sample(managers, min(25, len(managers))):
        refs = sorted(ref for r in index.direct_reports[manager] for ref in sources_of(world, r))
        rng.shuffle(refs)
        campaign = Campaign(
            campaign_id=ids.campaign_id(),
            reviewer_id=manager,
            scope_kind=CampaignScopeKind.manager,
            scope_ref=manager,
            items=tuple(
                ReviewItem(item_id=ids.item_id(), kind=kind, ref=ref) for kind, ref in refs[:50]
            ),
        )
        assert approx_tokens(builder.build_view(campaign)) <= REVIEW_TOKEN_CAP
