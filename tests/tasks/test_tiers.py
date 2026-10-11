"""Tests for tiers and tags (SPEC §10.1, §10.2).

Each trigger and tag is checked on a small hand-built view whose gold comes from the real oracle,
so the tests pin the documented procedure end to end: view -> oracle -> tier and tags. A smoke
test then runs the same functions over generated views of both worlds.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any

import pytest

from identitybench.policy.access import OracleInputError, expand_role
from identitybench.policy.jml import plan_jml
from identitybench.policy.oracle import decide_request, decide_review
from identitybench.schemas.case import (
    AccessRequest,
    Campaign,
    CampaignScopeKind,
    CaseView,
    HrAttributes,
    HrEvent,
    ReviewItem,
    ReviewItemKind,
)
from identitybench.schemas.enums import Task, Tier
from identitybench.schemas.gold import JmlGold, RequestGold, ReviewGold, ReviewItemGold
from identitybench.schemas.ops import OpType
from identitybench.tasks import tiers
from identitybench.tasks.common import IdAllocator
from identitybench.tasks.tiers import (
    DISTRACTOR_HEAVY_MIN,
    compute_tags,
    compute_tier,
    item_tags,
    item_tier,
)
from identitybench.tasks.view_builder import ViewBuilder, approx_tokens
from identitybench.world.entities import (
    Account,
    Application,
    Assignment,
    AssignmentSource,
    AttributeRule,
    EmploymentType,
    Entitlement,
    HrEventType,
    Identity,
    Location,
    Membership,
    MembershipSource,
    MitigatingControl,
    PolicyPack,
    Role,
    RoleKind,
    SodRuleSides,
    TelemetryStatus,
    Ticket,
    UsageRecord,
    World,
)
from identitybench.world.query import index_for
from tests.tasks.test_view_builder import prior_stint, random_event

AS_OF = date(2026, 10, 10)
ORG = "Tessellate Test Co"


def day(offset: int) -> date:
    return AS_OF + timedelta(days=offset)


RECENT = day(-5)
"""Default last use: well inside the dormancy window."""
LONG_AGO = day(-400)
"""Default grant date: old enough that only usage decides dormancy."""
CONTROL_START, CONTROL_END = day(-100), day(200)


# --------------------------------------------------------------------------------------------
# A hand-built world in a view
# --------------------------------------------------------------------------------------------


class Kit:
    """Records for one small, complete view.

    Defaults make no rule fire: an active Finance employee ``u_ben`` managed by ``u_mgr``, a
    low-risk internal production application ``app_main`` owned by ``u_owner``, and a low-risk,
    read-only, requestable entitlement ``ent_req``. Grants create the holder's account in each
    application they reach and a recent usage record for every provisioned pair, so the view
    always meets the SPEC §9.2 completeness rules.
    """

    def __init__(self) -> None:
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
        self.sod: dict[str, SodRuleSides] = {}
        for person in ("u_mgr", "u_owner"):
            self.person(person)
        self.person("u_ben", manager_id="u_mgr")
        self.app("app_main")
        self.entitlement("ent_req")

    # -- records ------------------------------------------------------------------------------

    def person(self, id: str, **fields: Any) -> Identity:
        data: dict[str, Any] = {
            "id": id,
            "display_name": f"Person {id}",
            "title": "Analyst",
            "department": "Finance",
            "cost_center": "CC-1100",
            "location": Location(country="US", region="NA"),
            "employment_type": EmploymentType.employee,
            "status": "active",
            "hire_date": date(2018, 1, 8),
        }
        if id in self.identities:
            data = self.identities[id].model_dump()
        data.update(fields)
        if data["employment_type"] == EmploymentType.contractor:
            data.setdefault("contract_end_date", day(365))
            if data["contract_end_date"] is None:
                data["contract_end_date"] = day(365)
        identity = Identity.model_validate(data)
        self.identities[id] = identity
        return identity

    def app(self, id: str, **fields: Any) -> Application:
        data: dict[str, Any] = {
            "id": id,
            "name": id,
            "risk_tier": "low",
            "data_classification": "internal",
            "sox_in_scope": False,
            "environment": "prod",
            "owner_id": "u_owner",
        }
        data.update(fields)
        app = Application.model_validate(data)
        self.applications[id] = app
        return app

    def entitlement(self, id: str, app: str = "app_main", **fields: Any) -> Entitlement:
        data: dict[str, Any] = {
            "id": id,
            "app_id": app,
            "name": id,
            "description": "Grants access.",
            "privilege": "read",
            "privileged": False,
            "risk_tier": "low",
            "data_classification": "internal",
            "requestable": True,
            "eligible_employment_types": tuple(EmploymentType),
        }
        if id in self.entitlements:
            data = self.entitlements[id].model_dump()
        data.update(fields)
        entitlement = Entitlement.model_validate(data)
        self.entitlements[id] = entitlement
        return entitlement

    def role(
        self,
        id: str,
        entitlements: Sequence[str] = (),
        children: Sequence[str] = (),
        *,
        birthright_for: Sequence[str] | None = None,
    ) -> Role:
        """A business role, or a birthright role for the given departments."""
        rule = None if birthright_for is None else AttributeRule(departments=tuple(birthright_for))
        role = Role(
            id=id,
            name=id,
            kind=RoleKind.business if rule is None else RoleKind.birthright,
            entitlements=tuple(entitlements),
            child_roles=tuple(children),
            attribute_rule=rule,
        )
        self.roles[id] = role
        return role

    def account(self, identity: str | None, app: str = "app_main", **fields: Any) -> str:
        account_id = fields.pop("id", f"acct_{identity}_{app}")
        if account_id not in self.accounts or fields:
            data: dict[str, Any] = {
                "id": account_id,
                "app_id": app,
                "identity_id": identity,
                "kind": "human",
                "status": "active",
            }
            data.update(fields)
            self.accounts[account_id] = Account.model_validate(data)
        return account_id

    def use(
        self,
        account: str,
        entitlement: str,
        last_used: date | None = RECENT,
        telemetry: TelemetryStatus = TelemetryStatus.available,
    ) -> None:
        usage_id = f"use_{account}_{entitlement}"
        self.usage[usage_id] = UsageRecord(
            id=usage_id,
            account_id=account,
            entitlement_id=entitlement,
            last_used_on=None if telemetry is TelemetryStatus.unavailable else last_used,
            telemetry=telemetry,
        )

    def grant_role(
        self,
        id: str,
        identity: str,
        role: str,
        *,
        granted: date = LONG_AGO,
        expires: date | None = None,
        last_used: date | None = RECENT,
    ) -> Membership:
        birthright = self.roles[role].kind is RoleKind.birthright
        membership = Membership(
            id=id,
            identity_id=identity,
            role_id=role,
            source=MembershipSource.birthright if birthright else MembershipSource.request,
            granted_on=granted,
            expires_on=expires,
        )
        self.memberships[id] = membership
        for entitlement_id in sorted(expand_role(self.roles, role)):
            account = self.account(identity, self.entitlements[entitlement_id].app_id)
            self.use(account, entitlement_id, last_used)
        return membership

    def grant(
        self,
        id: str,
        identity: str | None,
        entitlement: str,
        *,
        account: str | None = None,
        granted: date = LONG_AGO,
        expires: date | None = None,
        last_used: date | None = RECENT,
        telemetry: TelemetryStatus = TelemetryStatus.available,
    ) -> Assignment:
        account_id = account or self.account(identity, self.entitlements[entitlement].app_id)
        assignment = Assignment(
            id=id,
            account_id=account_id,
            entitlement_id=entitlement,
            source=AssignmentSource.request,
            request_id="req_00009",
            granted_on=granted,
            expires_on=expires,
        )
        self.assignments[id] = assignment
        self.use(account_id, entitlement, last_used, telemetry)
        return assignment

    def control(
        self, id: str, rule: str, *, granted: date = CONTROL_START, expires: date = CONTROL_END
    ) -> None:
        self.controls[id] = MitigatingControl(
            id=id,
            rule_id=rule,
            identity_id="u_ben",
            description="Compensating review by the controller.",
            approver_id="u_owner",
            granted_on=granted,
            expires_on=expires,
        )

    def ticket(self, id: str, **fields: Any) -> None:
        data: dict[str, Any] = {
            "id": id,
            "type": "change",
            "status": "open",
            "assignee_id": None,
            "related_app_ids": ("app_main",),
            "opened_on": day(-3),
            "summary": "Quarterly patching.",
        }
        data.update(fields)
        self.tickets[id] = Ticket.model_validate(data)

    def separate(self, rule: str, side_a: Sequence[str], side_b: Sequence[str]) -> None:
        self.sod[rule] = SodRuleSides(
            rule_id=rule, title=rule, side_a=tuple(side_a), side_b=tuple(side_b)
        )

    def journal_conflict(self) -> None:
        """SOD-002 with ``ent_post`` on one side and ``ent_approve`` on the other, both low-risk
        and internal so the SoD rule is the only one in play."""
        self.entitlement("ent_post")
        self.entitlement("ent_approve")
        self.separate("SOD-002", ["ent_post"], ["ent_approve"])

    # -- views --------------------------------------------------------------------------------

    def view(self, **header: Any) -> CaseView:
        return CaseView(
            as_of=AS_OF,
            org_name=ORG,
            policy_pack=PolicyPack(org_name=ORG, sod=tuple(self.sod[r] for r in sorted(self.sod))),
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
            **header,
        )

    def request(self, entitlement: str = "ent_req", **fields: Any) -> CaseView:
        data: dict[str, Any] = {
            "request_id": "req_00001",
            "requester_id": "u_ben",
            "beneficiary_id": "u_ben",
            "entitlement_id": entitlement,
            "justification": "Covering month-end close.",
            "submitted_on": AS_OF,
        }
        data.update(fields)
        return self.view(request=AccessRequest.model_validate(data))

    def campaign(self, *refs: str) -> CaseView:
        items = tuple(
            ReviewItem(
                item_id=f"rv_{n:05d}",
                kind=ReviewItemKind.membership
                if ref in self.memberships
                else ReviewItemKind.assignment,
                ref=ref,
            )
            for n, ref in enumerate(refs, 1)
        )
        campaign = Campaign(
            campaign_id="cmp_00001",
            reviewer_id="u_mgr",
            scope_kind=CampaignScopeKind.manager,
            scope_ref="u_mgr",
            items=items,
        )
        return self.view(campaign=campaign)

    def event(self, type: HrEventType, effective: date, **attributes: Any) -> CaseView:
        identity = self.identities["u_ben"]
        before = HrAttributes(
            department=identity.department,
            title=identity.title,
            employment_type=identity.employment_type,
            location=identity.location,
            manager_id=identity.manager_id,
            cost_center=identity.cost_center,
        )
        event = HrEvent(
            event_id="evt_00001",
            type=type,
            identity_id="u_ben",
            effective_on=effective,
            before=before,
            after=before.model_copy(update=attributes),
        )
        return self.view(event=event)


def request_result(view: CaseView) -> tuple[Tier, tuple[str, ...], RequestGold]:
    gold = decide_request(view)
    return (
        compute_tier(view, gold, Task.access_request),
        compute_tags(view, gold, Task.access_request),
        gold,
    )


def item_result(view: CaseView, ref: str) -> tuple[Tier, tuple[str, ...], ReviewItemGold]:
    gold = decide_review(view)
    assert view.campaign is not None
    item_id = next(i.item_id for i in view.campaign.items if i.ref == ref)
    item = next(g for g in gold.items if g.item_id == item_id)
    return item_tier(view, item), item_tags(view, item), item


def jml_result(view: CaseView) -> tuple[Tier, tuple[str, ...], JmlGold]:
    gold = plan_jml(view)
    return compute_tier(view, gold, Task.jml), compute_tags(view, gold, Task.jml), gold


# --------------------------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------------------------


def test_default_approval_is_l1_without_tags() -> None:
    tier, tags, gold = request_result(Kit().request())
    assert gold.decision.value == "APPROVE" and not gold.findings
    assert (tier, tags) == (Tier.L1, ())


def test_one_rule_on_one_record_is_l1() -> None:
    kit = Kit()
    kit.person(
        "u_ben", status="terminated", termination_date=day(-30), termination_type="voluntary"
    )
    assert request_result(kit.request())[:2] == (Tier.L1, ("life",))
    kit = Kit()
    kit.entitlement("ent_req", risk_tier="high")
    assert request_result(kit.request())[:2] == (Tier.L1, ("appr",))


def test_two_rules_make_l2() -> None:
    kit = Kit()
    kit.entitlement("ent_req", risk_tier="high", data_classification="confidential")
    tier, tags, gold = request_result(kit.request())
    assert {f.rule for f in gold.findings} == {"APPR-001", "JUST-001"}
    assert (tier, tags) == (Tier.L2, ("appr", "just", "multi_rule"))


def test_facts_on_several_records_make_l2() -> None:
    kit = Kit()
    kit.person("u_ben", employment_type=EmploymentType.intern)
    kit.entitlement("ent_req", privilege="admin")
    tier, tags, gold = request_result(kit.request())
    assert [f.rule for f in gold.findings] == ["ELIG-001"]
    assert (tier, tags) == (Tier.L2, ("elig",))


@pytest.mark.parametrize(
    "eligible", [(EmploymentType.employee,), ()], ids=["cited-list", "empty-list"]
)
def test_elig_005_reads_the_entitlement(eligible: tuple[EmploymentType, ...]) -> None:
    """ELIG-005 compares the identity with the entitlement's eligible list. The certificate
    cites the list's elements, and an empty list, with nothing to cite, still counts as read."""
    kit = Kit()
    kit.person("u_ben", employment_type=EmploymentType.intern)
    kit.entitlement("ent_req", eligible_employment_types=eligible)
    tier, tags, gold = request_result(kit.request())
    assert [f.rule for f in gold.findings] == ["ELIG-005"]
    cited = {claim.id for claim in gold.certificates[0].claim_sets[0]}
    assert cited == ({"u_ben", "ent_req"} if eligible else {"u_ben"})
    assert (tier, tags) == (Tier.L2, ("elig",))


