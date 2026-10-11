"""Shared machinery for the request and review rules (SPEC §7.1, §7.4, §9.1).

A rule's predicate runs against a context that wraps the ``CaseView``:

- ``RequestContext`` for an access request;
- ``ItemContext`` for one access-review item, created by a ``ReviewContext`` that caches
  per-identity facts across the campaign.

Contexts compute the facts several rules share (held access, ticket checks, SoD analyses) once,
so two rules can never disagree about the same fact. Every claim is built with ``make_claim``
against the view's index, so a certificate always validates against the view it came from.

Claim recipes follow four conventions so certificates stay minimal and auditor-obvious:

- Only facts the rule's truth depends on are cited. A join that merely reaches the record whose
  attribute matters is not: request → beneficiary or entitlement, review item → its record,
  membership → identity, assignment → account, entitlement → application. So ELIG-003 cites the
  application's ``region_restriction`` but not the entitlement's ``app_id``, and HYG-002 cites the
  account's ``kind`` but not the assignment's ``account_id``.
- Two kinds of reference are facts in their own right and are cited. When a rule depends on
  *which* entitlement a source confers or a request asks for, the conferral is cited
  (``membership.role_id``, each ``child_roles`` hop, then ``role.entitlements``; or
  ``assignment.entitlement_id``; or ``request.entitlement_id``). When a rule compares a value
  with another record, both sides of the comparison are cited: the requester against the
  beneficiary, their manager and the application owner (APPR-002); a ticket's ``assignee_id``
  against the request's ``beneficiary_id``, and the entitlement's ``app_id`` against the
  ticket's ``related_app_ids`` (APPR-003, APPR-004, JUST-001); a holder attribute against the
  values the role's ``attribute_rule`` allows (HYG-005). When the value is in a list, the
  matching element is cited; when it is missing, every element is, since only the whole list
  shows it is missing.
- An absence (a null field, an ID missing from a list) can't be cited, so the recipe cites the
  positive facts around it instead.
- Every claim set is minimal: it cites exactly one role path per entitlement it needs, and no
  claim set contains another (``normalize_claim_sets``). Alternative paths are separate claim
  sets.

The view-wide input contract every request and review case must meet is checked once, when the
context is built (``RequestContext``, ``ReviewContext``); each rule's docstring states what else
its predicate reads and which gaps make it raise ``OracleInputError`` (SPEC §9.2).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from functools import cached_property
from itertools import pairwise
from types import MappingProxyType
from typing import ClassVar

from pydantic import BaseModel

from identitybench.policy.access import (
    Grant,
    OracleInputError,
    RecordIndex,
    Source,
    control_valid,
    is_source_valid,
    opposite_side,
    pack_sides,
    require_account,
    require_application,
    require_entitlement,
    require_identity,
    review_source,
    source_grants,
)
from identitybench.policy.registry import Rule
from identitybench.schemas.case import AccessRequest, Campaign, CaseView, ReviewItem
from identitybench.schemas.enums import Effect, Task
from identitybench.schemas.evidence import canonical, index_view, make_claim
from identitybench.schemas.gold import Claim, ClaimValue, ExceptionEval
from identitybench.world.entities import (
    Account,
    Application,
    Entitlement,
    Identity,
    Membership,
    MitigatingControl,
    Ticket,
    TicketStatus,
    TicketType,
)

MIT_001 = "MIT-001"

ClaimSet = tuple[Claim, ...]
"""Claims that together certify a finding. A certificate lists alternative claim sets."""

_ClaimKey = tuple[str, str, str]


def exception_eval(rule_id: str, for_rule: str, applies: bool) -> ExceptionEval:
    """An evaluated exception, built from its wire form because the ``for`` alias is a keyword."""
    return ExceptionEval.model_validate({"rule": rule_id, "for": for_rule, "applies": applies})


def effects_of(
    *, request: Effect | None = None, review: Effect | None = None
) -> Mapping[Task, Effect]:
    """A rule's read-only effect table. Requests come first, so headers read like SPEC §7.1:
    ``SOD-002 (requests: DENY; reviews: ESCALATE)``."""
    table: dict[Task, Effect] = {}
    if request is not None:
        table[Task.access_request] = request
    if review is not None:
        table[Task.access_review] = review
    return MappingProxyType(table)


def days_param(days: int) -> Mapping[str, object]:
    """Read-only ``params`` for a rule whose summary prints one threshold as ``{days}``."""
    return MappingProxyType({"days": days})


def _claim_key(claim: Claim) -> _ClaimKey:
    return (claim.id, claim.field, canonical(claim.value))


def normalize_claim_sets(claim_sets: Iterable[Iterable[Claim]]) -> tuple[ClaimSet, ...]:
    """Drop repeated claims within a set and repeated sets, keep only the minimal sets, and sort
    them.

    A set that strictly contains another is dropped. The sets are alternatives, so a citation
    that satisfies the larger set also satisfies the smaller one, and the larger set's extra
    claims are facts the finding doesn't depend on (SPEC §9.1). Rules build their alternatives
    by combining every way each needed fact holds, so the minimal combinations are always among
    them. For example, a review item whose role reaches a role listing both sides of a conflict
    along two paths yields one set per path, not one set mixing both paths: in the mixed set,
    either path could be dropped and the rest would still show the conflict.

    Claims keep their constructed order inside a set, which reads as a chain (request, role
    path, control). The sets are sorted: gold must not depend on the order records happen to
    appear in the view.
    """
    unique: dict[frozenset[_ClaimKey], ClaimSet] = {}
    for claim_set in claim_sets:
        keys: set[_ClaimKey] = set()
        kept: list[Claim] = []
        for claim in claim_set:
            key = _claim_key(claim)
            if key not in keys:
                keys.add(key)
                kept.append(claim)
        unique.setdefault(frozenset(keys), tuple(kept))
    minimal = [s for keys, s in unique.items() if not any(other < keys for other in unique)]
    return tuple(sorted(minimal, key=lambda s: [_claim_key(c) for c in s]))


@dataclass(frozen=True, slots=True)
class Hit:
    """A rule that applies: its effect, the alternative claim sets that certify it and, for a
    CONDITION rule, the duration cap it imposes."""

    rule_id: str
    effect: Effect
    claim_sets: tuple[ClaimSet, ...]
    cap_days: int | None = None


@dataclass(frozen=True, slots=True)
class Evaluation:
    """What running one rule's predicate produced: a hit if the rule applies, plus every
    exception the predicate evaluated on the way, whether or not it applied."""

    hit: Hit | None = None
    exceptions: tuple[ExceptionEval, ...] = ()


NOT_APPLICABLE = Evaluation()


def grant_claims(index: dict[str, BaseModel], grant: Grant) -> ClaimSet:
    """Claims that show a source confers an entitlement.

    Assignment: ``assignment.entitlement_id``. Membership: ``membership.role_id``, then one
    ``child_roles`` element per hop down the path, then the last role's ``entitlements`` element.
    """
    if grant.kind == "assignment":
        return (make_claim(index, grant.source_id, "entitlement_id"),)
    hops = [
        make_claim(index, parent, "child_roles", child) for parent, child in pairwise(grant.path)
    ]
    return (
        make_claim(index, grant.source_id, "role_id"),
        *hops,
        make_claim(index, grant.path[-1], "entitlements", grant.entitlement_id),
    )


# --------------------------------------------------------------------------------------------
# Separation of duties
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SodAnalysis:
    """A SoD conflict found for a request or a review item, with its MIT-001 evaluation.

    ``claim_sets`` holds one alternative per way the conflict arises (each combination of one
    role path to each conflicting entitlement); a finding keeps the minimal ones
    (``normalize_claim_sets``). When a control exists, every set also cites the control date
    behind the MIT-001 evaluation: ``granted_on`` for a control that hasn't started yet,
    otherwise ``expires_on`` (the expiry that ended it, or the one that bounds the mitigation).
    With no control there is nothing to cite: the missing control is an absence.
    """

    rule_id: str
    control: MitigatingControl | None
    mitigated: bool
    claim_sets: tuple[ClaimSet, ...]

    @property
    def exceptions(self) -> tuple[ExceptionEval, ...]:
        """MIT-001 for this rule, recorded for every conflict found.

        Deciding a conflict means looking for a valid control, so MIT-001 is evaluated whether or
        not the identity has one (SPEC §7.1, §9.1 "whether or not they applied"); with no
        control it doesn't apply. Citing it is then legitimate, as in Appendix A. A rule with no
        conflict never reaches the exception, so nothing is recorded for it.
        """
        return (exception_eval(MIT_001, self.rule_id, self.mitigated),)

    @property
    def mitigation_expires_on(self) -> date | None:
        """Expiry of the control that mitigates this conflict; None if it isn't mitigated."""
        if self.control is None or not self.mitigated:
            return None
        return self.control.expires_on


