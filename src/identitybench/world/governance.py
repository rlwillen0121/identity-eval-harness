"""Mitigating controls and tickets (SPEC §6.2, §6.4, §7.4).

Controls cover a subset of the world's live SoD conflicts, with the date spread that makes
MIT-001/MIT-002 interesting: some expired, a fixed share expiring within the 30-day renewal
window (so every world has MIT-002 material), the rest comfortably valid. Stale controls sit
alongside them, mostly expired: for conflicts whose completing grant has lapsed but is still on
file, and for remediated conflicts where only one side is still held. Without them, having a
control at all would give away a live conflict (SPEC §6.4 distractors, §9.4). There is at most
one control per (identity, rule).

Every control fits the people it names: it is granted no earlier than the holder's and the
approver's hire dates and the start of the conflict it covers, no later than the holder's last
day (termination or the start of a leave), and the approver is an active leader. A control
that predated its holder would be impossible data, and a careful model could rightly cite it
as suspect even though SPEC §6.3 says it is valid.

Tickets are ordinary IT work (incidents, changes, projects) with structured assignees and
related applications; they're what break-glass and JUST-001 checks look up. A ticket's
assignee was already on staff when it was opened.
"""

from __future__ import annotations

import random
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from identitybench.policy.constants import MIT_RENEWAL_WINDOW_DAYS
from identitybench.world.catalog import AppDef
from identitybench.world.entities import (
    EmploymentType,
    Identity,
    IdentityStatus,
    MitigatingControl,
    Ticket,
    TicketStatus,
    TicketType,
)
from identitybench.world.ids import IdFactory
from identitybench.world.people import CEO, DIRECTOR, HEAD, LEADER_LEVELS, Workforce
from identitybench.world.rolegraph import SodIndex
from identitybench.world.specs import EVERYONE

CONTROL_SHARE = 0.55
"""Share of live SoD conflicts that have a mitigating control on file (an exact count)."""
LAPSED_CONTROL_SHARE = 0.5
"""Share of lapsed conflicts (completing grant expired but still on file) with a control."""
REMEDIATED_CONTROL_SHARE = 0.03
"""Share of one-sided SoD holders with a control left over from a remediated conflict."""

_EXPIRED, _RENEWAL, _VALID = "expired", "renewal", "valid"
LIVE_EXPIRY_MIX: tuple[float, float] = (0.25, 0.30)
"""Shares of live-conflict controls that are expired / expire within the renewal window (the
rest are comfortably valid). Allocated exactly, not sampled, so MIT-002 material never thins
out by chance."""
STALE_EXPIRY_MIX: tuple[float, float] = (0.80, 0.05)
"""Same shares for stale controls: mostly expired, a few still running past the conflict."""
CONTROL_TERMS: tuple[int, ...] = (90, 180, 365)
"""Nominal control terms in days; a late start (a recent hire or a fresh conflict) shortens
the term, never below ``MIN_CONTROL_DAYS``."""
MIN_CONTROL_DAYS = 30
"""Shortest term a generated control runs. An expired control therefore needs at least this
much history between its earliest possible grant date and the anchor."""

APPROVER_LEVELS: frozenset[str] = frozenset({CEO, HEAD, DIRECTOR})
RULE_APPROVER_DEPARTMENTS: dict[str, tuple[str, ...]] = {
    "SOD-001": ("Finance", "Accounting", "Procurement"),
    "SOD-002": ("Finance", "Accounting"),
    "SOD-003": ("Security", "IT", "Engineering"),
    "SOD-004": ("Security", "IT"),
    "SOD-005": ("HR", "Finance"),
    "SOD-006": ("Procurement", "Finance", "Operations"),
}
"""Departments whose leaders (with the executive office) sign off each rule's exceptions,
matching who performs the control in its description."""
APPROVER_DEPARTMENTS: tuple[str, ...] = ("Finance", "Accounting", "Security", "Legal", "Executive")
"""Fallback approver departments when no rule-specific leader is available."""
ASSIGNEE_DEPARTMENTS: tuple[str, ...] = (
    "IT",
    "Engineering",
    "Security",
    "Data",
    "Operations",
    "Customer Support",
)

