"""Review rules one at a time: identity-wide vs source-specific, boundaries (SPEC §7.2, §7.4)."""

from __future__ import annotations

from typing import Any

import pytest

from identitybench.policy.oracle import OracleInputError, decide_review
from identitybench.schemas.case import CaseView
from identitybench.schemas.gold import ReviewGold
from identitybench.world.entities import AttributeRule, EmploymentType
from tests.policy.builders import (
    AS_OF,
    CaseBuilder,
    assert_gold_consistent,
    certificate,
    certificate_rules,
    claim_triples,
    day,
    finding_rules,
    item,
)


def review(view: CaseView) -> ReviewGold:
    gold = decide_review(view)
    assert_gold_consistent(view, gold)
    return gold


def young_items(b: CaseBuilder) -> None:
    """A valid business membership, a valid assignment, an expired and a future-dated one, all
    too young to be dormant."""
    b.entitlement("ent_a")
    b.entitlement("ent_b")
    b.role("role_a", entitlements=["ent_a"])
    b.membership("rm_a", "u_ben", "role_a", granted_on=day(-10))
    b.assignment("as_b", "acct_ben_main", "ent_b", granted_on=day(-10))
    b.assignment("as_old", "acct_ben_main", "ent_b", granted_on=day(-20), expires_on=day(-1))
    b.assignment("as_future", "acct_ben_main", "ent_b", granted_on=day(5))


ALL_ITEMS = ("rm_a", "as_b", "as_old", "as_future")


def test_clean_items_certify_with_no_certificate(rev: CaseBuilder) -> None:
    young_items(rev)
    gold = review(rev.campaign("rm_a", "as_b", "as_future"))
    assert [i.decision for i in gold.items] == ["CERTIFY"] * 3
    assert all(i.default_outcome and i.findings == () for i in gold.items)
    assert [i.item_id for i in gold.items] == ["rv_rm_a", "rv_as_b", "rv_as_future"]


# ---- identity-wide rules ------------------------------------------------------------------------


def test_life_001_revokes_every_item_of_a_terminated_holder(rev: CaseBuilder) -> None:
    young_items(rev)
    rev.set("u_ben", status="terminated")
    gold = review(rev.campaign(*ALL_ITEMS))
    for record in gold.items:
        assert record.decision == "REVOKE"
        assert "LIFE-001" in finding_rules(record)
        assert claim_triples(certificate(record, "LIFE-001")) == [
            [("u_ben", "status", "terminated")]
        ]
    assert certificate_rules(item(gold, "rv_as_old")) == ["LIFE-001", "HYG-004"]


def test_leave_alone_is_not_grounds_to_revoke(rev: CaseBuilder) -> None:
    young_items(rev)
    rev.set("u_ben", status="leave_of_absence")
    gold = review(rev.campaign("rm_a", "as_b"))
    assert [i.decision for i in gold.items] == ["CERTIFY", "CERTIFY"]


def test_life_003_revokes_every_item_of_an_ended_contractor(rev: CaseBuilder) -> None:
    young_items(rev)
    rev.identity("u_ben", employment_type="contractor", contract_end_date=day(-1))
    gold = review(rev.campaign("rm_a", "as_b"))
    for record in gold.items:
        assert record.decision == "REVOKE"
        assert claim_triples(certificate(record, "LIFE-003")) == [
            [
                ("u_ben", "employment_type", "contractor"),
                ("u_ben", "contract_end_date", "2026-10-09"),
            ]
        ]
    rev.set("u_ben", contract_end_date=AS_OF)
    assert [i.decision for i in review(rev.campaign("rm_a", "as_b")).items] == ["CERTIFY"] * 2


def test_elig_001_applies_to_every_item_conferring_the_access(rev: CaseBuilder) -> None:
    rev.identity("u_ben", employment_type="contractor")
    rev.entitlement("ent_admin", privilege="admin")
    rev.entitlement("ent_read")
    rev.role("role_inner", entitlements=["ent_admin"])
    rev.role("role_outer", entitlements=["ent_read"], child_roles=["role_inner"])
    rev.membership("rm_outer", "u_ben", "role_outer", granted_on=day(-10))
    rev.assignment("as_admin", "acct_ben_main", "ent_admin", granted_on=day(-10))
    rev.assignment("as_read", "acct_ben_main", "ent_read", granted_on=day(-10))
    gold = review(rev.campaign("rm_outer", "as_admin", "as_read"))
    facts = [
        ("u_ben", "employment_type", "contractor"),
        ("ent_admin", "privilege", "admin"),
        ("app_main", "environment", "prod"),
    ]
    assert claim_triples(certificate(item(gold, "rv_rm_outer"), "ELIG-001")) == [
        [
            ("rm_outer", "role_id", "role_outer"),
            ("role_outer", "child_roles", "role_inner"),
            ("role_inner", "entitlements", "ent_admin"),
            *facts,
        ]
    ]
    assert claim_triples(certificate(item(gold, "rv_as_admin"), "ELIG-001")) == [
        [("as_admin", "entitlement_id", "ent_admin"), *facts]
    ]
    assert item(gold, "rv_as_read").decision == "CERTIFY"


