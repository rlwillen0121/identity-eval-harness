"""runner.parse: one INVALID decision per response, per-item review validation, repair re-asks."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from identitybench.providers.base import Message, ModelRequest, ModelResponse, Role
from identitybench.runner.parse import (
    MAX_REASON_ERRORS,
    REQUIRED_KEYS,
    UNKNOWN_ITEM,
    ParseResult,
    answer_text,
    needs_repair,
    parse_response,
    repair_improves,
    repair_request,
)
from identitybench.schemas.case import (
    Campaign,
    CampaignScopeKind,
    Case,
    CaseView,
    ReviewItem,
    ReviewItemKind,
)
from identitybench.schemas.enums import (
    RequestDecision,
    ReviewDecision,
    Split,
    Suite,
    Task,
    Tier,
)
from identitybench.schemas.gold import ReviewGold, ReviewItemGold
from identitybench.schemas.ops import OpType
from identitybench.schemas.outputs import JmlOutput, RequestOutput, ReviewOutput
from identitybench.schemas.predictions import PredictionStatus
from identitybench.world.entities import PolicyPack, RiskTier
from tests.runner._cases import AS_OF, make_case

REQUEST_ANSWER: dict[str, Any] = {
    "decision": "DENY",
    "conditions": None,
    "citations": {"rules": ["SOD-002"], "evidence": []},
    "rationale": "Toxic combination.",
    "confidence": 0.8,
}


def review_case(
    case_id: str = "c_rev",
    item_ids: Sequence[str] = ("rv_1", "rv_2", "rv_3"),
    split: Split = Split.test,
) -> Case:
    """A review case whose campaign lists ``item_ids`` (gold: certify everything)."""
    campaign = Campaign(
        campaign_id=f"camp_{case_id}",
        reviewer_id="u_9",
        scope_kind=CampaignScopeKind.application,
        scope_ref="app_1",
        items=tuple(
            ReviewItem(item_id=i, kind=ReviewItemKind.membership, ref=f"rm_{i}") for i in item_ids
        ),
    )
    view = CaseView(
        as_of=AS_OF,
        org_name="Northwind Analytics",
        policy_pack=PolicyPack(org_name="Northwind Analytics", sod=()),
        campaign=campaign,
    )
    gold = ReviewGold(
        items=tuple(
            ReviewItemGold(item_id=i, decision=ReviewDecision.CERTIFY, risk_tier=RiskTier.low)
            for i in item_ids
        )
    )
    return Case(
        case_id=case_id,
        task=Task.access_review,
        suite=Suite.reviews,
        split=split,
        world_id="w_1",
        family_id=f"fam_{case_id}",
        tier=Tier.L2,
        view=view,
        gold=gold,
    )


def review_item(item_id: str, decision: str = "CERTIFY") -> dict[str, Any]:
    return {
        "item_id": item_id,
        "decision": decision,
        "citations": {"rules": [], "evidence": []},
        "rationale": "ok",
        "confidence": 0.9,
    }


REQUEST = make_case("c_req")
JML = make_case("c_jml", task=Task.jml, suite=Suite.jml)


# --------------------------------------------------------------------------------------------
# Statuses
# --------------------------------------------------------------------------------------------


def test_native_structured_output_is_validated() -> None:
    result = parse_response(REQUEST, ModelResponse(parsed=REQUEST_ANSWER))
    assert result.ok and result.status is PredictionStatus.ok
    assert isinstance(result.output, RequestOutput)
    assert result.output.decision is RequestDecision.DENY
    assert result.invalid_reason is None and dict(result.item_errors) == {}


def test_parsed_wins_over_text() -> None:
    text = json.dumps({**REQUEST_ANSWER, "decision": "APPROVE"})
    result = parse_response(REQUEST, ModelResponse(text=text, parsed=REQUEST_ANSWER))
    assert isinstance(result.output, RequestOutput)
    assert result.output.decision is RequestDecision.DENY


def test_json_is_extracted_from_prose_and_fences() -> None:
    text = f"Here is my decision:\n```json\n{json.dumps(REQUEST_ANSWER)}\n```\nThanks."
    result = parse_response(REQUEST, ModelResponse(text=text))
    assert isinstance(result.output, RequestOutput)
    assert result.output.decision is RequestDecision.DENY


def test_a_trailing_aside_does_not_hide_the_answer() -> None:
    """Extraction prefers the last object holding the task's required keys."""
    text = f'{json.dumps(REQUEST_ANSWER)}\nNote: see {{"rule": "LIFE-001"}}'
    result = parse_response(REQUEST, ModelResponse(text=text))
    assert result.ok