@pytest.mark.parametrize(
    ("contract_end", "expected"),
    [
        (day(40), (Tier.L1, ("life",))),
        (day(5), (Tier.L3, ("life", "temporal_boundary"))),
        (AS_OF, (Tier.L3, ("life", "near_miss", "temporal_boundary"))),
        (day(-3), (Tier.L3, ("life", "multi_rule", "temporal_boundary"))),
    ],
)
def test_contract_end_boundary(contract_end: date, expected: tuple[Tier, tuple[str, ...]]) -> None:
    kit = Kit()
    kit.person("u_ben", employment_type=EmploymentType.contractor, contract_end_date=contract_end)
    assert request_result(kit.request(requested_duration_days=30))[:2] == expected


def test_contract_end_counts_only_while_it_can_change_the_decision() -> None:
    """A contract ending in five days decides an escalation (it would be a denial had the
    contract ended), but not a request another rule already denies: the date is near LIFE-003's
    threshold either way, but only the first decision depends on it."""
    kit = Kit()
    kit.person("u_ben", employment_type=EmploymentType.contractor, contract_end_date=day(5))
    kit.entitlement("ent_req", requestable=False)
    tier, tags, gold = request_result(kit.request(requested_duration_days=1))
    assert (gold.decision.value, [f.rule for f in gold.findings]) == (
        "DENY",
        ["LIFE-004", "ELIG-004"],
    )
    assert (tier, tags) == (Tier.L2, ("elig", "life", "multi_rule"))
    kit.entitlement("ent_req", requestable=True, risk_tier="high")
    tier, tags, gold = request_result(kit.request(requested_duration_days=1))
    assert gold.decision.value == "ESCALATE"
    assert (tier, tags) == (Tier.L3, ("appr", "life", "multi_rule", "temporal_boundary"))


def nested_post(kit: Kit, *, direct_too: bool = False) -> None:
    """``u_ben`` holds ``ent_post`` through role_parent -> role_child (and directly on
    role_parent too when ``direct_too``)."""
    kit.journal_conflict()
    kit.role("role_child", ["ent_post"])
    kit.role("role_parent", ["ent_post"] if direct_too else ["ent_req"], ["role_child"])
    kit.grant_role("rm_00001", "u_ben", "role_parent")


def test_nested_role_on_every_path_is_l3() -> None:
    kit = Kit()
    nested_post(kit)
    tier, tags, gold = request_result(kit.request("ent_approve"))
    assert gold.decision.value == "DENY"
    assert (tier, tags) == (Tier.L3, ("nested_role", "sod"))


def test_a_direct_alternative_avoids_the_nesting() -> None:
    kit = Kit()
    nested_post(kit, direct_too=True)
    tier, tags, gold = request_result(kit.request("ent_approve"))
    assert len(gold.certificates[0].claim_sets) == 2
    assert (tier, tags) == (Tier.L2, ("sod",))


