"""scoring.tooluse: certificate retrieval (coarse and strict), lucky guesses, calls per case and
over-budget rate, on hand-built cases whose numbers are worked out by hand from SPEC §8.6, plus
strict retrieval on traces a real ``render.tools.ToolEnvironment`` recorded."""

from __future__ import annotations

import math
from typing import Any

import pytest

from identitybench.render.tools import ToolEnvironment
from identitybench.schemas.case import (
    Campaign,
    CampaignScopeKind,
    CaseView,
    ReviewItem,
    ReviewItemKind,
)
from identitybench.schemas.enums import ReviewDecision, Task
from identitybench.schemas.gold import Certificate, Claim, ReviewGold, ReviewItemGold
from identitybench.schemas.predictions import Prediction, ToolTrace
from identitybench.scoring.metrics import Resampling
from identitybench.scoring.settings import ScoreSettings
from identitybench.scoring.tooluse import (
    ENDED_BY_MODEL,
    TOOL_CASE_UNITS,
    ToolUnit,
    available_ids,
    certificate_retrieved,
    header_ids,
    tool_case_record,
    tool_case_records,
    tool_scores,
    tool_units,
)
from identitybench.world.entities import RiskTier
from identitybench.world.generator import build_world
from tests.render.world_cases import request_cases
from tests.scoring.builders import (
    AS_OF,
    ORG,
    POLICY,
    claim_dict,
    leaver_case,
    make_case,
    oracle_output,
    predict,
    request_case,
    request_output,
    review_output,
)

STATUS = Claim(id="u_1", field="status", value="terminated")
MEMBERSHIP = Claim(id="rm_1", field="role_id", value="role_x")
ROLE = Claim(id="role_x", field="kind", value="business")
DENY_CERT = Certificate(rule="LIFE-001", claim_sets=((STATUS,), (MEMBERSHIP, ROLE)))
"""Two alternative claim sets: the beneficiary's status, or a membership and its role."""

FAST = ScoreSettings(n_boot=50)


def trace(*ids: str, calls: int | None = None, requested: tuple[str, ...] = ()) -> ToolTrace:
    names = ("get_identity",) * (len(ids) if calls is None else calls)
    return ToolTrace(calls=len(names), fetched_ids=ids, tool_names=names, requested_ids=requested)


def deny_case(case_id: str = "c1", **fields: Any):
    return request_case(case_id, "DENY", certificates=[DENY_CERT], **fields)


def traced(
    case: Any, output: dict[str, Any] | None, tool_trace: ToolTrace, **kw: Any
) -> Prediction:
    return predict(case, output, tool_trace=tool_trace, **kw)


# --------------------------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------------------------


def test_one_full_claim_set_of_one_certificate_is_a_retrieval() -> None:
    assert certificate_retrieved([DENY_CERT], {"u_1"})
    assert certificate_retrieved([DENY_CERT], {"rm_1", "role_x", "u_9"})
    assert not certificate_retrieved([DENY_CERT], {"rm_1"})  # half of the second set
    assert not certificate_retrieved([], {"u_1"})  # nothing to retrieve
    other = Certificate(
        rule="SOD-001", claim_sets=((Claim(id="as_1", field="source", value="x"),),)
    )
    assert certificate_retrieved([other, DENY_CERT], {"as_1"})


def test_the_task_header_counts_as_fetched() -> None:
    """Claims on the request, campaign, items or event: the prompt states them."""
    case = request_case(
        "c1",
        "DENY",
        certificates=[
            Certificate(
                rule="APPR-001",
                claim_sets=((Claim(id="req_1", field="is_break_glass", value=False),),),
            )
        ],
    )
    assert header_ids(case.view) == {"req_1"}
    assert available_ids(case, trace()) == {"req_1"}
    [unit] = tool_units(case, traced(case, request_output("DENY"), trace()))
    assert unit.retrieved and unit.correct and not unit.lucky


