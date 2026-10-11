"""Separation of duties and mitigating controls (SPEC §7.4 "SoD scope", MIT-001..003)."""

from __future__ import annotations

from identitybench.policy.oracle import decide_request, decide_review
from identitybench.schemas.case import CaseView
from identitybench.schemas.gold import RequestGold, ReviewGold, ReviewItemGold
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

REQ_CLAIM = ("req_1", "entitlement_id", "ent_approve")
NO_CONTROL = [("MIT-001", "SOD-002", False)]
"""MIT-001 is evaluated for every conflict found, even with no control (AMBIGUITIES A2 (a))."""
PATH = [
    ("rm_ops", "role_id", "role_ops"),
    ("role_ops", "child_roles", "role_close"),
    ("role_close", "entitlements", "ent_post"),
]


def exceptions(gold: RequestGold | ReviewItemGold) -> list[tuple[str, str, bool]]:
    return [(x.rule, x.for_rule, x.applies) for x in gold.exceptions]


def decide(view: CaseView) -> RequestGold:
    gold = decide_request(view)
    assert_gold_consistent(view, gold)
    return gold


def review(view: CaseView) -> ReviewGold:
    gold = decide_review(view)
    assert_gold_consistent(view, gold)
    return gold


def journal_world(b: CaseBuilder) -> None:
    """SOD-002: post (side A) vs approve (side B). The beneficiary holds post via
    rm_ops -> role_ops -> role_close and asks for approve."""
    b.entitlement("ent_post")
    b.entitlement("ent_post_bulk")
    b.entitlement("ent_approve")
    b.sod("SOD-002", ["ent_post", "ent_post_bulk"], ["ent_approve"])
    b.role("role_close", entitlements=["ent_post"])
    b.role("role_ops", entitlements=["ent_view"], child_roles=["role_close"], kind="birthright")
    b.membership("rm_ops", "u_ben", "role_ops", source="birthright")


# ---- requests -----------------------------------------------------------------------------------


def test_request_introducing_a_conflict_through_nested_roles_is_denied(req: CaseBuilder) -> None:
    journal_world(req)
    gold = decide(req.request("ent_approve"))
    assert gold.decision == "DENY"
    assert finding_rules(gold) == ["SOD-002"]
    assert exceptions(gold) == NO_CONTROL
    assert claim_triples(certificate(gold, "SOD-002")) == [[REQ_CLAIM, *PATH]]


def test_sides_are_symmetric(req: CaseBuilder) -> None:
    journal_world(req)
    req.drop("rm_ops")
    req.assignment("as_approve", "acct_ben_main", "ent_approve")
    gold = decide(req.request("ent_post_bulk"))
    assert claim_triples(certificate(gold, "SOD-002")) == [
        [
            ("req_1", "entitlement_id", "ent_post_bulk"),
            ("as_approve", "entitlement_id", "ent_approve"),
        ]
    ]


def test_pre_existing_conflict_does_not_block_an_unrelated_request(req: CaseBuilder) -> None:
    journal_world(req)
    req.assignment("as_approve", "acct_ben_main", "ent_approve")
    gold = decide(req.request("ent_req"))
    assert gold.decision == "APPROVE"
    assert gold.findings == ()


def test_without_a_conflict_mit_001_is_not_evaluated(req: CaseBuilder) -> None:
    """A control for the rule doesn't matter when the request introduces no conflict."""
    journal_world(req)
    req.control("mc_1", "SOD-002", "u_ben")
    gold = decide(req.request("ent_req"))
    assert (gold.decision, gold.findings, gold.exceptions) == ("APPROVE", (), ())


def test_conflict_through_a_direct_assignment(req: CaseBuilder) -> None:
    journal_world(req)
    req.drop("rm_ops")
    req.assignment("as_post", "acct_ben_main", "ent_post")
    gold = decide(req.request("ent_approve"))
    assert claim_triples(certificate(gold, "SOD-002")) == [
        [REQ_CLAIM, ("as_post", "entitlement_id", "ent_post")]
    ]


def test_expired_or_future_sources_do_not_block(req: CaseBuilder) -> None:
    journal_world(req)
    req.drop("rm_ops")
    req.assignment("as_post_expired", "acct_ben_main", "ent_post", expires_on=day(-1))
    req.membership("rm_future", "u_ben", "role_close", granted_on=day(1))
    assert decide(req.request("ent_approve")).decision == "APPROVE"
    req.set("as_post_expired", expires_on=AS_OF)
    assert decide(req.request("ent_approve")).decision == "DENY"