# --------------------------------------------------------------------------------------------
# Contexts
# --------------------------------------------------------------------------------------------


class Context:
    """What every rule predicate can rely on: the view, its claim index, its record index and
    the task."""

    task: ClassVar[Task]

    def __init__(self, view: CaseView, index: dict[str, BaseModel], records: RecordIndex) -> None:
        self.view = view
        self.index = index
        self.records = records
        self.as_of = view.as_of

    def claim(self, entity_id: str, field: str, element: ClaimValue | None = None) -> Claim:
        """A claim about the view (pass ``element`` for a list field)."""
        return make_claim(self.index, entity_id, field, element)

    def entitlement(self, entitlement_id: str) -> Entitlement:
        return require_entitlement(self.view, entitlement_id)

    def application(self, app_id: str) -> Application:
        return require_application(self.view, app_id)

    def conferral(self, grant: Grant) -> ClaimSet:
        """Claims that show how ``grant``'s source confers its entitlement."""
        return grant_claims(self.index, grant)

    def sod_analysis(
        self, identity_id: str, rule_id: str, conflicts: Sequence[ClaimSet]
    ) -> SodAnalysis:
        """Attach the MIT-001 evaluation for (identity, rule) to a list of conflict claim sets."""
        control = self.records.control_for(identity_id, rule_id)
        if control is None:
            return SodAnalysis(rule_id, None, False, tuple(conflicts))
        mitigated = control_valid(control, self.as_of)
        date_field = "granted_on" if control.granted_on > self.as_of else "expires_on"
        date_claim = self.claim(control.id, date_field)
        return SodAnalysis(rule_id, control, mitigated, tuple((*c, date_claim) for c in conflicts))


