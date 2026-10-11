"""Access-hygiene rules HYG-001..005 (SPEC §7.4 and its "Dormancy" subsection).

All five judge one review item's own record, whatever its dates. HYG-001, HYG-004 and HYG-005 are
source-specific (SPEC §7.2): when an entitlement is held through several sources, only the
described item is affected.
"""

from __future__ import annotations

from datetime import date, timedelta

from identitybench.dates import days_since
from identitybench.policy.access import (
    OracleInputError,
    expand_role,
    require_identity,
    require_role,
)
from identitybench.policy.constants import DORMANCY_DAYS, MOVE_GRACE_DAYS
from identitybench.policy.registry import register
from identitybench.policy.rules.base import (
    NOT_APPLICABLE,
    ClaimSet,
    Evaluation,
    ItemContext,
    ReviewRule,
    days_param,
    effects_of,
)
from identitybench.schemas.enums import Effect
from identitybench.schemas.gold import Claim
from identitybench.world.entities import (
    AccountKind,
    Assignment,
    AttributeRule,
    IdentityStatus,
    Membership,
    MembershipSource,
    TelemetryStatus,
    UsageRecord,
)


def _usage_pairs(ctx: ItemContext, source: Membership | Assignment) -> list[tuple[str, str]]:
    """The (account, entitlement) pairs whose telemetry decides the item's dormancy.

    An assignment has one: its own account and entitlement. A business-role membership has one
    per entitlement in the role's expansion, through each of the holder's human accounts in
    that entitlement's application. A holder without such an account can't have the telemetry
    the rule needs, so the case is incomplete.
    """
    if isinstance(source, Assignment):
        return [(source.account_id, source.entitlement_id)]
    accounts = ctx.records.accounts_of(source.identity_id)
    pairs: list[tuple[str, str]] = []
    for entitlement_id in sorted(expand_role(ctx.view.roles, source.role_id)):
        app_id = ctx.entitlement(entitlement_id).app_id
        in_app = [a.id for a in accounts if a.app_id == app_id and a.kind == AccountKind.human]
        if not in_app:
            raise OracleInputError(
                f"HYG-001 needs usage for {entitlement_id} ({ctx.item.item_id}), but "
                f"{source.identity_id} has no human account in {app_id}"
            )
        pairs.extend((account_id, entitlement_id) for account_id in in_app)
    return pairs


def _usage_records(ctx: ItemContext, source: Membership | Assignment) -> list[UsageRecord]:
    """Every usage record for the item's (account, entitlement) pairs, sorted by ID.

    Each pair is a provisioned pair, and the review context has already rejected a view in which
    a provisioned pair has no record (``RecordIndex.check_usage_records``, SPEC §9.2), so no pair
    comes back empty: unknown usage is a record with telemetry ``unavailable``.
    """
    pairs = _usage_pairs(ctx, source)
    return sorted((u for pair in pairs for u in ctx.records.usage_for(*pair)), key=lambda u: u.id)