def test_campaign_and_event_header_ids() -> None:
    review = review_case_with_certificates()
    assert header_ids(review.view) == {"camp_r1", "rv_1", "rv_2", "rv_3"}
    assert header_ids(leaver_case("j1").view) == {"ev_1"}


def test_request_units_are_retrieved_lucky_or_neither() -> None:
    case = deny_case()
    deny = request_output("DENY")

    def unit(output: dict[str, Any], *ids: str) -> ToolUnit:
        [only] = tool_units(case, traced(case, output, trace(*ids)))
        return only

    found = unit(deny, "u_2", "u_1")
    assert (found.has_certificate, found.retrieved, found.correct, found.lucky) == (
        True,
        True,
        True,
        False,
    )
    lucky = unit(deny, "rm_1")
    assert (lucky.retrieved, lucky.correct, lucky.lucky) == (False, True, True)
    wrong = unit(request_output("APPROVE"), "rm_1")
    assert (wrong.retrieved, wrong.correct, wrong.lucky) == (False, False, False)
    assert wrong.cluster == "c1" and wrong.item_id is None and wrong.task is Task.access_request


def test_a_default_outcome_has_nothing_to_retrieve_and_is_never_lucky() -> None:
    case = request_case("c1", "APPROVE", requested=7, duration=7)
    [unit] = tool_units(case, traced(case, request_output("APPROVE", cap=7), trace()))
    assert unit.correct and not unit.has_certificate and not unit.retrieved and not unit.lucky


def test_an_invalid_prediction_can_still_have_retrieved() -> None:
    """Retrieval reads the trace, whatever the status (here: over budget)."""
    case = deny_case()
    [unit] = tool_units(case, traced(case, None, trace("u_1"), status="over_budget"))
    assert unit.retrieved and not unit.correct and not unit.lucky


def test_units_need_a_trace() -> None:
    case = deny_case()
    with pytest.raises(ValueError, match="no tool trace"):
        tool_units(case, predict(case, request_output("DENY")))
    with pytest.raises(ValueError, match="no tool trace"):
        tool_case_record(case, predict(case, request_output("DENY")))


def review_case_with_certificates(case_id: str = "r1", **fields: Any):
    """Three items: rv_1 REVOKE (certificate on as_1 and use_1), rv_2 CERTIFY (default),
    rv_3 ESCALATE (certificate on mc_1)."""
    items = (
        ReviewItem(item_id="rv_1", kind=ReviewItemKind.assignment, ref="as_1"),
        ReviewItem(item_id="rv_2", kind=ReviewItemKind.membership, ref="rm_2"),
        ReviewItem(item_id="rv_3", kind=ReviewItemKind.membership, ref="rm_3"),
    )
    campaign = Campaign(
        campaign_id=f"camp_{case_id}",
        reviewer_id="u_reviewer",
        scope_kind=CampaignScopeKind.application,
        scope_ref="app_1",
        items=items,
    )
    dormant = Certificate(
        rule="HYG-001",
        claim_sets=(
            (
                Claim(id="as_1", field="source", value="request"),
                Claim(id="use_1", field="telemetry", value="available"),
            ),
        ),
    )
    conflict = Certificate(
        rule="SOD-002", claim_sets=((Claim(id="mc_1", field="expires_on", value="2026-01-01"),),)
    )
    gold = ReviewGold(
        items=(
            ReviewItemGold(
                item_id="rv_1",
                decision=ReviewDecision.REVOKE,
                risk_tier=RiskTier.medium,
                certificates=(dormant,),
            ),
            ReviewItemGold(item_id="rv_2", decision=ReviewDecision.CERTIFY, risk_tier=RiskTier.low),
            ReviewItemGold(
                item_id="rv_3",
                decision=ReviewDecision.ESCALATE,
                risk_tier=RiskTier.high,
                certificates=(conflict,),
            ),
        )
    )
    view = CaseView(as_of=AS_OF, org_name=ORG, policy_pack=POLICY, campaign=campaign)
    return make_case(case_id, view, gold, task=Task.access_review, **fields)