def test_items_that_havent_started_are_judged_on_their_records(rev: CaseBuilder) -> None:
    """The reading the rendered precedence note states: the ELIG rules and APPR-005 judge what a
    future-dated item's record names, while SoD sees only sources valid on the as-of date."""
    rev.identity("u_ben", employment_type="contractor")
    rev.app("app_crit", risk_tier="critical")
    rev.account("acct_crit", "app_crit", "u_ben")
    rev.entitlement("ent_admin", privilege="admin")
    rev.entitlement("ent_root", app_id="app_crit", privileged=True)
    rev.entitlement("ent_approve")
    rev.sod("SOD-002", ["ent_root"], ["ent_approve"])
    rev.assignment("as_admin_soon", "acct_ben_main", "ent_admin", granted_on=day(5))
    rev.assignment("as_root_soon", "acct_crit", "ent_root", granted_on=day(5))
    rev.assignment("as_approve", "acct_ben_main", "ent_approve", granted_on=day(-10))
    gold = review(rev.campaign("as_admin_soon", "as_root_soon", "as_approve"))
    assert [(i.decision, finding_rules(i)) for i in gold.items] == [
        ("REVOKE", ["ELIG-001"]),
        ("ESCALATE", ["APPR-005"]),
        ("CERTIFY", []),
    ]


@pytest.mark.parametrize(
    ("holder", "entitlement", "app", "rule"),
    [
        ({"employment_type": "vendor"}, {"data_classification": "restricted"}, {}, "ELIG-002"),
        ({}, {}, {"region_restriction": "APAC"}, "ELIG-003"),
        (
            {"employment_type": "intern"},
            {"eligible_employment_types": ("employee",)},
            {},
            "ELIG-005",
        ),
    ],
)
def test_other_eligibility_rules_revoke(
    rev: CaseBuilder,
    holder: dict[str, Any],
    entitlement: dict[str, Any],
    app: dict[str, Any],
    rule: str,
) -> None:
    rev.identity("u_ben", **holder)
    rev.entitlement("ent_x", **entitlement)
    if app:
        rev.set("app_main", **app)
    rev.assignment("as_x", "acct_ben_main", "ent_x", granted_on=day(-10))
    record = item(review(rev.campaign("as_x")), "rv_as_x")
    assert record.decision == "REVOKE"
    assert finding_rules(record) == [rule]
    assert claim_triples(certificate(record, rule))[0][0] == ("as_x", "entitlement_id", "ent_x")


# ---- APPR-005 -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("privileged", "app_tier", "decision"),
    [(True, "critical", "ESCALATE"), (True, "high", "CERTIFY"), (False, "critical", "CERTIFY")],
)
def test_appr_005_privileged_on_critical(
    rev: CaseBuilder, privileged: bool, app_tier: str, decision: str
) -> None:
    rev.set("app_main", risk_tier=app_tier)
    rev.entitlement("ent_root", privileged=privileged, privilege="admin")
    rev.role("role_root", entitlements=["ent_root"])
    rev.membership("rm_root", "u_ben", "role_root", granted_on=day(-10))
    record = item(review(rev.campaign("rm_root")), "rv_rm_root")
    assert record.decision == decision
    if decision == "ESCALATE":
        assert claim_triples(certificate(record, "APPR-005")) == [
            [
                ("rm_root", "role_id", "role_root"),
                ("role_root", "entitlements", "ent_root"),
                ("ent_root", "privileged", True),
                ("app_main", "risk_tier", "critical"),
            ]
        ]


def test_revoke_beats_escalate(rev: CaseBuilder) -> None:
    rev.set("app_main", risk_tier="critical")
    rev.entitlement("ent_root", privileged=True)
    rev.assignment("as_root", "acct_ben_main", "ent_root", granted_on=day(-20), expires_on=day(-1))
    record = item(review(rev.campaign("as_root")), "rv_as_root")
    assert record.decision == "REVOKE"
    assert finding_rules(record) == ["APPR-005", "HYG-004"]
    assert certificate_rules(record) == ["HYG-004"]


