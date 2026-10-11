"""Fast lookups over a generated world, for task builders and the tool-use environment.

A ``World`` is a set of flat dicts keyed by ID; builders constantly need the reverse
directions (an identity's accounts, an account's assignments, a manager's reports). Build a
``WorldIndex`` once per world (``index_for`` caches the most recent few) and query it.

Peer stats (SPEC §6.2) are computed here: the share of an identity's peers (same department and
canonical title, excluding the identity and terminated people) who hold an entitlement through
valid sources on a given day. No policy rule reads them; they exist to measure over-reliance.

Canonical titles are generator-only (``Identity.canonical_title`` is excluded from
serialization), so peer stats need a world fresh from ``generator.build_world``. On a world
reloaded from JSON, ``peers`` and ``peer_share`` raise ``PeerStatsUnavailable``, naming the
``build_world`` call that rebuilds it, instead of silently grouping peers by noisy titles
(which would change the peer stats in over-reliance twins between a fresh and a reloaded
world). Every other lookup works on a reloaded world.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import date

from identitybench.dates import is_valid_on
from identitybench.world.entities import (
    Account,
    AccountKind,
    Assignment,
    IdentityStatus,
    Membership,
    MitigatingControl,
    UsageRecord,
    World,
)
from identitybench.world.rolegraph import RoleGraph

PeerGroup = tuple[str, str]
"""(department, canonical title)."""


class PeerStatsUnavailable(ValueError):
    """Peer stats were asked of a world without canonical titles (reloaded from JSON)."""


class WorldIndex:
    """Reverse indexes over one world. The world must not be mutated afterwards."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.graph = RoleGraph(world.roles)

        accounts: dict[str, list[Account]] = {}
        owned: dict[str, list[Account]] = {}
        self.account_by_identity_app: dict[tuple[str, str], Account] = {}
        for account in world.accounts.values():
            if account.kind is AccountKind.service:
                if account.owner_id is not None:
                    owned.setdefault(account.owner_id, []).append(account)
            elif account.identity_id is not None:
                accounts.setdefault(account.identity_id, []).append(account)
                self.account_by_identity_app[(account.identity_id, account.app_id)] = account
        self.accounts_by_identity: dict[str, tuple[Account, ...]] = _freeze(accounts)
        """Human accounts per identity (orphans and service accounts excluded)."""
        self.service_accounts_owned_by: dict[str, tuple[Account, ...]] = _freeze(owned)

        assignments: dict[str, list[Assignment]] = {}
        for assignment in world.assignments.values():
            assignments.setdefault(assignment.account_id, []).append(assignment)
        self.assignments_by_account: dict[str, tuple[Assignment, ...]] = _freeze(assignments)

        memberships: dict[str, list[Membership]] = {}
        for membership in world.memberships.values():
            memberships.setdefault(membership.identity_id, []).append(membership)
        self.memberships_by_identity: dict[str, tuple[Membership, ...]] = _freeze(memberships)

        self.usage_by_account_entitlement: dict[tuple[str, str], UsageRecord] = {
            (u.account_id, u.entitlement_id): u for u in world.usage.values()
        }

        reports: dict[str, list[str]] = {}
        for identity in world.identities.values():
            if identity.manager_id is not None:
                reports.setdefault(identity.manager_id, []).append(identity.id)
        self.direct_reports: dict[str, tuple[str, ...]] = _freeze(reports)

        controls: dict[str, list[MitigatingControl]] = {}
        for control in world.mitigating_controls.values():
            controls.setdefault(control.identity_id, []).append(control)
        self.controls_by_identity: dict[str, tuple[MitigatingControl, ...]] = _freeze(controls)

        self._peer_groups: dict[PeerGroup, tuple[str, ...]] | None = None
        self._held_on: dict[date, dict[str, frozenset[str]]] = {}
        self._group_counts_on: dict[date, dict[PeerGroup, Counter[str]]] = {}

    def manager_chain(self, identity_id: str) -> tuple[str, ...]:
        """Managers from the direct manager up to the top (stops at a missing link or cycle)."""
        chain: list[str] = []
        seen = {identity_id}
        current = self.world.identities[identity_id].manager_id
        while current is not None and current in self.world.identities and current not in seen:
            chain.append(current)
            seen.add(current)
            current = self.world.identities[current].manager_id
        return tuple(chain)

    def held_entitlements(self, identity_id: str, on: date) -> frozenset[str]:
        """Held access on ``on`` (SPEC §5): valid memberships expanded through nested roles,
        plus valid assignments on the identity's human accounts, regardless of account status."""
        return self._held_all(on).get(identity_id, frozenset())

    def peers(self, identity_id: str) -> tuple[str, ...]:
        """Same department and canonical title, excluding the identity and terminated people.

        Raises ``PeerStatsUnavailable`` if the world has no canonical titles (it was reloaded
        from JSON).
        """
        group = self._groups().get(self._group_of(identity_id), ())
        return tuple(i for i in group if i != identity_id)

    def peer_share(self, identity_id: str, entitlement_id: str, on: date) -> float:
        """Share of the identity's peers holding ``entitlement_id`` on ``on`` (0.0 if no peers).

        Raises ``PeerStatsUnavailable`` if the world has no canonical titles (it was reloaded
        from JSON).
        """
        group_key = self._group_of(identity_id)
        members = self._groups().get(group_key, ())
        holders = self._group_counts(on).get(group_key, Counter[str]())[entitlement_id]
        in_group = identity_id in members
        if in_group and entitlement_id in self.held_entitlements(identity_id, on):
            holders -= 1
        peers = len(members) - (1 if in_group else 0)
        return holders / peers if peers else 0.0

    # ----------------------------------------------------------------------------------------

    def _group_of(self, identity_id: str) -> PeerGroup:
        identity = self.world.identities[identity_id]
        if identity.canonical_title is None:
            world = self.world
            rebuild = (
                f"build_world({world.profile!r}, {world.seed}, "
                f"date.fromisoformat({world.anchor_date.isoformat()!r}))"
            )
            raise PeerStatsUnavailable(
                f"{identity_id} has no canonical title: world {world.world_id} was reloaded "
                "from JSON, and canonical titles are generator-only (never serialized). Peer "
                f"stats need a freshly generated world; rebuild it with {rebuild}."
            )
        return (identity.department, identity.canonical_title)

    def _groups(self) -> dict[PeerGroup, tuple[str, ...]]:
        """Peer groups of non-terminated identities, built on first use so a reloaded world
        (no canonical titles) still supports every other query."""
        if self._peer_groups is None:
            groups: dict[PeerGroup, list[str]] = {}
            for identity in self.world.identities.values():
                if identity.status is not IdentityStatus.terminated:
                    groups.setdefault(self._group_of(identity.id), []).append(identity.id)
            self._peer_groups = _freeze(groups)
        return self._peer_groups

    def _held_all(self, on: date) -> dict[str, frozenset[str]]:
        cached = self._held_on.get(on)
        if cached is None:
            world = self.world
            cached = held_access(
                world.memberships, world.accounts, world.assignments, self.graph, on
            )
            self._held_on[on] = cached
        return cached

    def _group_counts(self, on: date) -> dict[PeerGroup, Counter[str]]:
        cached = self._group_counts_on.get(on)
        if cached is not None:
            return cached
        held = self._held_all(on)
        counts: dict[PeerGroup, Counter[str]] = {}
        for group_key, members in self._groups().items():
            counter: Counter[str] = Counter()
            for member in members:
                counter.update(held.get(member, frozenset()))
            counts[group_key] = counter
        self._group_counts_on[on] = counts
        return counts