_CONTROL_TEXT: dict[str, str] = {
    "SOD-001": "Monthly independent review of vendor master changes against released payments, "
    "performed by Accounts Payable leadership.",
    "SOD-002": "Controller reviews every manual journal entry this user posts before period "
    "close; the ledger workflow blocks self-approval of the user's own entries.",
    "SOD-003": "Post-deployment review of each production release against its approved change "
    "ticket, signed off weekly by Security.",
    "SOD-004": "Weekly reconciliation of identity-provider admin actions against approved access "
    "requests, reviewed by the GRC team.",
    "SOD-005": "Independent HR leader reviews the payroll register variance report each cycle "
    "before funds are released.",
    "SOD-006": "Procurement leadership reviews purchase orders and goods receipts entered by this "
    "user every month, including three-way-match exceptions.",
}
_CONTROL_CONTEXT: tuple[str, ...] = (
    "Compensating control: ",
    "Temporary exception during team coverage gap. ",
    "Approved SoD exception (small team). ",
    "",
)

_TICKET_TEMPLATES: dict[TicketType, tuple[str, ...]] = {
    TicketType.incident: (
        "{app}: users unable to sign in after SSO certificate rotation",
        "{app} nightly batch job failed; downstream reports stale",
        "Elevated error rates on the {app} API",
        "{app} reports timing out for the {dept} team",
        "Suspected phishing: credential reset needed for a {app} user",
        "{app} integration sync stuck since the weekend",
        "Data discrepancy between {app} and the warehouse",
        "{app} unavailable for EU users",
    ),
    TicketType.change: (
        "Upgrade {app} to the latest minor release",
        "Rotate service credentials used by {app} integrations",
        "Apply the quarterly patch bundle to {app}",
        "Update the approval workflow in {app}",
        "Decommission a legacy integration in {app}",
        "Enable audit logging for admin actions in {app}",
    ),
    TicketType.project: (
        "Migrate {app} reporting to the new BI layer",
        "Q4 access review remediation for {app}",
        "Roll out {app} to the {region} region",
        "Consolidate {app} roles ahead of the SOX audit",
        "Implement SSO and automated provisioning for {app}",
        "Archive historical data in {app}",
    ),
}
_TICKET_MIX: tuple[tuple[TicketType, float], ...] = (
    (TicketType.incident, 0.45),
    (TicketType.change, 0.35),
    (TicketType.project, 0.20),
)


@dataclass(frozen=True, slots=True)
class _Candidate:
    identity_id: str
    rule_id: str
    floor: date
    """Earliest plausible grant date: when the conflict (or the side still held) began."""


def build_controls(
    workforce: Workforce,
    granted_now: Mapping[str, Mapping[str, date]],
    granted_on_file: Mapping[str, Mapping[str, date]],
    sod: SodIndex,
    rng: random.Random,
    ids: IdFactory,
    anchor: date,
) -> dict[str, MitigatingControl]:
    """Controls for live, lapsed and remediated conflicts; at most one per (identity, rule).

    ``granted_now`` maps each identity's held access on the anchor date to the earliest grant
    date among the sources valid then; ``granted_on_file`` does the same over every source on
    file, expired ones included (``query.first_granted``). Each (identity, rule) pair falls in
    exactly one class, which is what keeps controls unique per pair.
    """
    identities = workforce.identities
    live, lapsed, remediated = _candidates(identities, granted_now, granted_on_file, sod)
    pools = _approver_pools(workforce)

    plan: list[tuple[_Candidate, Identity, date, str]] = []
    for pairs, share, minimum, mix in (
        (live, CONTROL_SHARE, 0, LIVE_EXPIRY_MIX),
        # At least one stale control of each kind (when there is a candidate), so even a
        # small org's controls don't all point at live conflicts.
        (lapsed, LAPSED_CONTROL_SHARE, 1, STALE_EXPIRY_MIX),
        (remediated, REMEDIATED_CONTROL_SHARE, 1, STALE_EXPIRY_MIX),
    ):
        count = min(len(pairs), max(minimum, round(share * len(pairs))))
        chosen = [pairs[k] for k in sorted(rng.sample(range(len(pairs)), count))]
        # The approver comes first: a later-hired approver moves the earliest grant date,
        # which decides whether an expired control still fits before the anchor.
        staffed: list[tuple[_Candidate, Identity, date]] = []
        for candidate in chosen:
            holder = identities[candidate.identity_id]
            floor = max(candidate.floor, holder.hire_date)
            latest = min(workforce.last_day(holder, anchor), anchor)
            approver = _approver(pools[candidate.rule_id], holder.id, floor, latest, rng)
            if approver is not None:
                staffed.append((candidate, approver, max(floor, approver.hire_date)))
        room = [(anchor - floor).days >= MIN_CONTROL_DAYS for _, _, floor in staffed]
        buckets = _expiry_buckets(room, mix, rng)
        plan.extend((c, a, f, b) for (c, a, f), b in zip(staffed, buckets, strict=True))

    controls: dict[str, MitigatingControl] = {}
    for candidate, approver, floor, bucket in plan:
        holder = identities[candidate.identity_id]
        last_day = workforce.last_day(holder, anchor)
        granted, expires = _control_dates(bucket, floor, last_day, anchor, rng)
        control = MitigatingControl(
            id=ids.control(),
            rule_id=candidate.rule_id,
            identity_id=holder.id,
            description=rng.choice(_CONTROL_CONTEXT) + _CONTROL_TEXT[candidate.rule_id],
            approver_id=approver.id,
            granted_on=granted,
            expires_on=expires,
        )
        controls[control.id] = control
    return controls


