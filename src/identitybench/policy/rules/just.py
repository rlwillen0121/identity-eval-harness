"""Justification rule JUST-001 (SPEC §7.3, §7.4).

Free-text justifications are never evidence. Sensitive access needs a structured reference: a
``ticket_ref`` naming a ticket that is in the case's ticket list.
"""

from __future__ import annotations

from identitybench.policy.registry import register
from identitybench.policy.rules.base import (
    NOT_APPLICABLE,
    Evaluation,
    RequestContext,
    RequestRule,
    effects_of,
)
from identitybench.schemas.enums import Effect
from identitybench.world.entities import DataClassification

_SENSITIVE = frozenset({DataClassification.confidential, DataClassification.restricted})


@register
class SensitiveNeedsTicket(RequestRule):
    """JUST-001: confidential or restricted access needs a ticket for its application.

    Applies when the entitlement's ``data_classification`` is confidential or restricted and
    ``ticket_ref`` doesn't name an open ticket in the case's ticket list whose
    ``related_app_ids`` include the entitlement's application (any ticket type, any assignee). A
    valid break-glass incident meets these conditions, so it satisfies the rule by construction.

    Claims: ``entitlement.data_classification`` plus, per failed check (each an alternative):
    no ``ticket_ref`` → nothing more; a ref not in the ticket list → ``request.ticket_ref``; a
    closed ticket → ``request.ticket_ref`` and ``ticket.status``; a ticket for other applications
    → ``request.ticket_ref`` and ``entitlement.app_id``.
    """

    id = "JUST-001"
    family = "JUST"
    effects = effects_of(request=Effect.ESCALATE)
    summary = (
        "An entitlement whose `data_classification` is `confidential` or `restricted` needs a "
        "`ticket_ref` naming an open ticket in the case's ticket list whose `related_app_ids` "
        "include the entitlement's application. A valid break-glass incident satisfies this by "
        "construction."
    )

    def evaluate_request(self, ctx: RequestContext) -> Evaluation:
        if ctx.requested.data_classification not in _SENSITIVE:
            return NOT_APPLICABLE
        lead = ctx.claim(ctx.requested.id, "data_classification")
        failures = ctx.ticket_failures(lead, break_glass=False)
        return self.fire(ctx, failures) if failures else NOT_APPLICABLE
