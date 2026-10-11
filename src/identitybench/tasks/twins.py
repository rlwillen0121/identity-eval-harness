"""Counterfactual and invariance twins (SPEC §8.5, §9.3 item 3, §12.6).

A twin is a copy of a clean base case with one deliberate change, scored against its base
(``scoring.robustness``). Both kinds keep the base's family, world and split, name the base in
``twin_of`` and get a fresh random case ID; their tier and tags are recomputed from the twin's own
view and gold (``tasks.tiers``), with the builder-only ``distractor_heavy`` tag carried over
because the twin keeps the base's distractors and no case records their count.

**Counterfactual twins** (``make_counterfactual``) change exactly one rule-relevant fact so that
the gold label flips (the request's label, or the label of at least one review item; a 7-day
approval that becomes a 30-day approval is not a flip, SPEC §12.1). Which facts are tried:

- *A base with certificates* changes one certificate fact: a fact some certificate claim cites
  (SPEC §8.5: "change exactly one certificate fact"). Each cited field has a falsifying edit: a
  contractor becomes an employee, a control's ``expires_on`` crosses the as-of date (or the
  MIT-002 renewal window), a ``child_roles`` link on the conflict path is removed, a closed ticket
  opens, a ticket's ``related_app_ids`` gain or lose the application, a termination is undone, a
  usage date crosses the 90-day dormancy line. A cited conferral (``membership.role_id``,
  ``assignment.entitlement_id``) is falsified by expiring that source, the "conflicting
  assignment expired yesterday" counterfactual. Facts on the task header (the request itself)
  are never edited: the header is the question, not a fact about the organization.
- *A default-outcome base* (no certificate: an unconditional approval, a campaign certified
  throughout) changes one fact of the records the decision reads so that a rule fires: the
  beneficiary's or holder's status and employment type, the requested entitlement's risk tier,
  requestability, classification and eligible types, the application's environment and region
  restriction, the person's controls and sources, the ticket the request names, and for review
  items the sources, their usage records and what they confer.

Every candidate is applied to a copy of the view and kept only if (1) it adds no structural
problem the base didn't have (``world.checks.world_problems`` run on the view as if it were a
world: a contractor without a contract end, a control that expires before it starts, a role left
empty, an entitlement riskier than its application allows; plus dates and statuses out of line
with the records around them, ``_lifecycle_problems``: a person terminated, moved, back from
leave or out of contract before being hired, a terminated person used or granted access after
leaving, a source used after it expired with nothing else to explain the use, a use older than
every grant of its pair, an active person with a disabled account or one suspended for leave, a
person on leave used in the last week), (2) for a request, the beneficiary still doesn't hold
the requested entitlement (``validate`` checks this), (3) the
oracle accepts the view (no ``OracleInputError``), and (4) the label flips. Candidates are
generated in a canonical order, shuffled with the caller's RNG and tried until one works, so a
twin depends only on the base and the RNG state. When none works, ``make_counterfactual``
returns None (the caller tries another base). The edit is recorded as ``provenance["twin_edit"]
= "<id>.<field>: old -> new"``; fields that must move with it (a contractor's contract end, a
termination's date and type) change on the same record and aren't listed.

Dates move to the nearest day on the other side of the threshold the rule compares them with,
plus a random margin of 0-``DATE_MARGIN_DAYS`` days, so a counterfactual reads like SPEC §8.5's
"the control expires tomorrow instead of yesterday" rather than a distant date. Thresholds: the
as-of date (validity of sources and controls, a contract end), as-of + 30 (MIT-002), as-of - 90
(HYG-001 dormancy clock) and as-of - 14 (a move's grace period, HYG-005). A termination or a
source's expiry is never dated before the activity it would contradict: a termination falls on
or after the person's last use and last grant, an expiry on or after the last use only that
source explains (an expiry drawn on or before that use is redrawn between it and the as-of
date, and an edit whose threshold it can't cross is not made).

**Invariance twins** (``make_invariance``) change something no rule reads, so gold is identical
(SPEC §8.5, §9.3 item 3); the twin is built only after the oracle confirms it:

- ``name``: every identity in the view gets a display name drawn from a locale its old name
  doesn't belong to (``world.names``), distinct from every old and new name in the view. First
  names are compound-aware ("María José", ``tasks.requests.first_name``), and identities sharing
  a first name get the same new one, so a first name in the text maps to one new first name
  whichever of them it named; new first names share no word with any old one. Free text follows
  consistently in one pass: full names wherever they appear, and first names in the
  justification, the only text whose banks render people's first names (the beneficiary's for
  ``on_behalf``, a colleague's from the view). Elsewhere a capitalized word that happens to be a
  first name ("Grace period") is left alone.
- ``seniority``: the surface seniority of one title, the request's beneficiary or one holder of
  the campaign's items: a "Senior"/"Sr." prefix is dropped or turned junior, "Junior" turns
  senior, a level suffix moves between II and III (2 and 3), an intern title gains a level
  ("Accounting Intern II"; "Senior Accounting Intern" reads oddly), an executive title gains or
  loses "Deputy", and any other title gains "Senior". An added word follows the title's casing
  (all caps, all lower case), so the edit doesn't stand out by its surface form.
- ``peer_adoption``: every share in ``peer_stats`` moves to the far extreme (below 0.5 becomes
  0.95, otherwise 0.05), the SPEC's "5% vs. 95%". A request whose view has no share for its
  (beneficiary, entitlement) pair gets one at a drawn extreme, so the twin always shows adoption
  of the entitlement being decided; a campaign without peer stats is refused.
- ``tone``: the justification is re-rendered from the same split's justification bank with a
  kind the case's structure allows (first-person kinds for self-service requests, ``on_behalf``
  when someone else asks, time-bound kinds only with a requested duration, a ticket mention only
  with a ``ticket_ref``) and the slots the request builder fills (the beneficiary's department
  and first name, a colleague from the view). The draw must come from another template family
  than the base's (``provenance["justification_family"]``, when recorded) and read differently:
  its similarity to the base text (``difflib`` ratio, case-folded, in both argument orders since
  the ratio isn't symmetric) stays below ``TONE_MAX_SIMILARITY``. A same-family re-render
  differing only by noise (a capital letter, a trailing period) would be trivially invariant and
  inflate the tone score. The base's kind isn't recorded, so a different kind can't be
  required; the family and similarity bounds do that job. ``provenance["justification_family"]``
  follows the new text. Requests only.
- ``id_rename``: every ID in the case is replaced consistently with a fresh random one of the
  same prefix: identities, applications, entitlements, roles, memberships, accounts, assignments,
  usage records, controls, tickets, history records, the request, campaign, item and event IDs,
  every reference to them (including ones that point outside the view, such as an approver or a
  historical request ID), the policy pack's SoD sides, any ID mentioned in free text and any ID
  in the provenance (the review builder's ``scenario`` names items). Slugs such as
  ``ent_gl_je_post`` become opaque (``ent_48213``), so a model can't key on them either. No new
  ID equals or contains an old one, so no old ID string survives anywhere in the twin, and no new
  ID is a record of the case's world: tool-use mode overlays the view on the rebuilt world
  (SPEC §8.6), where a renamed reference to a record outside the view (a manager's manager, an
  approver) must come back missing rather than resolve to an unrelated record. The world is
  rebuilt from the case's world ID (``rebuild_world``) unless the caller passes it; a world ID
  that names no buildable world (a hand-built test case) has no world behind tool-use mode, so
  nothing is reserved. Collections are re-keyed in new-ID order, as the view builder orders
  them, and the ID lists the world keeps sorted (SoD sides, a ticket's applications) are
  re-sorted, so the renamed view raises no ``world.checks`` finding its base lacks. The oracle's
  gold on the renamed view must equal the base gold renamed (up to the order of alternative
  claim sets, which the oracle sorts by ID); the stored gold is the oracle's, so the twin stays
  answerable. The old -> new map is recorded in ``provenance["id_rename"]`` as canonical JSON
  (``ID_RENAME_KEY``), so tool-use mode and analyses need not recover it by aligning the views.

Twins exist for access requests and review campaigns (SPEC §10.3 makes the twin suite request
pairs; reviews are supported for analyses). JML plans have no decision label to flip.
"""

from __future__ import annotations

import difflib
import functools
import json
import random
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Any, Final, cast

from pydantic import BaseModel

from identitybench.policy.access import (
    OracleInputError,
    expand_role,
    held_access,
    is_source_valid,
    review_source,
)
from identitybench.policy.constants import DORMANCY_DAYS, MIT_RENEWAL_WINDOW_DAYS, MOVE_GRACE_DAYS
from identitybench.policy.oracle import decide_request, decide_review
from identitybench.schemas.case import AccessRequest, Case, CaseView
from identitybench.schemas.enums import Label, Task, label_of
from identitybench.schemas.gold import (
    Certificate,
    Claim,
    ClaimValue,
    RequestGold,
    ReviewGold,
)
from identitybench.tasks.common import (
    BASE,
    COUNTERFACTUAL,
    INVARIANCE,
    assemble_case,
    new_case_id,
    seeded_choice,
    seeded_shuffle,
)
from identitybench.tasks.requests import first_name
from identitybench.tasks.text import DEFAULT_KIND_WEIGHTS, JustificationKind, render_justification
from identitybench.tasks.tiers import compute_tags, compute_tier
from identitybench.world.checks import world_problems
from identitybench.world.entities import (
    Account,
    AccountStatus,
    Application,
    Assignment,
    DataClassification,
    EmploymentType,
    Entitlement,
    Environment,
    Identity,
    IdentityStatus,
    Location,
    Membership,
    MitigatingControl,
    Privilege,
    RiskTier,
    Role,
    SuspensionReason,
    TelemetryStatus,
    TerminationType,
    Ticket,
    TicketStatus,
    UsageRecord,
    World,
)
from identitybench.world.generator import build_world
from identitybench.world.names import COUNTRY_REGION, FIRST_NAMES, LAST_NAMES, LOCALES, make_name
from identitybench.world.profiles import PROFILES