def test_appendix_a() -> None:
    """SPEC Appendix A: DENY by SOD-002 through depth-2 nesting with a control that expired two
    days before the as-of date. Tier L3; the appendix's tags plus the families of the other
    findings (APPR-001, JUST-001) and of the evaluated exception (MIT-001).

    A known SPEC inconsistency, not an extension: Appendix A lists the case's tags as exactly
    sod, nested_role, expired_exception, temporal_boundary and multi_rule, yet its own gold has
    APPR-001 and JUST-001 findings and an MIT-001 evaluation, and §10.2 (with the build brief)
    tags the rule family of every finding and evaluated exception. The module follows §10.2; the
    integrator is adding ``appr``, ``just`` and ``mit`` to the appendix's tag list (SPEC 0.4.2).
    """
    kit = Kit()
    kit.person("u_10087")
    kit.person("u_10233", title="Sr. Acct", manager_id="u_10087")
    kit.app("app_ledger", risk_tier="medium", sox_in_scope=True)
    kit.app("app_ap")
    kit.entitlement("ent_ap_invoice_view", app="app_ap")
    kit.entitlement(
        "ent_gl_je_approve",
        app="app_ledger",
        privilege="write",
        risk_tier="high",
        data_classification="confidential",
    )
    kit.entitlement(
        "ent_gl_je_post",
        app="app_ledger",
        privilege="write",
        risk_tier="medium",
        data_classification="confidential",
    )
    kit.separate("SOD-002", ["ent_gl_je_post"], ["ent_gl_je_approve"])
    kit.role("role_gl_close", ["ent_gl_je_post"])
    kit.role("role_fin_ops", ["ent_ap_invoice_view"], ["role_gl_close"], birthright_for=["Finance"])
    kit.grant_role("rm_55120", "u_10233", "role_fin_ops", granted=date(2023, 2, 1))
    kit.controls["mc_017"] = MitigatingControl(
        id="mc_017",
        rule_id="SOD-002",
        identity_id="u_10233",
        description="Controller reviews JE approvals.",
        approver_id="u_10087",
        granted_on=date(2026, 4, 8),
        expires_on=date(2026, 10, 8),
    )
    view = kit.request(
        "ent_gl_je_approve",
        requester_id="u_10233",
        beneficiary_id="u_10233",
        request_id="req_88412",
    )
    tier, tags, gold = request_result(view)
    assert gold.decision.value == "DENY"
    assert [f.rule for f in gold.findings] == ["SOD-002", "APPR-001", "JUST-001"]
    assert tier is Tier.L3
    appendix = {"sod", "nested_role", "expired_exception", "temporal_boundary", "multi_rule"}
    assert set(tags) == appendix | {"appr", "just", "mit"}


def test_mitigated_conflict() -> None:
    kit = Kit()
    kit.journal_conflict()
    kit.grant("as_00001", "u_ben", "ent_post")
    kit.control("mc_001", "SOD-002")
    tier, tags, gold = request_result(kit.request("ent_approve"))
    assert gold.decision.value == "APPROVE"
    assert [x.applies for x in gold.exceptions] == [True]
    assert (tier, tags) == (Tier.L3, ("mit", "sod"))


def test_control_not_yet_in_force() -> None:
    kit = Kit()
    kit.journal_conflict()
    kit.grant("as_00001", "u_ben", "ent_post")
    kit.control("mc_001", "SOD-002", granted=day(3))
    tier, tags, _ = request_result(kit.request("ent_approve"))
    assert (tier, tags) == (Tier.L3, ("expired_exception", "mit", "sod", "temporal_boundary"))


def test_control_about_to_expire() -> None:
    kit = Kit()
    kit.journal_conflict()
    kit.grant("as_00001", "u_ben", "ent_post")
    kit.control("mc_001", "SOD-002", expires=day(2))
    tier, tags, gold = request_result(kit.request("ent_approve"))
    assert gold.effective_duration_days == 3
    assert (tier, tags) == (Tier.L3, ("mit", "near_miss", "sod", "temporal_boundary"))


@pytest.mark.parametrize("duration", [7, None], ids=["requested-binds", "control-cap-binds"])
def test_mitigated_conflict_through_a_nested_role(duration: int | None) -> None:
    """A valid control leaves a 7-day request with no certificate, while a standing request
    gets MIT-003's cap: both rest on the same nested role, so both are tagged ``nested_role``."""
    kit = Kit()
    nested_post(kit)
    kit.control("mc_001", "SOD-002")
    tier, tags, gold = request_result(kit.request("ent_approve", requested_duration_days=duration))
    assert gold.decision.value == "APPROVE"
    assert bool(gold.certificates) is (duration is None)
    assert (tier, tags) == (Tier.L3, ("mit", "nested_role", "sod"))


def test_mitigated_conflict_with_a_direct_alternative_is_not_nested() -> None:
    kit = Kit()
    nested_post(kit, direct_too=True)
    kit.control("mc_001", "SOD-002")
    tier, tags, _ = request_result(kit.request("ent_approve", requested_duration_days=7))
    assert (tier, tags) == (Tier.L3, ("mit", "sod"))


def test_conflicting_access_through_two_sources() -> None:
    kit = Kit()
    kit.journal_conflict()
    kit.role("role_poster", ["ent_post"])
    kit.grant_role("rm_00001", "u_ben", "role_poster")
    kit.grant("as_00001", "u_ben", "ent_post")
    tier, tags, _ = request_result(kit.request("ent_approve"))
    assert (tier, tags) == (Tier.L3, ("multi_source", "sod"))
    kit.control("mc_001", "SOD-002")
    tier, tags, gold = request_result(kit.request("ent_approve", requested_duration_days=7))
    assert not gold.certificates
    assert (tier, tags) == (Tier.L3, ("mit", "multi_source", "sod"))


@pytest.mark.parametrize(
    ("expired", "expected"),
    [
        (day(-60), (Tier.L1, ("near_miss",))),
        (day(-3), (Tier.L3, ("near_miss", "temporal_boundary"))),
    ],
)
def test_expired_conflicting_source_is_a_near_miss(
    expired: date, expected: tuple[Tier, tuple[str, ...]]
) -> None:
    kit = Kit()
    kit.journal_conflict()
    kit.grant("as_00001", "u_ben", "ent_post", expires=expired, last_used=expired)
    tier, tags, gold = request_result(kit.request("ent_approve"))
    assert gold.decision.value == "APPROVE"
    assert (tier, tags) == expected


def test_a_conflicting_source_backed_up_by_another_decides_nothing() -> None:
    """``as_00001`` expires in three days, but ``as_00002`` keeps the conflict on its own, so the
    request is denied whichever side of the date ``as_00001`` sits."""
    kit = Kit()
    kit.journal_conflict()
    kit.entitlement("ent_post_bulk")
    kit.separate("SOD-002", ["ent_post", "ent_post_bulk"], ["ent_approve"])
    kit.grant("as_00001", "u_ben", "ent_post", expires=day(3))
    kit.grant("as_00002", "u_ben", "ent_post_bulk")
    tier, tags, gold = request_result(kit.request("ent_approve"))
    assert gold.decision.value == "DENY"
    assert (tier, tags) == (Tier.L2, ("sod",))


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (None, (Tier.L3, ("mit", "sod", "temporal_boundary"))),
        (7, (Tier.L3, ("mit", "sod"))),
    ],
    ids=["standing", "seven-days"],
)
def test_a_mitigated_conflicts_source_date_counts_when_it_moves_the_duration(
    duration: int | None, expected: tuple[Tier, tuple[str, ...]]
) -> None:
    """The conflicting grant started three days ago, and a valid control mitigates it, so
    MIT-003 caps the approval at the control's expiry (201 days). Had the grant not started, a
    standing request would be approved without a cap: the date decides the duration. A 7-day
    request is approved for 7 days either way."""
    kit = Kit()
    kit.journal_conflict()
    kit.grant("as_00001", "u_ben", "ent_post", granted=day(-3))
    kit.control("mc_001", "SOD-002")
    tier, tags, gold = request_result(kit.request("ent_approve", requested_duration_days=duration))
    assert gold.decision.value == "APPROVE"
    assert gold.effective_duration_days == (201 if duration is None else 7)
    assert (tier, tags) == expected


def incident(kit: Kit) -> None:
    kit.ticket("INC-00001", type="incident", assignee_id="u_ben")


def test_valid_break_glass_bypassing_appr_001_is_l3() -> None:
    kit = Kit()
    kit.entitlement("ent_req", risk_tier="critical")
    incident(kit)
    tier, tags, gold = request_result(kit.request(is_break_glass=True, ticket_ref="INC-00001"))
    assert (gold.decision.value, gold.effective_duration_days) == ("APPROVE", 1)
    assert (tier, tags) == (Tier.L3, ("appr", "break_glass"))