# ---- HYG-001 ------------------------------------------------------------------------------------


def dormant_assignment(b: CaseBuilder, *, last_used: int | None, granted: int = -400) -> None:
    b.entitlement("ent_x")
    b.assignment("as_x", "acct_ben_main", "ent_x", granted_on=day(granted))
    b.use("use_x", "acct_ben_main", "ent_x", None if last_used is None else day(last_used))


def hyg_001(b: CaseBuilder, ref: str = "as_x") -> tuple[str, list[list[tuple[str, str, object]]]]:
    record = item(review(b.campaign(ref)), f"rv_{ref}")
    if "HYG-001" not in finding_rules(record):
        return record.decision.value, []
    return record.decision.value, claim_triples(certificate(record, "HYG-001"))


def test_hyg_001_dormancy_boundary_90_vs_91(rev: CaseBuilder) -> None:
    dormant_assignment(rev, last_used=-91)
    assert hyg_001(rev) == (
        "REVOKE",
        [[("use_x", "last_used_on", "2026-07-11"), ("use_x", "telemetry", "available")]],
    )
    rev.set("use_x", last_used_on=day(-90))
    assert hyg_001(rev) == ("CERTIFY", [])


def test_hyg_001_never_used_counts_from_the_grant(rev: CaseBuilder) -> None:
    dormant_assignment(rev, last_used=None, granted=-91)
    rev.set("as_x", source="direct")
    assert hyg_001(rev)[1] == [
        [("as_x", "granted_on", "2026-07-11"), ("use_x", "telemetry", "available")]
    ]
    rev.set("as_x", granted_on=day(-90))
    assert hyg_001(rev) == ("CERTIFY", [])


def test_hyg_001_usage_before_the_grant_does_not_age_it(rev: CaseBuilder) -> None:
    dormant_assignment(rev, last_used=-300, granted=-60)
    assert hyg_001(rev) == ("CERTIFY", [])


def test_hyg_001_unknown_telemetry_is_not_non_use(rev: CaseBuilder) -> None:
    dormant_assignment(rev, last_used=None)
    rev.set("use_x", telemetry="unavailable")
    assert hyg_001(rev) == ("CERTIFY", [])


def test_hyg_001_tied_clock_starts_are_alternatives(rev: CaseBuilder) -> None:
    dormant_assignment(rev, last_used=-120, granted=-120)
    _, claim_sets = hyg_001(rev)
    assert [cs[0][:2] for cs in claim_sets] == [("as_x", "granted_on"), ("use_x", "last_used_on")]


def test_hyg_001_leave_return_restarts_the_clock(rev: CaseBuilder) -> None:
    dormant_assignment(rev, last_used=-300)
    rev.set("u_ben", leave_returned_on=day(-30))
    assert hyg_001(rev) == ("CERTIFY", [])
    rev.set("u_ben", leave_returned_on=day(-91))
    assert hyg_001(rev)[1] == [
        [("u_ben", "leave_returned_on", "2026-07-11"), ("use_x", "telemetry", "available")]
    ]


def test_hyg_001_never_applies_while_on_leave(rev: CaseBuilder) -> None:
    dormant_assignment(rev, last_used=-300)
    rev.set("u_ben", status="leave_of_absence")
    assert hyg_001(rev) == ("CERTIFY", [])


def test_hyg_001_exempts_birthright_memberships(rev: CaseBuilder) -> None:
    rev.entitlement("ent_x")
    rev.role("role_b", entitlements=["ent_x"], kind="birthright")
    rev.membership("rm_b", "u_ben", "role_b", source="birthright")
    rev.use("use_x", "acct_ben_main", "ent_x", day(-300))
    assert hyg_001(rev, "rm_b") == ("CERTIFY", [])


def business_membership(b: CaseBuilder) -> None:
    """role_biz expands to ent_x (app_main) and ent_y (app_crm, through a child role)."""
    b.app("app_crm")
    b.account("acct_ben_crm", "app_crm", "u_ben")
    b.entitlement("ent_x")
    b.entitlement("ent_y", app_id="app_crm")
    b.role("role_child", entitlements=["ent_y"])
    b.role("role_biz", entitlements=["ent_x"], child_roles=["role_child"])
    b.membership("rm_biz", "u_ben", "role_biz")
    b.use("use_x", "acct_ben_main", "ent_x", day(-200))
    b.use("use_y", "acct_ben_crm", "ent_y", day(-95))