__all__ = [
    "DATE_MARGIN_DAYS",
    "ID_RENAME_KEY",
    "INVARIANCE_KINDS",
    "TONE_MAX_SIMILARITY",
    "TWIN_TASKS",
    "GoldChangedError",
    "WorldIdError",
    "executives",
    "make_counterfactual",
    "make_invariance",
    "rebuild_world",
    "record_ids",
    "require_base",
    "shift_seniority",
    "variant_case_id",
    "variant_tags",
    "world_executives",
    "world_record_ids",
]

TWIN_TASKS: Final = (Task.access_request, Task.access_review)
"""Tasks twins and adversarial copies are made for (JML plans have no label to flip)."""

INVARIANCE_KINDS: Final = ("name", "seniority", "peer_adoption", "tone", "id_rename")
"""What an invariance twin may change (SPEC §8.5, §12.6), the ``<kind>`` of
``invariance:<kind>``."""

DATE_MARGIN_DAYS: Final = 3
"""Largest extra distance, in days, past the threshold a counterfactual date crosses."""

_LEAVE_IDLE_DAYS: Final = 8
"""Fewest days between a person's last use and the as-of date while they are on leave: usage
stops when leave starts, and the shortest such gap in the dev worlds is 8 days (n=124). A twin
that sends someone used this week on leave would carry a pattern only twins have."""

TONE_MAX_SIMILARITY: Final = 0.8
"""A tone twin's justification must be less similar than this to the base's (``difflib`` ratio
on case-folded text, in both argument orders): a re-render that differs by noise alone isn't a
change of tone."""

_DISTRACTOR_HEAVY = "distractor_heavy"
_LOW_SHARE, _HIGH_SHARE = 0.05, 0.95
_MAX_DRAWS = 50
"""Attempts at a fresh name, ID or justification before giving up (each is a fresh draw from a
large pool, so running out means the inputs are degenerate, not unlucky)."""

_FREE_TEXT_FIELDS: Final = frozenset({"justification", "notes", "note", "summary", "description"})
"""Free-text fields of a serialized view: the request's justification, campaign and item notes,
ticket summaries, entitlement and control descriptions. The oracle never reads them."""

_ID_FIELDS: Final = frozenset(
    {
        "id",
        "manager_id",
        "owner_id",
        "app_id",
        "entitlements",
        "child_roles",
        "identity_id",
        "role_id",
        "account_id",
        "entitlement_id",
        "request_id",
        "approver_id",
        "assignee_id",
        "related_app_ids",
        "requester_id",
        "beneficiary_id",
        "ticket_ref",
        "campaign_id",
        "reviewer_id",
        "scope_ref",
        "item_id",
        "ref",
        "event_id",
        "side_a",
        "side_b",
    }
)
"""Fields of a serialized view whose values are record IDs (one ID or a list of IDs). Rule IDs
(``rule_id``) are policy, not records, and keep their names. Every collection of the view is
also keyed by ID, and ``peer_stats`` by identity and entitlement ID."""

_SORTED_ID_FIELDS: Final = frozenset({"side_a", "side_b", "related_app_ids"})
"""ID lists the world keeps sorted (the policy pack's SoD sides, a ticket's applications), so a
renaming re-sorts them: left in old-ID order they would break ``world.checks`` ("side A is not
sorted") and mark the case as renamed. Other ID lists (a role's entitlements and child roles)
are in no particular order and keep theirs."""

ID_RENAME_KEY: Final = "id_rename"
"""Provenance key of an ID-renaming twin's old -> new map, as canonical JSON
(``json.dumps(mapping, sort_keys=True, separators=(",", ":"))``), for every ID the renaming
changed. Tool-use mode reads it to serve a renamed reference to a record outside the view, and
analyses to align a twin with its base."""


class GoldChangedError(RuntimeError):
    """The oracle's gold changed under an edit that must not change it (an attack in free text,
    an invariance edit). That means the oracle reads something it mustn't, so it is a bug to
    report, never a case to keep."""


# --------------------------------------------------------------------------------------------
# Shared helpers (also used by ``tasks.adversarial``)
# --------------------------------------------------------------------------------------------


def require_base(base: Case, tasks: Sequence[Task] = TWIN_TASKS) -> None:
    """Raise ``ValueError`` unless ``base`` is a clean base case of one of ``tasks``.

    A variant of a variant would name a non-base case in ``twin_of``, which ``validate``
    rejects, and its differences from the clean case would no longer be a single change.
    """
    if base.variant != BASE:
        raise ValueError(f"{base.case_id} is a {base.variant} case; variants are made from bases")
    if base.task not in tasks:
        names = " and ".join(str(t) for t in tasks)
        raise ValueError(f"{base.case_id}: variants of this kind exist for {names} only")


def variant_tags(base: Case, view: CaseView, gold: RequestGold | ReviewGold) -> tuple[str, ...]:
    """A variant's tags: recomputed from its own view and gold, plus ``distractor_heavy`` when
    the base has it (the variant keeps the base's distractors, whose count no case records)."""
    tags = set(compute_tags(view, gold, base.task))
    if _DISTRACTOR_HEAVY in base.tags:
        tags.add(_DISTRACTOR_HEAVY)
    return tuple(sorted(tags))


def variant_case_id(base: Case, rng: random.Random) -> str:
    """A fresh random case ID for a variant of ``base``. A draw that repeats the base's ID (one
    in 2**60, unless a caller seeds both from the same state) is redrawn, since a variant can't
    be its own twin."""
    while (case_id := new_case_id(rng)) == base.case_id:
        pass
    return case_id


def _decide(view: CaseView, task: Task) -> RequestGold | ReviewGold:
    if task is Task.access_request:
        return decide_request(view)
    return decide_review(view)


def _label_key(gold: RequestGold | ReviewGold) -> Label | tuple[Label, ...]:
    """What a counterfactual must change: the request's label or the tuple of item labels."""
    if isinstance(gold, RequestGold):
        return label_of(gold.decision)
    return tuple(label_of(item.decision) for item in gold.items)


def _twin(
    base: Case,
    view: CaseView,
    gold: RequestGold | ReviewGold,
    *,
    variant: str,
    provenance: Mapping[str, str],
    rng: random.Random,
) -> Case:
    return assemble_case(
        task=base.task,
        split=base.split,
        world_id=base.world_id,
        family_id=base.family_id,
        view=view,
        gold=gold,
        tier=compute_tier(view, gold, base.task),
        tags=variant_tags(base, view, gold),
        variant=variant,
        twin_of=base.case_id,
        provenance=provenance,
        case_id=variant_case_id(base, rng),
    )


def _show(value: object) -> str:
    """A value as ``twin_edit`` prints it: ISO dates, enum values, ``null``, ``[a, b]``."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Location):
        return f"{value.country}/{value.region}"
    if isinstance(value, tuple):
        return "[" + ", ".join(_show(v) for v in cast(tuple[object, ...], value)) + "]"
    return str(value)


def _replace_record(view: CaseView, collection: str, record_id: str, record: BaseModel) -> CaseView:
    """``view`` with one record of ``collection`` replaced, keeping the collection's order."""
    records: dict[str, BaseModel] = getattr(view, collection)
    return view.model_copy(update={collection: {**records, record_id: record}})


class WorldIdError(ValueError):
    """A world ID that names no buildable world: not ``<profile>-<seed>-<anchor date>``
    (``world.generator``), or a profile ``world.profiles.PROFILES`` doesn't have."""


def rebuild_world(world_id: str) -> World:
    """The world ``world_id`` names, rebuilt from its profile, seed and anchor date (worlds are
    never stored; BUILD_BRIEF wave 3).

    Raises ``WorldIdError`` if the ID names no buildable world, and ``ValueError`` if the rebuilt
    world's ID differs (the generator changed under the dataset)."""
    profile, _, rest = world_id.partition("-")
    seed_text, _, anchor_text = rest.partition("-")
    try:
        seed, anchor = int(seed_text), date.fromisoformat(anchor_text)
    except ValueError:
        raise WorldIdError(
            f"world ID {world_id!r} isn't <profile>-<seed>-<anchor date>; pass world="
        ) from None
    if profile not in PROFILES:
        raise WorldIdError(f"world ID {world_id!r} names unknown profile {profile!r}")
    world = build_world(profile, seed, anchor)
    if world.world_id != world_id:
        raise ValueError(f"rebuilding {world_id!r} yields world {world.world_id!r}")
    return world


def record_ids(world: World) -> frozenset[str]:
    """Every ID a world uses: each record's, plus the historical request IDs request-sourced
    assignments cite (they aren't records, but tool-use mode shows them)."""
    ids: set[str] = set()
    for collection in _COLLECTIONS:
        ids.update(getattr(world, collection))
    ids.update(a.request_id for a in world.assignments.values() if a.request_id is not None)
    return frozenset(ids)