@register
class Dormant(ReviewRule):
    """HYG-001: dormant access is revoked.

    Applies to assignments (source ``request`` or ``direct``) and to business-role memberships
    (source ``request``); birthright memberships are exempt. Never applies while the holder is on
    leave. Telemetry must establish dormancy: the assignment's usage record, or the record of
    every entitlement in the role's expansion, must have telemetry ``available``. The clock
    starts at ``max(granted_on, last_used_on, leave_returned_on)``, ignoring nulls, where a
    membership's last use is the latest across its expansion. Dormant iff
    ``as_of - clock start > 90`` days. A role that confers nothing needs no telemetry ("every
    entitlement" holds vacuously), so its clock is just the grant or leave-return date.

    Like every item rule it judges the item's record whatever its dates (SPEC §7.2: REVOKE removes
    the record), so validity on the as-of date isn't a precondition. An expired item that has gone
    unused is dormant too, and HYG-004 revokes it as well. A future-dated item can never be
    dormant: its clock starts no earlier than ``granted_on``, which is after the as-of date, so
    ``as_of - clock start`` is negative.

    Claims: ``membership.source = request`` for a membership, since birthright memberships are
    exempt (every assignment is request- or direct-sourced, so an assignment's ``source`` decides
    nothing and isn't cited); the field that set the clock start (``granted_on``, a usage
    record's ``last_used_on``, or ``holder.leave_returned_on``; ties are alternatives); and
    ``telemetry = available`` for every usage record consulted. SPEC §8.2's example output also
    cites the assignment's ``source``; an extra true claim doesn't stop a citation from
    satisfying the certificate (SPEC §13.1), and the conformance tables settle this reading
    (AMBIGUITIES A3).

    Input contract (SPEC §9.2): the holder's ``status`` and ``leave_returned_on`` and, for an item
    old enough to be dormant, the usage record of every (account, entitlement) pair the item
    covers: an assignment's own pair; for a membership, each entitlement in the role's expansion
    through each of the holder's human accounts in that entitlement's application. The review
    context rejects any view missing a usage record (unknown usage is a record with telemetry
    ``unavailable``, never a missing one). For such an item the rule itself raises
    ``OracleInputError`` when a membership's expansion reaches an entitlement the view doesn't
    define, or an application in which the holder has no human account: the telemetry it needs
    can't exist.
    """

    id = "HYG-001"
    family = "HYG"
    effects = effects_of(review=Effect.REVOKE)
    summary = (
        "Dormant access: an assignment (`source` `request` or `direct`) or a business-role "
        "membership (`source` `request`) unused for more than {days} days. Birthright memberships "
        "(`source` `birthright`) are exempt. See Dormancy below."
    )
    params = days_param(DORMANCY_DAYS)

    def evaluate_review(self, ctx: ItemContext) -> Evaluation:
        source = ctx.source
        if isinstance(source, Membership) and source.source != MembershipSource.request:
            return NOT_APPLICABLE
        holder = ctx.holder
        if holder is not None and holder.status == IdentityStatus.leave_of_absence:
            return NOT_APPLICABLE
        returned = None if holder is None else holder.leave_returned_on
        floor = source.granted_on if returned is None else max(source.granted_on, returned)
        # Usage can only move the clock later, so an item this young is never dormant and its
        # telemetry isn't needed.
        if days_since(floor, ctx.as_of) <= DORMANCY_DAYS:
            return NOT_APPLICABLE
        records = _usage_records(ctx, source)
        if any(r.telemetry != TelemetryStatus.available for r in records):
            return NOT_APPLICABLE
        used: list[date] = [r.last_used_on for r in records if r.last_used_on is not None]
        clock = max([floor, *used])
        if days_since(clock, ctx.as_of) <= DORMANCY_DAYS:
            return NOT_APPLICABLE
        starts: list[Claim] = []
        if source.granted_on == clock:
            starts.append(ctx.claim(source.id, "granted_on"))
        starts.extend(ctx.claim(r.id, "last_used_on") for r in records if r.last_used_on == clock)
        if holder is not None and holder.leave_returned_on == clock:
            starts.append(ctx.claim(holder.id, "leave_returned_on"))
        in_scope = [ctx.claim(source.id, "source")] if isinstance(source, Membership) else []
        telemetry = [ctx.claim(r.id, "telemetry") for r in records]
        return self.fire(ctx, [(*in_scope, start, *telemetry) for start in starts])


@register
class OrphanedAccount(ReviewRule):
    """HYG-002: an assignment on an orphaned human account (no owning identity) is revoked.

    Claims: ``account.kind = human`` (the missing ``identity_id`` is an absence, so it can't be
    cited).

    Input contract: the assignment's account, whose presence the review context checks.
    """

    id = "HYG-002"
    family = "HYG"
    effects = effects_of(review=Effect.REVOKE)
    summary = "An assignment on an orphaned human account (`kind` `human` with no `identity_id`)."

    def evaluate_review(self, ctx: ItemContext) -> Evaluation:
        account = ctx.account
        if account is None or account.kind != AccountKind.human:
            return NOT_APPLICABLE
        if account.identity_id is not None:
            return NOT_APPLICABLE
        return self.fire(ctx, [[ctx.claim(account.id, "kind")]])


@register
class UnownedServiceAccount(ReviewRule):
    """HYG-003: an item on a service account whose owner is terminated or missing needs a human.

    Claims: ``account.kind = service`` alone when there is no ``owner_id``; otherwise also
    ``account.owner_id`` and ``owner.status = terminated``.

    Input contract: the account's ``kind`` and ``owner_id`` and, when there is an owner, the
    owner's identity record. An owner ID that names no identity in the view raises
    ``OracleInputError``: it is an incomplete case, not a missing owner.
    """

    id = "HYG-003"
    family = "HYG"
    effects = effects_of(review=Effect.ESCALATE)
    summary = "An item on a service account whose owner (`owner_id`) is terminated or missing."

    def evaluate_review(self, ctx: ItemContext) -> Evaluation:
        account = ctx.account
        if account is None or account.kind != AccountKind.service:
            return NOT_APPLICABLE
        kind = ctx.claim(account.id, "kind")
        if account.owner_id is None:
            return self.fire(ctx, [[kind]])
        owner = require_identity(ctx.view, account.owner_id, f"{account.id}.owner_id")
        if owner.status != IdentityStatus.terminated:
            return NOT_APPLICABLE
        claims = [kind, ctx.claim(account.id, "owner_id"), ctx.claim(owner.id, "status")]
        return self.fire(ctx, [claims])