def test_hyg_001_membership_uses_the_latest_use_across_the_expansion(rev: CaseBuilder) -> None:
    business_membership(rev)
    assert hyg_001(rev, "rm_biz")[1] == [
        [
            ("rm_biz", "source", "request"),
            ("use_y", "last_used_on", "2026-07-07"),
            ("use_x", "telemetry", "available"),
            ("use_y", "telemetry", "available"),
        ]
    ]
    rev.set("use_y", last_used_on=day(-10))
    assert hyg_001(rev, "rm_biz") == ("CERTIFY", [])


def test_hyg_001_membership_needs_telemetry_for_every_entitlement(rev: CaseBuilder) -> None:
    business_membership(rev)
    rev.set("use_y", telemetry="unavailable", last_used_on=None)
    assert hyg_001(rev, "rm_biz") == ("CERTIFY", [])


def test_hyg_001_role_that_confers_nothing_ages_from_its_grant(rev: CaseBuilder) -> None:
    """An empty role needs no telemetry: "every entitlement has telemetry" holds vacuously."""
    rev.role("role_empty")
    rev.membership("rm_empty", "u_ben", "role_empty", granted_on=day(-91))
    assert hyg_001(rev, "rm_empty") == (
        "REVOKE",
        [[("rm_empty", "source", "request"), ("rm_empty", "granted_on", "2026-07-11")]],
    )
    rev.set("rm_empty", granted_on=day(-90))
    assert hyg_001(rev, "rm_empty") == ("CERTIFY", [])


def test_hyg_001_missing_usage_record_is_an_input_error(rev: CaseBuilder) -> None:
    dormant_assignment(rev, last_used=-300)
    rev.drop("use_x")
    with pytest.raises(OracleInputError, match="usage record"):
        decide_review(rev.campaign("as_x"))
    rev.set("as_x", granted_on=day(-90))
    assert item(review(rev.campaign("as_x")), "rv_as_x").decision == "CERTIFY"


def test_hyg_001_membership_holder_without_an_account_is_an_input_error(rev: CaseBuilder) -> None:
    business_membership(rev)
    rev.drop("acct_ben_crm")
    with pytest.raises(OracleInputError, match="no human account in app_crm"):
        decide_review(rev.campaign("rm_biz"))


# ---- HYG-002..005 -------------------------------------------------------------------------------


def test_hyg_002_orphaned_human_account(rev: CaseBuilder) -> None:
    rev.entitlement("ent_x")
    rev.account("acct_orphan", "app_main", None)
    rev.assignment("as_orphan", "acct_orphan", "ent_x", granted_on=day(-10))
    record = item(review(rev.campaign("as_orphan")), "rv_as_orphan")
    assert record.decision == "REVOKE"
    assert claim_triples(certificate(record, "HYG-002")) == [[("acct_orphan", "kind", "human")]]


def test_orphaned_access_can_also_be_dormant(rev: CaseBuilder) -> None:
    rev.entitlement("ent_x")
    rev.account("acct_orphan", "app_main", None)
    rev.assignment("as_orphan", "acct_orphan", "ent_x")
    rev.use("use_o", "acct_orphan", "ent_x", None)
    record = item(review(rev.campaign("as_orphan")), "rv_as_orphan")
    assert finding_rules(record) == ["HYG-001", "HYG-002"]


def service_item(b: CaseBuilder, owner_id: str | None) -> None:
    b.entitlement("ent_etl")
    b.account("acct_svc", "app_main", None, kind="service", owner_id=owner_id)
    b.assignment("as_svc", "acct_svc", "ent_etl", granted_on=day(-10))


def test_hyg_003_service_account_with_a_terminated_owner(rev: CaseBuilder) -> None:
    rev.identity("u_gone", status="terminated")
    service_item(rev, "u_gone")
    record = item(review(rev.campaign("as_svc")), "rv_as_svc")
    assert record.decision == "ESCALATE"
    assert claim_triples(certificate(record, "HYG-003")) == [
        [
            ("acct_svc", "kind", "service"),
            ("acct_svc", "owner_id", "u_gone"),
            ("u_gone", "status", "terminated"),
        ]
    ]


def test_hyg_003_service_account_without_an_owner(rev: CaseBuilder) -> None:
    service_item(rev, None)
    record = item(review(rev.campaign("as_svc")), "rv_as_svc")
    assert claim_triples(certificate(record, "HYG-003")) == [[("acct_svc", "kind", "service")]]


def test_hyg_003_active_or_on_leave_owner_is_fine(rev: CaseBuilder) -> None:
    rev.identity("u_away", status="leave_of_absence")
    service_item(rev, "u_away")
    assert item(review(rev.campaign("as_svc")), "rv_as_svc").decision == "CERTIFY"