def test_review_items_are_units_clustered_by_campaign() -> None:
    case = review_case_with_certificates()
    output = review_output([("rv_1", "REVOKE"), ("rv_2", "CERTIFY")])  # rv_3 missing
    units = tool_units(case, traced(case, output, trace("as_1", "use_1", "mc_9")))
    assert [u.item_id for u in units] == ["rv_1", "rv_2", "rv_3"]
    assert [u.has_certificate for u in units] == [True, False, True]
    assert [u.retrieved for u in units] == [True, False, False]
    assert [u.correct for u in units] == [True, True, False]  # a missing item is INVALID
    assert {u.cluster for u in units} == {"r1"}


def test_jml_units_use_the_exact_timeline_match() -> None:
    case = leaver_case("j1")
    [unit] = tool_units(case, traced(case, oracle_output(case), trace("u_1")))
    assert (unit.task, unit.has_certificate, unit.retrieved, unit.correct) == (
        Task.jml,
        True,
        True,
        True,
    )
    wrong = {"operations": [], "citations": {"rules": [], "evidence": []}}
    [miss] = tool_units(case, traced(case, wrong, trace()))
    assert not miss.correct and not miss.retrieved and not miss.lucky


# --------------------------------------------------------------------------------------------
# Per-case records
# --------------------------------------------------------------------------------------------


def test_a_case_record_counts_the_case() -> None:
    case = review_case_with_certificates()
    output = review_output([("rv_1", "REVOKE"), ("rv_2", "CERTIFY"), ("rv_3", "ESCALATE")])
    record = tool_case_record(
        case, traced(case, output, trace("mc_1", "as_1", calls=7, requested=("mc_1",)))
    )
    assert record.model_dump() == {
        "case_id": "r1",
        "task": Task.access_review,
        "variant": "base",
        "status": "ok",
        "calls": 7,
        "over_budget": False,
        "fetched": 2,
        "requested": 1,
        "units": 3,
        "certificate_units": 2,
        "retrieved_units": 1,
        "strictly_retrieved_units": 1,
        "correct_units": 3,
        "lucky_units": 1,
    }
    over = tool_case_record(case, traced(case, None, trace(calls=25), status="over_budget"))
    assert over.over_budget and over.status == "over_budget" and over.correct_units == 0


def test_records_cover_traced_cases_in_id_order() -> None:
    cases = [deny_case("c3"), deny_case("c1"), deny_case("c2")]
    predictions = [
        traced(cases[0], request_output("DENY"), trace("u_1")),
        predict(cases[1], request_output("DENY")),  # no trace: a context-mode prediction
        traced(cases[2], request_output("DENY"), trace()),
    ]
    records = tool_case_records(cases, predictions)
    assert [r.case_id for r in records] == ["c2", "c3"]
    assert tool_case_records(cases, [], config_id="cfg") == []
    with pytest.raises(ValueError, match="duplicate case"):
        tool_case_records([cases[0], cases[0]], predictions[:1])


# --------------------------------------------------------------------------------------------
# Aggregates
# --------------------------------------------------------------------------------------------