def test_break_glass_bypass_counts_only_when_it_decides() -> None:
    """When another rule denies or escalates the request, the decision stands without the
    bypass, so it isn't an exception the case turns on (the ``appr`` family stays)."""
    kit = Kit()
    kit.entitlement("ent_req", risk_tier="critical")
    kit.person(
        "u_ben", status="terminated", termination_date=day(-30), termination_type="voluntary"
    )
    incident(kit)
    tier, tags, gold = request_result(kit.request(is_break_glass=True, ticket_ref="INC-00001"))
    assert gold.decision.value == "DENY"
    assert (tier, tags) == (Tier.L2, ("appr", "break_glass", "life", "multi_rule"))
    kit = Kit()
    kit.person("u_other")
    kit.entitlement("ent_req", risk_tier="critical")
    incident(kit)
    view = kit.request(is_break_glass=True, ticket_ref="INC-00001", requester_id="u_other")
    tier, tags, gold = request_result(view)
    assert [f.rule for f in gold.findings] == ["APPR-002", "APPR-004"]
    assert gold.decision.value == "ESCALATE"
    assert (tier, tags) == (Tier.L2, ("appr", "break_glass", "multi_rule", "on_behalf_of"))


def test_break_glass_without_a_bypass() -> None:
    kit = Kit()
    incident(kit)
    tier, tags, _ = request_result(kit.request(is_break_glass=True, ticket_ref="INC-00001"))
    assert (tier, tags) == (Tier.L2, ("appr", "break_glass"))


def test_break_glass_ticket_one_check_short() -> None:
    kit = Kit()
    kit.ticket("INC-00001", type="incident", assignee_id="u_mgr")
    tier, tags, gold = request_result(kit.request(is_break_glass=True, ticket_ref="INC-00001"))
    assert gold.decision.value == "DENY"
    assert (tier, tags) == (Tier.L2, ("appr", "break_glass", "near_miss"))


def test_on_behalf_of() -> None:
    tier, tags, _ = request_result(Kit().request(requester_id="u_mgr"))
    assert (tier, tags) == (Tier.L1, ("on_behalf_of",))


def test_tickets_that_almost_satisfy_just_001() -> None:
    def confidential() -> Kit:
        kit = Kit()
        kit.entitlement("ent_req", data_classification="confidential")
        return kit

    kit = confidential()
    kit.ticket("CHG-00001", status="closed")
    assert request_result(kit.request(ticket_ref="CHG-00001"))[:2] == (
        Tier.L2,
        ("just", "near_miss"),
    )
    kit = confidential()
    kit.ticket("CHG-00002")
    assert request_result(kit.request())[:2] == (Tier.L1, ("just", "near_miss"))
    kit = confidential()
    kit.ticket("CHG-00003", status="closed", related_app_ids=("app_other",))
    assert request_result(kit.request(ticket_ref="CHG-00003"))[:2] == (Tier.L2, ("just",))
    kit = confidential()
    kit.ticket("CHG-00004")
    assert request_result(kit.request(ticket_ref="CHG-00004"))[:2] == (Tier.L1, ())


def test_leave_and_notice_are_lookalikes_of_termination() -> None:
    kit = Kit()
    kit.person("u_ben", status="leave_of_absence")
    assert request_result(kit.request())[:2] == (Tier.L1, ("life", "near_miss"))
    kit = Kit()
    kit.person("u_ben", termination_date=day(20), termination_type="voluntary")
    assert request_result(kit.request())[:2] == (Tier.L1, ("near_miss",))


def test_distractor_heavy() -> None:
    view = Kit().request()
    gold = decide_request(view)
    assert "distractor_heavy" in compute_tags(
        view, gold, Task.access_request, distractors=DISTRACTOR_HEAVY_MIN
    )
    assert "distractor_heavy" not in compute_tags(
        view, gold, Task.access_request, distractors=DISTRACTOR_HEAVY_MIN - 1
    )


def test_long_context(monkeypatch: pytest.MonkeyPatch) -> None:
    view = Kit().request()
    gold = decide_request(view)
    size = approx_tokens(view)
    monkeypatch.setattr(tiers, "LONG_CONTEXT_MIN_TOKENS", {Task.access_request: size})
    assert "long_context" in compute_tags(view, gold, Task.access_request)
    monkeypatch.setattr(tiers, "LONG_CONTEXT_MIN_TOKENS", {Task.access_request: size + 1})
    assert "long_context" not in compute_tags(view, gold, Task.access_request)


# --------------------------------------------------------------------------------------------
# Review items
# --------------------------------------------------------------------------------------------


def staff_kit() -> Kit:
    """``u_ben`` with a Finance birthright membership ``rm_00001`` (a CERTIFY item)."""
    kit = Kit()
    kit.role("role_staff", ["ent_req"], birthright_for=["Finance"])
    kit.grant_role("rm_00001", "u_ben", "role_staff")
    return kit


def test_certify_is_l1() -> None:
    kit = staff_kit()
    tier, tags, item = item_result(kit.campaign("rm_00001"), "rm_00001")
    assert item.decision.value == "CERTIFY"
    assert (tier, tags) == (Tier.L1, ())


@pytest.mark.parametrize(
    ("expires", "decision", "expected"),
    [
        (day(-60), "REVOKE", (Tier.L1, ("hyg",))),
        (day(-4), "REVOKE", (Tier.L3, ("hyg", "temporal_boundary"))),
        (day(2), "CERTIFY", (Tier.L3, ("near_miss", "temporal_boundary"))),
    ],
)
def test_item_expiry(expires: date, decision: str, expected: tuple[Tier, tuple[str, ...]]) -> None:
    kit = staff_kit()
    kit.entitlement("ent_extra")
    kit.grant("as_00001", "u_ben", "ent_extra", expires=expires, last_used=min(expires, day(-5)))
    tier, tags, item = item_result(kit.campaign("rm_00001", "as_00001"), "as_00001")
    assert item.decision.value == decision
    assert (tier, tags) == expected


@pytest.mark.parametrize(
    ("last_used", "decision", "expected"),
    [
        (day(-120), "REVOKE", (Tier.L1, ("hyg",))),
        (day(-95), "REVOKE", (Tier.L3, ("hyg", "temporal_boundary"))),
        (day(-85), "CERTIFY", (Tier.L3, ("near_miss", "temporal_boundary"))),
        (day(-60), "CERTIFY", (Tier.L1, ())),
    ],
)
def test_dormancy(last_used: date, decision: str, expected: tuple[Tier, tuple[str, ...]]) -> None:
    kit = staff_kit()
    kit.entitlement("ent_extra")
    kit.grant("as_00001", "u_ben", "ent_extra", last_used=last_used)
    tier, tags, item = item_result(kit.campaign("as_00001"), "as_00001")
    assert item.decision.value == decision
    assert (tier, tags) == expected


def test_item_dates_count_only_when_no_other_rule_revokes() -> None:
    """An item expiring in five days, or 85 days unused, is near a threshold, but a terminated
    holder's items are revoked whatever their dates (LIFE-001): not L3. The dormancy near miss
    still shows. Likewise a clock near 90 days decides nothing on an item HYG-004 revokes."""

    def terminated() -> Kit:
        kit = staff_kit()
        kit.person(
            "u_ben", status="terminated", termination_date=day(-30), termination_type="voluntary"
        )
        kit.entitlement("ent_extra")
        return kit

    kit = terminated()
    kit.grant("as_00001", "u_ben", "ent_extra", expires=day(5))
    tier, tags, item = item_result(kit.campaign("as_00001"), "as_00001")
    assert [f.rule for f in item.findings] == ["LIFE-001"]
    assert (tier, tags) == (Tier.L1, ("life",))
    kit = terminated()
    kit.grant("as_00001", "u_ben", "ent_extra", last_used=day(-85))
    assert item_result(kit.campaign("as_00001"), "as_00001")[:2] == (Tier.L1, ("life", "near_miss"))
    kit = staff_kit()
    kit.entitlement("ent_extra")
    kit.grant("as_00001", "u_ben", "ent_extra", expires=day(-60), last_used=day(-95))
    tier, tags, item = item_result(kit.campaign("as_00001"), "as_00001")
    assert [f.rule for f in item.findings] == ["HYG-001", "HYG-004"]
    assert (tier, tags) == (Tier.L2, ("hyg", "multi_rule"))


def test_never_used_grant_combines_two_records() -> None:
    kit = staff_kit()
    kit.entitlement("ent_extra")
    kit.grant("as_00001", "u_ben", "ent_extra", granted=day(-200), last_used=None)
    tier, tags, item = item_result(kit.campaign("as_00001"), "as_00001")
    assert item.decision.value == "REVOKE"
    assert (tier, tags) == (Tier.L2, ("hyg",))


def test_leave_exemption_from_dormancy() -> None:
    kit = staff_kit()
    kit.person("u_ben", status="leave_of_absence")
    kit.entitlement("ent_extra")
    kit.grant("as_00001", "u_ben", "ent_extra", last_used=day(-150))
    tier, tags, item = item_result(kit.campaign("as_00001"), "as_00001")
    assert item.decision.value == "CERTIFY"
    assert (tier, tags) == (Tier.L3, ("hyg", "near_miss"))