class RequestContext(Context):
    """Facts about one access request, computed once and shared by every request rule.

    Input contract, checked here before any rule runs (each failure raises
    ``OracleInputError``): the view has a ``request``; the beneficiary, the requested
    entitlement and its application are in the view; and the view is complete
    (``RecordIndex.check_complete``: every assignment's account is in the view, every
    membership's role is in the view with the source its kind admits, and every provisioned
    (account, entitlement) pair has a usage record), whether or not a request rule reads it
    (SPEC §9.2).
    """

    task = Task.access_request

    def __init__(self, view: CaseView) -> None:
        if view.request is None:
            raise OracleInputError("an access-request case needs view.request")
        records = RecordIndex(view)
        records.check_complete()
        super().__init__(view, index_view(view), records)
        self.request: AccessRequest = view.request
        self.beneficiary: Identity = require_identity(
            view, view.request.beneficiary_id, "request.beneficiary_id"
        )
        self.requested: Entitlement = self.entitlement(view.request.entitlement_id)
        self.app: Application = self.application(self.requested.app_id)

    @cached_property
    def held(self) -> dict[str, tuple[Grant, ...]]:
        """The beneficiary's held access on the as-of date."""
        return self.records.held_access(self.beneficiary.id, self.as_of)

    @cached_property
    def ticket(self) -> Ticket | None:
        """The ticket named by ``ticket_ref``, if it is in the case's ticket list."""
        ref = self.request.ticket_ref
        return None if ref is None else self.view.tickets.get(ref)

    def ticket_failures(self, lead: Claim, *, break_glass: bool) -> tuple[ClaimSet, ...]:
        """One claim set per reason ``ticket_ref`` doesn't name a usable ticket.

        A usable ticket is in the case's ticket list, open, and related to the entitlement's
        application; a break-glass ticket must also be an incident assigned to the beneficiary.
        ``lead`` heads every set: it is the fact that made a ticket necessary. Then come
        ``request.ticket_ref`` and the failing comparison, both sides of it: the ticket's
        ``type`` or ``status``; its ``assignee_id`` and the request's ``beneficiary_id``; or the
        entitlement's ``app_id`` and every element of the ticket's ``related_app_ids``, which
        together show the application isn't among them. Empty when the ticket is usable.

        An unassigned break-glass ticket fails on an absence, which can't be cited, so the bare
        ``(lead, ticket_ref)`` certifies it, but only when nothing else fails. Next to another
        failure it would be a strict subset of that alternative, letting a model be graded
        grounded without citing any fact that invalidates the ticket.
        """
        if self.request.ticket_ref is None:
            return ((lead,),)
        request_id = self.request.request_id
        ref = self.claim(request_id, "ticket_ref")
        ticket = self.ticket
        if ticket is None:
            return ((lead, ref),)
        failures: list[ClaimSet] = []
        if break_glass and ticket.type != TicketType.incident:
            failures.append((lead, ref, self.claim(ticket.id, "type")))
        if ticket.status != TicketStatus.open:
            failures.append((lead, ref, self.claim(ticket.id, "status")))
        if break_glass and ticket.assignee_id not in (None, self.beneficiary.id):
            assignee = self.claim(ticket.id, "assignee_id")
            failures.append((lead, ref, assignee, self.claim(request_id, "beneficiary_id")))
        if self.app.id not in ticket.related_app_ids:
            covered = [
                self.claim(ticket.id, "related_app_ids", app_id)
                for app_id in sorted(set(ticket.related_app_ids))
            ]
            failures.append((lead, ref, self.claim(self.requested.id, "app_id"), *covered))
        if break_glass and ticket.assignee_id is None and not failures:
            failures.append((lead, ref))
        return tuple(failures)

    @cached_property
    def break_glass_failures(self) -> tuple[ClaimSet, ...]:
        """Why a break-glass request is invalid (APPR-003); empty if valid or not break-glass."""
        if not self.request.is_break_glass:
            return ()
        lead = self.claim(self.request.request_id, "is_break_glass")
        return self.ticket_failures(lead, break_glass=True)

    @property
    def break_glass_ticket(self) -> Ticket | None:
        """The incident that makes this a valid break-glass request; None if it isn't one."""
        if not self.request.is_break_glass or self.break_glass_failures:
            return None
        return self.ticket

    @cached_property
    def sod(self) -> dict[str, SodAnalysis]:
        """SoD conflicts the request would introduce, keyed by rule ID (SPEC §7.4, SoD scope).

        A rule is in conflict iff the requested entitlement is on one side and the beneficiary's
        held access (valid sources only) already includes an entitlement on the other side.
        Each claim set is ``request.entitlement_id`` followed by the conferral of one
        conflicting held grant.
        """
        requested = self.claim(self.request.request_id, "entitlement_id")
        analyses: dict[str, SodAnalysis] = {}
        for rule_id, sides in pack_sides(self.view.policy_pack).items():
            other = opposite_side(sides, self.requested.id)
            grants = [g for ent in sorted(other) for g in self.held.get(ent, ())]
            if grants:
                conflicts = [(requested, *self.conferral(g)) for g in grants]
                analyses[rule_id] = self.sod_analysis(self.beneficiary.id, rule_id, conflicts)
        return analyses