def test_expired_direct_access_next_to_valid_role_access_still_blocks(req: CaseBuilder) -> None:
    journal_world(req)
    req.assignment("as_post_expired", "acct_ben_main", "ent_post", expires_on=day(-10))
    gold = decide(req.request("ent_approve"))
    assert claim_triples(certificate(gold, "SOD-002")) == [[REQ_CLAIM, *PATH]]


def test_access_on_a_disabled_account_is_still_held(req: CaseBuilder) -> None:
    journal_world(req)
    req.drop("rm_ops")
    req.set("acct_ben_main", status="disabled")
    req.assignment("as_post", "acct_ben_main", "ent_post")
    assert decide(req.request("ent_approve")).decision == "DENY"


def test_a_service_account_the_beneficiary_owns_is_not_held_access(req: CaseBuilder) -> None:
    """Held access is attributed through an account's ``identity_id``, never its ``owner_id``,
    as the rendered preamble states."""
    journal_world(req)
    req.drop("rm_ops")
    req.account("acct_svc", "app_main", None, kind="service", owner_id="u_ben")
    req.assignment("as_svc_post", "acct_svc", "ent_post")
    gold = decide(req.request("ent_approve"))
    assert (gold.decision, gold.findings, gold.exceptions) == ("APPROVE", (), ())


def test_every_path_to_the_other_side_is_an_alternative(req: CaseBuilder) -> None:
    journal_world(req)
    req.assignment("as_bulk", "acct_ben_main", "ent_post_bulk")
    req.role("role_shortcut", entitlements=["ent_post"])
    req.set("role_ops", child_roles=("role_close", "role_shortcut"))
    gold = decide(req.request("ent_approve"))
    assert claim_triples(certificate(gold, "SOD-002")) == [
        [REQ_CLAIM, ("as_bulk", "entitlement_id", "ent_post_bulk")],
        [REQ_CLAIM, *PATH],
        [
            REQ_CLAIM,
            ("rm_ops", "role_id", "role_ops"),
            ("role_ops", "child_roles", "role_shortcut"),
            ("role_shortcut", "entitlements", "ent_post"),
        ],
    ]


def test_valid_control_mitigates_and_mit_003_caps_to_its_expiry(req: CaseBuilder) -> None:
    journal_world(req)
    req.control("mc_1", "SOD-002", "u_ben", expires_on=day(20))
    gold = decide(req.request("ent_approve"))
    assert gold.decision == "APPROVE"
    assert gold.effective_duration_days == 21
    assert finding_rules(gold) == ["MIT-003"]
    assert [(x.rule, x.for_rule, x.applies) for x in gold.exceptions] == [
        ("MIT-001", "SOD-002", True)
    ]
    assert claim_triples(certificate(gold, "MIT-003")) == [
        [REQ_CLAIM, *PATH, ("mc_1", "expires_on", "2026-10-30")]
    ]


def test_control_expiring_today_still_mitigates(req: CaseBuilder) -> None:
    journal_world(req)
    req.control("mc_1", "SOD-002", "u_ben", expires_on=AS_OF)
    gold = decide(req.request("ent_approve", duration=30))
    assert (gold.decision, gold.effective_duration_days) == ("APPROVE", 1)


def test_control_expired_yesterday_doesnt_and_is_cited(req: CaseBuilder) -> None:
    journal_world(req)
    req.control("mc_1", "SOD-002", "u_ben", granted_on=day(-200), expires_on=day(-1))
    gold = decide(req.request("ent_approve"))
    assert gold.decision == "DENY"
    assert [(x.rule, x.for_rule, x.applies) for x in gold.exceptions] == [
        ("MIT-001", "SOD-002", False)
    ]
    assert claim_triples(certificate(gold, "SOD-002")) == [
        [REQ_CLAIM, *PATH, ("mc_1", "expires_on", "2026-10-09")]
    ]


def test_control_granted_tomorrow_is_not_yet_valid(req: CaseBuilder) -> None:
    journal_world(req)
    req.control("mc_1", "SOD-002", "u_ben", granted_on=day(1), expires_on=day(90))
    gold = decide(req.request("ent_approve"))
    assert gold.decision == "DENY"
    assert gold.exceptions[0].applies is False
    assert claim_triples(certificate(gold, "SOD-002")) == [
        [REQ_CLAIM, *PATH, ("mc_1", "granted_on", "2026-10-11")]
    ]