@register
class PastExpiry(ReviewRule):
    """HYG-004: a membership or assignment past its expiry date is revoked.

    Applies iff ``expires_on < as_of``; an item expiring today is still valid. Claims:
    ``item.expires_on``.

    Input contract: the item's own ``expires_on`` (null means no expiry), always present.
    """

    id = "HYG-004"
    family = "HYG"
    effects = effects_of(review=Effect.REVOKE)
    summary = "A membership or assignment whose `expires_on` is before the as-of date."

    def evaluate_review(self, ctx: ItemContext) -> Evaluation:
        expires_on = ctx.source.expires_on
        if expires_on is None or expires_on >= ctx.as_of:
            return NOT_APPLICABLE
        return self.fire(ctx, [[ctx.claim(ctx.source.id, "expires_on")]])


@register
class StaleBirthright(ReviewRule):
    """HYG-005: a mover's old birthright membership is revoked once the grace period is over.

    Applies to a membership with source ``birthright`` whose role's attribute rule no longer
    matches the holder, on or after ``move_date + 14`` days. It needs a move date: without one
    the rule doesn't fire (SPEC 0.4.1).

    Claims, per holder attribute the rule no longer matches (several mismatches are
    alternatives): ``membership.source = birthright``, ``holder.move_date``,
    ``membership.role_id`` (whose rule it is), every value the role's ``attribute_rule`` allows
    on that dimension (``attribute_rule.departments``, ``.employment_types`` or ``.regions``)
    and the holder's ``department``, ``employment_type`` or ``location.region``: both sides of
    the comparison.

    Input contract: the holder's ``move_date`` and attributes, and the membership's role with its
    ``attribute_rule``. A birthright role without an attribute rule raises ``OracleInputError``.
    """

    id = "HYG-005"
    family = "HYG"
    effects = effects_of(review=Effect.REVOKE)
    summary = (
        "A birthright membership (`source` `birthright`) whose role's `attribute_rule` no longer "
        "matches the identity, on or after `move_date` + {days} days. Without a move date the "
        "rule doesn't fire."
    )
    params = days_param(MOVE_GRACE_DAYS)

    def evaluate_review(self, ctx: ItemContext) -> Evaluation:
        source = ctx.source
        if not isinstance(source, Membership) or source.source != MembershipSource.birthright:
            return NOT_APPLICABLE
        holder = ctx.holder
        if holder is None or holder.move_date is None:
            return NOT_APPLICABLE
        if ctx.as_of < holder.move_date + timedelta(days=MOVE_GRACE_DAYS):
            return NOT_APPLICABLE
        role = require_role(ctx.view.roles, source.role_id)
        rule = role.attribute_rule
        if rule is None:
            raise OracleInputError(f"HYG-005 needs the attribute rule of birthright role {role.id}")
        # One single-dimension rule per attribute reuses AttributeRule's matching semantics and
        # names the attribute that no longer matches. A dimension can only mismatch when it
        # lists values (None matches any value), so ``allowed`` is never None below.
        dimensions: tuple[tuple[str, str, AttributeRule, tuple[str, ...] | None], ...] = (
            (
                "department",
                "departments",
                AttributeRule(departments=rule.departments),
                rule.departments,
            ),
            (
                "employment_type",
                "employment_types",
                AttributeRule(employment_types=rule.employment_types),
                rule.employment_types,
            ),
            ("location.region", "regions", AttributeRule(regions=rule.regions), rule.regions),
        )
        lead = (
            ctx.claim(source.id, "source"),
            ctx.claim(holder.id, "move_date"),
            ctx.claim(source.id, "role_id"),
        )
        claim_sets: list[ClaimSet] = []
        for holder_field, rule_field, part, allowed in dimensions:
            if part.matches(holder):
                continue
            required = [
                ctx.claim(role.id, f"attribute_rule.{rule_field}", value)
                for value in sorted({str(v) for v in allowed or ()})
            ]
            claim_sets.append((*lead, *required, ctx.claim(holder.id, holder_field)))
        return self.fire(ctx, claim_sets) if claim_sets else NOT_APPLICABLE
