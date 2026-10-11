"""SPEC Appendix A: the worked example must reproduce the §9.1 gold record exactly."""

from __future__ import annotations

from datetime import date
from typing import Any

from identitybench.policy.oracle import decide_request
from identitybench.schemas.case import AccessRequest, CaseView
from identitybench.schemas.evidence import canonical, check_claim, index_view
from identitybench.schemas.gold import RequestGold
from identitybench.world.entities import (
    Application,
    AttributeRule,
    DataClassification,
    EmploymentType,
    Entitlement,
    Environment,
    Identity,
    IdentityStatus,
    Location,
    Membership,
    MembershipSource,
    MitigatingControl,
    PolicyPack,
    Privilege,
    RiskTier,
    Role,
    RoleKind,
    SodRuleSides,
)

AS_OF = date(2026, 10, 10)

GOLD_RECORD: dict[str, Any] = {
    "decision": "DENY",
    "effective_duration_days": None,
    "findings": [
        {"rule": "SOD-002", "effect": "DENY"},
        {"rule": "APPR-001", "effect": "ESCALATE"},
        {"rule": "JUST-001", "effect": "ESCALATE"},
    ],
    "exceptions": [{"rule": "MIT-001", "for": "SOD-002", "applies": False}],
    "certificates": [
        {
            "rule": "SOD-002",
            "claim_sets": [
                [
                    {"id": "req_88412", "field": "entitlement_id", "value": "ent_gl_je_approve"},
                    {"id": "rm_55120", "field": "role_id", "value": "role_fin_ops"},
                    {"id": "role_fin_ops", "field": "child_roles", "value": "role_gl_close"},
                    {"id": "role_gl_close", "field": "entitlements", "value": "ent_gl_je_post"},
                    {"id": "mc_017", "field": "expires_on", "value": "2026-10-08"},
                ]
            ],
        }
    ],
}
"""Copied verbatim from SPEC §9.1."""

OUTPUT_A_EVIDENCE = [
    {"id": "rm_55120", "field": "role_id", "value": "role_fin_ops"},
    {"id": "role_fin_ops", "field": "child_roles", "value": "role_gl_close"},
    {"id": "role_gl_close", "field": "entitlements", "value": "ent_gl_je_post"},
    {"id": "req_88412", "field": "entitlement_id", "value": "ent_gl_je_approve"},
    {"id": "mc_017", "field": "expires_on", "value": "2026-10-08"},
]


