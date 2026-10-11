"""Access semantics shared by every policy rule (SPEC §5, §6.3, §7.3, §7.4).

Vocabulary (SPEC §5):

- A *source* is a membership (identity → role) or an assignment (account → entitlement).
- A source is *valid* on day d iff it has started and hasn't expired (``dates.is_valid_on``).
  Expired records stay in the view for cleanup rules and as evidence, but confer nothing.
- *Held access* is every entitlement an identity gets through its valid memberships, expanded
  through nested roles, plus the valid assignments on its accounts, regardless of account status.
- *Effective access* is the part of held access exercised through an account that is active.

An assignment belongs to the identity named by its account's ``identity_id``. Orphaned accounts
(no identity) and service accounts (which carry an ``owner_id``, not an identity) therefore add
nothing to anyone's held access.

Every function here is a pure function of its inputs. A lookup that hits a missing record raises
``OracleInputError`` instead of guessing, because SPEC §9.2 requires incomplete cases to be
rejected rather than filled in with defaults.

``RecordIndex`` groups a view's records the way the rules look them up, so the oracle builds
each table once per case; the module-level functions answer one-off questions through a
throwaway index.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from functools import cached_property
from typing import Literal, Protocol

from identitybench.dates import is_valid_on
from identitybench.policy.registry import ALL_RULE_IDS
from identitybench.schemas.case import CaseView, ReviewItem, ReviewItemKind
from identitybench.world.entities import (
    ACCOUNT_PERMISSIVENESS,
    Account,
    AccountKind,
    AccountStatus,
    Application,
    Assignment,
    Entitlement,
    Identity,
    Membership,
    MitigatingControl,
    PolicyPack,
    RiskTier,
    Role,
    SodRuleSides,
    UsageRecord,
    max_risk,
)

SOD_RULE_IDS: tuple[str, ...] = tuple(i for i in ALL_RULE_IDS if i.startswith("SOD-"))

RolePath = tuple[str, ...]
"""Role IDs from a membership's role down to the role whose ``entitlements`` lists the target."""

SourceKind = Literal["membership", "assignment"]

Source = Membership | Assignment


class OracleInputError(ValueError):
    """The view lacks a fact that a rule needs, or holds contradictory records (SPEC §9.2).

    Case builders catch this to reject incomplete cases instead of letting the oracle fill the
    gap with a default.
    """


@dataclass(frozen=True, slots=True, order=True)
class Grant:
    """One way an identity holds one entitlement.

    A membership that reaches the same entitlement along two role paths yields two grants with
    the same ``source_id``; each path is a separate, citable alternative (SPEC §9.1).
    """

    entitlement_id: str
    source_id: str
    """The membership (``rm_…``) or assignment (``as_…``) that confers the entitlement."""
    kind: SourceKind
    path: RolePath = ()
    """Role path for membership grants, starting at the membership's role; empty for assignments."""


# --------------------------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------------------------


def require_role(roles: Mapping[str, Role], role_id: str) -> Role:
    """The role with this ID; raises ``OracleInputError`` if the view doesn't include it."""
    role = roles.get(role_id)
    if role is None:
        raise OracleInputError(f"role {role_id!r} is referenced but not in the view")
    return role


def require_identity(view: CaseView, identity_id: str, referenced_by: str) -> Identity:
    """The identity with this ID; ``referenced_by`` names the field that points at it."""
    identity = view.identities.get(identity_id)
    if identity is None:
        raise OracleInputError(f"identity {identity_id!r} ({referenced_by}) is not in the view")
    return identity


def require_entitlement(view: CaseView, entitlement_id: str) -> Entitlement:
    """The entitlement with this ID; raises ``OracleInputError`` if missing."""
    entitlement = view.entitlements.get(entitlement_id)
    if entitlement is None:
        raise OracleInputError(f"entitlement {entitlement_id!r} is referenced but not in the view")
    return entitlement


def require_application(view: CaseView, app_id: str) -> Application:
    """The application with this ID; raises ``OracleInputError`` if missing."""
    app = view.applications.get(app_id)
    if app is None:
        raise OracleInputError(f"application {app_id!r} is referenced but not in the view")
    return app


def require_account(view: CaseView, account_id: str) -> Account:
    """The account with this ID; raises ``OracleInputError`` if missing."""
    account = view.accounts.get(account_id)
    if account is None:
        raise OracleInputError(f"account {account_id!r} is referenced but not in the view")
    return account


# --------------------------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------------------------


def expand_role(roles: Mapping[str, Role], role_id: str) -> frozenset[str]:
    """Every entitlement a role confers, through child roles at any depth.

    Cycle-safe: each role is visited once, so a malformed ``A → B → A`` hierarchy terminates.
    """
    seen: set[str] = set()
    entitlements: set[str] = set()
    pending = [role_id]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        role = require_role(roles, current)
        entitlements.update(role.entitlements)
        pending.extend(role.child_roles)
    return frozenset(entitlements)