def test_required_keys_are_the_output_models_required_fields() -> None:
    assert {
        Task.access_request: ("decision",),
        Task.access_review: ("items",),
        Task.jml: ("operations",),
    } == REQUIRED_KEYS


def test_an_answer_without_optional_keys_is_valid() -> None:
    result = parse_response(REQUEST, ModelResponse(text='{"decision": "ESCALATE"}'))
    assert isinstance(result.output, RequestOutput)
    assert result.output.decision is RequestDecision.ESCALATE


def test_out_of_range_confidence_is_dropped_not_invalid() -> None:
    result = parse_response(REQUEST, ModelResponse(parsed={**REQUEST_ANSWER, "confidence": 7}))
    assert isinstance(result.output, RequestOutput)
    assert result.output.confidence is None


def test_provider_error_is_error() -> None:
    result = parse_response(REQUEST, ModelResponse(error="HTTP 503 after 4 attempts"))
    assert result.status is PredictionStatus.error
    assert result.invalid_reason == "HTTP 503 after 4 attempts"
    assert result.output is None


def test_refusal_is_refused_even_with_an_answer() -> None:
    response = ModelResponse(parsed=REQUEST_ANSWER, refused=True, stop_reason="refusal")
    result = parse_response(REQUEST, response)
    assert result.status is PredictionStatus.refused
    assert result.output is None
    assert result.invalid_reason == "model refused (stop_reason: refusal)"


def test_error_takes_precedence_over_refusal() -> None:
    result = parse_response(REQUEST, ModelResponse(error="boom", refused=True))
    assert result.status is PredictionStatus.error


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (ModelResponse(text=None), "empty response"),
        (ModelResponse(text="   \n"), "empty response"),
        (ModelResponse(text="I would deny this."), "no JSON object in the response"),
        (ModelResponse(text='{"decision": "DENY"'), "no JSON object in the response"),
        (
            ModelResponse(text='{"decision": "DEN', stop_reason="max_tokens"),
            "no JSON object in the response (stop_reason: max_tokens)",
        ),
    ],
)
def test_unparseable_text_is_invalid(response: ModelResponse, reason: str) -> None:
    result = parse_response(REQUEST, response)
    assert result.status is PredictionStatus.invalid
    assert result.invalid_reason == reason
    assert result.output is None


def test_schema_violation_is_invalid_with_the_field_named() -> None:
    result = parse_response(REQUEST, ModelResponse(parsed={**REQUEST_ANSWER, "decision": "OK"}))
    assert result.status is PredictionStatus.invalid
    assert result.invalid_reason is not None
    assert result.invalid_reason.startswith("decision: Input should be")


def test_a_wrong_task_shape_is_invalid() -> None:
    result = parse_response(REQUEST, ModelResponse(parsed={"items": []}))
    assert result.status is PredictionStatus.invalid
    assert result.invalid_reason == "decision: Field required"


@pytest.mark.parametrize("case", [REQUEST, review_case(), JML], ids=lambda c: c.task.value)
@pytest.mark.parametrize(
    ("parsed", "found"), [([1, 2], "list"), ("DENY", "str"), (3, "int")], ids=str
)
def test_native_output_that_is_not_an_object_is_invalid(
    case: Case, parsed: Any, found: str
) -> None:
    """``parsed`` is typed as a dict, but a hand-edited or foreign recording can hold anything:
    that is an INVALID answer, never a crash of the whole run."""
    result = parse_response(case, ModelResponse(parsed=parsed))
    assert result.status is PredictionStatus.invalid
    assert result.invalid_reason == f"expected a JSON object, found a {found}"