class ReviewContext:
    """Campaign-wide facts for an access review, shared by every item.

    Input contract, checked here before any item is judged (each failure raises
    ``OracleInputError``): the view has a ``campaign`` whose item IDs are unique, and the view is
    complete (``RecordIndex.check_complete``, as for requests), including the usage records of
    holders and items outside the campaign (SPEC §9.2). Each item's record and holder are checked
    when its ``ItemContext`` is built.
    """

    def __init__(self, view: CaseView) -> None:
        if view.campaign is None:
            raise OracleInputError("an access-review case needs view.campaign")
        item_ids = [item.item_id for item in view.campaign.items]
        repeated = sorted({i for i in item_ids if item_ids.count(i) > 1})
        if repeated:
            raise OracleInputError(f"campaign repeats item IDs: {', '.join(repeated)}")
        self.view = view
        self.campaign: Campaign = view.campaign
        self.index = index_view(view)
        self.records = RecordIndex(view)
        self.records.check_complete()
        self._held: dict[str, dict[str, tuple[Grant, ...]]] = {}

    def held(self, identity_id: str) -> dict[str, tuple[Grant, ...]]:
        """An identity's held access on the as-of date (cached per identity)."""
        if identity_id not in self._held:
            self._held[identity_id] = self.records.held_access(identity_id, self.view.as_of)
        return self._held[identity_id]

    def items(self) -> list[ItemContext]:
        """One context per campaign item, in campaign order."""
        return [ItemContext(self, item) for item in self.campaign.items]