def role_grants(roles: Mapping[str, Role], role_id: str) -> tuple[tuple[str, RolePath], ...]:
    """Every (entitlement, role path) pair a role confers, sorted.

    A path never revisits a role, so cycles terminate. Distinct paths to the same entitlement
    are all returned: each one is an alternative way to cite the access.
    """
    found: set[tuple[str, RolePath]] = set()

    def walk(path: RolePath) -> None:
        role = require_role(roles, path[-1])
        for entitlement_id in role.entitlements:
            found.add((entitlement_id, path))
        for child in role.child_roles:
            if child not in path:
                walk((*path, child))

    walk((role_id,))
    return tuple(sorted(found))


def role_paths(
    roles: Mapping[str, Role], role_id: str, entitlement_id: str
) -> tuple[RolePath, ...]:
    """Every role path from ``role_id`` to a role that lists ``entitlement_id``, sorted."""
    return tuple(path for ent, path in role_grants(roles, role_id) if ent == entitlement_id)


# --------------------------------------------------------------------------------------------
# Sources, held and effective access
# --------------------------------------------------------------------------------------------


def is_source_valid(source: Source, day: date) -> bool:
    """True iff the membership or assignment has started and hasn't expired on ``day``."""
    return is_valid_on(source.granted_on, source.expires_on, day)


def source_grants(roles: Mapping[str, Role], source: Source) -> tuple[Grant, ...]:
    """What a source's record confers when valid: every (entitlement, path) of a membership's
    role, or an assignment's single entitlement."""
    if isinstance(source, Membership):
        return tuple(
            Grant(entitlement_id, source.id, "membership", path)
            for entitlement_id, path in role_grants(roles, source.role_id)
        )
    return (Grant(source.entitlement_id, source.id, "assignment"),)


def group_grants(grants: Iterable[Grant]) -> dict[str, tuple[Grant, ...]]:
    """Grants grouped by entitlement, with keys and grants in sorted order."""
    by_entitlement: dict[str, set[Grant]] = {}
    for grant in grants:
        by_entitlement.setdefault(grant.entitlement_id, set()).add(grant)
    return {ent: tuple(sorted(by_entitlement[ent])) for ent in sorted(by_entitlement)}


class _Record(Protocol):
    """Any view record: every entity carries its own string ``id``."""

    @property
    def id(self) -> str: ...


def _group_by[K, R: _Record](
    records: Iterable[R], key: Callable[[R], K | None]
) -> dict[K, tuple[R, ...]]:
    """Records grouped by ``key`` (records whose key is None are left out), each group sorted
    by record ID so no answer depends on dict order."""
    groups: dict[K, list[R]] = {}
    for record in sorted(records, key=lambda r: r.id):
        group = key(record)
        if group is not None:
            groups.setdefault(group, []).append(record)
    return {group: tuple(members) for group, members in groups.items()}


