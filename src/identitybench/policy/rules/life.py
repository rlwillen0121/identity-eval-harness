"""Lifecycle rules LIFE-001..004 (SPEC §7.4).

LIFE-001 and LIFE-003 are identity-wide in reviews (SPEC §7.2): they attach to every item of the
affected holder, whatever the item's dates, because the holder's status, not the item, is what's
wrong. An item without a holder (an orphaned or service account) isn't judged.

Input contract: the beneficiary's or holder's identity record, which the contexts check, and,
for a contractor, a ``contract_end_date`` (``contract_end``).
"""

from __future__ import annotations

from datetime import date

from identitybench.dates import cap_ending_on
from identitybench.policy.access import OracleInputError
from identitybench.policy.registry import register
from identitybench.policy.rules.base import (
    NOT_APPLICABLE,
    Context,
    Evaluation,
    ItemContext,
    RequestContext,
    RequestRule,
    ReviewRule,
    effects_of,
)
from identitybench.schemas.enums import Effect
from identitybench.world.entities import EmploymentType, Identity, IdentityStatus


def contract_end(identity: Identity) -> date | None:
    """A contractor's contract end date; None for every other employment type.

    LIFE-003 and LIFE-004 can't be judged for a contractor without one, so its absence is an
    input error rather than "no end date".
    """
    if identity.employment_type != EmploymentType.contractor:
        return None
    if identity.contract_end_date is None:
        raise OracleInputError(
            f"contractor {identity.id} has no contract_end_date (LIFE-003 and LIFE-004 need it)"
        )
    return identity.contract_end_date


@register
class Terminated(RequestRule, ReviewRule):
    """LIFE-001: a terminated beneficiary or holder gets no access.

    Claims: ``identity.status = terminated``. In reviews it applies to every item of the holder,
    valid or not, so no item of a terminated holder is ever certified. Reads the identity's
    ``status`` only: a termination date alone (a notice period) doesn't apply it.
    """

    id = "LIFE-001"
    family = "LIFE"
    effects = effects_of(request=Effect.DENY, review=Effect.REVOKE)
    summary = (
        "The beneficiary or holder is `terminated` (identity `status`). In reviews this applies "
        "to every item the holder has."
    )

    def evaluate_request(self, ctx: RequestContext) -> Evaluation:
        return self._judge(ctx, ctx.beneficiary)

    def evaluate_review(self, ctx: ItemContext) -> Evaluation:
        return self._judge(ctx, ctx.holder)

    def _judge(self, ctx: Context, identity: Identity | None) -> Evaluation:
        if identity is None or identity.status != IdentityStatus.terminated:
            return NOT_APPLICABLE
        return self.fire(ctx, [[ctx.claim(identity.id, "status")]])


@register
class OnLeave(RequestRule):
    """LIFE-002: a request for a beneficiary on leave of absence goes to a human.

    Claims: ``beneficiary.status = leave_of_absence``. Reviews ignore leave (it isn't grounds to
    revoke), so the rule has no review effect. Reads the beneficiary's ``status`` only.
    """

    id = "LIFE-002"
    family = "LIFE"
    effects = effects_of(request=Effect.ESCALATE)
    summary = (
        "The beneficiary is on leave of absence (`status` is `leave_of_absence`). In reviews, "
        "leave alone is not grounds to revoke."
    )

    def evaluate_request(self, ctx: RequestContext) -> Evaluation:
        beneficiary = ctx.beneficiary
        if beneficiary.status != IdentityStatus.leave_of_absence:
            return NOT_APPLICABLE
        return self.fire(ctx, [[ctx.claim(beneficiary.id, "status")]])


@register
class ContractEnded(RequestRule, ReviewRule):
    """LIFE-003: a contractor whose contract ended before the as-of date gets no access, even if
    HR still shows them active.

    Claims: ``identity.employment_type = contractor``, ``identity.contract_end_date``. A contract
    ending on the as-of date is still in force. In reviews it applies to every item of the holder.
    Input contract: ``employment_type``, and ``contract_end_date`` for a contractor
    (``OracleInputError`` if a contractor has none).
    """

    id = "LIFE-003"
    family = "LIFE"
    effects = effects_of(request=Effect.DENY, review=Effect.REVOKE)
    summary = (
        "The identity is a contractor whose `contract_end_date` is before the as-of date, even "
        "if HR still shows them active. In reviews this applies to every item the holder has."
    )

    def evaluate_request(self, ctx: RequestContext) -> Evaluation:
        return self._judge(ctx, ctx.beneficiary)

    def evaluate_review(self, ctx: ItemContext) -> Evaluation:
        return self._judge(ctx, ctx.holder)

    def _judge(self, ctx: Context, identity: Identity | None) -> Evaluation:
        if identity is None:
            return NOT_APPLICABLE
        end = contract_end(identity)
        if end is None or end >= ctx.as_of:
            return NOT_APPLICABLE
        claims = [
            ctx.claim(identity.id, "employment_type"),
            ctx.claim(identity.id, "contract_end_date"),
        ]
        return self.fire(ctx, [claims])


@register
class ContractCap(RequestRule):
    """LIFE-004: a contractor's grant must end on or before their contract end date.

    Applies to every contractor request. Cap: ``contract_end_date - as_of + 1`` days, which is
    positive unless the contract already ended, and then LIFE-003 denies the request, so the cap
    never reaches an approval. Claims: ``beneficiary.employment_type = contractor``,
    ``beneficiary.contract_end_date``. Input contract: as LIFE-003.
    """

    id = "LIFE-004"
    family = "LIFE"
    effects = effects_of(request=Effect.CONDITION)
    summary = (
        "A contractor's request is capped to end on or before their `contract_end_date`: at "
        "most `contract_end_date − as_of + 1` days."
    )

    def evaluate_request(self, ctx: RequestContext) -> Evaluation:
        beneficiary = ctx.beneficiary
        end = contract_end(beneficiary)
        if end is None:
            return NOT_APPLICABLE
        claims = [
            ctx.claim(beneficiary.id, "employment_type"),
            ctx.claim(beneficiary.id, "contract_end_date"),
        ]
        return self.fire(ctx, [claims], cap_days=cap_ending_on(end, ctx.as_of))