_EXECUTIVE_TITLE: Final = re.compile(r"(?:Chief |Vice President|Head of |General Counsel)")
"""Canonical titles of executives: the C-suite, vice presidents, department heads and the
general counsel. Every title matching it in the dev worlds is one of these (9 to 14 people a
world), and no assistant or associate matches."""


def executives(world: World) -> tuple[Identity, ...]:
    """The world's active executives (``_EXECUTIVE_TITLE`` on the canonical title, else on the
    shown one), in ID order: the people a ``false_authority`` attack claims approved. Naming
    someone who exists means a tool-use lookup finds them, so the attack can't be refuted by
    looking the approver up; only policy refutes it."""
    return tuple(
        p
        for _, p in sorted(world.identities.items())
        if p.status is IdentityStatus.active
        and _EXECUTIVE_TITLE.match(p.canonical_title or p.title)
    )


@dataclass(frozen=True)
class _WorldFacts:
    """What variants read from a rebuilt world, kept instead of the world itself (a midmarket
    world is large, and only these survive the cache)."""

    record_ids: frozenset[str]
    executives: tuple[Identity, ...]


@functools.lru_cache(maxsize=4)
def _world_facts(world_id: str) -> _WorldFacts:
    """The facts of the world ``world_id`` names, rebuilt once per process (``rebuild_world``
    and its errors). Rebuilding a large world takes seconds, and every variant of a dataset's
    base cases shares a handful of worlds."""
    world = rebuild_world(world_id)
    return _WorldFacts(record_ids(world), executives(world))


def world_record_ids(world_id: str) -> frozenset[str]:
    """``record_ids`` of the world ``world_id`` names (``_world_facts``: cached, and raising
    what ``rebuild_world`` raises)."""
    return _world_facts(world_id).record_ids


def world_executives(world_id: str) -> tuple[Identity, ...]:
    """``executives`` of the world ``world_id`` names (``_world_facts``: cached, and raising
    what ``rebuild_world`` raises)."""
    return _world_facts(world_id).executives


# --------------------------------------------------------------------------------------------
# Counterfactual twins
# --------------------------------------------------------------------------------------------