def test_unknown_telemetry() -> None:
    kit = staff_kit()
    kit.entitlement("ent_extra")
    kit.grant("as_00001", "u_ben", "ent_extra", telemetry=TelemetryStatus.unavailable)
    tier, tags, item = item_result(kit.campaign("as_00001"), "as_00001")
    assert item.decision.value == "CERTIFY"
    assert (tier, tags) == (Tier.L1, ("telemetry_unknown",))


def test_unknown_telemetry_only_counts_when_nothing_else_shows_use() -> None:
    kit = staff_kit()
    kit.entitlement("ent_a")
    kit.entitlement("ent_b")
    kit.role("role_pair", ["ent_a", "ent_b"])
    kit.grant_role("rm_00002", "u_ben", "role_pair", last_used=day(-200))
    account = kit.account("u_ben")
    kit.use(account, "ent_a", telemetry=TelemetryStatus.unavailable)
    assert item_result(kit.campaign("rm_00002"), "rm_00002")[:2] == (
        Tier.L1,
        ("telemetry_unknown",),
    )
    kit.use(account, "ent_b", last_used=day(-10))
    assert item_result(kit.campaign("rm_00002"), "rm_00002")[:2] == (Tier.L1, ())


@pytest.mark.parametrize(
    ("move", "decision", "expected"),
    [
        (day(-60), "REVOKE", (Tier.L2, ("hyg",))),
        (day(-17), "REVOKE", (Tier.L3, ("hyg", "temporal_boundary"))),
        (day(-12), "CERTIFY", (Tier.L3, ("near_miss", "temporal_boundary"))),
    ],
)
def test_stale_birthright_after_a_move(
    move: date, decision: str, expected: tuple[Tier, tuple[str, ...]]
) -> None:
    kit = Kit()
    kit.role("role_sales", ["ent_req"], birthright_for=["Sales"])
    kit.grant_role("rm_00001", "u_ben", "role_sales")
    kit.person("u_ben", move_date=move, prior_department="Sales")
    tier, tags, item = item_result(kit.campaign("rm_00001"), "rm_00001")
    assert item.decision.value == decision
    assert (tier, tags) == expected


def test_control_due_for_renewal() -> None:
    kit = staff_kit()
    kit.journal_conflict()
    kit.grant("as_00001", "u_ben", "ent_post")
    kit.grant("as_00002", "u_ben", "ent_approve")
    kit.control("mc_001", "SOD-002", expires=day(28))
    tier, tags, item = item_result(kit.campaign("as_00001", "as_00002"), "as_00001")
    assert [f.rule for f in item.findings] == ["MIT-002"]
    assert (tier, tags) == (Tier.L3, ("mit", "sod", "temporal_boundary"))


def conflicted_kit(**control: date) -> Kit:
    """``u_ben`` holds both sides of SOD-002 through two assignments, with a control for it."""
    kit = staff_kit()
    kit.journal_conflict()
    kit.grant("as_00001", "u_ben", "ent_post")
    kit.grant("as_00002", "u_ben", "ent_approve")
    kit.control("mc_001", "SOD-002", **control)
    return kit


@pytest.mark.parametrize(
    ("control", "rules", "expected"),
    [
        ({"expires": day(3)}, ["MIT-002"], (Tier.L3, ("mit", "near_miss", "sod"))),
        ({"expires": day(-3)}, ["SOD-002"], (Tier.L3, ("expired_exception", "mit", "sod"))),
        (
            {"granted": day(3)},
            ["SOD-002"],
            (Tier.L3, ("expired_exception", "mit", "sod", "temporal_boundary")),
        ),
        (
            {"granted": day(20), "expires": day(29)},
            ["SOD-002"],
            (Tier.L3, ("expired_exception", "mit", "sod")),
        ),
    ],
    ids=["expires-in-3-days", "expired-3-days-ago", "starts-in-3-days", "not-started"],
)
def test_review_control_dates_count_when_they_change_the_decision(
    control: dict[str, date], rules: list[str], expected: tuple[Tier, tuple[str, ...]]
) -> None:
    """A control expiring within days of the as-of date leaves the item escalated either way
    (by MIT-002 while it lasts, by SOD-002 once it lapses), so its expiry isn't a boundary. A
    control starting in three days with a distant expiry would certify the item once in force,
    so its start is. A control not yet in force can't be renewed, so its expiry near
    as-of + 30 (MIT-002's threshold) decides nothing."""
    kit = conflicted_kit(**control)
    tier, tags, item = item_result(kit.campaign("as_00001", "as_00002"), "as_00001")
    assert [f.rule for f in item.findings] == rules
    assert (tier, tags) == expected


def test_another_sources_expiry_decides_a_renewal() -> None:
    """``as_00002`` expires in five days: had it expired, ``as_00001`` would confer no side of a
    conflict and MIT-002 wouldn't escalate it, so the date decides ``as_00001`` too."""
    kit = staff_kit()
    kit.journal_conflict()
    kit.grant("as_00001", "u_ben", "ent_post")
    kit.grant("as_00002", "u_ben", "ent_approve", expires=day(5))
    kit.control("mc_001", "SOD-002", expires=day(20))
    view = kit.campaign("as_00001", "as_00002")
    for ref in ("as_00001", "as_00002"):
        tier, tags, item = item_result(view, ref)
        assert [f.rule for f in item.findings] == ["MIT-002"]
        assert (tier, tags) == (Tier.L3, ("mit", "sod", "temporal_boundary"))


def test_expired_item_next_to_live_role_access_is_multi_source() -> None:
    kit = staff_kit()
    kit.grant("as_00001", "u_ben", "ent_req", expires=day(-60), last_used=day(-60))
    tier, tags, item = item_result(kit.campaign("as_00001"), "as_00001")
    assert [f.rule for f in item.findings] == ["HYG-004"]
    assert (tier, tags) == (Tier.L3, ("hyg", "multi_source"))


def test_identity_wide_and_account_rules() -> None:
    kit = staff_kit()
    kit.person(
        "u_ben", status="terminated", termination_date=day(-30), termination_type="voluntary"
    )
    assert item_result(kit.campaign("rm_00001"), "rm_00001")[:2] == (Tier.L1, ("life",))

    kit = staff_kit()
    orphan = kit.account(None, id="acct_orphan")
    kit.grant("as_00001", None, "ent_req", account=orphan)
    assert item_result(kit.campaign("as_00001"), "as_00001")[:2] == (Tier.L1, ("hyg",))

    kit = staff_kit()
    kit.person("u_svc_owner", status="terminated", termination_date=day(-9))
    kit.person("u_svc_owner", termination_type="involuntary")
    service = kit.account(None, id="acct_svc", kind="service", owner_id="u_svc_owner")
    kit.grant("as_00001", None, "ent_req", account=service)
    tier, tags, item = item_result(kit.campaign("as_00001"), "as_00001")
    assert item.decision.value == "ESCALATE"
    assert (tier, tags) == (Tier.L2, ("hyg",))


def test_review_conflict_completed_by_a_future_grant() -> None:
    kit = staff_kit()
    kit.journal_conflict()
    kit.grant("as_00001", "u_ben", "ent_post")
    kit.grant("as_00002", "u_ben", "ent_approve", granted=day(3), last_used=None)
    view = kit.campaign("as_00001", "as_00002")
    for ref in ("as_00001", "as_00002"):
        tier, tags, item = item_result(view, ref)
        assert item.decision.value == "CERTIFY"
        assert (tier, tags) == (Tier.L3, ("near_miss", "temporal_boundary"))


def test_review_item_conferring_through_a_nested_role() -> None:
    kit = Kit()
    kit.person("u_ben", employment_type=EmploymentType.intern)
    kit.entitlement("ent_secret", data_classification="restricted")
    kit.role("role_child", ["ent_secret"])
    kit.role("role_parent", ["ent_req"], ["role_child"])
    kit.grant_role("rm_00001", "u_ben", "role_parent")
    tier, tags, item = item_result(kit.campaign("rm_00001"), "rm_00001")
    assert [f.rule for f in item.findings] == ["ELIG-002"]
    assert (tier, tags) == (Tier.L3, ("elig", "nested_role"))


