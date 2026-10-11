"""Separation-of-duties rules SOD-001..006 (SPEC §7.1, §7.4 and its "SoD scope" subsection).

Each rule's two sides are explicit entitlement ID lists from the world's ``PolicyPack``; the
rendered text lists them, and the predicate reads the same lists.

Scope:

- **Requests** check conflicts the request would *introduce*: the requested entitlement is on
  one side and the beneficiary's held access (valid sources only) already includes the other.
  A conflict that exists already and doesn't involve the requested entitlement is left to
  reviews.
- **Reviews** escalate only items that confer a side of an unmitigated conflict; when one side is
  conferred by several items, all of them escalate.

MIT-001 is an exception inside the predicate: a control for the (identity, rule) pair that is
valid on the as-of date stops the rule from applying. Deciding a conflict means looking for that
control, so whenever the rule finds a conflict it records
``ExceptionEval(MIT-001, for=SOD-x, applies=...)``, with ``applies`` false when the identity has
no control at all. An unmitigated finding cites the control date that defeated the exception
when a control exists; a missing control is an absence and adds no claim.

Claim recipe (request): ``request.entitlement_id``; the conflicting held grant's conferral
(``membership.role_id`` → each ``child_roles`` hop → ``role.entitlements``, or
``assignment.entitlement_id``); then the control's ``expires_on`` (``granted_on`` if it hasn't
started) when a control exists. Review: this item's conferral of one side, the conferral of one
other-side grant, then the control date. Each role path is a separate alternative.
"""

from __future__ import annotations

from identitybench.policy.access import pack_sides
from identitybench.policy.registry import register
from identitybench.policy.rules.base import (
    NOT_APPLICABLE,
    Context,
    Evaluation,
    ItemContext,
    RequestContext,
    RequestRule,
    ReviewRule,
    SodAnalysis,
    effects_of,
)
from identitybench.schemas.enums import Effect
from identitybench.world.entities import PolicyPack

_NO_SIDE = "none in this organization"


def _side_text(entitlement_ids: tuple[str, ...]) -> str:
    return ", ".join(f"`{e}`" for e in sorted(entitlement_ids)) or _NO_SIDE


class SodRule(RequestRule, ReviewRule):
    """A SoD rule whose sides come from the policy pack (see the module docstring)."""

    family = "SOD"
    effects = effects_of(request=Effect.DENY, review=Effect.ESCALATE)

    def text(self, pack: PolicyPack) -> str:
        sides = pack_sides(pack).get(self.id)
        side_a = _side_text(sides.side_a) if sides else _NO_SIDE
        side_b = _side_text(sides.side_b) if sides else _NO_SIDE
        return f"{self.summary} Side A: {side_a}. Side B: {side_b}. Exception: MIT-001."

    def evaluate_request(self, ctx: RequestContext) -> Evaluation:
        return self._judge(ctx, ctx.sod.get(self.id))

    def evaluate_review(self, ctx: ItemContext) -> Evaluation:
        return self._judge(ctx, ctx.sod.get(self.id))

    def _judge(self, ctx: Context, analysis: SodAnalysis | None) -> Evaluation:
        if analysis is None:
            return NOT_APPLICABLE
        if analysis.mitigated:
            return Evaluation(exceptions=analysis.exceptions)
        return self.fire(ctx, analysis.claim_sets, exceptions=analysis.exceptions)


@register
class VendorMasterVsPayment(SodRule):
    """SOD-001: create or modify vendors ⟂ approve vendor payments."""

    id = "SOD-001"
    summary = (
        "A person must not be able to both create or modify vendors and approve vendor payments."
    )


@register
class JournalPostVsApprove(SodRule):
    """SOD-002: post journal entries ⟂ approve journal entries."""

    id = "SOD-002"
    summary = "A person must not be able to both post and approve general-ledger journal entries."


@register
class ChangeApproveVsDeploy(SodRule):
    """SOD-003: approve change requests ⟂ deploy to production (SOX-scoped apps)."""

    id = "SOD-003"
    summary = (
        "A person must not be able to both approve change requests and deploy to production on "
        "SOX-scoped applications."
    )


@register
class IdpAdminVsAccessApproval(SodRule):
    """SOD-004: administer identity-provider accounts ⟂ approve access requests."""

    id = "SOD-004"
    summary = (
        "A person must not be able to both administer identity-provider accounts and approve "
        "access requests."
    )


@register
class PayrollEditVsApprove(SodRule):
    """SOD-005: edit payroll ⟂ approve payroll runs."""

    id = "SOD-005"
    summary = "A person must not be able to both edit payroll and approve payroll runs."


@register
class PurchaseOrderVsReceipt(SodRule):
    """SOD-006: create purchase orders ⟂ record goods receipt."""

    id = "SOD-006"
    summary = "A person must not be able to both create purchase orders and record goods receipt."