def test_request_aggregates() -> None:
    """Four base requests: calls 2, 4, 6 and 25 (over budget); a twin and an untraced case are
    left out."""
    base = [deny_case(f"c{i}") for i in range(1, 5)]
    approve = request_case("c5", "APPROVE", requested=7, duration=7)
    twin = deny_case("c6", variant="counterfactual", twin_of="c1")
    deny = request_output("DENY")
    predictions = [
        traced(base[0], deny, trace("u_1", calls=2)),  # retrieved, correct
        traced(base[1], deny, trace("rm_1", calls=4)),  # lucky
        traced(base[2], request_output("APPROVE"), trace(calls=6)),  # wrong, nothing fetched
        traced(base[3], None, trace("rm_1", "role_x", calls=25), status="over_budget"),
        traced(twin, deny, trace("u_1", calls=3)),
    ]
    scores = tool_scores([*base, approve, twin], predictions, settings=FAST)
    assert (scores.n_cases, scores.n_untraced) == (4, 1)
    assert scores.reviews is None and scores.jml is None
    requests = scores.requests
    assert requests is not None
    calls = requests.calls
    assert calls.n_cases == 4
    assert calls.mean_calls.point == pytest.approx(37 / 4)
    assert calls.p50_calls == pytest.approx(5.0)
    assert calls.p95_calls == pytest.approx(6 + 0.85 * 19)  # linear between 6 and 25
    assert calls.over_budget_rate.point == pytest.approx(0.25)
    assert calls.mean_calls.units == "base request case" and calls.mean_calls.cluster is None
    retrieval = requests.retrieval
    assert (retrieval.n_units, retrieval.n_certificate_units, retrieval.n_lucky) == (4, 4, 1)
    assert retrieval.retrieval_rate.point == pytest.approx(2 / 4)  # c1 and the over-budget c4
    assert retrieval.lucky_guess_rate.point == pytest.approx(1 / 2)  # c2 of the correct c1, c2
    assert retrieval.lucky_guess_rate.denominator == 2
    assert scores.calls.mean_calls.units == TOOL_CASE_UNITS.units
    assert scores.calls.n_cases == 4


def test_review_and_jml_aggregates_use_their_units() -> None:
    review = review_case_with_certificates()
    leaver = leaver_case("j1")
    output = review_output([("rv_1", "REVOKE"), ("rv_2", "CERTIFY"), ("rv_3", "ESCALATE")])
    predictions = [
        traced(review, output, trace("as_1", "use_1", calls=10)),
        traced(leaver, oracle_output(leaver), trace(calls=3)),
    ]
    scores = tool_scores([review, leaver], predictions, settings=FAST)
    assert scores.requests is None
    reviews, jml = scores.reviews, scores.jml
    assert reviews is not None and jml is not None
    assert reviews.calls.mean_calls.units == "campaign"
    assert reviews.retrieval.retrieval_rate.units == "review item"
    assert reviews.retrieval.retrieval_rate.cluster == "campaign"
    assert reviews.retrieval.retrieval_rate.point == pytest.approx(1 / 2)
    assert reviews.retrieval.lucky_guess_rate.point == pytest.approx(1 / 2)  # rv_3
    assert jml.retrieval.retrieval_rate.units == "JML event"
    assert jml.retrieval.retrieval_rate.point == 0.0
    assert jml.retrieval.lucky_guess_rate.point == 1.0  # exact plan, nothing fetched
    assert scores.calls.mean_calls.point == pytest.approx(6.5)


def test_empty_populations_are_na() -> None:
    case = request_case("c1", "APPROVE", requested=7, duration=7)
    scores = tool_scores([case], [], settings=FAST, config_id="cfg")
    assert (scores.n_cases, scores.n_untraced) == (0, 1)
    assert scores.requests is None
    assert scores.calls.mean_calls.point is None and scores.calls.p50_calls is None
    assert scores.calls.over_budget_rate.is_na
    defaults = tool_scores(
        [case], [traced(case, request_output("APPROVE", cap=7), trace())], settings=FAST
    )
    assert defaults.requests is not None
    assert defaults.requests.retrieval.retrieval_rate.is_na  # no certificate anywhere
    assert defaults.requests.retrieval.lucky_guess_rate.is_na


def test_intervals_do_not_depend_on_prediction_order() -> None:
    cases = [deny_case(f"c{i}") for i in range(1, 7)]
    predictions = [
        traced(c, request_output("DENY"), trace("u_1" if i % 2 else "rm_1", calls=i))
        for i, c in enumerate(cases)
    ]
    settings = ScoreSettings(n_boot=200, seed=3)
    forward = tool_scores(cases, predictions, settings=settings)
    backward = tool_scores(list(reversed(cases)), list(reversed(predictions)), settings=settings)
    assert forward == backward
    requests = forward.requests
    assert requests is not None
    rate = requests.retrieval.retrieval_rate
    assert rate.point is not None and rate.low is not None and rate.high is not None
    assert rate.low <= rate.point <= rate.high
    assert math.isclose(rate.alpha, Resampling().alpha)