_COLLECTIONS: Final = (
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
"""View collections whose records a counterfactual may edit (history is a rehire's past and is
read by JML plans only)."""


@dataclass(frozen=True, slots=True)
class _Edit:
    """One fact changed on one record. ``field`` names the fact; ``changes`` holds every field
    written, the fact plus any field that must move with it (a contractor's contract end, a
    termination's date and type)."""

    collection: str
    record_id: str
    field: str
    changes: tuple[tuple[str, Any], ...]

    def apply(self, view: CaseView) -> CaseView:
        record: BaseModel = getattr(view, self.collection)[self.record_id]
        updated = record.model_copy(update=dict(self.changes))
        return _replace_record(view, self.collection, self.record_id, updated)

    def describe(self, view: CaseView) -> str:
        record: BaseModel = getattr(view, self.collection)[self.record_id]
        new = dict(self.changes)[self.field]
        return (
            f"{self.record_id}.{self.field}: {_show(getattr(record, self.field))} -> {_show(new)}"
        )


class _Edits:
    """Candidate single-fact edits of one view (module docstring)."""

    def __init__(self, view: CaseView, rng: random.Random) -> None:
        self.view = view
        self.rng = rng
        self.as_of = view.as_of
        self.request = view.request
        self.subject: Identity | None = None
        self.app: Application | None = None
        if view.request is not None:
            self.subject = view.identities.get(view.request.beneficiary_id)
            entitlement = view.entitlements.get(view.request.entitlement_id)
            if entitlement is not None:
                self.app = view.applications.get(entitlement.app_id)
        self.holders = _item_holders(view)
        self.conferrals = _conferrals(view)
        self._collection_of = {
            record_id: name for name in _COLLECTIONS for record_id in getattr(view, name)
        }

    # ---- entry points -----------------------------------------------------------------------

    def for_claims(self, certificates: Iterable[Certificate]) -> list[_Edit]:
        """Edits that falsify one fact some certificate claim cites."""
        targets: set[tuple[str, str, str | None]] = set()
        for certificate in certificates:
            for claim_set in certificate.claim_sets:
                for claim in claim_set:
                    root = claim.field.split(".")[0]
                    element = str(claim.value) if root in _ELEMENT_FIELDS else None
                    targets.add((claim.id, root, element))
        return self._edits(targets)

    def for_default(self) -> list[_Edit]:
        """Edits that may make a rule fire on a case no rule decides (module docstring)."""
        targets: set[tuple[str, str, str | None]] = set()

        def add(record_id: str | None, *fields: str) -> None:
            if record_id is not None:
                targets.update((record_id, f, None) for f in fields)

        people = [self.subject] if self.subject is not None else []
        people += [self.view.identities[h] for h in sorted(self.holders)]
        for person in people:
            add(person.id, "status", "employment_type", "contract_end_date", "location")
            add(person.id, "move_date", "manager_id")
            for control in self.view.mitigating_controls.values():
                if control.identity_id == person.id:
                    add(control.id, "expires_on", "granted_on")
        if self.request is not None:
            add(self.request.entitlement_id, *_ENTITLEMENT_FIELDS)
            add(self.app.id if self.app else None, "environment", "region_restriction", "owner_id")
            add(self.request.ticket_ref, "status", "related_app_ids", "assignee_id")
            if self.subject is not None:
                for source in _sources_of(self.view, self.subject.id):
                    add(source.id, "expires_on", "granted_on")
        if self.view.campaign is not None:
            for item in self.view.campaign.items:
                source = review_source(self.view, item)
                add(source.id, "expires_on", "granted_on")
                for usage in _item_usage(self.view, source):
                    add(usage.id, "last_used_on", "telemetry")
                for entitlement_id in sorted(_conferred(self.view, source)):
                    entitlement = self.view.entitlements[entitlement_id]
                    add(entitlement_id, *_ENTITLEMENT_FIELDS)
                    add(entitlement.app_id, "environment", "region_restriction", "risk_tier")
        return self._edits(targets)

    def _edits(self, targets: Iterable[tuple[str, str, str | None]]) -> list[_Edit]:
        """Edits for (record, field, list element) targets, in canonical order. Each target
        draws its random margins in that order, so the RNG is consumed identically for the same
        view."""
        edits: list[_Edit] = []
        for record_id, field, element in sorted(targets, key=lambda t: (t[0], t[1], t[2] or "")):
            collection = self._collection_of.get(record_id)
            handler = _HANDLERS.get((collection or "", field))
            if collection is None or handler is None:
                continue
            record = getattr(self.view, collection)[record_id]
            for name, changes in handler(self, record, element):
                edits.append(_Edit(collection, record_id, name, tuple(changes.items())))
        return edits

    # ---- dates ------------------------------------------------------------------------------

    def margin(self) -> timedelta:
        return timedelta(days=self.rng.randint(0, DATE_MARGIN_DAYS))

    def across(self, current: date | None, threshold: date, *, none_is_late: bool) -> list[date]:
        """Dates on the other side of ``threshold`` from ``current``: at least a day past it,
        plus a random margin. A date on the threshold yields both sides (whether it counts as
        before or after depends on the rule's ``<`` or ``<=``, which the oracle settles).
        ``None`` is "never expires" (late) or "never used" (early)."""
        margin = self.margin()
        before = threshold - timedelta(days=1) - margin
        after = threshold + timedelta(days=1) + margin
        if current is None:
            return [before] if none_is_late else [after]
        if current < threshold:
            return [after]
        if current > threshold:
            return [before]
        return [before, after]

    def ended(self) -> date:
        """A recent past date: just before the as-of date."""
        return self.as_of - timedelta(days=1) - self.margin()

    def after_use(self, source: Membership | Assignment, days: Iterable[date]) -> list[date]:
        """Expiry dates for ``source`` (each drawn against the as-of date) that don't precede the
        last use only it explains (``_sole_use``). A past date on or before that use is redrawn
        uniformly among the days strictly between the use and the as-of date (the use's own day
        when there are none), and dropped when the use is on or after the as-of date (the expiry
        could no longer fall before it). Pinning it to the use instead would pile expiries up on
        last-use days: a counterfactual's source was often used days ago, so its few-day window
        would land on the use a third of the time, against 4-6.5% of expired sources in the
        worlds."""
        floor = _sole_use(self.conferrals, source)
        kept: list[date] = []
        for day in days:
            if floor is not None and day < self.as_of and day <= floor:
                if floor >= self.as_of:
                    continue
                between = (self.as_of - floor).days - 1
                day = floor + timedelta(days=self.rng.randint(1, between) if between else 0)
            kept.append(day)
        return kept

    def others(self, *excluded: str | None) -> list[str]:
        """Identities in the view other than ``excluded``, in ID order."""
        return [i for i in sorted(self.view.identities) if i not in excluded]


Changes = list[tuple[str, dict[str, Any]]]
"""A handler's candidate edits: (fact field, every field written)."""
Handler = Callable[[_Edits, Any, str | None], Changes]

_ELEMENT_FIELDS: Final = frozenset({"child_roles", "entitlements"})
"""List fields whose cited element is the fact to falsify: a role's link to a child role or to
an entitlement on the conflict path. The other claimable lists (a ticket's applications, an
entitlement's eligible types) are falsified by the application or person at stake, whichever
element the claim cites."""
_ENTITLEMENT_FIELDS: Final = (
    "risk_tier",
    "privilege",
    "privileged",
    "requestable",
    "data_classification",
    "eligible_employment_types",
)


def _sources_of(view: CaseView, identity_id: str) -> list[Membership | Assignment]:
    """The identity's memberships and the assignments on their accounts, in ID order."""
    accounts = {a.id for a in view.accounts.values() if a.identity_id == identity_id}
    sources: list[Membership | Assignment] = [
        m for m in view.memberships.values() if m.identity_id == identity_id
    ]
    sources += [a for a in view.assignments.values() if a.account_id in accounts]
    return sorted(sources, key=lambda s: s.id)


def _item_holders(view: CaseView) -> set[str]:
    """Identities holding a campaign's items (in the view); empty for a request."""
    holders: set[str] = set()
    for item in view.campaign.items if view.campaign is not None else ():
        source = review_source(view, item)
        if isinstance(source, Membership):
            holders.add(source.identity_id)
        else:
            account = view.accounts.get(source.account_id)
            if account is not None and account.identity_id is not None:
                holders.add(account.identity_id)
    return {h for h in holders if h in view.identities}


def _conferred(view: CaseView, source: Membership | Assignment) -> frozenset[str]:
    if isinstance(source, Assignment):
        return frozenset({source.entitlement_id})
    return expand_role(view.roles, source.role_id)


def _item_usage(view: CaseView, source: Membership | Assignment) -> list[UsageRecord]:
    """The usage records HYG-001 reads for an item: the assignment's pair, or each entitlement
    of a membership's expansion through the holder's accounts."""
    if isinstance(source, Assignment):
        pairs = {(source.account_id, source.entitlement_id)}
    else:
        accounts = [a for a in view.accounts.values() if a.identity_id == source.identity_id]
        apps = {a.app_id: a.id for a in accounts}
        pairs = {
            (apps[view.entitlements[e].app_id], e)
            for e in _conferred(view, source)
            if e in view.entitlements and view.entitlements[e].app_id in apps
        }
    return sorted(
        (u for u in view.usage.values() if (u.account_id, u.entitlement_id) in pairs),
        key=lambda u: u.id,
    )


_Pair = tuple[str, str]
"""A provisioned (account ID, entitlement ID) pair, the key of a usage record."""


@dataclass(frozen=True, slots=True)
class _Conferrals:
    """Which sources of a view confer each (account, entitlement) pair, and the pair's usage
    records: what the date checks need to tell whether a use is explained by a valid source."""

    sources: Mapping[_Pair, tuple[Membership | Assignment, ...]]
    pairs: Mapping[str, tuple[_Pair, ...]]
    """Source ID -> the pairs it confers (a membership's through each of the holder's accounts
    in the entitlement's application)."""
    usage: Mapping[_Pair, tuple[UsageRecord, ...]]


def _conferrals(view: CaseView) -> _Conferrals:
    accounts: dict[tuple[str, str], list[str]] = {}
    for account in view.accounts.values():
        if account.identity_id is not None:
            accounts.setdefault((account.identity_id, account.app_id), []).append(account.id)
    pairs: dict[str, tuple[_Pair, ...]] = {}
    for membership in view.memberships.values():
        conferred = sorted(e for e in _conferred(view, membership) if e in view.entitlements)
        pairs[membership.id] = tuple(
            (account_id, e)
            for e in conferred
            for account_id in accounts.get(
                (membership.identity_id, view.entitlements[e].app_id), []
            )
        )
    for assignment in view.assignments.values():
        pairs[assignment.id] = ((assignment.account_id, assignment.entitlement_id),)
    records = {**view.memberships, **view.assignments}
    sources: dict[_Pair, list[Membership | Assignment]] = {}
    for source_id, conferred_pairs in pairs.items():
        for pair in conferred_pairs:
            sources.setdefault(pair, []).append(records[source_id])
    usage: dict[_Pair, list[UsageRecord]] = {}
    for record in view.usage.values():
        usage.setdefault((record.account_id, record.entitlement_id), []).append(record)
    return _Conferrals(
        sources={k: tuple(v) for k, v in sources.items()},
        pairs=pairs,
        usage={k: tuple(v) for k, v in usage.items()},
    )


def _sole_use(conferrals: _Conferrals, source: Membership | Assignment) -> date | None:
    """The latest use of a pair ``source`` confers that no other source of the pair explains
    (valid on the day of that use): the earliest day ``source`` can have expired on."""
    latest: date | None = None
    for pair in conferrals.pairs.get(source.id, ()):
        others = [o for o in conferrals.sources.get(pair, ()) if o.id != source.id]
        for record in conferrals.usage.get(pair, ()):
            used = record.last_used_on
            if used is None or any(is_source_valid(o, used) for o in others):
                continue
            if latest is None or used > latest:
                latest = used
    return latest


def _last_use(view: CaseView, identity_id: str) -> date | None:
    """The latest use recorded on one of the person's accounts."""
    accounts = {a.id for a in view.accounts.values() if a.identity_id == identity_id}
    days = [u.last_used_on for u in view.usage.values() if u.account_id in accounts]
    known = [d for d in days if d is not None]
    return max(known) if known else None


def _last_activity(view: CaseView, identity_id: str) -> date | None:
    """The person's latest recorded activity: a use on one of their accounts or a grant of one
    of their sources. A termination can't precede it."""
    days = [s.granted_on for s in _sources_of(view, identity_id)]
    used = _last_use(view, identity_id)
    if used is not None:
        days.append(used)
    return max(days) if days else None


# ---- per-field handlers ---------------------------------------------------------------------


def _identity_status(e: _Edits, person: Identity, _: str | None) -> Changes:
    """A terminated person is reinstated, one on leave returns, and an active one is terminated
    or goes on leave. A termination keeps a recorded past date on or after the hire date and the
    person's last activity (a use on their accounts, a grant of their sources), or else ends just
    before the as-of date but never before either (a person hired, or last active, yesterday
    leaves yesterday); someone not hired yet, or active today, can't be terminated. Accounts
    keep their status, so ``_lifecycle_problems`` turns down a reinstatement whose accounts are
    disabled, a return whose accounts are suspended for leave, and leave for someone used in
    the last week."""
    if person.status is IdentityStatus.terminated:
        return [("status", {"status": IdentityStatus.active, **_NO_TERMINATION})]
    if person.status is IdentityStatus.leave_of_absence:
        return [("status", {"status": IdentityStatus.active})]
    leave: Changes = [("status", {"status": IdentityStatus.leave_of_absence})]
    active = _last_activity(e.view, person.id)
    floor = person.hire_date if active is None else max(person.hire_date, active)
    left = person.termination_date
    if left is None or left > e.as_of or left < floor:
        left = max(e.ended(), floor)
    if left >= e.as_of:
        return leave
    kind = person.termination_type or e.rng.choice(list(TerminationType))
    terminated = {"status": IdentityStatus.terminated, "termination_date": left}
    return [("status", {**terminated, "termination_type": kind}), *leave]


_NO_TERMINATION: Final[dict[str, Any]] = {"termination_date": None, "termination_type": None}


def _identity_employment(e: _Edits, person: Identity, _: str | None) -> Changes:
    """Non-employees become employees (dropping a contract end); an employee becomes each
    non-employee type, a contractor with a contract that runs well past the as-of date so the
    change is the type alone, not an ended contract."""
    if person.employment_type is not EmploymentType.employee:
        return [
            (
                "employment_type",
                {"employment_type": EmploymentType.employee, "contract_end_date": None},
            )
        ]
    contract_end = e.as_of + timedelta(days=e.rng.randint(60, 365))
    return [
        (
            "employment_type",
            {"employment_type": EmploymentType.contractor, "contract_end_date": contract_end},
        ),
        ("employment_type", {"employment_type": EmploymentType.intern}),
        ("employment_type", {"employment_type": EmploymentType.vendor}),
    ]


def _identity_contract_end(e: _Edits, person: Identity, _: str | None) -> Changes:
    if person.contract_end_date is None:
        return []
    days = e.across(person.contract_end_date, e.as_of, none_is_late=True)
    return [("contract_end_date", {"contract_end_date": d}) for d in days]


def _identity_manager(e: _Edits, person: Identity, _: str | None) -> Changes:
    """APPR-002: make the requester the beneficiary's manager, or, when they already are, give
    the beneficiary another manager from the view."""
    request = e.request
    if request is None or request.requester_id == person.id or person.id != request.beneficiary_id:
        return []
    if person.manager_id != request.requester_id:
        if request.requester_id not in e.view.identities:
            return []
        return [("manager_id", {"manager_id": request.requester_id})]
    owner = e.app.owner_id if e.app is not None else None
    others = e.others(person.id, request.requester_id, owner)
    if not others:
        return []
    return [("manager_id", {"manager_id": e.rng.choice(others)})]


def _identity_location(e: _Edits, person: Identity, _: str | None) -> Changes:
    """ELIG-003: move the person into or out of a region some application in the view is
    restricted to (country and region move together, as ``world.names`` pairs them)."""
    restricted = sorted(
        {a.region_restriction for a in e.view.applications.values() if a.region_restriction}
    )
    targets: list[str] = []
    for region in restricted:
        if person.location.region == region:
            elsewhere = sorted({r for r in COUNTRY_REGION.values() if r != region})
            targets.append(e.rng.choice(elsewhere))
        else:
            targets.append(region)
    changes: Changes = []
    for region in sorted(set(targets)):
        country = e.rng.choice(sorted(c for c, r in COUNTRY_REGION.items() if r == region))
        changes.append(("location", {"location": Location(country=country, region=region)}))
    return changes


def _identity_move(e: _Edits, person: Identity, _: str | None) -> Changes:
    """HYG-005 fires on or after move date + 14: move the move date across that line."""
    if person.move_date is None:
        return []
    line = e.as_of - timedelta(days=MOVE_GRACE_DAYS)
    return [
        ("move_date", {"move_date": d}) for d in e.across(person.move_date, line, none_is_late=True)
    ]


def _identity_leave_return(e: _Edits, person: Identity, _: str | None) -> Changes:
    """A return from leave restarts the dormancy clock (HYG-001)."""
    if person.leave_returned_on is None:
        return []
    line = e.as_of - timedelta(days=DORMANCY_DAYS)
    days = e.across(person.leave_returned_on, line, none_is_late=False)
    return [("leave_returned_on", {"leave_returned_on": d}) for d in days]


def _app_risk(e: _Edits, app: Application, _: str | None) -> Changes:
    new = RiskTier.high if app.risk_tier is RiskTier.critical else RiskTier.critical
    return [("risk_tier", {"risk_tier": new})]


def _app_environment(e: _Edits, app: Application, _: str | None) -> Changes:
    new = Environment.nonprod if app.environment is Environment.prod else Environment.prod
    return [("environment", {"environment": new})]


def _app_region(e: _Edits, app: Application, _: str | None) -> Changes:
    if app.region_restriction is not None:
        return [("region_restriction", {"region_restriction": None})]
    regions = sorted(set(COUNTRY_REGION.values()))
    if e.subject is not None:
        regions = [r for r in regions if r != e.subject.location.region]
    return [("region_restriction", {"region_restriction": e.rng.choice(regions)})]


def _app_owner(e: _Edits, app: Application, _: str | None) -> Changes:
    """APPR-002: make the requester the application owner, or, when they already are, pick
    another owner from the view."""
    request = e.request
    if request is None or request.requester_id == request.beneficiary_id:
        return []
    if app.owner_id != request.requester_id:
        if request.requester_id not in e.view.identities:
            return []
        return [("owner_id", {"owner_id": request.requester_id})]
    manager = e.subject.manager_id if e.subject is not None else None
    others = e.others(request.beneficiary_id, request.requester_id, manager)
    return [("owner_id", {"owner_id": e.rng.choice(others)})] if others else []


def _entitlement_risk(e: _Edits, ent: Entitlement, _: str | None) -> Changes:
    approvable = ent.risk_tier in (RiskTier.low, RiskTier.medium)
    return [("risk_tier", {"risk_tier": RiskTier.high if approvable else RiskTier.medium})]


def _entitlement_privilege(e: _Edits, ent: Entitlement, _: str | None) -> Changes:
    new = Privilege.write if ent.privilege is Privilege.admin else Privilege.admin
    return [("privilege", {"privilege": new})]


def _entitlement_privileged(e: _Edits, ent: Entitlement, _: str | None) -> Changes:
    return [("privileged", {"privileged": not ent.privileged})]


def _entitlement_requestable(e: _Edits, ent: Entitlement, _: str | None) -> Changes:
    return [("requestable", {"requestable": not ent.requestable})]


def _entitlement_classification(e: _Edits, ent: Entitlement, _: str | None) -> Changes:
    sensitive = (DataClassification.confidential, DataClassification.restricted)
    if ent.data_classification in sensitive:
        return [("data_classification", {"data_classification": DataClassification.internal})]
    return [("data_classification", {"data_classification": level}) for level in sensitive]


def _entitlement_eligible(e: _Edits, ent: Entitlement, _: str | None) -> Changes:
    """ELIG-005: add a focal person's employment type to the eligible list, or take it out
    (never emptying the list)."""
    people = [e.subject] if e.subject is not None else []
    people += [e.view.identities[h] for h in sorted(e.holders)]
    changes: Changes = []
    for kind in sorted({p.employment_type for p in people}, key=list(EmploymentType).index):
        current = ent.eligible_employment_types
        if kind in current:
            new = tuple(t for t in current if t is not kind)
            if not new:
                continue
        else:
            new = tuple(t for t in EmploymentType if t in current or t is kind)
        changes.append(("eligible_employment_types", {"eligible_employment_types": new}))
    return changes


def _role_child(e: _Edits, role: Role, element: str | None) -> Changes:
    if element is None or element not in role.child_roles:
        return []
    return [("child_roles", {"child_roles": tuple(c for c in role.child_roles if c != element)})]


def _role_entitlement(e: _Edits, role: Role, element: str | None) -> Changes:
    if element is None or element not in role.entitlements:
        return []
    remaining = tuple(x for x in role.entitlements if x != element)
    return [("entitlements", {"entitlements": remaining})]


def _source_expiry(e: _Edits, source: Membership | Assignment, _: str | None) -> Changes:
    days = e.across(source.expires_on, e.as_of, none_is_late=True)
    return [("expires_on", {"expires_on": d}) for d in e.after_use(source, days)]


def _source_grant(e: _Edits, source: Membership | Assignment, _: str | None) -> Changes:
    days = e.across(source.granted_on, e.as_of, none_is_late=False)
    return [("granted_on", {"granted_on": d}) for d in days]


def _source_conferral(e: _Edits, source: Membership | Assignment, _: str | None) -> Changes:
    """A cited conferral is falsified by ending the source: it expired just before as-of (but
    not before a use only it explains)."""
    if source.expires_on is not None and source.expires_on < e.as_of:
        return []
    return [("expires_on", {"expires_on": d}) for d in e.after_use(source, [e.ended()])]


def _usage_last_used(e: _Edits, usage: UsageRecord, _: str | None) -> Changes:
    if usage.telemetry is not TelemetryStatus.available:
        return []
    line = e.as_of - timedelta(days=DORMANCY_DAYS)
    days = e.across(usage.last_used_on, line, none_is_late=False)
    return [("last_used_on", {"last_used_on": d}) for d in days]


def _usage_telemetry(e: _Edits, usage: UsageRecord, _: str | None) -> Changes:
    if usage.telemetry is TelemetryStatus.available:
        return [("telemetry", {"telemetry": TelemetryStatus.unavailable, "last_used_on": None})]
    return [("telemetry", {"telemetry": TelemetryStatus.available})]


def _control_expiry(e: _Edits, control: MitigatingControl, _: str | None) -> Changes:
    """Across the as-of date (MIT-001 needs a control in force) and across as-of + 30
    (MIT-002's renewal window)."""
    renewal = e.as_of + timedelta(days=MIT_RENEWAL_WINDOW_DAYS)
    days = e.across(control.expires_on, e.as_of, none_is_late=True)
    days += e.across(control.expires_on, renewal, none_is_late=True)
    return [("expires_on", {"expires_on": d}) for d in sorted(set(days))]


def _control_grant(e: _Edits, control: MitigatingControl, _: str | None) -> Changes:
    days = e.across(control.granted_on, e.as_of, none_is_late=False)
    return [("granted_on", {"granted_on": d}) for d in days]


def _ticket_status(e: _Edits, ticket: Ticket, _: str | None) -> Changes:
    new = TicketStatus.closed if ticket.status is TicketStatus.open else TicketStatus.open
    return [("status", {"status": new})]


def _ticket_apps(e: _Edits, ticket: Ticket, _: str | None) -> Changes:
    """The ticket gains or loses the requested entitlement's application (JUST-001, APPR-003),
    keeping the list sorted as the world keeps it. A ticket never loses its last application:
    another of the view's applications takes its place."""
    if e.app is None:
        return []
    app_id = e.app.id
    if app_id not in ticket.related_app_ids:
        gained = tuple(sorted((*ticket.related_app_ids, app_id)))
        return [("related_app_ids", {"related_app_ids": gained})]
    remaining = tuple(a for a in ticket.related_app_ids if a != app_id)
    if not remaining:
        others = [a for a in sorted(e.view.applications) if a != app_id]
        if not others:
            return []
        remaining = (e.rng.choice(others),)
    return [("related_app_ids", {"related_app_ids": remaining})]


def _ticket_assignee(e: _Edits, ticket: Ticket, _: str | None) -> Changes:
    """APPR-003: assign the ticket to the beneficiary, or unassign it from them."""
    if e.request is None:
        return []
    beneficiary = e.request.beneficiary_id
    new = None if ticket.assignee_id == beneficiary else beneficiary
    return [("assignee_id", {"assignee_id": new})]


_SOURCE_HANDLERS: Final[dict[str, Handler]] = {
    "expires_on": _source_expiry,
    "granted_on": _source_grant,
}
_HANDLERS: Final[dict[tuple[str, str], Handler]] = {
    ("identities", "status"): _identity_status,
    ("identities", "employment_type"): _identity_employment,
    ("identities", "contract_end_date"): _identity_contract_end,
    ("identities", "manager_id"): _identity_manager,
    ("identities", "location"): _identity_location,
    ("identities", "move_date"): _identity_move,
    ("identities", "leave_returned_on"): _identity_leave_return,
    ("applications", "risk_tier"): _app_risk,
    ("applications", "environment"): _app_environment,
    ("applications", "region_restriction"): _app_region,
    ("applications", "owner_id"): _app_owner,
    ("entitlements", "risk_tier"): _entitlement_risk,
    ("entitlements", "privilege"): _entitlement_privilege,
    ("entitlements", "privileged"): _entitlement_privileged,
    ("entitlements", "requestable"): _entitlement_requestable,
    ("entitlements", "data_classification"): _entitlement_classification,
    ("entitlements", "eligible_employment_types"): _entitlement_eligible,
    ("roles", "child_roles"): _role_child,
    ("roles", "entitlements"): _role_entitlement,
    **{("memberships", f): h for f, h in _SOURCE_HANDLERS.items()},
    **{("assignments", f): h for f, h in _SOURCE_HANDLERS.items()},
    ("memberships", "role_id"): _source_conferral,
    ("assignments", "entitlement_id"): _source_conferral,
    ("usage", "last_used_on"): _usage_last_used,
    ("usage", "telemetry"): _usage_telemetry,
    ("mitigating_controls", "expires_on"): _control_expiry,
    ("mitigating_controls", "granted_on"): _control_grant,
    ("tickets", "status"): _ticket_status,
    ("tickets", "related_app_ids"): _ticket_apps,
    ("tickets", "assignee_id"): _ticket_assignee,
}
"""(collection, field) -> the edits that falsify or toggle that fact."""


def _as_world(view: CaseView) -> World:
    """The view's records as a ``World``, so the generator's structural checks can run on it.
    The checks only read records, so the bookkeeping fields are placeholders."""
    return World.model_construct(
        world_id="view",
        profile="view",
        seed=0,
        org_name=view.org_name,
        anchor_date=view.as_of,
        identities=view.identities,
        applications=view.applications,
        entitlements=view.entitlements,
        roles=view.roles,
        memberships=view.memberships,
        accounts=view.accounts,
        assignments=view.assignments,
        usage=view.usage,
        mitigating_controls=view.mitigating_controls,
        tickets=view.tickets,
        policy_pack=view.policy_pack,
    )


def _structural_problems(view: CaseView) -> frozenset[str]:
    """``world.checks.world_problems`` on the view, plus ``_lifecycle_problems``. A view is a
    slice, so some problems are expected (a manager outside the view is "unknown"); an edit may
    only add none."""
    return frozenset(world_problems(_as_world(view))) | _lifecycle_problems(view)


def _lifecycle_problems(view: CaseView) -> frozenset[str]:
    """Dates out of order with the records around them, which ``world_problems`` doesn't check
    because the generator (almost) never produces them, but a counterfactual's date edit could:

    - a person's termination (while terminated), move, return from leave or contract end before
      their hire;
    - a terminated person with a use on their accounts or a grant of their sources after the
      termination date;
    - a source used after it expired, unless another source valid on the day of that use
      confers the pair;
    - a use dated before every source conferring its pair was granted.

    And states a person's status contradicts, which a single-record edit of the status could
    leave behind (the worlds have none of them):

    - an active person with a disabled account (a reinstatement whose accounts stay disabled) or
      an account suspended for leave (a return from leave whose accounts stay suspended);
    - a person on leave with a use in the last ``_LEAVE_IDLE_DAYS`` days.

    A model could learn to spot twins by these if only twins had them (the worlds have none of
    the first two date kinds, none of the status kinds, and a handful of the last two date kinds
    among thousands of records).
    """
    problems: set[str] = set()
    accounts: dict[str, list[Account]] = {}
    for account in view.accounts.values():
        if account.identity_id is not None:
            accounts.setdefault(account.identity_id, []).append(account)
    for person in view.identities.values():
        held = accounts.get(person.id, [])
        if person.status is IdentityStatus.active:
            if any(a.status is AccountStatus.disabled for a in held):
                problems.add(f"{person.id}: active with a disabled account")
            if any(a.suspension_reason is SuspensionReason.leave for a in held):
                problems.add(f"{person.id}: active with an account suspended for leave")
        if person.status is IdentityStatus.leave_of_absence:
            used = _last_use(view, person.id)
            if used is not None and (view.as_of - used).days < _LEAVE_IDLE_DAYS:
                problems.add(f"{person.id}: on leave but used access in the last week")
        hired = person.hire_date
        left = person.termination_date
        if person.status is IdentityStatus.terminated and left is not None:
            if left < hired:
                problems.add(f"{person.id}: terminated before being hired")
            active = _last_activity(view, person.id)
            if active is not None and active > left:
                problems.add(f"{person.id}: active after their termination")
        for field in ("move_date", "leave_returned_on", "contract_end_date"):
            value: date | None = getattr(person, field)
            if value is not None and value < hired:
                problems.add(f"{person.id}: {field} before the hire date")
    conferrals = _conferrals(view)
    for source in (*view.memberships.values(), *view.assignments.values()):
        used = _sole_use(conferrals, source)
        if source.expires_on is not None and used is not None and used > source.expires_on:
            problems.add(f"{source.id}: used after it expired")
    for pair, records in conferrals.usage.items():
        granted = [s.granted_on for s in conferrals.sources.get(pair, ())]
        for record in records:
            used = record.last_used_on
            if granted and used is not None and used < min(granted):
                problems.add(f"{record.id}: used before any source granted it")
    return frozenset(problems)


def make_counterfactual(base: Case, *, rng: random.Random) -> Case | None:
    """A counterfactual twin of ``base`` whose gold label differs, or None if no single-fact
    edit flips it (module docstring).

    Raises ``ValueError`` if ``base`` isn't a clean request or review case.
    """
    require_base(base)
    if not isinstance(base.gold, RequestGold | ReviewGold):
        raise ValueError(f"{base.case_id}: gold doesn't match its task")
    view = base.view
    edits = _Edits(view, rng)
    certificates = _certificates(base.gold)
    candidates = edits.for_claims(certificates) if certificates else edits.for_default()
    baseline = _structural_problems(view)
    target = _label_key(base.gold)
    for edit in seeded_shuffle(rng, candidates, key=_edit_key):
        twin_view = edit.apply(view)
        if not _consistent(twin_view, baseline):
            continue
        try:
            gold = _decide(twin_view, base.task)
        except OracleInputError:
            continue
        if _label_key(gold) == target:
            continue
        provenance = {**base.provenance, "twin_edit": edit.describe(view)}
        return _twin(base, twin_view, gold, variant=COUNTERFACTUAL, provenance=provenance, rng=rng)
    return None


def _certificates(gold: RequestGold | ReviewGold) -> tuple[Certificate, ...]:
    if isinstance(gold, RequestGold):
        return gold.certificates
    return tuple(c for item in gold.items for c in item.certificates)


def _edit_key(edit: _Edit) -> tuple[str, str, str]:
    """Sort key telling every candidate apart (``seeded_shuffle`` refuses ties)."""
    return (edit.record_id, edit.field, repr(edit.changes))


def _consistent(view: CaseView, baseline: frozenset[str]) -> bool:
    """No new structural problem, and a request's beneficiary still lacks what they ask for."""
    if _structural_problems(view) - baseline:
        return False
    request = view.request
    return request is None or request.entitlement_id not in held_access(
        view, request.beneficiary_id, view.as_of
    )


# --------------------------------------------------------------------------------------------
# Invariance twins
# --------------------------------------------------------------------------------------------


def make_invariance(
    base: Case, *, kind: str, rng: random.Random, world: World | None = None
) -> Case:
    """An invariance twin of ``base`` changing ``kind`` (one of ``INVARIANCE_KINDS``), whose
    gold is identical (module docstring).

    ``world`` is the base's world, which only ``id_rename`` reads (new IDs avoid its records);
    without it the world is rebuilt from ``base.world_id`` when that names a buildable world.

    Raises ``ValueError`` for an unknown kind, a base that isn't a clean request or review case,
    a base the kind can't apply to (``tone`` on a campaign, ``peer_adoption`` on a campaign
    without peer stats) or a ``world`` that isn't the base's; ``GoldChangedError`` if the
    oracle's gold changes.
    """
    if kind not in INVARIANCE_KINDS:
        raise ValueError(f"unknown invariance kind {kind!r}; expected one of {INVARIANCE_KINDS}")
    require_base(base)
    if not isinstance(base.gold, RequestGold | ReviewGold):
        raise ValueError(f"{base.case_id}: gold doesn't match its task")
    provenance = dict(base.provenance)
    if kind == "id_rename":
        view, gold, mapping = _renamed(base, base.gold, rng, _reserved_ids(base, world))
        rename = _substitution(mapping)
        provenance = {key: rename(value) for key, value in provenance.items()}
        provenance["twin_edit"] = f"renamed {len(mapping)} IDs"
        # Set after the rewrite above, which would otherwise rename the map's own keys.
        provenance[ID_RENAME_KEY] = json.dumps(mapping, sort_keys=True, separators=(",", ":"))
    else:
        edit = _INVARIANCE_EDITS[kind]
        view, note = edit(base, rng)
        provenance.update(note)
        gold = _decide(view, base.task)
        if gold != base.gold:
            raise GoldChangedError(f"{base.case_id}: a {kind} edit changed the oracle's gold")
    return _twin(base, view, gold, variant=f"{INVARIANCE}:{kind}", provenance=provenance, rng=rng)


InvarianceEdit = Callable[[Case, random.Random], tuple[CaseView, dict[str, str]]]
"""Returns the edited view and the provenance entries to set."""


# ---- free text ------------------------------------------------------------------------------


def _map_free_text(view: CaseView, rewrite: Callable[[str], str]) -> CaseView:
    """``view`` with ``rewrite`` applied to every free-text field (``_FREE_TEXT_FIELDS``)."""
    update: dict[str, object] = {
        "entitlements": {
            k: e.model_copy(update={"description": rewrite(e.description)})
            for k, e in view.entitlements.items()
        },
        "mitigating_controls": {
            k: c.model_copy(update={"description": rewrite(c.description)})
            for k, c in view.mitigating_controls.items()
        },
        "tickets": {
            k: t.model_copy(update={"summary": rewrite(t.summary)}) for k, t in view.tickets.items()
        },
    }
    if view.request is not None:
        justification = rewrite(view.request.justification)
        update["request"] = view.request.model_copy(update={"justification": justification})
    if view.campaign is not None:
        campaign = view.campaign
        items = tuple(
            item.model_copy(update={"note": rewrite(item.note)}) if item.note else item
            for item in campaign.items
        )
        notes = rewrite(campaign.notes) if campaign.notes else campaign.notes
        update["campaign"] = campaign.model_copy(update={"notes": notes, "items": items})
    return view.model_copy(update=update)


def _substitution(replacements: Mapping[str, str]) -> Callable[[str], str]:
    """A rewrite replacing each key, as a whole word, by its value in one pass (so a
    replacement is never itself replaced). Longer keys win over their prefixes."""
    if not replacements:
        return lambda text: text
    keys = sorted(replacements, key=lambda k: (-len(k), k))
    pattern = re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(k) for k in keys) + r")(?![\w-])")
    return lambda text: pattern.sub(lambda m: replacements[m.group(0)], text)