def _candidates(
    identities: Mapping[str, Identity],
    granted_now: Mapping[str, Mapping[str, date]],
    granted_on_file: Mapping[str, Mapping[str, date]],
    sod: SodIndex,
) -> tuple[list[_Candidate], list[_Candidate], list[_Candidate]]:
    """(live, lapsed, remediated) control candidates, each with the date its conflict began.

    A conflict begins when its later side was first granted; a remediated conflict's other
    side is gone from the records, so the side still held dates it.
    """
    live: list[_Candidate] = []
    lapsed: list[_Candidate] = []
    remediated: list[_Candidate] = []
    empty: Mapping[str, date] = {}
    for identity_id in identities:
        now = granted_now.get(identity_id, empty)
        on_file = granted_on_file.get(identity_id, empty)
        for rule_id in sod.rules:
            side_a, side_b = sod.sides[rule_id]
            a_now = [now[e] for e in side_a if e in now]
            b_now = [now[e] for e in side_b if e in now]
            a_file = [on_file[e] for e in side_a if e in on_file]
            b_file = [on_file[e] for e in side_b if e in on_file]
            if a_now and b_now:
                live.append(_Candidate(identity_id, rule_id, max(min(a_now), min(b_now))))
            elif a_file and b_file:
                lapsed.append(_Candidate(identity_id, rule_id, max(min(a_file), min(b_file))))
            elif a_now or b_now:
                remediated.append(_Candidate(identity_id, rule_id, min(a_now + b_now)))
    return live, lapsed, remediated


def _approver_pools(workforce: Workforce) -> dict[str, tuple[list[Identity], ...]]:
    """Per rule, approver pools in order of preference; all of them active employees.

    Rule-specific leaders and the executive office first, then leaders of the general
    approver departments, then any leader at all (managers included).
    """
    staff = [
        i
        for i in workforce.identities.values()
        if i.status is IdentityStatus.active and i.employment_type is EmploymentType.employee
    ]
    senior = [i for i in staff if workforce.levels[i.id] in APPROVER_LEVELS]
    leaders = [i for i in staff if workforce.levels[i.id] in LEADER_LEVELS]
    general = [i for i in senior if i.department in APPROVER_DEPARTMENTS]
    return {
        rule_id: (
            [i for i in senior if i.department in (*departments, "Executive")],
            general,
            leaders,
        )
        for rule_id, departments in RULE_APPROVER_DEPARTMENTS.items()
    }


def _approver(
    pools: Sequence[list[Identity]],
    holder_id: str,
    floor: date,
    latest: date,
    rng: random.Random,
) -> Identity | None:
    """An approver from the first pool with someone hired by ``latest`` (the last day the
    control could be granted), preferring people already on staff on ``floor`` so the
    approver doesn't push the grant date later. Never the holder."""
    for pool in pools:
        eligible = [a for a in pool if a.id != holder_id and a.hire_date <= latest]
        if eligible:
            settled = [a for a in eligible if a.hire_date <= floor]
            return rng.choice(settled or eligible)
    return None


def _expiry_buckets(room: list[bool], mix: tuple[float, float], rng: random.Random) -> list[str]:
    """Exactly ``round(share * count)`` expired and renewal-window slots, the rest valid, in a
    shuffled order.

    An expired control needs ``MIN_CONTROL_DAYS`` between its earliest grant date and the
    anchor (``room``). Expired slots that land on a pair without that room swap with a valid
    or renewal slot of a pair that has it; only when no such pair is left does the slot become
    valid instead, so the expired count can fall short but the renewal count never changes.
    """
    count = len(room)
    expired = round(mix[0] * count)
    renewal = min(count - expired, round(mix[1] * count))
    buckets = [_EXPIRED] * expired + [_RENEWAL] * renewal
    buckets += [_VALID] * (count - len(buckets))
    rng.shuffle(buckets)
    spare = [j for j in range(count) if room[j] and buckets[j] != _EXPIRED]
    for i in range(count):
        if buckets[i] == _EXPIRED and not room[i]:
            if spare:
                j = spare.pop()
                buckets[i], buckets[j] = buckets[j], _EXPIRED
            else:
                buckets[i] = _VALID
    return buckets