def test_strict_retrieval_needs_every_entity_requested() -> None:
    """A listing that carried the claim's records along counts coarsely, not strictly: the
    agent has to have passed each claim's entity ID itself (or the entity is the header's)."""
    case = deny_case()
    deny = request_output("DENY")

    def unit(fetched: tuple[str, ...], requested: tuple[str, ...]) -> ToolUnit:
        [only] = tool_units(case, traced(case, deny, trace(*fetched, requested=requested)))
        return only

    listed = unit(("rm_1", "role_x"), ())  # one listing fetched both records
    assert listed.retrieved and not listed.strictly_retrieved
    asked = unit(("rm_1", "role_x"), ("role_x", "rm_1"))
    assert asked.retrieved and asked.strictly_retrieved
    half = unit(("u_9", "rm_1", "role_x"), ("u_9", "role_x"))
    assert half.retrieved and not half.strictly_retrieved
    alone = unit(("u_1",), ("u_1",))  # the first claim set alone
    assert alone.strictly_retrieved
    named = unit((), ("u_1", "rm_1", "role_x"))  # asked for every entity, got no record back
    assert not named.retrieved and not named.strictly_retrieved
    both = trace("u_1", "u_2", requested=("u_2", "u_3"))
    assert available_ids(case, both, strict=True) == {"u_2", "req_1"}
    assert available_ids(case, both, strict=True) <= available_ids(case, both)

    header_only = request_case(
        "c2",
        "DENY",
        certificates=[
            Certificate(
                rule="APPR-001",
                claim_sets=((Claim(id="req_1", field="is_break_glass", value=False),),),
            )
        ],
    )
    [unit_c2] = tool_units(header_only, traced(header_only, deny, trace()))
    assert unit_c2.strictly_retrieved  # the header's own object: nothing to request


def test_strict_retrieval_aggregates_beside_the_coarse_rate() -> None:
    cases = [deny_case(f"c{i}") for i in range(1, 5)]
    deny = request_output("DENY")
    predictions = [
        traced(cases[0], deny, trace("u_1", requested=("u_1",))),
        traced(cases[1], deny, trace("u_1")),  # fetched by a listing only
        traced(cases[2], deny, trace("rm_1", "role_x", requested=("rm_1", "role_x"))),
        traced(cases[3], deny, trace()),
    ]
    scores = tool_scores(cases, predictions, settings=FAST)
    assert scores.requests is not None
    retrieval = scores.requests.retrieval
    assert retrieval.retrieval_rate.point == pytest.approx(3 / 4)
    assert retrieval.strict_retrieval_rate.point == pytest.approx(2 / 4)
    assert retrieval.strict_retrieval_rate.denominator == 4
    for case, prediction in zip(cases, predictions, strict=True):
        assert all(u.retrieved for u in tool_units(case, prediction) if u.strictly_retrieved)


def test_the_aggregates_cover_the_cases_the_model_ended() -> None:
    """A provider error or a refusal (often after no call at all, or midway through a search)
    says nothing about how the agent explores: those cases are left out of every aggregate,
    retrieval included, and counted apart. Their per-case records stay."""
    assert {status.value for status in ENDED_BY_MODEL} == {"ok", "invalid", "over_budget"}
    cases = [deny_case(f"c{i}") for i in range(1, 6)]
    deny = request_output("DENY")
    predictions = [
        traced(cases[0], deny, trace("u_1", calls=4)),
        traced(cases[1], None, trace(calls=6), status="invalid"),
        traced(cases[2], None, trace(calls=25), status="over_budget"),
        traced(cases[3], None, trace(calls=0), status="error"),
        traced(cases[4], None, trace("u_1", calls=2), status="refused"),
    ]
    scores = tool_scores(cases, predictions, settings=FAST)
    assert scores.n_cases == 5
    for calls in (scores.calls, scores.requests.calls if scores.requests else None):
        assert calls is not None
        assert (calls.n_cases, calls.n_excluded) == (3, 2)
        assert calls.mean_calls.point == pytest.approx(35 / 3)
        assert calls.p50_calls == pytest.approx(6.0)
        assert calls.over_budget_rate.point == pytest.approx(1 / 3)
    assert scores.requests is not None
    retrieval = scores.requests.retrieval
    assert retrieval.n_units == 3  # the refused c5 fetched u_1, but is left out
    assert retrieval.retrieval_rate.point == pytest.approx(1 / 3)
    assert retrieval.lucky_guess_rate.denominator == 1  # c1, the one correct unit
    records = tool_case_records(cases, predictions)
    assert [r.status for r in records] == ["ok", "invalid", "over_budget", "error", "refused"]

    failed_only = tool_scores(cases[3:], predictions[3:], settings=FAST)
    assert failed_only.n_cases == 2 and failed_only.requests is not None
    assert (failed_only.calls.n_cases, failed_only.calls.n_excluded) == (0, 2)
    assert failed_only.requests.retrieval.n_units == 0
    assert failed_only.requests.retrieval.retrieval_rate.is_na