class ItemContext(Context):
    """Facts about one review item: its record, its holder and what it confers.

    The holder of a membership is its identity; the holder of an assignment is its account's
    identity, which is None for orphaned and service accounts. Identity-wide rules need a holder.

    Input contract (``OracleInputError`` otherwise): the item's membership or assignment is in
    the view, and so is its holder when the record names one.
    """

    task = Task.access_review

    def __init__(self, review: ReviewContext, item: ReviewItem) -> None:
        super().__init__(review.view, review.index, review.records)
        self.review = review
        self.item = item
        source = review_source(self.view, item)
        self.source: Source = source
        self.account: Account | None
        if isinstance(source, Membership):
            self.account = None
            holder_id: str | None = source.identity_id
            referenced_by = f"{source.id}.identity_id"
        else:
            self.account = require_account(self.view, source.account_id)
            holder_id = self.account.identity_id
            referenced_by = f"{self.account.id}.identity_id"
        self.holder: Identity | None = (
            None if holder_id is None else require_identity(self.view, holder_id, referenced_by)
        )

    @property
    def valid(self) -> bool:
        """True iff the item's record is valid on the as-of date, so it confers access."""
        return is_source_valid(self.source, self.as_of)

    @cached_property
    def record_grants(self) -> tuple[Grant, ...]:
        """What the item's record grants, whatever its dates.

        Identity-wide and item-level rules judge the record, because REVOKE removes the record:
        a terminated holder's expired or future-dated items are revoked, never certified.
        """
        return source_grants(self.view.roles, self.source)

    @cached_property
    def sod(self) -> dict[str, SodAnalysis]:
        """SoD conflicts this item confers a side of, keyed by rule ID (SPEC §7.4, SoD scope).

        Conflicts live in held access, so only a valid item confers a side, and the other side
        must be in the holder's held access (possibly through this same item). Each claim set is
        this item's conferral of one side followed by the conferral of one other-side grant.
        Every pairing is listed. When both paths run through the same roles, a pairing can cite
        two paths to one role, and the finding then keeps only the pairings that don't contain
        another (``normalize_claim_sets``).
        """
        if self.holder is None or not self.valid:
            return {}
        held = self.review.held(self.holder.id)
        analyses: dict[str, SodAnalysis] = {}
        for rule_id, sides in pack_sides(self.view.policy_pack).items():
            conflicts: list[ClaimSet] = []
            for grant in self.record_grants:
                for ent in sorted(opposite_side(sides, grant.entitlement_id)):
                    for other in held.get(ent, ()):
                        conflicts.append((*self.conferral(grant), *self.conferral(other)))
            if conflicts:
                analyses[rule_id] = self.sod_analysis(self.holder.id, rule_id, conflicts)
        return analyses


# --------------------------------------------------------------------------------------------
# Rule bases
# --------------------------------------------------------------------------------------------


class AccessRule(Rule):
    """A rule the request or review oracle evaluates."""

    def fire(
        self,
        ctx: Context,
        claim_sets: Iterable[Iterable[Claim]],
        *,
        cap_days: int | None = None,
        exceptions: Iterable[ExceptionEval] = (),
    ) -> Evaluation:
        """The evaluation of a rule that applies, carrying its effect for ``ctx``'s task."""
        hit = Hit(self.id, self.effects[ctx.task], normalize_claim_sets(claim_sets), cap_days)
        return Evaluation(hit, tuple(exceptions))


class RequestRule(AccessRule, ABC):
    """A rule with an effect on access requests."""

    @abstractmethod
    def evaluate_request(self, ctx: RequestContext) -> Evaluation:
        """Run the predicate against one access request."""


class ReviewRule(AccessRule, ABC):
    """A rule with an effect on access-review items."""

    @abstractmethod
    def evaluate_review(self, ctx: ItemContext) -> Evaluation:
        """Run the predicate against one review item."""
