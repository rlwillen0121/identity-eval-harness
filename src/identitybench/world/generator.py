"""Synthetic world generator (SPEC §6, §9.1 step 1).

``build_world(profile, seed)`` is a pure function of its arguments: all randomness flows from
one ``random.Random(seed)`` passed explicitly through every stage, nothing reads the clock, and
no stage iterates an unordered set, so the same (profile, seed, anchor) always yields a
byte-identical ``World`` (``model_dump_json``) in any process.

Stages, in order (each consumes the shared RNG, so the order is part of the contract):
catalog -> workforce -> application owners -> policy pack -> provisioning -> controls ->
tickets. The finished world is checked with ``checks.world_problems``; a violation is a
generator bug and raises.

``generate_world`` also returns the generator-only facts that aren't part of the ``World``
contract (org-chart levels, when people on leave went on leave), so tests can check bounds the
generator promises but a serialized world can't show.

Every keyed collection of the returned ``World`` is ordered by ID. Stages build their dicts in
generation order (the CEO first, birthright before requested access, injected SoD conflicts in
one run, orphans and service accounts last), and iterating or serializing a world would leak
that order. IDs are random, so ID order carries no information (SPEC §6.2, §9.4).
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from identitybench.world.catalog import AppDef, build_catalog
from identitybench.world.checks import world_problems
from identitybench.world.entities import (
    Application,
    EmploymentType,
    Identity,
    IdentityStatus,
    PolicyPack,
    World,
)
from identitybench.world.governance import build_controls, build_tickets
from identitybench.world.ids import IdFactory
from identitybench.world.people import DIRECTOR, HEAD, MANAGER, build_workforce
from identitybench.world.profiles import PROFILES
from identitybench.world.provisioning import provision
from identitybench.world.query import first_granted
from identitybench.world.rolegraph import RoleGraph, SodIndex

DEFAULT_ANCHOR = date(2026, 10, 10)
"""Default as-of date for cases drawn from a world (SPEC Appendix A)."""


class WorldInvariantError(RuntimeError):
    """A generated world failed its structural checks (a generator bug)."""


@dataclass(frozen=True)
class GeneratedWorld:
    """A world plus generator-only facts about its people."""

    world: World
    levels: dict[str, str]
    """Org-chart level per identity: ``ceo``, ``head``, ``director``, ``manager`` or ``ic``."""
    leave_started: dict[str, date]
    """When each identity currently on leave went on leave (no grant or use after it)."""


def build_world(profile: str, seed: int, anchor_date: date = DEFAULT_ANCHOR) -> World:
    """Generate the synthetic org for ``profile`` (``startup``, ``midmarket``, ``enterprise``)."""
    return generate_world(profile, seed, anchor_date).world


def generate_world(profile: str, seed: int, anchor_date: date = DEFAULT_ANCHOR) -> GeneratedWorld:
    """``build_world`` plus the generator-only facts (see ``GeneratedWorld``)."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {sorted(PROFILES)}")
    spec = PROFILES[profile]
    rng = random.Random(seed)
    ids = IdFactory(rng, spec.id_widths)

    org_name = rng.choice(spec.org_names)
    catalog = build_catalog(spec, rng)
    ids.reserve_all(catalog.apps)
    ids.reserve_all(catalog.entitlements)
    ids.reserve_all(catalog.roles)

    workforce = build_workforce(spec, catalog.departments, rng, ids, anchor_date)
    applications = _assign_owners(catalog.apps, workforce.identities, workforce.levels, rng)
    pack = PolicyPack(org_name=org_name, sod=catalog.sod)
    sod = SodIndex(pack)

    access = provision(spec, catalog, workforce, sod, rng, ids, anchor_date)
    graph = RoleGraph(catalog.roles)
    records = (access.memberships, access.accounts, access.assignments, graph)
    granted_now = first_granted(*records, anchor_date)
    granted_on_file = first_granted(*records, None)
    controls = build_controls(workforce, granted_now, granted_on_file, sod, rng, ids, anchor_date)
    tickets = build_tickets(
        workforce,
        catalog.apps,
        round(spec.ticket_rate * spec.identities),
        rng,
        ids,
        anchor_date,
    )

    world = World(
        world_id=f"{profile}-{seed}-{anchor_date.isoformat()}",
        profile=profile,
        seed=seed,
        org_name=org_name,
        anchor_date=anchor_date,
        identities=_by_id(workforce.identities),
        applications=_by_id(applications),
        entitlements=_by_id(catalog.entitlements),
        roles=_by_id(catalog.roles),
        memberships=_by_id(access.memberships),
        accounts=_by_id(access.accounts),
        assignments=_by_id(access.assignments),
        usage=_by_id(access.usage),
        mitigating_controls=_by_id(controls),
        tickets=_by_id(tickets),
        policy_pack=pack,
    )
    problems = world_problems(world)
    if problems:
        raise WorldInvariantError("; ".join(problems[:10]))
    return GeneratedWorld(world, workforce.levels, workforce.leave_started)


def _by_id[V](items: Mapping[str, V]) -> dict[str, V]:
    """A copy ordered by ID, which hides generation order (see the module docstring)."""
    return dict(sorted(items.items()))


def _assign_owners(
    apps: dict[str, AppDef],
    identities: dict[str, Identity],
    levels: dict[str, str],
    rng: random.Random,
) -> dict[str, Application]:
    """Each app is owned by an active employee leader of its owning department.

    Owners are always active, so JML transfers to an application owner (SPEC §7.5, JML-003)
    always have a valid target.
    """
    leaders = [
        i
        for i in identities.values()
        if i.status is IdentityStatus.active
        and i.employment_type is EmploymentType.employee
        and levels[i.id] in (HEAD, DIRECTOR, MANAGER)
    ]
    by_dept: dict[str, list[Identity]] = {}
    for leader in leaders:
        by_dept.setdefault(leader.department, []).append(leader)
    fallback = by_dept.get("IT") or leaders
    return {
        app_id: app.application(rng.choice(by_dept.get(app.owner_dept) or fallback).id)
        for app_id, app in apps.items()
    }