@pytest.fixture(scope="module")
def world_requests() -> tuple[Any, list[Any]]:
    world = build_world("startup", 11)
    return world, request_cases(world, 6, 11)


def test_an_id_named_but_never_returned_is_not_strictly_retrieved(
    world_requests: tuple[Any, list[Any]],
) -> None:
    """``list_access(u)`` never returns ``u``'s record, ``get_usage(a, entitlement_id=e)``
    returns neither ``a`` nor ``e``, and an unknown ID returns nothing: the agent named those
    IDs but never saw the facts behind them (``u``'s status, ``e``'s ``privileged`` flag), so
    they don't count, and strict retrieval never exceeds coarse retrieval. Counting every named
    ID instead would call most of these certificates strictly retrieved."""
    world, cases = world_requests
    named_only = 0
    for case in cases:
        request = case.view.request
        assert request is not None
        env = ToolEnvironment(case, world)
        env.call("list_access", {"identity_id": request.beneficiary_id})
        env.call("get_usage", {"account_id": "acct_none", "entitlement_id": request.entitlement_id})
        env.call("get_identity", {"identity_id": "u_missing"})
        names = ("list_access", "get_usage", "get_identity")
        recorded = ToolTrace(
            calls=3,
            fetched_ids=env.fetched_ids,
            tool_names=names,
            requested_ids=env.requested_ids,
        )
        unreturned = {request.beneficiary_id, request.entitlement_id, "u_missing"}
        assert unreturned <= set(recorded.requested_ids)
        assert not unreturned & set(recorded.fetched_ids)
        strict = available_ids(case, recorded, strict=True)
        assert not unreturned & strict
        assert strict <= available_ids(case, recorded)
        certificates = case.gold.certificates
        named_only += certificate_retrieved(
            certificates, set(recorded.requested_ids) | header_ids(case.view)
        )
        [unit] = tool_units(case, traced(case, None, recorded, status="invalid"))
        assert not unit.strictly_retrieved and not unit.retrieved

        env.call("get_records", {"record_ids": [request.beneficiary_id, request.entitlement_id]})
        fetched = ToolTrace(
            calls=4,
            fetched_ids=env.fetched_ids,
            tool_names=(*names, "get_records"),
            requested_ids=env.requested_ids,
        )
        assert {request.beneficiary_id, request.entitlement_id} <= available_ids(
            case, fetched, strict=True
        )
        [unit] = tool_units(case, traced(case, None, fetched, status="invalid"))
        assert unit.retrieved or not unit.strictly_retrieved
    assert named_only >= 3  # the old rule's false positives: the test isn't vacuous


def test_claims_helpers_match_by_entity_only() -> None:
    """Retrieval is about having fetched the entity; whether the claim's value is right is
    grounding's business (SPEC §13.1)."""
    case = deny_case()
    output = request_output("DENY", rules=["LIFE-001"], evidence=[claim_dict("u_1", "status", "x")])
    [unit] = tool_units(case, traced(case, output, trace("u_1")))
    assert unit.retrieved