def appendix_a_view() -> CaseView:
    """The abridged Appendix A view, completed with the fields the schema requires."""
    ledger_types = (EmploymentType.employee, EmploymentType.contractor)
    identity = Identity(
        id="u_10233",
        display_name="Dana Reyes",
        title="Sr. Acct",
        department="Finance",
        cost_center="CC-1100",
        location=Location(country="US", region="NA"),
        employment_type=EmploymentType.employee,
        status=IdentityStatus.active,
        manager_id="u_10087",
        hire_date=date(2023, 2, 1),
    )
    ledger = Application(
        id="app_ledger",
        name="General Ledger",
        risk_tier=RiskTier.high,
        data_classification=DataClassification.confidential,
        sox_in_scope=True,
        environment=Environment.prod,
        owner_id="u_10087",
    )
    entitlements = [
        Entitlement(
            id="ent_gl_je_approve",
            app_id="app_ledger",
            name="JE approve",
            description="Approve journal entries.",
            privilege=Privilege.write,
            privileged=False,
            risk_tier=RiskTier.high,
            data_classification=DataClassification.confidential,
            requestable=True,
            eligible_employment_types=ledger_types,
        ),
        Entitlement(
            id="ent_gl_je_post",
            app_id="app_ledger",
            name="JE post",
            description="Post journal entries.",
            privilege=Privilege.write,
            privileged=False,
            risk_tier=RiskTier.medium,
            data_classification=DataClassification.confidential,
            requestable=True,
            eligible_employment_types=ledger_types,
        ),
    ]
    roles = [
        Role(
            id="role_fin_ops",
            name="Finance Ops",
            kind=RoleKind.birthright,
            child_roles=("role_gl_close",),
            entitlements=("ent_ap_invoice_view",),
            attribute_rule=AttributeRule(departments=("Finance",)),
        ),
        Role(
            id="role_gl_close",
            name="GL Close",
            kind=RoleKind.business,
            entitlements=("ent_gl_je_post",),
        ),
    ]
    membership = Membership(
        id="rm_55120",
        identity_id="u_10233",
        role_id="role_fin_ops",
        source=MembershipSource.birthright,
        granted_on=date(2023, 2, 1),
    )
    control = MitigatingControl(
        id="mc_017",
        rule_id="SOD-002",
        identity_id="u_10233",
        description="Quarterly JE review by the controller.",
        approver_id="u_10087",
        granted_on=date(2026, 4, 8),
        expires_on=date(2026, 10, 8),
    )
    request = AccessRequest(
        request_id="req_88412",
        requester_id="u_10233",
        beneficiary_id="u_10233",
        entitlement_id="ent_gl_je_approve",
        requested_duration_days=None,
        is_break_glass=False,
        ticket_ref=None,
        justification="covering JE approvals for Priya during Q3 close, controller ok'd in standup",
        submitted_on=AS_OF,
    )
    pack = PolicyPack(
        org_name="Northwind Analytics",
        sod=(
            SodRuleSides(
                rule_id="SOD-002",
                title="Post journal entries vs approve journal entries",
                side_a=("ent_gl_je_post", "ent_gl_je_post_bulk"),
                side_b=("ent_gl_je_approve",),
            ),
        ),
    )
    return CaseView(
        as_of=AS_OF,
        org_name="Northwind Analytics",
        policy_pack=pack,
        identities={identity.id: identity},
        applications={ledger.id: ledger},
        entitlements={e.id: e for e in entitlements},
        roles={r.id: r for r in roles},
        memberships={membership.id: membership},
        mitigating_controls={control.id: control},
        request=request,
        peer_stats={"u_10233": {"ent_gl_je_approve": 0.42}},
    )


def test_gold_record_matches_section_9_1_exactly() -> None:
    gold = decide_request(appendix_a_view())
    dumped = gold.model_dump(mode="json", by_alias=True, exclude={"kind", "risk_tier"})
    assert dumped == GOLD_RECORD
    assert gold.risk_tier == "high"
    assert gold == RequestGold.model_validate({**GOLD_RECORD, "risk_tier": "high"})


def test_output_a_satisfies_the_certificate_and_output_c_is_caught() -> None:
    view = appendix_a_view()
    index = index_view(view)
    gold = decide_request(view)
    cited = {(c["id"], c["field"], canonical(c["value"])) for c in OUTPUT_A_EVIDENCE}
    assert all(check_claim(index, c["id"], c["field"], c["value"]) for c in OUTPUT_A_EVIDENCE)
    (cert,) = gold.certificates
    assert any(
        all((c.id, c.field, canonical(c.value)) in cited for c in claim_set)
        for claim_set in cert.claim_sets
    )
    cited_rules = {"SOD-002", "MIT-001"}
    supported = {f.rule for f in gold.findings} | {x.rule for x in gold.exceptions}
    assert cited_rules <= supported
    assert not check_claim(index, "mc_017", "expires_on", "2026-12-31")


def test_renewing_the_control_flips_the_decision() -> None:
    """Counterfactual twin: with the control valid, SOD-002 no longer applies."""
    view = appendix_a_view()
    renewed = view.mitigating_controls["mc_017"].model_copy(
        update={"expires_on": date(2026, 12, 31)}
    )
    twin = view.model_copy(update={"mitigating_controls": {"mc_017": renewed}})
    gold = decide_request(twin)
    assert gold.decision == "ESCALATE"
    assert [f.rule for f in gold.findings] == ["MIT-003", "APPR-001", "JUST-001"]
    assert [(x.for_rule, x.applies) for x in gold.exceptions] == [("SOD-002", True)]