def _control_dates(
    bucket: str, floor: date, last_day: date, anchor: date, rng: random.Random
) -> tuple[date, date]:
    """(granted_on, expires_on) for one control.

    ``floor`` is the earliest plausible grant date (hire dates, conflict start) and
    ``last_day`` the holder's last day around; ``floor <= min(last_day, anchor)`` always
    holds because every grant falls inside the holder's employment. Terms run at least
    ``MIN_CONTROL_DAYS``.
    """
    term = rng.choice(CONTROL_TERMS)
    latest = min(last_day, anchor)
    if bucket == _EXPIRED:
        expires = max(
            anchor - timedelta(days=rng.randint(1, 120)),
            floor + timedelta(days=MIN_CONTROL_DAYS - 1),
        )
        latest = min(latest, expires - timedelta(days=MIN_CONTROL_DAYS - 1))
        target = expires - timedelta(days=term - 1)
    else:
        if bucket == _RENEWAL:
            expires = anchor + timedelta(days=rng.randint(0, MIT_RENEWAL_WINDOW_DAYS))
        else:
            expires = anchor + timedelta(days=rng.randint(MIT_RENEWAL_WINDOW_DAYS + 1, 300))
        target = min(
            expires - timedelta(days=term - 1), anchor - timedelta(days=rng.randint(1, 60))
        )
    granted = min(max(target, floor), latest)
    if bucket == _RENEWAL:
        # A late grant date shortens the term; stretch the expiry within the renewal window.
        window_end = anchor + timedelta(days=MIT_RENEWAL_WINDOW_DAYS)
        expires = max(expires, min(window_end, granted + timedelta(days=MIN_CONTROL_DAYS - 1)))
    return granted, expires


def build_tickets(
    workforce: Workforce,
    apps: Mapping[str, AppDef],
    count: int,
    rng: random.Random,
    ids: IdFactory,
    anchor: date,
) -> dict[str, Ticket]:
    """IT tickets with structured assignees and related applications.

    The opening date is drawn first and the assignee among active staff already hired by
    then; a ticket opened before anyone suitable was hired stays unassigned.
    """
    active = [i for i in workforce.identities.values() if i.status is IdentityStatus.active]
    staff = sorted(
        [i for i in active if i.department in ASSIGNEE_DEPARTMENTS] or active,
        key=lambda i: (i.hire_date, i.id),
    )
    hired = [i.hire_date for i in staff]
    app_ids = sorted(apps)
    types = [t for t, _ in _TICKET_MIX]
    weights = [w for _, w in _TICKET_MIX]
    tickets: dict[str, Ticket] = {}
    for _ in range(count):
        ticket_type = rng.choices(types, weights=weights, k=1)[0]
        if ticket_type is TicketType.incident:
            age = rng.randint(0, 120)
            is_open = rng.random() < (0.6 if age <= 14 else 0.08)
            related = 1
        elif ticket_type is TicketType.change:
            age = rng.randint(0, 180)
            is_open = rng.random() < (0.35 if age <= 30 else 0.05)
            related = rng.choice((1, 1, 2))
        else:
            age = rng.randint(0, 400)
            is_open = rng.random() < 0.6
            related = rng.choice((1, 2, 3))
        opened = anchor - timedelta(days=age)
        apps_for = sorted(rng.sample(app_ids, related))
        on_staff = bisect_right(hired, opened)
        assignee = None if rng.random() < 0.05 or not on_staff else staff[rng.randrange(on_staff)]
        primary = apps[apps_for[0]]
        template = rng.choice(_TICKET_TEMPLATES[ticket_type])
        teams = [d for d in primary.user_depts if d != EVERYONE] or ["IT"]
        summary = template.format(
            app=primary.name,
            dept=assignee.department if assignee else rng.choice(teams),
            region=primary.region_restriction or rng.choice(("EU", "APAC", "LATAM")),
        )
        ticket = Ticket(
            id=ids.ticket(ticket_type),
            type=ticket_type,
            status=TicketStatus.open if is_open else TicketStatus.closed,
            assignee_id=assignee.id if assignee else None,
            related_app_ids=tuple(apps_for),
            opened_on=opened,
            summary=summary,
        )
        tickets[ticket.id] = ticket
    return tickets