def test_hyg_003_owner_outside_the_view_is_an_input_error(rev: CaseBuilder) -> None:
    service_item(rev, "u_ghost")
    with pytest.raises(OracleInputError, match="u_ghost"):
        decide_review(rev.campaign("as_svc"))


def test_hyg_004_expiry_today_vs_yesterday(rev: CaseBuilder) -> None:
    young_items(rev)
    rev.set("as_b", expires_on=AS_OF)
    gold = review(rev.campaign("as_b", "as_old"))
    assert item(gold, "rv_as_b").decision == "CERTIFY"
    old = item(gold, "rv_as_old")
    assert old.decision == "REVOKE"
    assert claim_triples(certificate(old, "HYG-004")) == [[("as_old", "expires_on", "2026-10-09")]]


def mover(b: CaseBuilder, moved_days_ago: int | None) -> None:
    b.entitlement("ent_fin")
    b.role(
        "role_fin",
        entitlements=["ent_fin"],
        kind="birthright",
        attribute_rule=AttributeRule(departments=("Finance",)),
    )
    b.membership("rm_fin", "u_ben", "role_fin", source="birthright")
    move_date = None if moved_days_ago is None else day(-moved_days_ago)
    b.set("u_ben", department="Sales", prior_department="Finance", move_date=move_date)


def test_hyg_005_grace_boundary_13_vs_14(rev: CaseBuilder) -> None:
    mover(rev, 14)
    record = item(review(rev.campaign("rm_fin")), "rv_rm_fin")
    assert record.decision == "REVOKE"
    assert claim_triples(certificate(record, "HYG-005")) == [
        [
            ("rm_fin", "source", "birthright"),
            ("u_ben", "move_date", "2026-09-26"),
            ("u_ben", "department", "Sales"),
        ]
    ]
    rev.set("u_ben", move_date=day(-13))
    assert item(review(rev.campaign("rm_fin")), "rv_rm_fin").decision == "CERTIFY"


def test_hyg_005_needs_a_move_date(rev: CaseBuilder) -> None:
    mover(rev, None)
    assert item(review(rev.campaign("rm_fin")), "rv_rm_fin").decision == "CERTIFY"


def test_hyg_005_still_matching_birthright_stays(rev: CaseBuilder) -> None:
    mover(rev, 60)
    rev.set("role_fin", attribute_rule=AttributeRule(departments=("Finance", "Sales")))
    assert item(review(rev.campaign("rm_fin")), "rv_rm_fin").decision == "CERTIFY"


def test_hyg_005_every_mismatch_is_an_alternative(rev: CaseBuilder) -> None:
    mover(rev, 60)
    rule = AttributeRule(
        departments=("Finance",), regions=("EU",), employment_types=(EmploymentType.employee,)
    )
    rev.set("role_fin", attribute_rule=rule)
    record = item(review(rev.campaign("rm_fin")), "rv_rm_fin")
    assert [cs[-1] for cs in claim_triples(certificate(record, "HYG-005"))] == [
        ("u_ben", "department", "Sales"),
        ("u_ben", "location.region", "NA"),
    ]


def test_hyg_005_only_judges_birthright_memberships(rev: CaseBuilder) -> None:
    mover(rev, 60)
    rev.set("rm_fin", source="request", granted_on=day(-10))
    assert item(review(rev.campaign("rm_fin")), "rv_rm_fin").decision == "CERTIFY"


def test_hyg_005_needs_the_attribute_rule(rev: CaseBuilder) -> None:
    mover(rev, 60)
    rev.set("role_fin", attribute_rule=None)
    with pytest.raises(OracleInputError, match="attribute rule"):
        decide_review(rev.campaign("rm_fin"))


# ---- input contract -----------------------------------------------------------------------------


def test_review_input_errors(rev: CaseBuilder) -> None:
    young_items(rev)
    with pytest.raises(OracleInputError, match=r"view\.campaign"):
        decide_review(rev.view())
    view = rev.campaign("as_b", "as_b")
    with pytest.raises(OracleInputError, match="repeats item IDs"):
        decide_review(view)
    rev.drop("acct_ben_main")
    with pytest.raises(OracleInputError, match=r"as_b \(account acct_ben_main\)"):
        decide_review(rev.campaign("rm_a"))
    for ref in ("as_b", "as_old", "as_future"):
        rev.drop(ref)
    rev.membership("rm_ghost", "u_ghost", "role_a")
    with pytest.raises(OracleInputError, match="u_ghost"):
        decide_review(rev.campaign("rm_ghost"))
