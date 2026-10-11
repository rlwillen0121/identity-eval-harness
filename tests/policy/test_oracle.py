"""Oracle-level behaviour: precedence, caps, certificates, dispatch and the input contract."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import cast

import pytest

from identitybench.policy.oracle import (
    OracleInputError,
    certificate_alternatives,
    decide,
    decide_request,
    decide_review,
)
from identitybench.policy.registry import RULES
from identitybench.policy.rules import request_rules, review_rules, rules_for_tasks
from identitybench.policy.rules.base import RequestRule, ReviewRule
from identitybench.schemas.case import CaseView
from identitybench.schemas.enums import Task
from identitybench.schemas.gold import Certificate, Claim, JmlGold, RequestGold, ReviewItemGold
from tests.policy.builders import (
    AS_OF,
    CaseBuilder,
    assert_gold_consistent,
    certificate_rules,
    day,
    finding_rules,
)


def decide_checked(view: CaseView) -> RequestGold:
    gold = decide_request(view)
    assert_gold_consistent(view, gold)
    return gold


# ---- the effect table, written independently from SPEC §7.4 -------------------------------------

SPEC_EFFECTS: dict[str, tuple[str | None, str | None]] = {
    "LIFE-001": ("DENY", "REVOKE"),
    "LIFE-002": ("ESCALATE", None),
    "LIFE-003": ("DENY", "REVOKE"),
    "LIFE-004": ("CONDITION", None),
    "ELIG-001": ("DENY", "REVOKE"),
    "ELIG-002": ("DENY", "REVOKE"),
    "ELIG-003": ("DENY", "REVOKE"),
    "ELIG-004": ("DENY", None),
    "ELIG-005": ("DENY", "REVOKE"),
    **{f"SOD-00{i}": ("DENY", "ESCALATE") for i in range(1, 7)},
    "MIT-001": ("EXCEPTION", "EXCEPTION"),
    "MIT-002": (None, "ESCALATE"),
    "MIT-003": ("CONDITION", None),
    "APPR-001": ("ESCALATE", None),
    "APPR-002": ("ESCALATE", None),
    "APPR-003": ("DENY", None),
    "APPR-004": ("CONDITION", None),
    "APPR-005": (None, "ESCALATE"),
    "TEMP-001": ("CONDITION", None),
    "TEMP-002": ("CONDITION", None),
    "JUST-001": ("ESCALATE", None),
    "HYG-001": (None, "REVOKE"),
    "HYG-002": (None, "REVOKE"),
    "HYG-003": (None, "ESCALATE"),
    "HYG-004": (None, "REVOKE"),
    "HYG-005": (None, "REVOKE"),
}


@pytest.mark.parametrize("rule_id", sorted(SPEC_EFFECTS))
def test_registered_effects_match_the_spec_table(rule_id: str) -> None:
    rule = RULES[rule_id]
    request, review = SPEC_EFFECTS[rule_id]
    got_request = rule.effect_for(Task.access_request)
    got_review = rule.effect_for(Task.access_review)
    assert (got_request and got_request.value, got_review and got_review.value) == (request, review)
    assert rule.effect_for(Task.jml) is None
    predicate_request = request not in (None, "EXCEPTION")
    predicate_review = review not in (None, "EXCEPTION")
    assert isinstance(rule, RequestRule) is predicate_request
    assert isinstance(rule, ReviewRule) is predicate_review


def test_hook_lists_cover_exactly_the_predicate_rules() -> None:
    assert len(request_rules()) == sum(
        r not in (None, "EXCEPTION") for r, _ in SPEC_EFFECTS.values()
    )
    assert len(review_rules()) == sum(
        r not in (None, "EXCEPTION") for _, r in SPEC_EFFECTS.values()
    )
    ids = [r.id for r in rules_for_tasks({Task.access_request, Task.access_review})]
    assert ids == sorted(SPEC_EFFECTS, key=list(SPEC_EFFECTS).index)


def test_header_matches_the_spec_example() -> None:
    assert RULES["SOD-002"].header() == "SOD-002 (requests: DENY; reviews: ESCALATE)"


# ---- precedence ---------------------------------------------------------------------------------


def test_deny_beats_escalate_and_only_deny_findings_certify(req: CaseBuilder) -> None:
    req.set("u_ben", status="terminated")
    req.set("ent_req", risk_tier="high", data_classification="confidential", requestable=False)
    gold = decide_checked(req.request())
    assert gold.decision == "DENY"
    assert finding_rules(gold) == ["LIFE-001", "ELIG-004", "APPR-001", "JUST-001"]
    assert certificate_rules(gold) == ["LIFE-001", "ELIG-004"]
    assert gold.effective_duration_days is None


def test_escalate_beats_approve_and_caps_are_findings_only(req: CaseBuilder) -> None:
    req.set("u_ben", status="leave_of_absence")
    req.set("ent_req", risk_tier="high", privileged=True)
    gold = decide_checked(req.request(duration=30))
    assert gold.decision == "ESCALATE"
    assert finding_rules(gold) == ["LIFE-002", "APPR-001", "TEMP-001"]
    assert certificate_rules(gold) == ["LIFE-002", "APPR-001"]
    assert gold.effective_duration_days is None


# ---- caps ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("contract_end", "requested", "duration", "certified"),
    [
        (3, None, 4, ["LIFE-004"]),
        (9, None, 7, ["TEMP-001"]),
        (6, None, 7, ["LIFE-004", "TEMP-001"]),
        (6, 5, 5, []),
        (6, 7, 7, []),
        (0, 30, 1, ["LIFE-004"]),
    ],
)
def test_contractor_cap_with_privileged_cap(
    req: CaseBuilder,
    contract_end: int,
    requested: int | None,
    duration: int,
    certified: list[str],
) -> None:
    req.identity("u_ben", employment_type="contractor", contract_end_date=day(contract_end))
    req.set("ent_req", privileged=True)
    gold = decide_checked(req.request(duration=requested))
    assert (gold.decision, gold.effective_duration_days) == ("APPROVE", duration)
    assert finding_rules(gold) == ["LIFE-004", "TEMP-001"]
    assert certificate_rules(gold) == certified


@pytest.mark.parametrize(
    ("maximum", "duration", "certified"),
    [(3, 3, ["TEMP-002"]), (7, 7, ["TEMP-001", "TEMP-002"]), (30, 7, ["TEMP-001"])],
)
def test_privileged_cap_with_max_duration(
    req: CaseBuilder, maximum: int, duration: int, certified: list[str]
) -> None:
    req.set("ent_req", privileged=True, max_duration_days=maximum)
    gold = decide_checked(req.request())
    assert gold.effective_duration_days == duration
    assert certificate_rules(gold) == certified


def test_break_glass_cap_with_privileged_cap(req: CaseBuilder) -> None:
    req.set("ent_req", privileged=True, risk_tier="high")
    req.ticket("INC-1")
    gold = decide_checked(req.request(break_glass=True, ticket_ref="INC-1", duration=3))
    assert gold.effective_duration_days == 1
    assert finding_rules(gold) == ["APPR-004", "TEMP-001"]
    assert certificate_rules(gold) == ["APPR-004"]


# ---- certificate groups for scoring (SPEC §9.1 "Tied caps are alternatives", §13.1) -------------


def groups(gold: RequestGold | ReviewItemGold | JmlGold) -> list[list[str]]:
    return [[c.rule for c in group] for group in certificate_alternatives(gold)]


def test_tied_binding_caps_are_one_group_of_alternatives(req: CaseBuilder) -> None:
    """Citing either cap fully covers the approval, so they must not count as two reasons."""
    req.set("ent_req", privileged=True, max_duration_days=7)
    gold = decide_checked(req.request())
    assert certificate_rules(gold) == ["TEMP-001", "TEMP-002"]
    assert groups(gold) == [["TEMP-001", "TEMP-002"]]


def test_a_single_binding_cap_is_one_group(req: CaseBuilder) -> None:
    req.set("ent_req", privileged=True, max_duration_days=30)
    assert groups(decide_checked(req.request())) == [["TEMP-001"]]


@pytest.mark.parametrize("requested", [None, 5])
def test_default_approvals_have_no_groups(req: CaseBuilder, requested: int | None) -> None:
    """No cap at all, or a request shorter than every cap: nothing to certify."""
    if requested is not None:
        req.set("ent_req", privileged=True)
    gold = decide_checked(req.request(duration=requested))
    assert (gold.decision, gold.certificates, certificate_alternatives(gold)) == ("APPROVE", (), ())


def test_deny_and_escalate_certificates_are_separate_reasons(req: CaseBuilder) -> None:
    req.set("ent_req", requestable=False, risk_tier="high")
    req.set("u_ben", status="terminated")
    assert groups(decide_checked(req.request())) == [["LIFE-001"], ["ELIG-004"]]
    req.set("ent_req", requestable=True)
    req.set("u_ben", status="leave_of_absence")
    assert groups(decide_checked(req.request())) == [["LIFE-002"], ["APPR-001"]]


def test_review_and_plan_certificates_are_separate_reasons(req: CaseBuilder) -> None:
    req.assignment("as_old", "acct_ben_main", "ent_req", granted_on=day(-30), expires_on=day(-5))
    req.set("u_ben", status="terminated")
    (record,) = decide_review(req.campaign("as_old")).items
    assert groups(record) == [["LIFE-001"], ["HYG-004"]]
    claim = Claim(id="u_ben", field="status", value="terminated")
    plan = JmlGold(
        operations=(),
        certificates=(Certificate(rule="JML-003", claim_sets=((claim,),)),),
        horizon_end=AS_OF,
    )
    assert groups(plan) == [["JML-003"]]


# ---- dispatch -----------------------------------------------------------------------------------


def test_decide_dispatches_by_task(req: CaseBuilder, monkeypatch: pytest.MonkeyPatch) -> None:
    view = req.request()
    assert decide(view, Task.access_request) == decide_request(view)
    req.assignment("as_1", "acct_ben_main", "ent_req", granted_on=day(-5))
    campaign = req.campaign("as_1")
    assert decide(campaign, Task.access_review) == decide_review(campaign)
    # A stand-in JML module, so this test doesn't depend on the real one importing cleanly.
    planned = JmlGold(operations=(), horizon_end=AS_OF)
    stub = ModuleType("identitybench.policy.jml")
    stub.plan_jml = lambda v: planned if v is view else None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "identitybench.policy.jml", stub)
    assert decide(view, Task.jml) is planned


def test_decide_rejects_an_unknown_task(req: CaseBuilder) -> None:
    with pytest.raises(ValueError, match="unknown task 'plans'"):
        decide(req.request(), cast(Task, "plans"))


# ---- input contract -----------------------------------------------------------------------------


def test_request_input_errors(req: CaseBuilder) -> None:
    with pytest.raises(OracleInputError, match=r"view\.request"):
        decide_request(req.view())
    with pytest.raises(OracleInputError, match="u_nobody"):
        decide_request(req.request(beneficiary_id="u_nobody"))
    with pytest.raises(OracleInputError, match="ent_nope"):
        decide_request(req.request("ent_nope"))
    req.entitlement("ent_orphan_app", app_id="app_gone")
    with pytest.raises(OracleInputError, match="app_gone"):
        decide_request(req.request("ent_orphan_app"))


def test_assignment_on_a_missing_account_fails_closed(req: CaseBuilder) -> None:
    """Dropping the account behind a conflicting assignment must not turn DENY into APPROVE."""
    req.entitlement("ent_post")
    req.sod("SOD-002", ["ent_post"], ["ent_req"])
    req.app("app_gl")
    req.account("acct_gl", "app_gl", "u_ben")
    req.assignment("as_post", "acct_gl", "ent_post")
    assert decide_checked(req.request()).decision == "DENY"
    req.drop("acct_gl")
    with pytest.raises(OracleInputError, match=r"as_post \(account acct_gl\)"):
        decide_request(req.request())
    with pytest.raises(OracleInputError, match="acct_gl"):
        decide_review(req.campaign())


def test_dangling_assignments_are_rejected_even_without_sod_rules(req: CaseBuilder) -> None:
    """The check runs for every case, not only when some rule needs held access."""
    req.assignment("as_lost", "acct_gone", "ent_req")
    with pytest.raises(OracleInputError, match="acct_gone"):
        decide_request(req.request())


def test_missing_role_in_held_access_is_rejected(req: CaseBuilder) -> None:
    req.sod("SOD-002", ["ent_x"], ["ent_req"])
    req.membership("rm_1", "u_ben", "role_missing")
    with pytest.raises(OracleInputError, match="role_missing"):
        decide_request(req.request())


def test_missing_usage_record_makes_a_request_view_incomplete(req: CaseBuilder) -> None:
    """SPEC §9.2: no request rule reads usage, but a view missing a record for a provisioned
    pair is incomplete, so it is refused (conformance A25)."""
    req.entitlement("ent_view")
    req.assignment("as_view", "acct_ben_main", "ent_view")
    assert decide_checked(req.request()).decision == "APPROVE"
    req.no_usage("acct_ben_main", "ent_view")
    message = r"no usage record for \(account acct_ben_main, entitlement ent_view\)"
    with pytest.raises(OracleInputError, match=message):
        decide_request(req.request())


def test_membership_source_must_match_its_role_kind_in_requests(req: CaseBuilder) -> None:
    """SPEC §9.2: the view is rejected even when the membership is someone else's and no rule
    would read it."""
    req.identity("u_peer")
    req.role("role_base", kind="birthright", attribute_rule=None)
    req.membership("rm_peer", "u_peer", "role_base", source="request")
    with pytest.raises(OracleInputError, match="rm_peer has source 'request' but its role"):
        decide_request(req.request())
    req.set("rm_peer", source="birthright")
    assert decide_checked(req.request()).decision == "APPROVE"


def test_contradictory_controls_and_packs_are_rejected(req: CaseBuilder) -> None:
    req.entitlement("ent_x")
    req.assignment("as_x", "acct_ben_main", "ent_x")
    req.sod("SOD-002", ["ent_x"], ["ent_req"])
    req.control("mc_1", "SOD-002", "u_ben")
    req.control("mc_2", "SOD-002", "u_ben")
    with pytest.raises(OracleInputError, match="several controls"):
        decide_request(req.request())
    req.drop("mc_2")
    req.sod("SOD-007", ["ent_x"], ["ent_req"])
    with pytest.raises(OracleInputError, match="unknown SoD rule"):
        decide_request(req.request())


def test_json_round_trip_gives_the_same_gold(req: CaseBuilder) -> None:
    """SPEC §9.2: the oracle re-run on the serialized-then-parsed view must agree."""
    req.entitlement("ent_post")
    req.role("role_close", entitlements=["ent_post"])
    req.membership("rm_1", "u_ben", "role_close")
    req.sod("SOD-002", ["ent_post"], ["ent_req"])
    req.control("mc_1", "SOD-002", "u_ben", expires_on=day(-1))
    view = req.request(duration=10)
    parsed = CaseView.model_validate_json(view.model_dump_json())
    assert decide_request(parsed) == decide_request(view)