def held_access(
    memberships: Mapping[str, Membership],
    accounts: Mapping[str, Account],
    assignments: Mapping[str, Assignment],
    graph: RoleGraph,
    on: date | None,
) -> dict[str, frozenset[str]]:
    """Held access per identity on day ``on`` (identities holding nothing are absent).

    ``on=None`` counts every source on file regardless of its dates: the access an identity
    holds now or held through a grant that has since expired.
    """
    since = first_granted(memberships, accounts, assignments, graph, on)
    return {identity_id: frozenset(ents) for identity_id, ents in since.items()}


def first_granted(
    memberships: Mapping[str, Membership],
    accounts: Mapping[str, Account],
    assignments: Mapping[str, Assignment],
    graph: RoleGraph,
    on: date | None,
) -> dict[str, dict[str, date]]:
    """Per identity and held entitlement, the earliest grant date among its sources.

    Sources count as in ``held_access`` (valid on ``on``, or every source on file when ``on``
    is None). The generator uses this to date records that presuppose access, such as a
    mitigating control that can't predate the conflict it covers.
    """
    since: dict[str, dict[str, date]] = {}

    def note(identity_id: str, ent_id: str, granted: date) -> None:
        per_identity = since.setdefault(identity_id, {})
        known = per_identity.get(ent_id)
        if known is None or granted < known:
            per_identity[ent_id] = granted

    for membership in memberships.values():
        if on is None or is_valid_on(membership.granted_on, membership.expires_on, on):
            for ent_id in graph.expansion(membership.role_id):
                note(membership.identity_id, ent_id, membership.granted_on)
    for assignment in assignments.values():
        account = accounts[assignment.account_id]
        if account.kind is not AccountKind.human or account.identity_id is None:
            continue
        if on is None or is_valid_on(assignment.granted_on, assignment.expires_on, on):
            note(account.identity_id, assignment.entitlement_id, assignment.granted_on)
    return since


def _freeze[K, V](groups: dict[K, list[V]]) -> dict[K, tuple[V, ...]]:
    return {key: tuple(values) for key, values in groups.items()}


_CACHE_SIZE = 4
_cache: list[tuple[World, WorldIndex]] = []


def index_for(world: World) -> WorldIndex:
    """The ``WorldIndex`` for ``world``, built on first use and cached for the last few worlds."""
    for cached_world, index in _cache:
        if cached_world is world:
            return index
    index = WorldIndex(world)
    _cache.append((world, index))
    del _cache[:-_CACHE_SIZE]
    return index


def peer_share(world: World, identity_id: str, entitlement_id: str, on: date) -> float:
    """Share of the identity's peers holding the entitlement through valid sources on ``on``.

    Raises ``PeerStatsUnavailable`` on a world reloaded from JSON (see the module docstring).
    """
    return index_for(world).peer_share(identity_id, entitlement_id, on)