def test_controls_are_scoped_to_identity_and_rule(req: CaseBuilder) -> None:
    journal_world(req)
    req.identity("u_peer")
    req.control("mc_peer", "SOD-002", "u_peer")
    req.control("mc_other_rule", "SOD-005", "u_ben")
    gold = decide(req.request("ent_approve"))
    assert gold.decision == "DENY"
    assert exceptions(gold) == NO_CONTROL
    assert claim_triples(certificate(gold, "SOD-002")) == [[REQ_CLAIM, *PATH]]


def test_break_glass_does_not_bypass_sod(req: CaseBuilder) -> None:
    journal_world(req)
    req.set("ent_approve", risk_tier="high")
    req.ticket("INC-1")
    gold = decide(req.request("ent_approve", break_glass=True, ticket_ref="INC-1"))
    assert gold.decision == "DENY"
    assert finding_rules(gold) == ["SOD-002", "APPR-004"]
    assert certificate_rules(gold) == ["SOD-002"]


def test_break_glass_with_an_incident_for_another_app_is_denied(req: CaseBuilder) -> None:
    req.set("ent_req", risk_tier="high")
    req.ticket("INC-1", related_app_ids=("app_crm",))
    gold = decide(req.request(break_glass=True, ticket_ref="INC-1"))
    assert gold.decision == "DENY"
    assert finding_rules(gold) == ["APPR-001", "APPR-003"]
    assert certificate_rules(gold) == ["APPR-003"]


def test_mit_003_binds_before_temp_001(req: CaseBuilder) -> None:
    journal_world(req)
    req.set("ent_approve", privileged=True)
    req.control("mc_1", "SOD-002", "u_ben", expires_on=day(2))
    gold = decide(req.request("ent_approve", duration=30))
    assert gold.effective_duration_days == 3
    assert finding_rules(gold) == ["MIT-003", "TEMP-001"]
    assert certificate_rules(gold) == ["MIT-003"]


def test_mit_003_and_temp_001_tie(req: CaseBuilder) -> None:
    journal_world(req)
    req.set("ent_approve", privileged=True)
    req.control("mc_1", "SOD-002", "u_ben", expires_on=day(6))
    gold = decide(req.request("ent_approve"))
    assert gold.effective_duration_days == 7
    assert certificate_rules(gold) == ["MIT-003", "TEMP-001"]


def _two_rule_world(b: CaseBuilder) -> None:
    """ent_hub is on side B of SOD-001 and SOD-005; the beneficiary holds both side As."""
    b.entitlement("ent_vendor")
    b.entitlement("ent_payroll")
    b.entitlement("ent_hub")
    b.sod("SOD-001", ["ent_vendor"], ["ent_hub"])
    b.sod("SOD-005", ["ent_payroll"], ["ent_hub"])
    b.assignment("as_vendor", "acct_ben_main", "ent_vendor")
    b.assignment("as_payroll", "acct_ben_main", "ent_payroll")


def test_mit_003_uses_the_earliest_control(req: CaseBuilder) -> None:
    _two_rule_world(req)
    req.control("mc_v", "SOD-001", "u_ben", expires_on=day(40))
    req.control("mc_p", "SOD-005", "u_ben", expires_on=day(10))
    gold = decide(req.request("ent_hub"))
    assert (gold.decision, gold.effective_duration_days) == ("APPROVE", 11)
    assert claim_triples(certificate(gold, "MIT-003")) == [
        [
            ("req_1", "entitlement_id", "ent_hub"),
            ("as_payroll", "entitlement_id", "ent_payroll"),
            ("mc_p", "expires_on", "2026-10-20"),
        ]
    ]
    assert [x.for_rule for x in gold.exceptions] == ["SOD-001", "SOD-005"]


def test_mit_003_ties_across_rules_are_alternatives(req: CaseBuilder) -> None:
    _two_rule_world(req)
    req.control("mc_v", "SOD-001", "u_ben", expires_on=day(10))
    req.control("mc_p", "SOD-005", "u_ben", expires_on=day(10))
    cert = certificate(decide(req.request("ent_hub")), "MIT-003")
    assert len(cert.claim_sets) == 2