@pytest.mark.parametrize(("expires", "decision"), [(CONTROL_END, "CERTIFY"), (day(-2), "ESCALATE")])
def test_review_items_of_a_conflict_through_a_nested_role(expires: date, decision: str) -> None:
    """Both items of a conflict whose post side comes through role_parent -> role_child are
    tagged ``nested_role``, whether a valid control certifies them (no certificate) or an
    expired one leaves them escalated by SOD-002."""
    kit = staff_kit()
    kit.journal_conflict()
    kit.entitlement("ent_extra")
    kit.role("role_child", ["ent_post"])
    kit.role("role_parent", ["ent_extra"], ["role_child"])
    kit.grant_role("rm_00002", "u_ben", "role_parent")
    kit.grant("as_00001", "u_ben", "ent_approve")
    kit.control("mc_001", "SOD-002", expires=expires)
    view = kit.campaign("rm_00002", "as_00001")
    for ref in ("rm_00002", "as_00001"):
        tier, tags, item = item_result(view, ref)
        assert item.decision.value == decision
        assert tier is Tier.L3
        assert {"mit", "nested_role", "sod"} <= set(tags)


def test_review_items_of_a_mitigated_conflict_held_through_two_sources() -> None:
    kit = staff_kit()
    kit.journal_conflict()
    kit.role("role_poster", ["ent_post"])
    kit.grant_role("rm_00002", "u_ben", "role_poster")
    kit.grant("as_00001", "u_ben", "ent_post")
    kit.grant("as_00002", "u_ben", "ent_approve")
    kit.control("mc_001", "SOD-002")
    view = kit.campaign("rm_00002", "as_00001", "as_00002")
    for ref in ("rm_00002", "as_00001", "as_00002"):
        tier, tags, item = item_result(view, ref)
        assert item.decision.value == "CERTIFY" and not item.certificates
        assert (tier, tags) == (Tier.L3, ("mit", "multi_source", "sod"))


def test_campaign_takes_the_hardest_item_and_all_tags() -> None:
    kit = staff_kit()
    kit.entitlement("ent_extra")
    kit.grant("as_00001", "u_ben", "ent_extra", last_used=day(-95))
    kit.grant("as_00002", "u_ben", "ent_req", expires=day(-60), last_used=day(-60))
    view = kit.campaign("rm_00001", "as_00001", "as_00002")
    gold = decide_review(view)
    item_tiers = [item_tier(view, item) for item in gold.items]
    assert compute_tier(view, gold, Task.access_review) == Tier.L3
    assert Tier.L1 in item_tiers
    union = {tag for item in gold.items for tag in item_tags(view, item)}
    assert set(compute_tags(view, gold, Task.access_review)) == union


# --------------------------------------------------------------------------------------------
# JML
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("effective", "expected"),
    [
        (day(20), (Tier.L1, ("jml", "leave_start"))),
        (day(3), (Tier.L3, ("jml", "leave_start", "temporal_boundary"))),
        (day(-2), (Tier.L3, ("jml", "leave_start", "temporal_boundary"))),
    ],
)
def test_event_date_near_as_of(effective: date, expected: tuple[Tier, tuple[str, ...]]) -> None:
    kit = staff_kit()
    assert jml_result(kit.event(HrEventType.leave_start, effective))[:2] == expected


def test_event_date_counts_only_when_it_dates_an_operation() -> None:
    """An empty plan, or an involuntary leaver's plan (IMMEDIATE whatever the date), has no
    date for the event to decide; a service-account transfer on the termination date does."""
    kit = Kit()
    kit.account("u_ben", status="disabled")
    tier, tags, gold = jml_result(kit.event(HrEventType.leave_start, day(3)))
    assert not gold.operations
    assert (tier, tags) == (Tier.L1, ("jml", "leave_start"))
    for termination in (day(3), day(-3)):
        kit = staff_kit()
        leaver(kit, termination, "involuntary")
        tier, tags, gold = jml_result(kit.event(HrEventType.leaver, termination))
        assert {op.effective for op in gold.operations} == {"IMMEDIATE"}
        assert (tier, tags) == (Tier.L1, ("jml", "leaver"))
        kit.account(None, id="acct_svc", kind="service", owner_id="u_ben")
        tier, tags, gold = jml_result(kit.event(HrEventType.leaver, termination))
        transfers = [op for op in gold.operations if op.op is OpType.TRANSFER_OWNERSHIP]
        assert [op.effective for op in transfers] == [
            "IMMEDIATE" if termination <= AS_OF else termination.isoformat()
        ]
        assert (tier, tags) == (Tier.L3, ("jml", "leaver", "temporal_boundary"))
    kit = staff_kit()
    leaver(kit, day(3), "voluntary")
    tier, tags, _ = jml_result(kit.event(HrEventType.leaver, day(3)))
    assert (tier, tags) == (Tier.L3, ("jml", "leaver", "temporal_boundary"))


def test_leave_end_with_a_security_hold() -> None:
    kit = staff_kit()
    kit.account("u_ben", status="suspended", suspension_reason="leave")
    kit.app("app_vault")
    kit.account("u_ben", "app_vault", status="suspended", suspension_reason="security_hold")
    tier, tags, gold = jml_result(kit.event(HrEventType.leave_end, day(20)))
    assert len(gold.operations) == 1
    assert (tier, tags) == (Tier.L3, ("jml", "leave_end", "near_miss"))


def test_joiner_matches_birthright_rules() -> None:
    kit = Kit()
    kit.role("role_staff", ["ent_req"], birthright_for=["Finance"])
    tier, tags, gold = jml_result(kit.event(HrEventType.joiner, day(20)))
    assert [op.role_id for op in gold.operations] == ["role_staff"]
    assert (tier, tags) == (Tier.L2, ("jml", "joiner"))


def leaver(kit: Kit, termination: date, termination_type: str) -> None:
    kit.person(
        "u_ben",
        status="terminated" if termination <= AS_OF else "active",
        termination_date=termination,
        termination_type=termination_type,
    )


def test_simple_leaver_is_l1() -> None:
    kit = staff_kit()
    leaver(kit, day(-30), "involuntary")
    assert jml_result(kit.event(HrEventType.leaver, day(-30)))[:2] == (Tier.L1, ("jml", "leaver"))


def test_leaver_transfer_skipping_an_inactive_manager() -> None:
    kit = staff_kit()
    kit.person("u_top")
    kit.person("u_mgr", status="leave_of_absence", manager_id="u_top")
    kit.account(None, id="acct_svc", kind="service", owner_id="u_ben")
    leaver(kit, day(30), "voluntary")
    tier, tags, gold = jml_result(kit.event(HrEventType.leaver, day(30)))
    transfers = [op for op in gold.operations if op.op.value == "TRANSFER_OWNERSHIP"]
    assert [op.new_owner_id for op in transfers] == ["u_top"]
    # The skipped manager is the only L3 trait: no date is near a threshold, no control, no
    # nesting, one source per entitlement.
    assert (tier, tags) == (Tier.L3, ("jml", "leaver"))
    kit.person("u_mgr", status="active")
    assert jml_result(kit.event(HrEventType.leaver, day(30)))[:2] == (Tier.L2, ("jml", "leaver"))


def mover_kit() -> Kit:
    """``u_ben`` moves from Finance to Sales in 20 days; the Sales birthright confers
    ``ent_sales``."""
    kit = staff_kit()
    kit.entitlement("ent_sales")
    kit.role("role_sales", ["ent_sales"], birthright_for=["Sales"])
    return kit


def test_mover_early_removal_through_a_nested_role() -> None:
    kit = mover_kit()
    kit.entitlement("ent_admin", privilege="admin", privileged=True)
    kit.role("role_admin_core", ["ent_admin"])
    kit.role("role_ops", ["ent_req"], ["role_admin_core"])
    kit.grant_role("rm_00002", "u_ben", "role_ops")
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(20), department="Sales"))
    early = [op for op in gold.operations if op.membership_id == "rm_00002"]
    assert [op.effective for op in early] == [day(20).isoformat()]
    assert tier is Tier.L3
    assert {"jml", "mover", "nested_role"} <= set(tags)


def test_mover_source_kept_by_a_control() -> None:
    kit = mover_kit()
    kit.entitlement("ent_post")
    kit.separate("SOD-002", ["ent_post"], ["ent_sales"])
    kit.grant("as_00001", "u_ben", "ent_post")
    kit.control("mc_001", "SOD-002")
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(20), department="Sales"))
    assert not [op for op in gold.operations if op.assignment_id == "as_00001"]
    assert tier is Tier.L3
    assert {"jml", "mit", "mover"} <= set(tags)


def test_mover_grace_period_ending_near_as_of() -> None:
    kit = mover_kit()
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(-10), department="Sales"))
    grace = [op for op in gold.operations if op.membership_id == "rm_00001"]
    assert [op.effective for op in grace] == [day(4).isoformat()]
    assert (tier, tags) == (Tier.L3, ("jml", "mover", "temporal_boundary"))