def test_long_error_lists_are_truncated() -> None:
    bad_evidence = [{"id": 1}] * 5  # each claim lacks id-as-string and field
    result = parse_response(
        REQUEST,
        ModelResponse(parsed={"decision": "DENY", "citations": {"evidence": bad_evidence}}),
    )
    assert result.invalid_reason is not None
    assert result.invalid_reason.count(";") == MAX_REASON_ERRORS
    assert result.invalid_reason.endswith("more)")


# --------------------------------------------------------------------------------------------
# Reviews
# --------------------------------------------------------------------------------------------


def test_review_items_are_validated_one_by_one() -> None:
    """Missing (rv_3), duplicated (rv_1) and invalid items: valid ones are kept in order,
    duplicates are kept for the scorer, invalid ones are dropped with a reason."""
    items = [
        review_item("rv_1"),
        review_item("rv_1", "REVOKE"),
        review_item("rv_2", "MAYBE"),
        {"decision": "CERTIFY"},
        "rv_4 looks fine",
        review_item("rv_9"),
    ]
    result = parse_response(review_case(), ModelResponse(parsed={"items": items}))
    assert result.status is PredictionStatus.ok
    assert isinstance(result.output, ReviewOutput)
    assert [(i.item_id, i.decision) for i in result.output.items] == [
        ("rv_1", ReviewDecision.CERTIFY),
        ("rv_1", ReviewDecision.REVOKE),
        ("rv_9", ReviewDecision.CERTIFY),
    ]
    assert set(result.item_errors) == {"rv_2", "#3", "#4"}
    assert result.item_errors["rv_2"].startswith("decision: Input should be")
    assert result.item_errors["#3"] == "item_id: Field required"
    assert "valid dictionary" in result.item_errors["#4"]


def test_items_the_campaign_lacks_are_kept_and_noted_for_the_prediction() -> None:
    """A model that invents an item (the pilot's ``rv_11909_placeholder``) keeps it in the
    output as written, but the prediction's ``item_errors`` notes it, once however often it is
    written, after the drop reason of a broken copy with the same ID. Repair isn't due for it:
    it lost no campaign item."""
    items = [
        review_item("rv_1"),
        review_item("rv_11909_placeholder"),
        review_item("rv_2"),
        review_item("rv_3"),
        review_item("rv_11909_placeholder", "REVOKE"),
        review_item("rv_ghost", "MAYBE"),
        review_item("rv_ghost"),
    ]
    result = parse_response(review_case(), ModelResponse(parsed={"items": items}))
    assert result.ok and isinstance(result.output, ReviewOutput)
    assert [i.item_id for i in result.output.items] == [
        "rv_1",
        "rv_11909_placeholder",
        "rv_2",
        "rv_3",
        "rv_11909_placeholder",
        "rv_ghost",
    ]
    assert result.unknown_items == ("rv_11909_placeholder", "rv_ghost")
    assert list(result.item_errors) == ["rv_ghost"]  # only the broken copy was dropped
    recorded = result.prediction_item_errors
    assert recorded["rv_11909_placeholder"] == UNKNOWN_ITEM
    reason, note = recorded["rv_ghost"].split("; ", 1)
    assert reason.startswith("decision: Input should be") and note == UNKNOWN_ITEM
    assert list(recorded) == ["rv_ghost", "rv_11909_placeholder"]
    clean = [review_item(i) for i in ("rv_1", "rv_2", "rv_3", "rv_11909_placeholder")]
    invented = parse_response(review_case(), ModelResponse(parsed={"items": clean}))
    assert invented.unknown_items == ("rv_11909_placeholder",) and not invented.item_errors
    assert not needs_repair(invented)
    complete = parse_response(review_case(), ModelResponse(parsed={"items": clean[:3]}))
    assert complete.unknown_items == () and complete.prediction_item_errors == {}


def test_two_broken_items_with_one_id_keep_both_reasons() -> None:
    items = [review_item("rv_2", "MAYBE"), {"item_id": "rv_2"}]
    result = parse_response(review_case(), ModelResponse(parsed={"items": items}))
    assert list(result.item_errors) == ["rv_2"]
    first, second = result.item_errors["rv_2"].split("; #1: ")
    assert first.startswith("decision: Input should be")
    assert second == "decision: Field required"