class RecordIndex:
    """A view's records grouped the way the rules look them up, each table built on first use.

    Every request rule and every review item asks the same questions (an identity's accounts, an
    account's assignments, a usage record, a control). Scanning the whole view for each answer
    would make a large campaign quadratic, so the oracle builds one index per case.

    An assignment belongs to whoever owns its account, so an assignment whose account isn't in
    the view makes every identity's held access unknowable. Dropping it instead would let the
    oracle approve a request that completes a toxic combination, so ``check_assignment_accounts``
    rejects the view (SPEC §9.2). The oracle's contexts run it up front for every case, and the
    assignment table runs it before it is first used, so the module-level lookups are covered too.
    """

    def __init__(self, view: CaseView) -> None:
        self.view = view

    def check_assignment_accounts(self) -> None:
        """Raise ``OracleInputError`` if any assignment's account is missing from the view."""
        dangling = sorted(
            f"{a.id} (account {a.account_id})"
            for a in self.view.assignments.values()
            if a.account_id not in self.view.accounts
        )
        if dangling:
            raise OracleInputError(
                f"assignments on accounts that aren't in the view: {', '.join(dangling)}"
            )

    @cached_property
    def _accounts_by_identity(self) -> dict[str, tuple[Account, ...]]:
        return _group_by(self.view.accounts.values(), lambda a: a.identity_id)

    @cached_property
    def _memberships_by_identity(self) -> dict[str, tuple[Membership, ...]]:
        return _group_by(self.view.memberships.values(), lambda m: m.identity_id)

    @cached_property
    def _assignments_by_account(self) -> dict[str, tuple[Assignment, ...]]:
        self.check_assignment_accounts()
        return _group_by(self.view.assignments.values(), lambda a: a.account_id)

    @cached_property
    def _usage_by_pair(self) -> dict[tuple[str, str], tuple[UsageRecord, ...]]:
        return _group_by(self.view.usage.values(), lambda u: (u.account_id, u.entitlement_id))

    @cached_property
    def _controls_by_pair(self) -> dict[tuple[str, str], tuple[MitigatingControl, ...]]:
        controls = self.view.mitigating_controls.values()
        return _group_by(controls, lambda c: (c.identity_id, c.rule_id))

    def accounts_of(self, identity_id: str) -> tuple[Account, ...]:
        """The identity's own accounts (``identity_id`` matches), sorted by ID."""
        return self._accounts_by_identity.get(identity_id, ())

    def memberships_of(self, identity_id: str) -> tuple[Membership, ...]:
        """Every recorded membership of the identity (valid or not), sorted by ID."""
        return self._memberships_by_identity.get(identity_id, ())

    def assignments_of(self, identity_id: str) -> tuple[Assignment, ...]:
        """Every recorded assignment on the identity's accounts (valid or not), sorted by ID."""
        by_account = self._assignments_by_account
        owned = [a for acct in self.accounts_of(identity_id) for a in by_account.get(acct.id, ())]
        return tuple(sorted(owned, key=lambda a: a.id))

    def valid_memberships(self, identity_id: str, day: date) -> tuple[Membership, ...]:
        """The identity's memberships that are valid on ``day``."""
        return tuple(m for m in self.memberships_of(identity_id) if is_source_valid(m, day))

    def valid_assignments(self, identity_id: str, day: date) -> tuple[Assignment, ...]:
        """Assignments on the identity's accounts that are valid on ``day``."""
        return tuple(a for a in self.assignments_of(identity_id) if is_source_valid(a, day))

    def held_access(self, identity_id: str, day: date) -> dict[str, tuple[Grant, ...]]:
        """Held access on ``day``: entitlement ID → every grant through a valid source.

        Account status is ignored (SPEC §5): a disabled account's assignments are still held.
        """
        sources: list[Source] = [
            *self.valid_memberships(identity_id, day),
            *self.valid_assignments(identity_id, day),
        ]
        return group_grants(g for source in sources for g in source_grants(self.view.roles, source))

    def usage_for(self, account_id: str, entitlement_id: str) -> tuple[UsageRecord, ...]:
        """Usage records for one (account, entitlement) pair, sorted by ID."""
        return self._usage_by_pair.get((account_id, entitlement_id), ())

    def control_for(self, identity_id: str, rule_id: str) -> MitigatingControl | None:
        """The identity's mitigating control for a SoD rule, whatever its dates.

        There is at most one control per (identity, rule) (SPEC §6.2); a view with two is
        contradictory and is rejected.
        """
        found = self._controls_by_pair.get((identity_id, rule_id), ())
        if len(found) > 1:
            ids = ", ".join(c.id for c in found)
            raise OracleInputError(f"{identity_id} has several controls for {rule_id}: {ids}")
        return found[0] if found else None


def accounts_of(view: CaseView, identity_id: str) -> tuple[Account, ...]:
    """The identity's own accounts (``identity_id`` matches), sorted by ID."""
    return RecordIndex(view).accounts_of(identity_id)


def account_for(view: CaseView, identity_id: str, app_id: str) -> Account | None:
    """The identity's human account in an application, or None if it has none.

    An identity normally has one human account per application. If the view holds several, the
    most permissive status wins (active, then suspended, then disabled), ties broken by ID, so
    the answer never depends on dict order.
    """
    candidates = [
        a
        for a in accounts_of(view, identity_id)
        if a.app_id == app_id and a.kind == AccountKind.human
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda a: (-ACCOUNT_PERMISSIVENESS[a.status], a.id))


def memberships_of(view: CaseView, identity_id: str) -> tuple[Membership, ...]:
    """Every recorded membership of the identity (valid or not), sorted by ID."""
    return RecordIndex(view).memberships_of(identity_id)


def assignments_of(view: CaseView, identity_id: str) -> tuple[Assignment, ...]:
    """Every recorded assignment on the identity's accounts (valid or not), sorted by ID.

    Raises ``OracleInputError`` if any assignment's account is missing (see ``RecordIndex``).
    """
    return RecordIndex(view).assignments_of(identity_id)


def valid_memberships(view: CaseView, identity_id: str, day: date) -> tuple[Membership, ...]:
    """The identity's memberships that are valid on ``day``."""
    return RecordIndex(view).valid_memberships(identity_id, day)