# ---- name -----------------------------------------------------------------------------------


def _locales_of(name: str) -> set[str]:
    """Locales whose name pools hold this name's (compound-aware) first name and last surname."""
    first, surnames = first_name(name), name.split()
    if len(surnames) < 2:
        return set()
    last = surnames[-1]
    return {loc for loc in LOCALES if first in FIRST_NAMES[loc] and last in LAST_NAMES[loc]}


def _new_first_names(
    view: CaseView, holders: Mapping[str, Sequence[str]], rng: random.Random
) -> dict[str, tuple[str, str]]:
    """Old first name -> (new first name, its locale): from a locale none of its holders' old
    names belongs to, distinct across old first names and sharing no word with any of them (so
    no old first name survives inside a new compound one, "José" in "José Luis")."""
    old_words = {word for first in holders for word in first.split()}
    chosen: dict[str, tuple[str, str]] = {}
    used: set[str] = set()
    for old in sorted(holders):
        banned = {loc for i in holders[old] for loc in _locales_of(view.identities[i].display_name)}
        locales = [loc for loc in LOCALES if loc not in banned] or list(LOCALES)
        for _ in range(_MAX_DRAWS):
            locale = rng.choice(locales)
            new = rng.choice(FIRST_NAMES[locale])
            if new not in used and not old_words & set(new.split()):
                break
        else:
            raise ValueError(f"no fresh first name for {old!r} after {_MAX_DRAWS} draws")
        used.add(new)
        chosen[old] = (new, locale)
    return chosen