def test_blank_item_ids_count_as_unusable() -> None:
    result = parse_response(
        review_case(), ModelResponse(parsed={"items": [{"item_id": "  ", "decision": "X"}]})
    )
    assert list(result.item_errors) == ["#0"]


def test_a_review_with_every_item_broken_is_ok_but_empty() -> None:
    """The scorer marks every campaign item missing (INVALID); the parser keeps the reasons."""
    result = parse_response(review_case(), ModelResponse(parsed={"items": [{"x": 1}]}))
    assert result.status is PredictionStatus.ok
    assert isinstance(result.output, ReviewOutput) and result.output.items == []
    assert list(result.item_errors) == ["#0"]


@pytest.mark.parametrize(
    ("payload", "found"),
    [({}, "missing"), ({"items": {"rv_1": "CERTIFY"}}, "a dict"), ({"items": "all"}, "a str")],
)
def test_review_without_an_items_list_is_invalid(payload: dict[str, Any], found: str) -> None:
    result = parse_response(review_case(), ModelResponse(parsed=payload))
    assert result.status is PredictionStatus.invalid
    assert result.invalid_reason == f"items: expected a list, found {found}"


def test_review_text_extraction() -> None:
    text = "Decisions follow.\n" + json.dumps({"items": [review_item("rv_1")]})
    result = parse_response(review_case(), ModelResponse(text=text))
    assert isinstance(result.output, ReviewOutput) and len(result.output.items) == 1


# --------------------------------------------------------------------------------------------
# JML
# --------------------------------------------------------------------------------------------


def test_jml_plan_is_validated_as_a_whole() -> None:
    plan = {
        "operations": [
            {"op": "REVOKE_ALL", "identity_id": "u_1", "effective": "IMMEDIATE"},
            {"op": "DISABLE_ACCOUNT", "account_id": "acct_1", "effective": "2026-10-17"},
        ]
    }
    result = parse_response(JML, ModelResponse(parsed=plan))
    assert isinstance(result.output, JmlOutput)
    assert [op.op for op in result.output.operations] == [
        OpType.REVOKE_ALL,
        OpType.DISABLE_ACCOUNT,
    ]


def test_one_bad_operation_makes_the_plan_invalid() -> None:
    """Plans are simulated as a whole (SPEC §8.3), so they aren't salvaged item by item."""
    plan = {
        "operations": [
            {"op": "REVOKE_ALL", "identity_id": "u_1", "effective": "IMMEDIATE"},
            {"op": "REVOKE", "effective": "IMMEDIATE"},
        ]
    }
    result = parse_response(JML, ModelResponse(parsed=plan))
    assert result.status is PredictionStatus.invalid
    assert result.invalid_reason == "operations.1: Value error, REVOKE requires assignment_id"


# --------------------------------------------------------------------------------------------
# Repair
# --------------------------------------------------------------------------------------------

ORIGINAL = ModelRequest(system="policy", messages=(Message(Role.user, "Decide."),))


def _invalid(response: ModelResponse) -> ParseResult:
    result = parse_response(REQUEST, response)
    assert result.status is PredictionStatus.invalid
    return result


def test_repair_request_appends_the_reply_and_the_error() -> None:
    response = ModelResponse(text="DENY, obviously.")
    repaired = repair_request(ORIGINAL, response, _invalid(response))
    assert repaired.system == ORIGINAL.system
    assert repaired.params == ORIGINAL.params
    assert repaired.messages[0] == ORIGINAL.messages[0]
    assert repaired.messages[1] == Message(Role.assistant, "DENY, obviously.")
    assert repaired.messages[2].role is Role.user
    assert "no JSON object in the response" in repaired.messages[2].content
    assert len(repaired.messages) == 3


def test_repair_of_an_empty_reply_uses_a_placeholder() -> None:
    response = ModelResponse(text="")
    repaired = repair_request(ORIGINAL, response, _invalid(response))
    assert repaired.messages[1].content == "(empty reply)"


