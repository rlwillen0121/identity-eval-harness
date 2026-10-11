"""Eligibility rules ELIG-001..005 (SPEC §7.4).

ELIG-001, -002, -003 and -005 judge an (identity, entitlement, application) triple. A request
judges the beneficiary against the requested entitlement. A review item is identity-wide
(SPEC §7.2): it is judged against every entitlement its record grants, so each item conferring
an ineligible entitlement is revoked, and its claim sets start with the conferral (role path or
``assignment.entitlement_id``) of that entitlement.
"""

from __future__ import annotations

from abc import abstractmethod

from identitybench.policy.registry import register
from identitybench.policy.rules.base import (
    NOT_APPLICABLE,
    ClaimSet,
    Context,
    Evaluation,
    ItemContext,
    RequestContext,
    RequestRule,
    ReviewRule,
    effects_of,
)
from identitybench.schemas.enums import Effect
from identitybench.world.entities import (
    NON_EMPLOYEES,
    Application,
    DataClassification,
    EmploymentType,
    Entitlement,
    Environment,
    Identity,
    Privilege,
)

_RESTRICTED_BARRED = frozenset({EmploymentType.intern, EmploymentType.vendor})


class EligibilityRule(RequestRule, ReviewRule):
    """An eligibility predicate over (identity, entitlement, application)."""

    effects = effects_of(request=Effect.DENY, review=Effect.REVOKE)

    @abstractmethod
    def violation(
        self, ctx: Context, identity: Identity, entitlement: Entitlement, app: Application
    ) -> ClaimSet | None:
        """The rule's claims if the identity may not hold the entitlement, else None."""

    def evaluate_request(self, ctx: RequestContext) -> Evaluation:
        found = self.violation(ctx, ctx.beneficiary, ctx.requested, ctx.app)
        return NOT_APPLICABLE if found is None else self.fire(ctx, [found])

    def evaluate_review(self, ctx: ItemContext) -> Evaluation:
        holder = ctx.holder
        if holder is None:
            return NOT_APPLICABLE
        claim_sets: list[ClaimSet] = []
        for grant in ctx.record_grants:
            entitlement = ctx.entitlement(grant.entitlement_id)
            found = self.violation(ctx, holder, entitlement, ctx.application(entitlement.app_id))
            if found is not None:
                claim_sets.append((*ctx.conferral(grant), *found))
        return self.fire(ctx, claim_sets) if claim_sets else NOT_APPLICABLE


@register
class NonEmployeeProdAdmin(EligibilityRule):
    """ELIG-001: non-employees may not hold admin privilege on a production application.

    Claims: ``identity.employment_type`` (contractor, intern or vendor),
    ``entitlement.privilege = admin``, ``application.environment = prod``.
    """

    id = "ELIG-001"
    family = "ELIG"
    summary = (
        "Non-employees (`contractor`, `intern`, `vendor`) may not hold an entitlement whose "
        "`privilege` is `admin` on an application whose `environment` is `prod`."
    )

    def violation(
        self, ctx: Context, identity: Identity, entitlement: Entitlement, app: Application
    ) -> ClaimSet | None:
        if (
            identity.employment_type not in NON_EMPLOYEES
            or entitlement.privilege != Privilege.admin
            or app.environment != Environment.prod
        ):
            return None
        return (
            ctx.claim(identity.id, "employment_type"),
            ctx.claim(entitlement.id, "privilege"),
            ctx.claim(app.id, "environment"),
        )


@register
class RestrictedData(EligibilityRule):
    """ELIG-002: interns and vendors may not hold restricted entitlements.

    Claims: ``identity.employment_type`` (intern or vendor),
    ``entitlement.data_classification = restricted``.
    """

    id = "ELIG-002"
    family = "ELIG"
    summary = (
        "Interns and vendors may not hold entitlements whose `data_classification` is `restricted`."
    )

    def violation(
        self, ctx: Context, identity: Identity, entitlement: Entitlement, app: Application
    ) -> ClaimSet | None:
        del app
        if (
            identity.employment_type not in _RESTRICTED_BARRED
            or entitlement.data_classification != DataClassification.restricted
        ):
            return None
        return (
            ctx.claim(identity.id, "employment_type"),
            ctx.claim(entitlement.id, "data_classification"),
        )


@register
class RegionRestricted(EligibilityRule):
    """ELIG-003: a region-restricted application is only for identities located in its region.

    Claims: ``application.region_restriction``, ``identity.location.region``.
    """

    id = "ELIG-003"
    family = "ELIG"
    summary = (
        "An application with a `region_restriction` is only for identities whose "
        "`location.region` equals it."
    )

    def violation(
        self, ctx: Context, identity: Identity, entitlement: Entitlement, app: Application
    ) -> ClaimSet | None:
        del entitlement
        if app.region_restriction is None or identity.location.region == app.region_restriction:
            return None
        return (
            ctx.claim(app.id, "region_restriction"),
            ctx.claim(identity.id, "location.region"),
        )


@register
class NotRequestable(RequestRule):
    """ELIG-004: an entitlement that isn't requestable can't be requested.

    Claims: ``entitlement.requestable = false``.
    """

    id = "ELIG-004"
    family = "ELIG"
    effects = effects_of(request=Effect.DENY)
    summary = "The entitlement is not requestable (`requestable` is false)."

    def evaluate_request(self, ctx: RequestContext) -> Evaluation:
        if ctx.requested.requestable:
            return NOT_APPLICABLE
        return self.fire(ctx, [[ctx.claim(ctx.requested.id, "requestable")]])


@register
class IneligibleEmploymentType(EligibilityRule):
    """ELIG-005: the identity's employment type must be one of the entitlement's eligible types.

    Claims: ``identity.employment_type``. The eligible list itself can't certify an absence, so
    the identity's type is the citable fact.
    """

    id = "ELIG-005"
    family = "ELIG"
    summary = (
        "The identity's `employment_type` isn't one of the entitlement's "
        "`eligible_employment_types`."
    )

    def violation(
        self, ctx: Context, identity: Identity, entitlement: Entitlement, app: Application
    ) -> ClaimSet | None:
        del app
        if identity.employment_type in entitlement.eligible_employment_types:
            return None
        return (ctx.claim(identity.id, "employment_type"),)