def test_mover_without_a_grace_period_removal() -> None:
    """The only old birthright membership expired long before the move, so JML-002 doesn't judge
    it and the plan has no grace-period removal for ``move_date + 14`` to date."""
    kit = Kit()
    kit.role("role_staff", ["ent_req"], birthright_for=["Finance"])
    kit.grant_role("rm_00001", "u_ben", "role_staff", expires=day(-300))
    kit.entitlement("ent_sales")
    kit.role("role_sales", ["ent_sales"], birthright_for=["Sales"])
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(-10), department="Sales"))
    assert [(op.op, op.effective) for op in gold.operations] == [(OpType.GRANT_ROLE, "IMMEDIATE")]
    assert (tier, tags) == (Tier.L2, ("jml", "mover"))


def test_mover_old_birthright_expiring_just_before_the_move() -> None:
    """Expiring two days before the move, the old birthright membership isn't judged; two days
    later it would be, and would get a grace-period removal: its expiry decides an operation."""
    kit = mover_kit()
    kit.memberships.clear()
    kit.grant_role("rm_00001", "u_ben", "role_staff", expires=day(18))
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(20), department="Sales"))
    assert [op.op for op in gold.operations] == [OpType.GRANT_ROLE]
    assert (tier, tags) == (Tier.L3, ("jml", "mover", "temporal_boundary"))


@pytest.mark.parametrize(
    ("starts", "expected"),
    [
        (day(22), (Tier.L3, ("jml", "mover", "temporal_boundary"))),
        (day(40), (Tier.L2, ("jml", "mover"))),
    ],
)
def test_mover_new_birthright_held_from_near_the_move_date(
    starts: date, expected: tuple[Tier, tuple[str, ...]]
) -> None:
    """A standing Sales membership starting after the move date doesn't count as held, so the
    plan grants the role; starting two days earlier it would count, and the grant would go."""
    kit = mover_kit()
    kit.grant_role("rm_00002", "u_ben", "role_sales", granted=starts)
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(20), department="Sales"))
    grants = [op.role_id for op in gold.operations if op.op is OpType.GRANT_ROLE]
    assert grants == ["role_sales"]
    assert (tier, tags) == expected


def late_birthright_kit() -> Kit:
    """The reviewer's Kit repro: ``u_ben`` holds ``role_all`` (birthright for Finance and Sales)
    since long ago, and a Finance birthright membership ``rm_00002`` in ``role_fin`` that starts
    on day 2. A move to Sales keeps ``role_all`` and grants nothing."""
    kit = Kit()
    kit.role("role_all", ["ent_req"], birthright_for=["Finance", "Sales"])
    kit.grant_role("rm_00001", "u_ben", "role_all")
    kit.entitlement("ent_fin")
    kit.role("role_fin", ["ent_fin"], birthright_for=["Finance"])
    kit.grant_role("rm_00002", "u_ben", "role_fin", granted=day(2))
    return kit


def test_future_move_judging_a_late_starting_source() -> None:
    """Moving on day 2, ahead of as-of, JML-002 judges every source on file, ``rm_00002`` (which
    starts on the move date) included, and removes it after the grace period. Had the move been
    yesterday, a grant starting after a past move wouldn't be judged and the plan would be empty:
    the event date decides the only operation, though no operation is dated on it."""
    kit = late_birthright_kit()
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(2), department="Sales"))
    assert [(op.op, op.membership_id, op.effective) for op in gold.operations] == [
        (OpType.REVOKE_ROLE, "rm_00002", day(16).isoformat())
    ]
    assert not plan_jml(kit.event(HrEventType.mover, day(-1), department="Sales")).operations
    assert (tier, tags) == (Tier.L3, ("jml", "mover", "temporal_boundary"))


def test_past_move_leaving_a_later_source_unjudged() -> None:
    """The symmetric case: a move yesterday leaves ``rm_00002``, granted eight days after it,
    unjudged (too far from the move for its own start to count), and the plan is empty; with the
    move on as-of it would be judged and removed. Moving the event day for day changes nothing
    else, so a move outside the window stays L2."""
    kit = late_birthright_kit()
    kit.grant_role("rm_00002", "u_ben", "role_fin", granted=day(7))
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(-1), department="Sales"))
    assert not gold.operations
    assert (tier, tags) == (Tier.L3, ("jml", "mover", "temporal_boundary"))
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(12), department="Sales"))
    assert [op.effective for op in gold.operations] == [day(26).isoformat()]
    assert (tier, tags) == (Tier.L2, ("jml", "mover"))


def test_event_date_move_the_oracle_rejects_counts_as_no_change() -> None:
    """A move to Sales on as-of re-enables u_ben's disabled ``app_x`` account, which holds a
    privileged assignment starting on day 3. Re-planned with the move yesterday, that assignment
    starts after a past move, so JML-002 leaves it unjudged and the birthright grant would switch
    it on: ``plan_jml`` rejects the moved view. The rejection is no other plan, so it must not
    raise out of ``compute_tier``; the move to day 1 still turns the ``IMMEDIATE`` grant into a
    dated one, so the event stays L3."""
    kit = Kit()
    kit.role("role_staff", ["ent_req"], birthright_for=["Finance"])
    kit.grant_role("rm_00001", "u_ben", "role_staff")
    kit.app("app_x")
    kit.entitlement("ent_x_user", app="app_x")
    kit.entitlement("ent_x_admin", app="app_x", privilege="admin", privileged=True)
    kit.role("role_sales", ["ent_x_user"], birthright_for=["Sales"])
    acct = kit.account("u_ben", "app_x", status="disabled")
    kit.grant("as_00001", "u_ben", "ent_x_admin", account=acct, granted=day(3), expires=day(9))
    with pytest.raises(OracleInputError):
        plan_jml(kit.event(HrEventType.mover, day(-1), department="Sales"))
    assert jml_result(kit.event(HrEventType.mover, AS_OF, department="Sales"))[:2] == (
        Tier.L3,
        ("jml", "mover", "temporal_boundary"),
    )


def test_event_date_shifts_that_follow_the_event_are_not_boundaries() -> None:
    """A past move whose plan is the same, day for day, with the move on as-of: the grant on the
    move date stays ``IMMEDIATE`` and the grace-period removal stays 14 days after the move. Only
    the switch to a future, dated grant changes the plan, and that is a boundary too (the date
    decides whether the grant is written ``IMMEDIATE``), so the past move is L3; the plan with no
    operation on the move day, an old birthright left to its grace period, isn't."""
    kit = mover_kit()
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(-3), department="Sales"))
    assert [(op.op, op.effective) for op in gold.operations] == [
        (OpType.GRANT_ROLE, "IMMEDIATE"),
        (OpType.REVOKE_ROLE, day(11).isoformat()),
    ]
    assert (tier, tags) == (Tier.L3, ("jml", "mover", "temporal_boundary"))
    kit = late_birthright_kit()
    kit.grant_role("rm_00002", "u_ben", "role_fin", granted=day(-30))
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(-3), department="Sales"))
    assert [op.effective for op in gold.operations] == [day(11).isoformat()]
    assert (tier, tags) == (Tier.L2, ("jml", "mover"))


def test_mover_grant_made_after_a_past_move() -> None:
    """A privileged grant two days after a move a month ago isn't judged (a grant made after a
    past move); two days earlier it would have been removed: its start decides an operation."""
    kit = mover_kit()
    kit.entitlement("ent_admin", privilege="admin", privileged=True)
    assert jml_result(kit.event(HrEventType.mover, day(-30), department="Sales"))[:2] == (
        Tier.L2,
        ("jml", "mover"),
    )
    kit.grant("as_00001", "u_ben", "ent_admin", granted=day(-28))
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(-30), department="Sales"))
    assert not [op for op in gold.operations if op.assignment_id == "as_00001"]
    assert (tier, tags) == (Tier.L3, ("jml", "mover", "temporal_boundary"))


def crossing_kit(**control: date) -> Kit:
    """A Finance-to-Sales mover whose ``ent_post`` assignment crosses the Sales birthright on
    SOD-002, with a control for that rule."""
    kit = mover_kit()
    kit.entitlement("ent_post")
    kit.separate("SOD-002", ["ent_post"], ["ent_sales"])
    kit.grant("as_00001", "u_ben", "ent_post")
    kit.control("mc_001", "SOD-002", **control)
    return kit


@pytest.mark.parametrize(
    ("control", "removed", "expected"),
    [
        (
            {"granted": day(40), "expires": day(300)},
            True,
            ("expired_exception", "jml", "mit", "mover", "multi_rule", "sod"),
        ),
        (
            {"granted": day(23), "expires": day(300)},
            True,
            ("expired_exception", "jml", "mit", "mover", "multi_rule", "sod", "temporal_boundary"),
        ),
        (
            {"expires": day(22)},
            False,
            ("jml", "mit", "mover", "multi_rule", "near_miss", "sod", "temporal_boundary"),
        ),
    ],
    ids=["starts-after-the-move", "starts-just-after-the-move", "expires-just-after-the-move"],
)
def test_mover_control_judged_on_the_move_date(
    control: dict[str, date], removed: bool, expected: tuple[str, ...]
) -> None:
    """The control is judged on the move date (day 20). Starting after it, it doesn't mitigate
    the crossing (``expired_exception``) and the assignment goes on the move date; starting or
    expiring within days of it decides whether the assignment goes. In force but expiring two
    days after the move date is also a near miss."""
    kit = crossing_kit(**control)
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(20), department="Sales"))
    early = [op.effective for op in gold.operations if op.assignment_id == "as_00001"]
    assert early == ([day(20).isoformat()] if removed else [])
    assert (tier, tags) == (Tier.L3, expected)