def test_repair_of_native_output_quotes_the_parsed_object() -> None:
    response = ModelResponse(parsed={"decision": "OK"})
    repaired = repair_request(ORIGINAL, response, _invalid(response))
    assert json.loads(repaired.messages[1].content) == {"decision": "OK"}


@pytest.mark.parametrize(
    ("response", "text"),
    [
        (ModelResponse(text="DENY", parsed={"decision": "DENY"}), "DENY"),
        (ModelResponse(text=" ", parsed={"b": 1, "a": "É"}), '{"a": "É", "b": 1}'),
        (ModelResponse(parsed={}), "{}"),
        (ModelResponse(text=""), ""),
        (ModelResponse(), None),
    ],
)
def test_answer_text_falls_back_to_the_native_output(
    response: ModelResponse, text: str | None
) -> None:
    assert answer_text(response) == text


@pytest.mark.parametrize(
    ("case", "response"),
    [
        (REQUEST, ModelResponse(parsed=REQUEST_ANSWER)),
        (REQUEST, ModelResponse(refused=True)),
        (REQUEST, ModelResponse(error="timeout")),
        (review_case(), ModelResponse(parsed={"items": [review_item("rv_1")]})),
    ],
)
def test_only_answers_that_failed_validation_can_be_repaired(
    case: Case, response: ModelResponse
) -> None:
    result = parse_response(case, response)
    assert not needs_repair(result)
    with pytest.raises(ValueError, match="only answers that failed validation"):
        repair_request(ORIGINAL, response, result)


def _review(*items: dict[str, Any]) -> tuple[ModelResponse, ParseResult]:
    response = ModelResponse(parsed={"items": list(items)})
    return response, parse_response(review_case(), response)


def test_a_review_that_lost_items_needs_repair() -> None:
    """Every dropped item would be scored INVALID: the repair flag exists for exactly that."""
    response, result = _review(review_item("rv_1", "APPROVE"), review_item("rv_2"))
    assert result.ok and needs_repair(result)
    reask = repair_request(ORIGINAL, response, result).messages[-1].content
    assert reask.startswith(
        "Your previous reply could not be used: these items failed validation and were "
        "dropped: rv_1 (decision: Input should be"
    )


def test_a_review_repair_quotes_only_the_first_few_items() -> None:
    broken = [review_item(f"rv_{i}", "MAYBE") for i in range(1, MAX_REASON_ERRORS + 3)]
    response, result = _review(*broken)
    reask = repair_request(ORIGINAL, response, result).messages[-1].content
    assert f"rv_{MAX_REASON_ERRORS} (" in reask and f"rv_{MAX_REASON_ERRORS + 1} (" not in reask
    assert "(+2 more)" in reask


def test_an_invalid_answer_needs_repair_and_any_valid_retry_improves_it() -> None:
    first = parse_response(REQUEST, ModelResponse(text="DENY."))
    retry = parse_response(REQUEST, ModelResponse(parsed=REQUEST_ANSWER))
    assert needs_repair(first)
    assert repair_improves(REQUEST, first, retry)
    assert not repair_improves(REQUEST, first, first)
    assert not repair_improves(REQUEST, first, parse_response(REQUEST, ModelResponse(error="x")))


def test_a_review_retry_must_settle_more_campaign_items() -> None:
    """Settled: a campaign item answered by exactly one valid item. A retry that fixes the
    broken item but leaves the others out, or duplicates one, is no better."""
    case = review_case()
    _, first = _review(review_item("rv_1"), review_item("rv_2", "MAYBE"), review_item("rv_3"))
    _, fixed = _review(review_item("rv_1"), review_item("rv_2"), review_item("rv_3"))
    _, narrower = _review(review_item("rv_2"))
    _, duplicated = _review(review_item("rv_1"), review_item("rv_1"), review_item("rv_3"))
    invalid = parse_response(case, ModelResponse(text="no"))
    assert repair_improves(case, first, fixed)
    assert not repair_improves(case, first, narrower)
    assert not repair_improves(case, first, duplicated)
    assert not repair_improves(case, first, invalid)