def valid_assignments(view: CaseView, identity_id: str, day: date) -> tuple[Assignment, ...]:
    """Assignments on the identity's accounts that are valid on ``day``."""
    return RecordIndex(view).valid_assignments(identity_id, day)


def held_access(view: CaseView, identity_id: str, day: date) -> dict[str, tuple[Grant, ...]]:
    """Held access on ``day``: entitlement ID → every grant through a valid source.

    Account status is ignored (SPEC §5): a disabled account's assignments are still held.
    """
    return RecordIndex(view).held_access(identity_id, day)


def effective_access(view: CaseView, identity_id: str, day: date) -> dict[str, tuple[Grant, ...]]:
    """Effective access on ``day``: the held grants exercised through an active account.

    An assignment grant needs its own account to be active; a membership grant needs an active
    account of the identity in the entitlement's application. Account statuses are the view's
    current ones, so ``day`` only affects which sources are valid.
    """
    records = RecordIndex(view)
    active = [a for a in records.accounts_of(identity_id) if a.status == AccountStatus.active]
    active_ids = {a.id for a in active}
    active_apps = {a.app_id for a in active}

    def exercised(grant: Grant) -> bool:
        if grant.kind == "assignment":
            return view.assignments[grant.source_id].account_id in active_ids
        return require_entitlement(view, grant.entitlement_id).app_id in active_apps

    held = records.held_access(identity_id, day)
    return group_grants(g for grants in held.values() for g in grants if exercised(g))


# --------------------------------------------------------------------------------------------
# Review items and risk
# --------------------------------------------------------------------------------------------


def review_source(view: CaseView, item: ReviewItem) -> Source:
    """The membership or assignment a review item refers to."""
    if item.kind == ReviewItemKind.membership:
        membership = view.memberships.get(item.ref)
        if membership is None:
            raise OracleInputError(
                f"review item {item.item_id} refers to unknown membership {item.ref!r}"
            )
        return membership
    assignment = view.assignments.get(item.ref)
    if assignment is None:
        raise OracleInputError(
            f"review item {item.item_id} refers to unknown assignment {item.ref!r}"
        )
    return assignment


def source_risk_tier(view: CaseView, source: Source) -> RiskTier:
    """Risk tier of what a source confers (SPEC §12.1).

    An assignment takes its entitlement's tier; a membership takes the highest tier in its
    role's expansion. A role that confers nothing carries the lowest tier.
    """
    if isinstance(source, Assignment):
        return require_entitlement(view, source.entitlement_id).risk_tier
    expansion = sorted(expand_role(view.roles, source.role_id))
    if not expansion:
        return RiskTier.low
    return max_risk([require_entitlement(view, ent).risk_tier for ent in expansion])


def item_risk_tier(view: CaseView, item: ReviewItem) -> RiskTier:
    """Risk tier of the access a review item confers."""
    return source_risk_tier(view, review_source(view, item))


# --------------------------------------------------------------------------------------------
# Separation of duties and mitigating controls
# --------------------------------------------------------------------------------------------


def pack_sides(pack: PolicyPack) -> dict[str, SodRuleSides]:
    """The pack's SoD rules keyed by rule ID, in rule order.

    Rejects duplicate or unknown rule IDs: either would make the rendered policy and the oracle
    disagree about which sides a rule has. A rule the pack omits has no sides in this world and
    can never apply.
    """
    sides: dict[str, SodRuleSides] = {}
    for rule in pack.sod:
        if rule.rule_id not in SOD_RULE_IDS:
            raise OracleInputError(f"policy pack lists unknown SoD rule {rule.rule_id!r}")
        if rule.rule_id in sides:
            raise OracleInputError(f"policy pack lists {rule.rule_id} more than once")
        sides[rule.rule_id] = rule
    return {rule_id: sides[rule_id] for rule_id in SOD_RULE_IDS if rule_id in sides}


def opposite_side(sides: SodRuleSides, entitlement_id: str) -> frozenset[str]:
    """Entitlements on the other side of a SoD rule from ``entitlement_id`` (empty if it is on
    neither side)."""
    other: set[str] = set()
    if entitlement_id in sides.side_a:
        other.update(sides.side_b)
    if entitlement_id in sides.side_b:
        other.update(sides.side_a)
    return frozenset(other)


def control_for(view: CaseView, identity_id: str, rule_id: str) -> MitigatingControl | None:
    """The identity's mitigating control for a SoD rule, whatever its dates (at most one; see
    ``RecordIndex.control_for``)."""
    return RecordIndex(view).control_for(identity_id, rule_id)


def control_valid(control: MitigatingControl, day: date) -> bool:
    """True iff the control authorizes its exception on ``day`` (started and unexpired)."""
    return is_valid_on(control.granted_on, control.expires_on, day)
