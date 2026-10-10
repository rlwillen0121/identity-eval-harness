"""Contract smoke tests: the shared schemas behave as the spec says."""

from __future__ import annotations

from datetime import date

import pytest

from identitybench.dates import cap_ending_on, effective_duration, is_valid_on, parse_effective
from identitybench.policy.registry import ALL_RULE_IDS, ordered
from identitybench.schemas.case import AccessRequest, CaseView
from identitybench.schemas.enums import Label, label_of
from identitybench.schemas.evidence import canonical, check_claim, index_view, make_claim
from identitybench.schemas.ops import Operation, OpType
from identitybench.schemas.outputs import RequestOutput
from identitybench.world.entities import (
    Identity,
    IdentityStatus,
    Location,
    MitigatingControl,
    PolicyPack,
    Role,
    RoleKind,
)

AS_OF = date(2026, 10, 10)


def _view() -> CaseView:
    ident = Identity(
        id="u_10233",
        display_name="Dana Reyes",
        title="Sr. Acct",
        department="Finance",
        cost_center="CC-1100",
        location=Location(country="US", region="NA"),
        employment_type="employee",
        status=IdentityStatus.active,
        manager_id="u_10087",
        hire_date=date(2023, 2, 1),
    )
    role = Role(
        id="role_fin_ops",
        name="Finance Ops",
        kind=RoleKind.birthright,
        child_roles=("role_gl_close",),
        entitlements=("ent_ap_invoice_view",),
    )
    control = MitigatingControl(
        id="mc_017",
        rule_id="SOD-002",
        identity_id="u_10233",
        description="Quarterly JE review",
        approver_id="u_10087",
        granted_on=date(2026, 1, 1),
        expires_on=date(2026, 10, 8),
    )
    request = AccessRequest(
        request_id="req_88412",
        requester_id="u_10233",
        beneficiary_id="u_10233",
        entitlement_id="ent_gl_je_approve",
        justification="covering JE approvals",
        submitted_on=AS_OF,
    )
    return CaseView(
        as_of=AS_OF,
        org_name="Northwind Analytics",
        policy_pack=PolicyPack(org_name="Northwind Analytics", sod=()),
        identities={ident.id: ident},
        roles={role.id: role},
        mitigating_controls={control.id: control},
        request=request,
    )


def test_rule_ids_complete() -> None:
    assert len(ALL_RULE_IDS) == 38
    assert ordered(["JML-001", "SOD-002", "LIFE-001", "HYG-005"]) == [
        "LIFE-001",
        "SOD-002",
        "HYG-005",
        "JML-001",
    ]


def test_date_semantics() -> None:
    assert is_valid_on(date(2026, 1, 1), date(2026, 10, 10), AS_OF)
    assert not is_valid_on(date(2026, 1, 1), date(2026, 10, 9), AS_OF)
    assert not is_valid_on(date(2026, 10, 11), None, AS_OF)
    assert cap_ending_on(date(2026, 10, 12), AS_OF) == 3
    assert effective_duration(2, [3, None]) == 2
    assert effective_duration(None, [7]) == 7
    assert effective_duration(None, [None]) is None
    assert parse_effective("IMMEDIATE", AS_OF) == AS_OF
    assert parse_effective("2026-10-17", AS_OF) == date(2026, 10, 17)


def test_evidence_claims_match_view() -> None:
    view = _view()
    index = index_view(view)
    assert check_claim(index, "mc_017", "expires_on", "2026-10-08")
    assert not check_claim(index, "mc_017", "expires_on", "2026-12-31")
    assert check_claim(index, "role_fin_ops", "child_roles", "role_gl_close")
    assert check_claim(index, "u_10233", "location.region", "na")
    assert check_claim(index, "req_88412", "entitlement_id", "ent_gl_je_approve")
    assert not check_claim(index, "u_10233", "canonical_title", "anything")  # excluded field
    assert not check_claim(index, "u_nope", "status", "active")
    claim = make_claim(index, "role_fin_ops", "child_roles", "role_gl_close")
    assert claim.value == "role_gl_close"
    assert make_claim(index, "mc_017", "expires_on").value == "2026-10-08"
    with pytest.raises(ValueError):
        make_claim(index, "role_fin_ops", "child_roles")


def test_canonical_forms() -> None:
    assert canonical(True) == canonical("TRUE") == "true"
    assert canonical(7) == canonical(7.0) == canonical("7") == "7"
    assert canonical(date(2026, 10, 8)) == "2026-10-08"


def test_output_parsing_and_labels() -> None:
    out = RequestOutput.model_validate(
        {
            "decision": "DENY",
            "conditions": None,
            "citations": {"rules": ["SOD-002"], "evidence": []},
            "rationale": "x",
            "confidence": 1.7,
        }
    )
    assert out.confidence is None
    assert label_of(out.decision) is Label.N
    assert label_of("CERTIFY") is Label.A
    assert label_of(None) is Label.INVALID


def test_operation_requires_fields() -> None:
    Operation(op=OpType.REVOKE_ALL, identity_id="u_1", effective="IMMEDIATE")
    with pytest.raises(ValueError):
        Operation(op=OpType.TRANSFER_OWNERSHIP, account_id="acct_1", effective="2026-10-17")