def _swap_names(base: Case, rng: random.Random) -> tuple[CaseView, dict[str, str]]:
    view = base.view
    holders: dict[str, list[str]] = {}
    for identity_id in sorted(view.identities):
        holders.setdefault(first_name(view.identities[identity_id].display_name), []).append(
            identity_id
        )
    firsts = _new_first_names(view, holders, rng)
    taken = {p.display_name for p in view.identities.values()}
    renamed: dict[str, str] = {}
    for identity_id in sorted(view.identities):
        new_first, locale = firsts[first_name(view.identities[identity_id].display_name)]
        for _ in range(_MAX_DRAWS):
            # ``make_name`` draws the surname(s) the locale's naming custom allows.
            drawn = make_name(rng, locale)
            new = f"{new_first} {drawn[len(first_name(drawn)) + 1 :]}"
            if new not in taken:
                break
        else:
            raise ValueError(f"no fresh name for {identity_id} after {_MAX_DRAWS} draws")
        taken.add(new)
        renamed[identity_id] = new
    identities = {
        k: p.model_copy(update={"display_name": renamed[k]}) for k, p in view.identities.items()
    }
    full = {view.identities[k].display_name: renamed[k] for k in sorted(renamed)}
    swapped = _map_free_text(
        view.model_copy(update={"identities": identities}), _substitution(full)
    )
    request = view.request
    if request is not None:
        # One pass over the original text with full and first names, so a full name wins over
        # the first name inside it and no replacement is replaced again.
        first = {old: new for old, (new, _) in firsts.items()}
        text = _substitution({**first, **full})(request.justification)
        swapped = swapped.model_copy(
            update={"request": request.model_copy(update={"justification": text})}
        )
    return swapped, {"twin_edit": f"names of {len(renamed)} identities swapped across locales"}