def test_mover_control_that_decides_nothing() -> None:
    """A privileged ``ent_post`` goes on the move date whatever the control says, so the
    control's expiry two days after the move isn't a boundary (it is still a near miss)."""
    kit = crossing_kit(expires=day(22))
    kit.entitlement("ent_post", privilege="admin", privileged=True)
    tier, tags, _ = jml_result(kit.event(HrEventType.mover, day(20), department="Sales"))
    assert (tier, tags) == (Tier.L3, ("jml", "mit", "mover", "multi_rule", "near_miss", "sod"))


def test_mover_kept_source_expiring_near_the_move_date() -> None:
    """A source a control keeps stays whether or not it is judged, so its expiry two days after
    the move date decides nothing."""
    kit = crossing_kit()
    kit.grant("as_00001", "u_ben", "ent_post", expires=day(22))
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(20), department="Sales"))
    assert not [op for op in gold.operations if op.assignment_id == "as_00001"]
    assert (tier, tags) == (Tier.L3, ("jml", "mit", "mover", "multi_rule", "sod"))


def test_mover_source_expiring_near_the_move_date() -> None:
    kit = mover_kit()
    kit.entitlement("ent_admin", privilege="admin", privileged=True)
    kit.grant("as_00001", "u_ben", "ent_admin", expires=day(23))
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(20), department="Sales"))
    assert [op.effective for op in gold.operations if op.assignment_id == "as_00001"] == [
        day(20).isoformat()
    ]
    assert tier is Tier.L3
    assert "temporal_boundary" in tags


def test_mover_privileged_access_through_two_sources() -> None:
    kit = mover_kit()
    kit.entitlement("ent_admin", privilege="admin", privileged=True)
    kit.role("role_admins", ["ent_admin"])
    kit.grant_role("rm_00002", "u_ben", "role_admins")
    kit.grant("as_00001", "u_ben", "ent_admin")
    tier, tags, gold = jml_result(kit.event(HrEventType.mover, day(20), department="Sales"))
    removed = {op.membership_id or op.assignment_id for op in gold.operations}
    assert {"rm_00002", "as_00001"} <= removed
    assert tier is Tier.L3
    assert {"jml", "mover", "multi_source"} <= set(tags)


def test_voluntary_leaver_with_privileged_access_ending_soon() -> None:
    kit = staff_kit()
    kit.entitlement("ent_admin", privilege="admin", privileged=True)
    kit.grant("as_00001", "u_ben", "ent_admin", expires=day(4))
    leaver(kit, day(30), "voluntary")
    tier, tags, gold = jml_result(kit.event(HrEventType.leaver, day(30)))
    assert [op.effective for op in gold.operations if op.assignment_id == "as_00001"] == [
        "IMMEDIATE"
    ]
    assert tier is Tier.L3
    assert "temporal_boundary" in tags


# --------------------------------------------------------------------------------------------
# Argument checks
# --------------------------------------------------------------------------------------------


def test_gold_and_task_must_agree() -> None:
    view = Kit().request()
    gold = decide_request(view)
    with pytest.raises(ValueError):
        compute_tier(view, gold, Task.jml)
    with pytest.raises(ValueError):
        compute_tags(view, gold, Task.access_review)


def test_review_arguments() -> None:
    kit = staff_kit()
    view = kit.campaign("rm_00001")
    gold = decide_review(view)
    stranger = gold.items[0].model_copy(update={"item_id": "rv_99999"})
    with pytest.raises(ValueError, match="not in the campaign"):
        item_tier(view, stranger)
    with pytest.raises(ValueError, match="at least one item"):
        compute_tier(view, ReviewGold(items=()), Task.access_review)
    with pytest.raises(ValueError, match="not in the campaign"):
        compute_tags(view, ReviewGold(items=(stranger,)), Task.access_review)


# --------------------------------------------------------------------------------------------
# Generated worlds
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("profile", ["startup", "midmarket"])
def test_tiers_and_tags_on_generated_views(request: pytest.FixtureRequest, profile: str) -> None:
    world: World = request.getfixturevalue(f"{profile}_world")
    rng = random.Random(70)
    ids = IdAllocator(world, rng)
    builder = ViewBuilder(world)
    people = sorted(world.identities)
    index = index_for(world)
    for _ in range(60):
        beneficiary = rng.choice(people)
        view = builder.build_view(
            AccessRequest(
                request_id=ids.request_id(),
                requester_id=rng.choice([beneficiary, rng.choice(people)]),
                beneficiary_id=beneficiary,
                entitlement_id=rng.choice(sorted(world.entitlements)),
                requested_duration_days=rng.choice([None, 7, 30]),
                is_break_glass=rng.random() < 0.2,
                ticket_ref=rng.choice([None, rng.choice(sorted(world.tickets))]),
                justification="x",
                submitted_on=world.anchor_date,
            )
        )
        gold = decide_request(view)
        tier = compute_tier(view, gold, Task.access_request)
        tags = compute_tags(view, gold, Task.access_request)
        assert tier in (Tier.L1, Tier.L2, Tier.L3)
        assert list(tags) == sorted(set(tags))
        if tier is Tier.L1:
            assert len(gold.findings) <= 1
        assert compute_tags(view, gold, Task.access_request) == tags
    managers = sorted(m for m, r in index.direct_reports.items() if 2 <= len(r) <= 6)
    for manager in rng.sample(managers, min(6, len(managers))):
        refs = sorted(
            (ReviewItemKind.membership, m.id)
            for r in index.direct_reports[manager]
            for m in index.memberships_by_identity.get(r, ())
        )
        refs += sorted(
            (ReviewItemKind.assignment, a.id)
            for r in index.direct_reports[manager]
            for account in index.accounts_by_identity.get(r, ())
            for a in index.assignments_by_account.get(account.id, ())
        )
        campaign = Campaign(
            campaign_id=ids.campaign_id(),
            reviewer_id=manager,
            scope_kind=CampaignScopeKind.manager,
            scope_ref=manager,
            items=tuple(ReviewItem(item_id=ids.item_id(), kind=k, ref=r) for k, r in refs),
        )
        view = builder.build_view(campaign)
        gold = decide_review(view)
        item_tiers = [item_tier(view, item) for item in gold.items]
        rank = {Tier.L1: 1, Tier.L2: 2, Tier.L3: 3}
        assert compute_tier(view, gold, Task.access_review) == max(item_tiers, key=rank.__getitem__)
        campaign_tags = set(compute_tags(view, gold, Task.access_review))
        for item, tier in zip(gold.items, item_tiers, strict=True):
            assert set(item_tags(view, item)) <= campaign_tags
            if tier is Tier.L1:
                assert len(item.findings) <= 1


@pytest.mark.parametrize("profile", ["startup", "midmarket"])
def test_jml_tiers_on_generated_views(request: pytest.FixtureRequest, profile: str) -> None:
    """Events of every type on generated worlds, so the JML paths (the event-date re-run, mover
    and leaver dates, nested birthright, controls, service-account transfers) run on real views:
    the tier is L1-L3, tags are sorted with ``jml`` and the event type, an L1 plan has only the
    event's own finding, and recomputing gives the same answer."""
    world: World = request.getfixturevalue(f"{profile}_world")
    rng = random.Random(71)
    ids = IdAllocator(world, rng)
    builder = ViewBuilder(world)
    seen: set[Tier] = set()
    for kind in HrEventType:
        for _ in range(4):
            event = random_event(world, rng, ids, kind)
            view = builder.build_view(event, history=prior_stint(world, event, ids))
            gold = plan_jml(view)
            tier = compute_tier(view, gold, Task.jml)
            tags = compute_tags(view, gold, Task.jml)
            assert tier in (Tier.L1, Tier.L2, Tier.L3)
            assert list(tags) == sorted(set(tags))
            assert {"jml", kind.value} <= set(tags)
            if tier is Tier.L1:
                assert len(gold.findings) == 1
            assert (compute_tier(view, gold, Task.jml), compute_tags(view, gold, Task.jml)) == (
                tier,
                tags,
            )
            seen.add(tier)
    assert len(seen) >= 2, f"every event on {profile} landed in {seen}"
