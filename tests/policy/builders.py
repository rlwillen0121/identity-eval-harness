"""Builders for small, complete CaseViews used by the policy tests.

``CaseBuilder`` holds mutable dicts of entities with defaults that make no rule fire: an active
employee in Finance/NA, a low-risk internal production application, a read-only requestable
entitlement. Tests change exactly the facts a scenario needs with ``set`` (which re-validates,
so enum fields stay enums) and then build a request or review view.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel

from identitybench.schemas.case import (
    AccessRequest,
    Campaign,
    CampaignScopeKind,
    CaseView,
    ReviewItem,
    ReviewItemKind,
)
from identitybench.schemas.evidence import check_claim, index_view
from identitybench.schemas.gold import Certificate, RequestGold, ReviewGold, ReviewItemGold
from identitybench.world.entities import (
    Account,
    Application,
    Assignment,
    AttributeRule,
    Entitlement,
    Identity,
    Membership,
    MitigatingControl,
    PolicyPack,
    Role,
    SodRuleSides,
    Ticket,
    UsageRecord,
)

AS_OF = date(2026, 10, 10)
ORG = "Northwind Analytics"
ALL_TYPES = ("employee", "contractor", "intern", "vendor")


def day(offset: int) -> date:
    """The as-of date shifted by ``offset`` days."""
    return AS_OF + timedelta(days=offset)


class CaseBuilder:
    """Mutable entity collections that build into a ``CaseView``."""

    def __init__(self, as_of: date = AS_OF) -> None:
        self.as_of = as_of
        self.identities: dict[str, Identity] = {}
        self.applications: dict[str, Application] = {}
        self.entitlements: dict[str, Entitlement] = {}
        self.roles: dict[str, Role] = {}
        self.memberships: dict[str, Membership] = {}
        self.accounts: dict[str, Account] = {}
        self.assignments: dict[str, Assignment] = {}
        self.usage: dict[str, UsageRecord] = {}
        self.controls: dict[str, MitigatingControl] = {}
        self.tickets: dict[str, Ticket] = {}
        self.sod_rules: dict[str, SodRuleSides] = {}
        self.peer_stats: dict[str, dict[str, float]] = {}

    # ---- entities ---------------------------------------------------------------------------

    def identity(self, id: str, **fields: Any) -> Identity:
        data: dict[str, Any] = {
            "id": id,
            "display_name": f"Person {id}",
            "title": "Analyst",
            "department": "Finance",
            "cost_center": "CC-1100",
            "location": {"country": "US", "region": "NA"},
            "employment_type": "employee",
            "status": "active",
            "manager_id": "u_mgr",
            "hire_date": date(2020, 1, 6),
        }
        data.update(fields)
        if data["employment_type"] == "contractor":
            data.setdefault("contract_end_date", day(365))
        entity = Identity.model_validate(data)
        self.identities[id] = entity
        return entity

    def app(self, id: str, **fields: Any) -> Application:
        data: dict[str, Any] = {
            "id": id,
            "name": f"App {id}",
            "risk_tier": "low",
            "data_classification": "internal",
            "sox_in_scope": False,
            "environment": "prod",
            "owner_id": "u_owner",
        }
        data.update(fields)
        entity = Application.model_validate(data)
        self.applications[id] = entity
        return entity

    def entitlement(self, id: str, app_id: str = "app_main", **fields: Any) -> Entitlement:
        data: dict[str, Any] = {
            "id": id,
            "app_id": app_id,
            "name": f"Entitlement {id}",
            "description": "Grants access.",
            "privilege": "read",
            "privileged": False,
            "risk_tier": "low",
            "data_classification": "internal",
            "requestable": True,
            "eligible_employment_types": ALL_TYPES,
        }
        data.update(fields)
        entity = Entitlement.model_validate(data)
        self.entitlements[id] = entity
        return entity

    def role(
        self,
        id: str,
        entitlements: Iterable[str] = (),
        child_roles: Iterable[str] = (),
        kind: str = "business",
        attribute_rule: AttributeRule | None = None,
    ) -> Role:
        entity = Role.model_validate(
            {
                "id": id,
                "name": f"Role {id}",
                "kind": kind,
                "entitlements": tuple(entitlements),
                "child_roles": tuple(child_roles),
                "attribute_rule": attribute_rule,
            }
        )
        self.roles[id] = entity
        return entity

    def membership(
        self,
        id: str,
        identity_id: str,
        role_id: str,
        source: str = "request",
        granted_on: date | None = None,
        expires_on: date | None = None,
    ) -> Membership:
        entity = Membership.model_validate(
            {
                "id": id,
                "identity_id": identity_id,
                "role_id": role_id,
                "source": source,
                "granted_on": granted_on or day(-400),
                "expires_on": expires_on,
            }
        )
        self.memberships[id] = entity
        return entity

    def account(self, id: str, app_id: str, identity_id: str | None, **fields: Any) -> Account:
        data: dict[str, Any] = {
            "id": id,
            "app_id": app_id,
            "identity_id": identity_id,
            "kind": "human",
            "status": "active",
        }
        data.update(fields)
        entity = Account.model_validate(data)
        self.accounts[id] = entity
        return entity

    def assignment(
        self,
        id: str,
        account_id: str,
        entitlement_id: str,
        source: str = "request",
        granted_on: date | None = None,
        expires_on: date | None = None,
    ) -> Assignment:
        entity = Assignment.model_validate(
            {
                "id": id,
                "account_id": account_id,
                "entitlement_id": entitlement_id,
                "source": source,
                "granted_on": granted_on or day(-400),
                "expires_on": expires_on,
            }
        )
        self.assignments[id] = entity
        return entity

    def use(
        self,
        id: str,
        account_id: str,
        entitlement_id: str,
        last_used_on: date | None,
        telemetry: str = "available",
    ) -> UsageRecord:
        entity = UsageRecord.model_validate(
            {
                "id": id,
                "account_id": account_id,
                "entitlement_id": entitlement_id,
                "last_used_on": last_used_on,
                "telemetry": telemetry,
            }
        )
        self.usage[id] = entity
        return entity

    def control(
        self,
        id: str,
        rule_id: str,
        identity_id: str,
        granted_on: date | None = None,
        expires_on: date | None = None,
    ) -> MitigatingControl:
        entity = MitigatingControl(
            id=id,
            rule_id=rule_id,
            identity_id=identity_id,
            description="Compensating monthly review.",
            approver_id="u_mgr",
            granted_on=granted_on or day(-100),
            expires_on=expires_on or day(100),
        )
        self.controls[id] = entity
        return entity

    def ticket(
        self,
        id: str,
        assignee_id: str | None = "u_ben",
        related_app_ids: Iterable[str] = ("app_main",),
        **fields: Any,
    ) -> Ticket:
        data: dict[str, Any] = {
            "id": id,
            "type": "incident",
            "status": "open",
            "assignee_id": assignee_id,
            "related_app_ids": tuple(related_app_ids),
            "opened_on": day(-1),
            "summary": "Something is broken.",
        }
        data.update(fields)
        entity = Ticket.model_validate(data)
        self.tickets[id] = entity
        return entity

    def sod(self, rule_id: str, side_a: Iterable[str], side_b: Iterable[str]) -> SodRuleSides:
        entity = SodRuleSides(
            rule_id=rule_id, title=rule_id, side_a=tuple(side_a), side_b=tuple(side_b)
        )
        self.sod_rules[rule_id] = entity
        return entity

    def set(self, entity_id: str, **changes: Any) -> BaseModel:
        """Change fields of an existing entity, re-validating so enums stay enums."""
        for collection in self._collections():
            if entity_id in collection:
                old = collection[entity_id]
                new = type(old).model_validate({**old.model_dump(), **changes})
                collection[entity_id] = new
                return new
        raise KeyError(entity_id)

    def drop(self, entity_id: str) -> None:
        for collection in self._collections():
            collection.pop(entity_id, None)

    def _collections(self) -> list[dict[str, Any]]:
        return [
            self.identities,
            self.applications,
            self.entitlements,
            self.roles,
            self.memberships,
            self.accounts,
            self.assignments,
            self.usage,
            self.controls,
            self.tickets,
        ]

    # ---- views ------------------------------------------------------------------------------

    def view(
        self, *, request: AccessRequest | None = None, campaign: Campaign | None = None
    ) -> CaseView:
        return CaseView(
            as_of=self.as_of,
            org_name=ORG,
            policy_pack=PolicyPack(org_name=ORG, sod=tuple(self.sod_rules.values())),
            identities=dict(self.identities),
            applications=dict(self.applications),
            entitlements=dict(self.entitlements),
            roles=dict(self.roles),
            memberships=dict(self.memberships),
            accounts=dict(self.accounts),
            assignments=dict(self.assignments),
            usage=dict(self.usage),
            mitigating_controls=dict(self.controls),
            tickets=dict(self.tickets),
            peer_stats={k: dict(v) for k, v in self.peer_stats.items()},
            request=request,
            campaign=campaign,
        )

    def request(
        self,
        entitlement_id: str = "ent_req",
        *,
        beneficiary_id: str = "u_ben",
        requester_id: str | None = None,
        duration: int | None = None,
        break_glass: bool = False,
        ticket_ref: str | None = None,
        justification: str = "Need this for my work.",
    ) -> CaseView:
        request = AccessRequest(
            request_id="req_1",
            requester_id=requester_id or beneficiary_id,
            beneficiary_id=beneficiary_id,
            entitlement_id=entitlement_id,
            requested_duration_days=duration,
            is_break_glass=break_glass,
            ticket_ref=ticket_ref,
            justification=justification,
            submitted_on=self.as_of,
        )
        return self.view(request=request)

    def campaign(self, *refs: str, notes: str | None = None) -> CaseView:
        """A campaign over the given membership/assignment IDs; item IDs are ``rv_<ref>``."""
        items: list[ReviewItem] = []
        for ref in refs:
            kind = (
                ReviewItemKind.membership if ref in self.memberships else ReviewItemKind.assignment
            )
            items.append(ReviewItem(item_id=f"rv_{ref}", kind=kind, ref=ref))
        campaign = Campaign(
            campaign_id="camp_1",
            reviewer_id="u_mgr",
            scope_kind=CampaignScopeKind.manager,
            scope_ref="u_mgr",
            items=tuple(items),
            notes=notes,
        )
        return self.view(campaign=campaign)


def request_case() -> CaseBuilder:
    """A request scenario where nothing fires: an employee asks for a harmless entitlement."""
    b = CaseBuilder()
    b.identity("u_ben")
    b.identity("u_mgr", manager_id=None)
    b.identity("u_owner", manager_id=None)
    b.app("app_main")
    b.entitlement("ent_req")
    b.account("acct_ben_main", "app_main", "u_ben")
    return b


def review_case() -> CaseBuilder:
    """A review scenario with one holder, one app and one account; no items yet."""
    b = CaseBuilder()
    b.identity("u_ben")
    b.identity("u_mgr", manager_id=None)
    b.identity("u_owner", manager_id=None)
    b.app("app_main")
    b.account("acct_ben_main", "app_main", "u_ben")
    return b


# ---- assertions ------------------------------------------------------------------------------


def finding_rules(gold: RequestGold | ReviewItemGold) -> list[str]:
    return [f.rule for f in gold.findings]


def certificate_rules(gold: RequestGold | ReviewItemGold) -> list[str]:
    return [c.rule for c in gold.certificates]


def certificate(gold: RequestGold | ReviewItemGold, rule_id: str) -> Certificate:
    matches = [c for c in gold.certificates if c.rule == rule_id]
    assert len(matches) == 1, f"expected one {rule_id} certificate, got {certificate_rules(gold)}"
    return matches[0]


def claim_triples(cert: Certificate) -> list[list[tuple[str, str, object]]]:
    return [[(c.id, c.field, c.value) for c in claim_set] for claim_set in cert.claim_sets]


def item(gold: ReviewGold, item_id: str) -> ReviewItemGold:
    matches = [i for i in gold.items if i.item_id == item_id]
    assert len(matches) == 1, item_id
    return matches[0]


def assert_gold_consistent(view: CaseView, gold: RequestGold | ReviewGold) -> None:
    """Every certificate claim validates, and certificates follow the SPEC §9.1 outcome table."""
    index = index_view(view)
    records: list[RequestGold | ReviewItemGold] = (
        [gold] if isinstance(gold, RequestGold) else list(gold.items)
    )
    for record in records:
        effects = {f.rule: f.effect.value for f in record.findings}
        for cert in record.certificates:
            assert cert.claim_sets, cert
            assert cert.rule in effects, (cert.rule, effects)
            for claim_set in cert.claim_sets:
                assert claim_set, cert
                for claim in claim_set:
                    assert check_claim(index, claim.id, claim.field, claim.value), claim
        decision = record.decision.value
        certified = {c.rule for c in record.certificates}
        if decision in ("DENY", "REVOKE", "ESCALATE"):
            assert certified == {r for r, e in effects.items() if e == decision}
        elif decision == "CERTIFY":
            assert not certified
        else:
            assert isinstance(record, RequestGold)
            assert all(effects[r] == "CONDITION" for r in certified)
        exceptions = {(x.rule, x.for_rule) for x in record.exceptions}
        assert len(exceptions) == len(record.exceptions)