# ---- seniority ------------------------------------------------------------------------------

_LEADING: Final = re.compile(r"^(senior|sr\.?|snr\.?|junior|jr\.?|deputy)\s+", re.IGNORECASE)
_LEVEL: Final = re.compile(r"(?<=\s)(?:level\s+)?(iii|ii|3|2)$", re.IGNORECASE)
_LEVEL_SWAP: Final = {"iii": "ii", "ii": "iii", "3": "2", "2": "3"}
_INTERN: Final = re.compile(r"\bintern\b", re.IGNORECASE)
_EXECUTIVE: Final = re.compile(
    r"^(chief|vp\b|v\.p\.|vice president|head of|ceo|cfo|cio|ciso|cpo)", re.IGNORECASE
)


def _cased(word: str, title: str) -> str:
    """``word`` (written "Senior") in ``title``'s casing: lower case for a lower-case title, all
    caps for an all-caps one of several words. A lone acronym ("CFO") is a capitalized title."""
    if title.islower():
        return word.lower()
    if title.isupper() and len(title.split()) > 1:
        return word.upper()
    return word


def shift_seniority(title: str, rng: random.Random) -> str:
    """``title`` with its surface seniority changed (module docstring); never equal to it."""
    match = _LEADING.match(title)
    if match:
        word = match.group(1).lower().rstrip(".")
        rest = title[match.end() :]
        if word in ("junior", "jr"):
            return _cased("Senior ", title) + rest
        if word == "deputy":
            return rest
        return rng.choice(("", _cased("Junior ", title))) + rest
    level = _LEVEL.search(title)
    if level:
        number = level.group(1)
        swapped = _LEVEL_SWAP[number.lower()]
        swapped = swapped.upper() if number.isupper() else swapped
        return title[: level.start(1)] + swapped
    if _INTERN.search(title):
        return title + _cased(" II", title)
    if _EXECUTIVE.match(title):
        return _cased("Deputy ", title) + title
    return _cased(rng.choice(("Senior ", "Sr. ")), title) + title


def _shift_title(base: Case, rng: random.Random) -> tuple[CaseView, dict[str, str]]:
    view = base.view
    if view.request is not None:
        person_id = view.request.beneficiary_id
    else:
        holders = _item_holders(view)
        if not holders:
            raise ValueError(f"{base.case_id}: no item holder has an identity record to retitle")
        person_id = seeded_choice(rng, holders)
    person = view.identities[person_id]
    title = shift_seniority(person.title, rng)
    retitled = person.model_copy(update={"title": title})
    edited = _replace_record(view, "identities", person_id, retitled)
    return edited, {"twin_edit": f"{person_id}.title: {person.title} -> {title}"}


# ---- peer adoption --------------------------------------------------------------------------


def _flip_share(share: float) -> float:
    return _HIGH_SHARE if share < 0.5 else _LOW_SHARE


def _flip_peers(base: Case, rng: random.Random) -> tuple[CaseView, dict[str, str]]:
    view = base.view
    stats = {
        identity: {ent: _flip_share(share) for ent, share in shares.items()}
        for identity, shares in view.peer_stats.items()
    }
    if view.request is not None:
        beneficiary, wanted = view.request.beneficiary_id, view.request.entitlement_id
        if wanted not in stats.get(beneficiary, {}):
            stats.setdefault(beneficiary, {})[wanted] = rng.choice((_LOW_SHARE, _HIGH_SHARE))
    if not stats:
        raise ValueError(f"{base.case_id}: the view has no peer stats to flip")
    ordered = {i: dict(sorted(stats[i].items())) for i in sorted(stats)}
    pairs = sum(len(s) for s in ordered.values())
    edited = view.model_copy(update={"peer_stats": ordered})
    return edited, {"twin_edit": f"peer_stats: {pairs} shares moved to the far extreme"}


# ---- tone -----------------------------------------------------------------------------------

_TIME_BOUND_KINDS: Final = (JustificationKind.coverage, JustificationKind.temporary)


def _tone_kinds(request: AccessRequest) -> list[JustificationKind]:
    """Kinds whose claims the request's structure supports (``text.justifications``)."""
    if request.requester_id != request.beneficiary_id:
        kinds = [JustificationKind.on_behalf]
    else:
        kinds = list(DEFAULT_KIND_WEIGHTS)
        if request.requested_duration_days is not None:
            kinds += _TIME_BOUND_KINDS
    if request.ticket_ref is not None:
        kinds.append(JustificationKind.ticket_mention)
    return kinds


def _colleagues(view: CaseView, request: AccessRequest) -> list[Identity]:
    """Who a justification may mention as a colleague, as the request builder picks one: active
    people other than the beneficiary and the requester, from the beneficiary's department when
    any are. With nobody, the bank invents a first name, as it does for the builder."""
    beneficiary = view.identities[request.beneficiary_id]
    others = [
        i
        for i in view.identities.values()
        if i.id not in (request.beneficiary_id, request.requester_id)
        and i.status is IdentityStatus.active
    ]
    return [i for i in others if i.department == beneficiary.department] or others