def test_one_unmitigated_rule_still_denies(req: CaseBuilder) -> None:
    _two_rule_world(req)
    req.control("mc_v", "SOD-001", "u_ben", expires_on=day(10))
    gold = decide(req.request("ent_hub"))
    assert gold.decision == "DENY"
    assert finding_rules(gold) == ["SOD-005", "MIT-003"]
    assert certificate_rules(gold) == ["SOD-005"]
    assert [(x.for_rule, x.applies) for x in gold.exceptions] == [
        ("SOD-001", True),
        ("SOD-005", False),
    ]


# ---- reviews ------------------------------------------------------------------------------------


def review_world(b: CaseBuilder) -> None:
    """The holder has post through a nested business role and approve directly."""
    b.entitlement("ent_post")
    b.entitlement("ent_approve")
    b.entitlement("ent_other")
    b.sod("SOD-002", ["ent_post"], ["ent_approve"])
    b.role("role_close", entitlements=["ent_post"])
    b.role("role_ops", child_roles=["role_close"])
    b.membership("rm_ops", "u_ben", "role_ops", granted_on=day(-30))
    b.assignment("as_approve", "acct_ben_main", "ent_approve", granted_on=day(-30))
    b.assignment("as_other", "acct_ben_main", "ent_other", granted_on=day(-30))


MEMBERSHIP_PATH = [
    ("rm_ops", "role_id", "role_ops"),
    ("role_ops", "child_roles", "role_close"),
    ("role_close", "entitlements", "ent_post"),
]
APPROVE_CLAIM = ("as_approve", "entitlement_id", "ent_approve")


def test_review_escalates_only_items_conferring_a_side(rev: CaseBuilder) -> None:
    review_world(rev)
    gold = review(rev.campaign("rm_ops", "as_approve", "as_other"))
    membership = item(gold, "rv_rm_ops")
    assert membership.decision == "ESCALATE"
    assert claim_triples(certificate(membership, "SOD-002")) == [[*MEMBERSHIP_PATH, APPROVE_CLAIM]]
    direct = item(gold, "rv_as_approve")
    assert direct.decision == "ESCALATE"
    assert claim_triples(certificate(direct, "SOD-002")) == [[APPROVE_CLAIM, *MEMBERSHIP_PATH]]
    assert exceptions(membership) == exceptions(direct) == NO_CONTROL
    other = item(gold, "rv_as_other")
    assert (other.decision, other.exceptions) == ("CERTIFY", ())


def test_every_item_conferring_a_side_escalates(rev: CaseBuilder) -> None:
    review_world(rev)
    rev.assignment("as_post", "acct_ben_main", "ent_post", granted_on=day(-30))
    gold = review(rev.campaign("rm_ops", "as_post", "as_approve"))
    assert [i.decision for i in gold.items] == ["ESCALATE"] * 3
    assert len(certificate(item(gold, "rv_as_approve"), "SOD-002").claim_sets) == 2


def test_an_owned_service_account_adds_no_side_in_reviews(rev: CaseBuilder) -> None:
    """The service account's item has no holder, and its access isn't the owner's held access,
    so neither item escalates (AMBIGUITIES A15 for the holder)."""
    review_world(rev)
    rev.drop("as_approve")
    rev.account("acct_svc", "app_main", None, kind="service", owner_id="u_ben")
    rev.assignment("as_svc_approve", "acct_svc", "ent_approve", granted_on=day(-30))
    gold = review(rev.campaign("rm_ops", "as_svc_approve"))
    assert [(i.decision, i.findings, i.exceptions) for i in gold.items] == [("CERTIFY", (), ())] * 2


def test_expired_item_confers_no_side(rev: CaseBuilder) -> None:
    review_world(rev)
    rev.set("rm_ops", expires_on=day(-1))
    gold = review(rev.campaign("rm_ops", "as_approve"))
    expired = item(gold, "rv_rm_ops")
    assert expired.decision == "REVOKE"
    assert finding_rules(expired) == ["HYG-004"]
    assert item(gold, "rv_as_approve").decision == "CERTIFY"