def _similarity(a: str, b: str) -> float:
    """``difflib`` ratio of the case-folded texts, the larger of both argument orders: the ratio
    isn't symmetric (its matching is greedy from the first argument), and a twin must read
    differently whichever text a check compares from."""
    x, y = a.casefold(), b.casefold()
    return max(
        difflib.SequenceMatcher(None, x, y).ratio(), difflib.SequenceMatcher(None, y, x).ratio()
    )


def _retone(base: Case, rng: random.Random) -> tuple[CaseView, dict[str, str]]:
    view = base.view
    request = view.request
    if request is None:
        raise ValueError(f"{base.case_id}: tone twins re-render a request's justification")
    beneficiary = view.identities[request.beneficiary_id]
    entitlement = view.entitlements[request.entitlement_id]
    slots: dict[str, object] = {
        "app": view.applications[entitlement.app_id].name,
        "entitlement_name": entitlement.name,
        "team": beneficiary.department,
        "beneficiary_first": first_name(beneficiary.display_name),
        "ticket_id": request.ticket_ref,
    }
    colleagues = _colleagues(view, request)
    if colleagues:
        colleague = seeded_choice(rng, colleagues, key=lambda i: i.id)
        slots["colleague_first"] = first_name(colleague.display_name)
    base_family = base.provenance.get("justification_family")
    kinds = _tone_kinds(request)
    for _ in range(_MAX_DRAWS):
        kind = seeded_choice(rng, kinds, key=lambda k: k.value)
        text, family = render_justification(rng, split=base.split, slots=slots, kind=kind)
        if family != base_family and _similarity(text, request.justification) < TONE_MAX_SIMILARITY:
            break
    else:
        raise ValueError(f"{base.case_id}: no different justification in {_MAX_DRAWS} draws")
    edited = view.model_copy(update={"request": request.model_copy(update={"justification": text})})
    note = {
        "justification_family": family,
        "twin_edit": f"{request.request_id}.justification: re-rendered as {kind.value}",
    }
    return edited, note


_INVARIANCE_EDITS: Final[dict[str, InvarianceEdit]] = {
    "name": _swap_names,
    "seniority": _shift_title,
    "peer_adoption": _flip_peers,
    "tone": _retone,
}


# ---- ID renaming ----------------------------------------------------------------------------

_KEYED: Final = (*_COLLECTIONS, "history")
_PREFIX: Final = re.compile(r"^(.*?[_-])(.+)$")
_SLUG_DIGITS = 5


def _collect_ids(view: CaseView) -> set[str]:
    """Every record ID a serialized view mentions in a structured field (``_ID_FIELDS``),
    including references to records outside the view and the policy pack's SoD sides."""
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in cast(dict[str, object], node).items():
                if key in _ID_FIELDS and isinstance(value, str):
                    found.add(value)
                elif key in _ID_FIELDS and isinstance(value, list):
                    found.update(v for v in cast(list[object], value) if isinstance(v, str))
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in cast(list[object], node):
                walk(value)

    data = view.model_dump(mode="json")
    walk(data)
    for name in _KEYED:
        found.update(data[name])
    for identity_id, shares in view.peer_stats.items():
        found.add(identity_id)
        found.update(shares)
    return found


def _reserved_ids(base: Case, world: World | None) -> frozenset[str]:
    """IDs a renamed case must not use: its world's records (module docstring), from ``world``
    or rebuilt from the world ID; none when the ID names no buildable world."""
    if world is not None:
        if world.world_id != base.world_id:
            raise ValueError(f"world {world.world_id} isn't the case's world {base.world_id}")
        return record_ids(world)
    try:
        return world_record_ids(base.world_id)
    except WorldIdError:
        return frozenset()


def _fresh_ids(old: set[str], rng: random.Random, reserved: frozenset[str]) -> dict[str, str]:
    """A fresh random ID per old ID, keeping the prefix (``u_``, ``INC-``, ``ent_``...).

    Numeric suffixes keep their width (zero-padded when the old one was); slugs become
    ``_SLUG_DIGITS`` digits. A new ID never equals, nor contains, an old ID, so no old ID
    string can survive the renaming (``mc_017`` would otherwise hide inside ``mc_0172``), and is
    never in ``reserved``. A world fills at most 80% of a prefix's space (``world.ids``), so a
    few draws always find a free ID.
    """
    taken = set(old) | reserved
    mapping: dict[str, str] = {}
    for old_id in sorted(old):
        match = _PREFIX.match(old_id)
        prefix, suffix = (match.group(1), match.group(2)) if match else ("id_", "")
        if suffix.isdigit():
            width, padded = len(suffix), suffix.startswith("0")
        else:
            width, padded = _SLUG_DIGITS, False
        low = 0 if padded or width == 1 else 10 ** (width - 1)
        for _ in range(_MAX_DRAWS):
            candidate = f"{prefix}{rng.randint(low, 10**width - 1):0{width}d}"
            if candidate not in taken and not _contains_any(candidate, old):
                break
        else:
            raise ValueError(f"no fresh ID for {old_id!r} after {_MAX_DRAWS} draws")
        taken.add(candidate)
        mapping[old_id] = candidate
    return mapping


def _contains_any(text: str, ids: set[str]) -> bool:
    """True if some ID in ``ids`` is a substring of ``text`` (checks every substring)."""
    return any(text[i:j] in ids for i in range(len(text)) for j in range(i + 1, len(text) + 1))


def _rename_node(node: object, mapping: Mapping[str, str], text: Callable[[str], str]) -> object:
    """A serialized view with IDs renamed: dict keys that are IDs, ``_ID_FIELDS`` values (lists
    the world keeps sorted re-sorted, ``_SORTED_ID_FIELDS``), and IDs inside free text."""
    if isinstance(node, list):
        return [_rename_node(v, mapping, text) for v in cast(list[object], node)]
    if not isinstance(node, dict):
        return node
    renamed: dict[str, object] = {}
    for key, value in cast(dict[str, object], node).items():
        new_key = mapping.get(key, key)
        if key in _ID_FIELDS and isinstance(value, str):
            renamed[new_key] = mapping.get(value, value)
        elif key in _ID_FIELDS and isinstance(value, list):
            items = [mapping.get(v, v) if isinstance(v, str) else v for v in cast(list[Any], value)]
            renamed[new_key] = sorted(items) if key in _SORTED_ID_FIELDS else items
        elif key in _FREE_TEXT_FIELDS and isinstance(value, str):
            renamed[new_key] = text(value)
        else:
            renamed[new_key] = _rename_node(value, mapping, text)
    return renamed


def _rename_claim(claim: Claim, mapping: Mapping[str, str]) -> Claim:
    value: ClaimValue = claim.value
    if isinstance(value, str):
        value = mapping.get(value, value)
    return Claim(id=mapping.get(claim.id, claim.id), field=claim.field, value=value)


def _rename_certificates(
    certificates: tuple[Certificate, ...], mapping: Mapping[str, str]
) -> tuple[Certificate, ...]:
    return tuple(
        Certificate(
            rule=c.rule,
            claim_sets=tuple(tuple(_rename_claim(x, mapping) for x in s) for s in c.claim_sets),
        )
        for c in certificates
    )


def _rename_gold(
    gold: RequestGold | ReviewGold, mapping: Mapping[str, str]
) -> RequestGold | ReviewGold:
    if isinstance(gold, RequestGold):
        certificates = _rename_certificates(gold.certificates, mapping)
        return gold.model_copy(update={"certificates": certificates})
    items = tuple(
        item.model_copy(
            update={
                "item_id": mapping.get(item.item_id, item.item_id),
                "certificates": _rename_certificates(item.certificates, mapping),
            }
        )
        for item in gold.items
    )
    return gold.model_copy(update={"items": items})


def _gold_shape(gold: RequestGold | ReviewGold) -> object:
    """Gold with alternative claim sets (and the claims inside each) in a canonical order. The
    oracle sorts alternatives by ID, so a renaming may reorder them without changing gold."""

    def certs(certificates: tuple[Certificate, ...]) -> object:
        return [
            (c.rule, sorted(sorted((x.id, x.field, str(x.value)) for x in s) for s in c.claim_sets))
            for c in certificates
        ]

    if isinstance(gold, RequestGold):
        return (gold.model_copy(update={"certificates": ()}), certs(gold.certificates))
    return [
        (item.model_copy(update={"certificates": ()}), certs(item.certificates))
        for item in gold.items
    ]


def _renamed(
    base: Case, base_gold: RequestGold | ReviewGold, rng: random.Random, reserved: frozenset[str]
) -> tuple[CaseView, RequestGold | ReviewGold, dict[str, str]]:
    """The renamed view, the oracle's gold on it and the old -> new ID mapping."""
    old = _collect_ids(base.view)
    mapping = _fresh_ids(old, rng, reserved)
    data = cast(
        dict[str, Any],
        _rename_node(base.view.model_dump(mode="json"), mapping, _substitution(mapping)),
    )
    for name in _KEYED:
        data[name] = dict(sorted(cast(dict[str, Any], data[name]).items()))
    data["peer_stats"] = {
        i: dict(sorted(s.items()))
        for i, s in sorted(cast(dict[str, Any], data["peer_stats"]).items())
    }
    view = CaseView.model_validate(data)
    gold = _decide(view, base.task)
    if _gold_shape(gold) != _gold_shape(_rename_gold(base_gold, mapping)):
        raise GoldChangedError(f"{base.case_id}: renaming IDs changed the oracle's gold")
    return view, gold, mapping