def test_role_with_both_sides_escalates_on_its_own(rev: CaseBuilder) -> None:
    rev.entitlement("ent_post")
    rev.entitlement("ent_approve")
    rev.sod("SOD-002", ["ent_post"], ["ent_approve"])
    rev.role("role_toxic", entitlements=["ent_post", "ent_approve"])
    rev.membership("rm_toxic", "u_ben", "role_toxic", granted_on=day(-30))
    gold = review(rev.campaign("rm_toxic"))
    assert claim_triples(certificate(item(gold, "rv_rm_toxic"), "SOD-002")) == [
        [
            ("rm_toxic", "role_id", "role_toxic"),
            ("role_toxic", "entitlements", "ent_approve"),
            ("role_toxic", "entitlements", "ent_post"),
        ]
    ]


def test_two_paths_to_a_role_with_both_sides_are_separate_minimal_sets(rev: CaseBuilder) -> None:
    """The shape hypothesis found: role_ops reaches role_close directly and through role_mid,
    and role_close lists both sides. Pairing one path to each side would also give a set citing
    both paths, from which either path could be dropped with the rest still showing the
    conflict. Only the minimal sets remain, one path each, for SOD-002 and for MIT-002."""
    rev.entitlement("ent_post")
    rev.entitlement("ent_approve")
    rev.sod("SOD-002", ["ent_post"], ["ent_approve"])
    rev.role("role_close", entitlements=["ent_post", "ent_approve"])
    rev.role("role_mid", child_roles=["role_close"])
    rev.role("role_ops", child_roles=["role_close", "role_mid"])
    rev.membership("rm_ops", "u_ben", "role_ops", granted_on=day(-30))
    both_sides = [
        ("role_close", "entitlements", "ent_approve"),
        ("role_close", "entitlements", "ent_post"),
    ]
    direct = [("rm_ops", "role_id", "role_ops"), ("role_ops", "child_roles", "role_close")]
    nested = [
        ("rm_ops", "role_id", "role_ops"),
        ("role_ops", "child_roles", "role_mid"),
        ("role_mid", "child_roles", "role_close"),
    ]
    record = item(review(rev.campaign("rm_ops")), "rv_rm_ops")
    assert claim_triples(certificate(record, "SOD-002")) == [
        [*direct, *both_sides],
        [*nested, *both_sides],
    ]
    rev.control("mc_1", "SOD-002", "u_ben", expires_on=day(10))
    record = item(review(rev.campaign("rm_ops")), "rv_rm_ops")
    expiry = ("mc_1", "expires_on", "2026-10-20")
    assert claim_triples(certificate(record, "MIT-002")) == [
        [*direct, *both_sides, expiry],
        [*nested, *both_sides, expiry],
    ]


def test_mitigated_conflict_certifies_with_the_exception_recorded(rev: CaseBuilder) -> None:
    review_world(rev)
    rev.control("mc_1", "SOD-002", "u_ben", expires_on=day(31))
    gold = review(rev.campaign("rm_ops", "as_approve", "as_other"))
    for item_id in ("rv_rm_ops", "rv_as_approve"):
        record = item(gold, item_id)
        assert record.decision == "CERTIFY"
        assert record.findings == ()
        assert [(x.rule, x.for_rule, x.applies) for x in record.exceptions] == [
            ("MIT-001", "SOD-002", True)
        ]
    assert item(gold, "rv_as_other").exceptions == ()


def test_mit_002_renewal_window_boundary(rev: CaseBuilder) -> None:
    review_world(rev)
    rev.control("mc_1", "SOD-002", "u_ben", expires_on=day(30))
    gold = review(rev.campaign("rm_ops", "as_approve"))
    record = item(gold, "rv_as_approve")
    assert record.decision == "ESCALATE"
    assert finding_rules(record) == ["MIT-002"]
    assert claim_triples(certificate(record, "MIT-002")) == [
        [APPROVE_CLAIM, *MEMBERSHIP_PATH, ("mc_1", "expires_on", "2026-11-09")]
    ]


def test_expired_control_escalates_the_conflict(rev: CaseBuilder) -> None:
    review_world(rev)
    rev.control("mc_1", "SOD-002", "u_ben", granted_on=day(-300), expires_on=day(-2))
    gold = review(rev.campaign("rm_ops"))
    record = item(gold, "rv_rm_ops")
    assert record.decision == "ESCALATE"
    assert record.exceptions[0].applies is False
    assert claim_triples(certificate(record, "SOD-002")) == [
        [*MEMBERSHIP_PATH, APPROVE_CLAIM, ("mc_1", "expires_on", "2026-10-08")]
    ]
