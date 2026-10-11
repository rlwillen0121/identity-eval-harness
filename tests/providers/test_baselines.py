"""SPEC §14 baselines: provider contract, outputs, and each baseline's behavior."""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Callable
from typing import Any

import pytest

from identitybench.policy.access import OracleInputError
from identitybench.policy.oracle import decide as oracle_decide
from identitybench.probe.shortcut import Probe, train_probe
from identitybench.providers.base import CaseAwareProvider, ModelResponse, Usage
from identitybench.providers.baselines import (
    BASELINE_NAMES,
    PEER_SHARE_THRESHOLD,
    RECENT_USE_DAYS,
    Baseline,
    make_baseline,
)
from identitybench.runner.config import BASELINES
from identitybench.schemas.case import (
    Campaign,
    CampaignScopeKind,
    Case,
    ReviewItem,
    ReviewItemKind,
)
from identitybench.schemas.enums import Label, ReviewDecision, Split, Task, label_of
from identitybench.schemas.gold import ReviewGold, ReviewItemGold
from identitybench.schemas.ops import OpType
from identitybench.schemas.outputs import Conditions, RequestOutput
from identitybench.schemas.predictions import OUTPUT_MODELS, PredictionStatus
from identitybench.world.entities import HrEventType, RiskTier
from tests.probe._cases import (
    ItemSpec,
    account,
    application,
    assignment,
    attributes,
    control,
    day,
    entitlement,
    identity,
    jml_case,
    make_case,
    make_view,
    membership,
    planted_requests,
    planted_reviews,
    request_case,
    review_case,
    role,
    usage,
    world_cases_with_variants,
)

DECISION_TASKS = {Task.access_request, Task.access_review}
NON_PROBE = [name for name in BASELINE_NAMES if name != "surface_probe"]


@pytest.fixture(scope="module")
def probe() -> Probe:
    dev = planted_requests("dev", 40, Split.dev) + planted_reviews("devr", 12, Split.dev)
    return train_probe(dev)


@pytest.fixture(scope="module")
def cases() -> dict[Task, Case]:
    return {
        Task.access_request: request_case("req", Label.N),
        Task.access_review: review_case(
            "rev",
            [ItemSpec(), ItemSpec(ReviewItemKind.membership, Label.N), ItemSpec(note="note")],
        ),
        Task.jml: jml_case(
            "jml",
            HrEventType.mover,
            effective_on=day(5),
            before=attributes("Finance"),
            after=attributes("Engineering"),
        ),
    }


def all_baselines(probe: Probe) -> list[Baseline]:
    return [make_baseline(name) for name in NON_PROBE] + [
        make_baseline("surface_probe", probe=probe)
    ]


def respond(baseline: Baseline, case: Case) -> dict[str, Any]:
    response = baseline.respond(case)
    assert response.ok, response.error
    assert response.parsed is not None
    return response.parsed


def decisions(output: dict[str, Any]) -> list[str]:
    if "items" in output:
        return [item["decision"] for item in output["items"]]
    return [output["decision"]]


# --------------------------------------------------------------------------------------------
# Factory and provider contract
# --------------------------------------------------------------------------------------------


def test_names_match_the_experiment_config() -> None:
    assert set(BASELINE_NAMES) == BASELINES
    assert len(BASELINE_NAMES) == len(set(BASELINE_NAMES))


def test_every_baseline_is_a_case_aware_provider(probe: Probe) -> None:
    for baseline in all_baselines(probe):
        assert isinstance(baseline, CaseAwareProvider)
        assert baseline.kind == "baseline"
        assert baseline.name == baseline.model
        if baseline.name == "surface_probe":
            assert baseline.endpoint_identity() == f"baseline:surface_probe:{probe.digest()[:16]}"
        else:
            assert baseline.endpoint_identity() == f"baseline:{baseline.name}"


def test_supported_tasks(probe: Probe) -> None:
    supported = {b.name: {t for t in Task if b.supports(t)} for b in all_baselines(probe)}
    assert supported == {
        "always_approve": DECISION_TASKS,
        "always_deny": DECISION_TASKS,
        "always_escalate": DECISION_TASKS,
        "peer_recommender": DECISION_TASKS,
        "surface_probe": DECISION_TASKS,
        "noop_plan": {Task.jml},
        "birthright_only_plan": {Task.jml},
        "oracle": set(Task),
    }
    requests_only = train_probe(planted_requests("d", 20, Split.dev))
    assert make_baseline("surface_probe", probe=requests_only).tasks == {Task.access_request}


def test_make_baseline_rejects_bad_arguments(probe: Probe) -> None:
    with pytest.raises(ValueError, match="unknown baseline 'always_maybe'"):
        make_baseline("always_maybe")
    with pytest.raises(ValueError, match="takes no options"):
        make_baseline("always_deny", probe=probe)
    with pytest.raises(ValueError, match="takes no options"):
        make_baseline("oracle", dev_cases=[])
    with pytest.raises(ValueError, match="needs a probe"):
        make_baseline("surface_probe")
    with pytest.raises(ValueError, match="needs a probe"):
        make_baseline("surface_probe", params=probe.to_json())
    with pytest.raises(ValueError, match="not both"):
        make_baseline("surface_probe", probe=probe, params=probe.to_json(), dev_cases=[])


async def test_outputs_validate_and_responses_are_model_shaped(
    probe: Probe, cases: dict[Task, Case], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_ideal(monkeypatch)
    for baseline in all_baselines(probe):
        for task, case in cases.items():
            if not baseline.supports(task):
                continue
            response = await baseline.decide(case)
            assert response.ok, (baseline.name, task, response.error)
            assert response.parsed is not None and response.text is not None
            assert json.loads(response.text) == response.parsed
            assert response.usage == Usage()
            assert response.latency_s == 0.0
            assert response.served_model == baseline.name
            OUTPUT_MODELS[task].model_validate(response.parsed)


async def test_responses_parse_ok_through_the_runner_parser(
    probe: Probe, cases: dict[Task, Case], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's parser, not the output models alone, decides VALID or INVALID (SPEC §11.1:
    scorers treat baselines exactly like models), so every supported response must parse ok,
    with every campaign item answered once and no item dropped."""
    parse = pytest.importorskip("identitybench.runner.parse")
    _fake_ideal(monkeypatch)
    for baseline in all_baselines(probe):
        for task, case in cases.items():
            if not baseline.supports(task):
                continue
            result = parse.parse_response(case, await baseline.decide(case))
            assert result.status is PredictionStatus.ok, (
                baseline.name,
                task,
                result.invalid_reason,
            )
            assert result.item_errors == {}
            if task is Task.access_review:
                assert case.view.campaign is not None
                assert [item.item_id for item in result.output.items] == [
                    item.item_id for item in case.view.campaign.items
                ]


def _invalid_responses(
    cases: dict[Task, Case], monkeypatch: pytest.MonkeyPatch
) -> list[tuple[Case, ModelResponse]]:
    """One response for each way a baseline declines a case: an unsupported task, an
    incomplete view, a failing ``tasks.ideal`` and a JML view the oracle would reject."""
    review = review_case("c", [ItemSpec(ReviewItemKind.membership)])
    incomplete = review.model_copy(
        update={"view": review.view.model_copy(update={"memberships": {}})}
    )

    def failing(case: Case) -> dict[str, Any]:
        raise ValueError(f"case {case.case_id}: no ideal output")

    _install_ideal(monkeypatch, failing)
    declined = [
        ("always_approve", cases[Task.jml]),
        ("peer_recommender", incomplete),
        ("oracle", cases[Task.access_request]),
        ("birthright_only_plan", _ruleless_birthright()),
    ]
    return [(case, make_baseline(name).respond(case)) for name, case in declined]


def test_error_responses_parse_invalid(
    cases: dict[Task, Case], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A baseline that can't answer comes back INVALID through the parser (status ``error``),
    never as an exception and never as a valid-looking answer."""
    parse = pytest.importorskip("identitybench.runner.parse")
    for case, response in _invalid_responses(cases, monkeypatch):
        result = parse.parse_response(case, response)
        assert result.status is PredictionStatus.error, (case.case_id, result)
        assert not result.ok and result.output is None
        assert result.invalid_reason == response.error


def test_unsupported_task_is_an_error_response(cases: dict[Task, Case]) -> None:
    response = make_baseline("always_approve").respond(cases[Task.jml])
    assert not response.ok
    assert response.parsed is None
    assert response.error == "baseline:always_approve does not answer jml"
    assert "does not answer" in (
        make_baseline("noop_plan").respond(cases[Task.access_request]).error or ""
    )


def test_responses_are_deterministic(probe: Probe, cases: dict[Task, Case]) -> None:
    for baseline in all_baselines(probe):
        if baseline.name == "oracle":
            continue
        for task, case in cases.items():
            if baseline.supports(task):
                assert baseline.respond(case).text == baseline.respond(case).text


# --------------------------------------------------------------------------------------------
# Constant baselines
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "request_decision", "review_decision"),
    [
        ("always_approve", "APPROVE", "CERTIFY"),
        ("always_deny", "DENY", "REVOKE"),
        ("always_escalate", "ESCALATE", "ESCALATE"),
    ],
)
def test_constant_baselines(
    cases: dict[Task, Case], name: str, request_decision: str, review_decision: str
) -> None:
    baseline = make_baseline(name)
    request_output = respond(baseline, cases[Task.access_request])
    assert decisions(request_output) == [request_decision]
    review_output = respond(baseline, cases[Task.access_review])
    assert [item["item_id"] for item in review_output["items"]] == ["rv_0000", "rv_0001", "rv_0002"]
    assert decisions(review_output) == [review_decision] * 3


def test_always_escalate_only_escalates(cases: dict[Task, Case]) -> None:
    baseline = make_baseline("always_escalate")
    for label in Label:
        if label is Label.INVALID:
            continue
        output = respond(baseline, request_case("c", label))
        assert label_of(output["decision"]) is Label.E
        assert "conditions" not in output
    long_campaign = review_case("big", [ItemSpec(label=label) for label in (Label.A, Label.N) * 20])
    assert set(decisions(respond(baseline, long_campaign))) == {"ESCALATE"}


def test_always_approve_grants_the_requested_duration() -> None:
    baseline = make_baseline("always_approve")
    timed = respond(baseline, request_case("c1", duration=45))
    assert timed["conditions"] == {"max_duration_days": 45}
    standing = respond(baseline, request_case("c2", duration=None))
    # No cap: the effective duration is the request's own (standing access).
    assert RequestOutput.model_validate(standing).conditions == Conditions(max_duration_days=None)
    assert standing["conditions"] == {}
    denied = respond(make_baseline("always_deny"), request_case("c3"))
    assert "conditions" not in denied


# --------------------------------------------------------------------------------------------
# Peer recommender
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("share", "decision"),
    [(None, "DENY"), (0.0, "DENY"), (0.59, "DENY"), (0.6, "APPROVE"), (0.95, "APPROVE")],
)
def test_peer_recommender_requests_threshold_at_sixty_percent(
    share: float | None, decision: str
) -> None:
    assert PEER_SHARE_THRESHOLD == 0.6
    output = respond(make_baseline("peer_recommender"), request_case("c", peer_share=share))
    assert output["decision"] == decision
    if decision == "APPROVE":
        assert output["conditions"] == {"max_duration_days": 30}


def test_peer_recommender_reads_the_beneficiarys_peers() -> None:
    case = request_case("c", requester_id="u_mgr", peer_share=None)
    peers = {"u_mgr": {"ent_req": 0.9}, "u_ben": {"ent_other": 0.9}}
    view = case.view.model_copy(update={"peer_stats": peers})
    output = respond(make_baseline("peer_recommender"), case.model_copy(update={"view": view}))
    assert output["decision"] == "DENY"


def _peer_review(
    peers: dict[str, dict[str, float]], last_used: dict[str, Any], telemetry: str = "available"
) -> Case:
    """Four items for ``u_h``: a membership in ``r_top`` (expanding to ``ent_x`` and, through
    ``r_child``, ``ent_y``), an assignment of ``ent_a``, an assignment of ``ent_b`` on an orphan
    account, and an assignment of ``ent_c`` whose own usage is stale while ``ent_c``'s
    ``last_used`` date goes on another account's record. The view is complete: the oracle
    accepts it (``test_the_peer_review_fixture_is_oracle_valid``)."""
    entities: list[Any] = [
        identity("u_h"),
        identity("u_mgr", manager_id=None),
        application(),
        account("acct_h", "u_h"),
        account("acct_orphan", None),
        account("acct_other", "u_mgr"),
        role("r_top", ["ent_x"], child_roles=["r_child"]),
        role("r_child", ["ent_y"]),
        membership("rm_1", "u_h", "r_top"),
        assignment("as_a", "acct_h", "ent_a"),
        assignment("as_b", "acct_orphan", "ent_b"),
        assignment("as_c", "acct_h", "ent_c"),
        *(entitlement(e) for e in ("ent_x", "ent_y", "ent_a", "ent_b", "ent_c")),
        usage("use_x", "acct_h", "ent_x", last_used.get("ent_x", day(-300)), telemetry),
        usage("use_y", "acct_h", "ent_y", last_used.get("ent_y", day(-300)), telemetry),
        usage("use_a", "acct_h", "ent_a", last_used.get("ent_a", day(-300)), telemetry),
        usage("use_b", "acct_orphan", "ent_b", last_used.get("ent_b", day(-300)), telemetry),
        usage("use_c_h", "acct_h", "ent_c", day(-300)),
        usage("use_c", "acct_other", "ent_c", last_used.get("ent_c", day(-300)), telemetry),
    ]
    refs = [("rm_1", ReviewItemKind.membership)] + [
        (ref, ReviewItemKind.assignment) for ref in ("as_a", "as_b", "as_c")
    ]
    items = tuple(
        ReviewItem(item_id=f"rv_{i}", kind=kind, ref=ref) for i, (ref, kind) in enumerate(refs)
    )
    campaign = Campaign(
        campaign_id="camp",
        reviewer_id="u_mgr",
        scope_kind=CampaignScopeKind.application,
        scope_ref="app_main",
        items=items,
    )
    gold = ReviewGold(
        items=tuple(
            ReviewItemGold(
                item_id=i.item_id, decision=ReviewDecision.CERTIFY, risk_tier=RiskTier.low
            )
            for i in items
        )
    )
    view = make_view(*entities, campaign=campaign, peer_stats=peers)
    return make_case("peer", Task.access_review, view, gold)


def _peer_decisions(case: Case) -> list[str]:
    return decisions(respond(make_baseline("peer_recommender"), case))


def test_the_peer_review_fixture_is_oracle_valid() -> None:
    """The refusal tests below drop one record each; this pins that the oracle rejects those
    views for the dropped record alone."""
    for last_used, telemetry in (({}, "available"), ({"ent_x": None}, "unavailable")):
        case = _peer_review({}, last_used, telemetry)
        assert isinstance(oracle_decide(case.view, case.task), ReviewGold)
    request = request_case("c", peer_share=0.9)
    oracle_decide(request.view, request.task)


def test_peer_recommender_reviews_default_to_revoke() -> None:
    assert _peer_decisions(_peer_review({}, {})) == ["REVOKE"] * 4


def test_peer_recommender_membership_takes_the_best_share_in_the_expansion() -> None:
    # ent_y is reached only through the child role; its share decides the membership.
    peers = {"u_h": {"ent_x": 0.3, "ent_y": 0.7, "ent_a": 0.59}}
    assert _peer_decisions(_peer_review(peers, {})) == ["CERTIFY", "REVOKE", "REVOKE", "REVOKE"]
    peers = {"u_h": {"ent_x": 0.3, "ent_y": 0.5, "ent_a": 0.6}}
    assert _peer_decisions(_peer_review(peers, {})) == ["REVOKE", "CERTIFY", "REVOKE", "REVOKE"]


def test_peer_recommender_certifies_recent_use_within_ninety_days() -> None:
    assert RECENT_USE_DAYS == 90
    recent = {"ent_y": day(-90), "ent_a": day(-90), "ent_b": day(-10)}
    # The orphan account's own usage counts; ent_c's usage sits on someone else's account.
    assert _peer_decisions(_peer_review({}, {**recent, "ent_c": day(-1)})) == [
        "CERTIFY",
        "CERTIFY",
        "CERTIFY",
        "REVOKE",
    ]
    stale = {"ent_x": day(-91), "ent_y": day(-91), "ent_a": day(-91), "ent_b": day(-91)}
    assert _peer_decisions(_peer_review({}, stale)) == ["REVOKE"] * 4


def test_peer_recommender_treats_unknown_usage_as_unused() -> None:
    unknown = {e: None for e in ("ent_x", "ent_y", "ent_a", "ent_b", "ent_c")}
    assert _peer_decisions(_peer_review({}, unknown, "unavailable")) == ["REVOKE"] * 4


def test_peer_recommender_ignores_peers_for_an_orphan_account() -> None:
    # An orphan has no holder, so no identity's peer stats apply to it.
    peers = {"u_h": {"ent_b": 0.99}, "u_mgr": {"ent_b": 0.99}}
    assert _peer_decisions(_peer_review(peers, {}))[2] == "REVOKE"


def test_incomplete_view_is_an_error_response() -> None:
    case = review_case("c", [ItemSpec(ReviewItemKind.membership)])
    view = case.view.model_copy(update={"memberships": {}})
    response = make_baseline("peer_recommender").respond(case.model_copy(update={"view": view}))
    assert not response.ok
    assert response.error is not None
    assert response.error.startswith("baseline:peer_recommender: incomplete view:")
    assert "unknown membership 'rm_0'" in response.error


def _without(case: Case, collection: str, record_id: str) -> Case:
    """``case`` with one record dropped from a view collection."""
    records = {k: v for k, v in getattr(case.view, collection).items() if k != record_id}
    return case.model_copy(update={"view": case.view.model_copy(update={collection: records})})


def _peer_incomplete_cases() -> list[tuple[Case, str]]:
    """Views the oracle rejects, each missing one record of a valid view, with a fragment of
    the oracle's complaint. ``peer_recommender`` would answer each by default if it didn't
    check: a 0.9 peer share approves the request whoever the beneficiary is, and a missing
    account, entitlement or usage record leaves an item with no peers or no recent use, so a
    REVOKE."""
    request = request_case("c", peer_share=0.9)
    review = _peer_review({}, {})
    return [
        (_without(request, "entitlements", "ent_req"), "entitlement 'ent_req' is referenced"),
        (_without(request, "identities", "u_ben"), "identity 'u_ben' (request.beneficiary_id)"),
        (_without(review, "accounts", "acct_orphan"), "as_b (account acct_orphan)"),
        (_without(review, "entitlements", "ent_b"), "entitlement 'ent_b' is referenced"),
        (_without(review, "entitlements", "ent_y"), "entitlement 'ent_y' is referenced"),
        (_without(review, "roles", "r_child"), "role 'r_child' is referenced"),
        (
            _without(review, "usage", "use_a"),
            "no usage record for (account acct_h, entitlement ent_a)",
        ),
        (
            _without(review, "usage", "use_y"),
            "no usage record for (account acct_h, entitlement ent_y)",
        ),
        (_without(review, "identities", "u_h"), "identity 'u_h' (rm_1.identity_id)"),
        (_without(review, "applications", "app_main"), "application 'app_main' is referenced"),
    ]


@pytest.mark.parametrize("index", range(len(_peer_incomplete_cases())))
def test_peer_recommender_refuses_views_the_oracle_refuses(index: int) -> None:
    """A view missing a record the oracle's input contract needs (the requested entitlement, the
    beneficiary, an item's account, holder or conferred entitlement, that entitlement's
    application, a role, a usage record) is an error response carrying the oracle's own
    complaint, not a default label."""
    case, message = _peer_incomplete_cases()[index]
    with pytest.raises(OracleInputError) as refused:
        oracle_decide(case.view, case.task)
    assert message in str(refused.value)
    response = make_baseline("peer_recommender").respond(case)
    assert not response.ok and response.parsed is None
    assert response.error == f"baseline:peer_recommender: incomplete view: {refused.value}"


def test_missing_task_header_is_an_error_response() -> None:
    case = request_case("c")
    view = case.view.model_copy(update={"request": None})
    response = make_baseline("always_deny").respond(case.model_copy(update={"view": view}))
    assert response.error is not None and "has no request" in response.error


# --------------------------------------------------------------------------------------------
# Surface probe
# --------------------------------------------------------------------------------------------


def test_surface_probe_predicts_with_the_frozen_probe(probe: Probe) -> None:
    baseline = make_baseline("surface_probe", probe=probe)
    for case in planted_requests("t", 10, Split.test):
        output = respond(baseline, case)
        assert label_of(output["decision"]) is probe.predict_request(case)
        assert label_of(output["decision"]) is label_of(case.gold.decision)  # type: ignore[union-attr]
    for case in planted_reviews("tr", 3, Split.test):
        output = respond(baseline, case)
        assert [label_of(d) for d in decisions(output)] == probe.predict_items(case)


def test_surface_probe_from_params_and_dev_cases_matches(probe: Probe) -> None:
    dev = planted_requests("dev", 40, Split.dev) + planted_reviews("devr", 12, Split.dev)
    rebuilt = make_baseline("surface_probe", params=probe.to_json(), dev_cases=dev)
    original = make_baseline("surface_probe", probe=probe)
    assert rebuilt.endpoint_identity() == original.endpoint_identity()
    for case in planted_requests("t", 10, Split.test) + planted_reviews("tr", 2, Split.test):
        assert rebuilt.respond(case).text == original.respond(case).text
    trained_on = set(probe.task_spec(Task.access_request).train_families)
    other_dev = [c for c in dev if c.family_id != min(trained_on)]
    with pytest.raises(ValueError, match="don't reproduce"):
        make_baseline("surface_probe", params=probe.to_json(), dev_cases=other_dev)


# --------------------------------------------------------------------------------------------
# JML floors
# --------------------------------------------------------------------------------------------


def _operations(name: str, case: Case) -> list[dict[str, Any]]:
    return respond(make_baseline(name), case)["operations"]


def test_noop_plan_is_empty(cases: dict[Task, Case]) -> None:
    assert _operations("noop_plan", cases[Task.jml]) == []


def test_birthright_only_plan_grants_the_matching_roles_on_the_effective_date(
    cases: dict[Task, Case],
) -> None:
    operations = _operations("birthright_only_plan", cases[Task.jml])
    assert operations == [
        {"op": "GRANT_ROLE", "identity_id": "u_j", "role_id": role_id, "effective": "2026-10-15"}
        for role_id in ("br_all", "br_eng")
    ]


@pytest.mark.parametrize(
    ("after", "roles"),
    [
        (attributes("Engineering", region="EU"), ["br_all", "br_eng", "br_eng_eu"]),
        (
            attributes("Finance", employment_type="contractor"),
            ["br_all", "br_contractor", "br_fin"],
        ),
        (attributes("Legal"), ["br_all"]),
    ],
)
def test_birthright_only_plan_matches_every_attribute_dimension(
    after: Any, roles: list[str]
) -> None:
    case = jml_case("j", HrEventType.joiner, after=after)
    operations = _operations("birthright_only_plan", case)
    assert [op["role_id"] for op in operations] == roles
    assert {op["effective"] for op in operations} == {"IMMEDIATE"}


def test_birthright_only_plan_moves_an_overdue_date_to_today() -> None:
    case = jml_case("j", HrEventType.joiner, effective_on=day(-3), after=attributes("Finance"))
    assert {op["effective"] for op in _operations("birthright_only_plan", case)} == {"IMMEDIATE"}


@pytest.mark.parametrize(
    "event_type", [HrEventType.leaver, HrEventType.leave_start, HrEventType.leave_end]
)
def test_birthright_only_plan_grants_nothing_for_events_that_grant_nothing(
    event_type: HrEventType,
) -> None:
    """These events' plans grant no birthright, so ``after`` (which ``HrEvent`` allows on any
    event, here equal to ``before``) doesn't make the floor re-grant held roles."""
    held = [
        membership("m_all", "u_j", "br_all", source="birthright"),
        membership("m_fin", "u_j", "br_fin", source="birthright"),
    ]
    finance = attributes("Finance")
    case = jml_case("j", event_type, before=finance, after=finance, held=held)
    assert _operations("birthright_only_plan", case) == []
    bare = jml_case("j", event_type, before=finance)
    assert _operations("birthright_only_plan", bare) == []


@pytest.mark.parametrize(
    "event_type",
    [HrEventType.joiner, HrEventType.rehire, HrEventType.mover, HrEventType.conversion],
)
def test_birthright_only_plan_needs_after_attributes_to_grant(event_type: HrEventType) -> None:
    case = jml_case("j", event_type, before=attributes("Finance"))
    response = make_baseline("birthright_only_plan").respond(case)
    assert response.error is not None
    assert response.error.startswith("baseline:birthright_only_plan: incomplete view:")
    assert "has no after attributes" in response.error


def _ruleless_birthright() -> Case:
    """A joiner whose view has a birthright role without an attribute rule."""
    case = jml_case("j", HrEventType.joiner, after=attributes("Finance"))
    roles = {**case.view.roles, "br_bare": role("br_bare", ["ent_all"], kind="birthright")}
    return case.model_copy(update={"view": case.view.model_copy(update={"roles": roles})})


def test_birthright_only_plan_rejects_a_birthright_role_without_a_rule() -> None:
    """The oracle can't plan without every birthright role's rule, so neither does the floor."""
    case = _ruleless_birthright()
    response = make_baseline("birthright_only_plan").respond(case)
    assert response.error == (
        "baseline:birthright_only_plan: incomplete view: birthright role br_bare has no "
        "attribute rule"
    )
    with pytest.raises(OracleInputError, match="br_bare has no attribute rule"):
        oracle_decide(case.view, Task.jml)


@pytest.mark.parametrize(
    ("collection", "record_id", "message"),
    [
        ("entitlements", "ent_fin", "entitlement 'ent_fin' is referenced"),
        ("roles", "br_fin_child", "role 'br_fin_child' is referenced"),
    ],
)
def test_birthright_only_plan_needs_each_granted_role_to_expand(
    collection: str, record_id: str, message: str
) -> None:
    """A grant provisions an account in every application its role reaches, so the oracle
    (``PlanContext.grant_roles``) refuses a granted role whose expansion isn't all in the view,
    and so does the floor."""
    case = jml_case("j", HrEventType.joiner, after=attributes("Finance"))
    roles = {
        **case.view.roles,
        "br_fin": case.view.roles["br_fin"].model_copy(update={"child_roles": ("br_fin_child",)}),
        "br_fin_child": role("br_fin_child", ["ent_all"]),
    }
    case = case.model_copy(update={"view": case.view.model_copy(update={"roles": roles})})
    assert [op["role_id"] for op in _operations("birthright_only_plan", case)] == [
        "br_all",
        "br_fin",
    ]
    broken = _without(case, collection, record_id)
    with pytest.raises(OracleInputError, match=message):
        oracle_decide(broken.view, Task.jml)
    response = make_baseline("birthright_only_plan").respond(broken)
    assert response.error is not None
    assert response.error.startswith("baseline:birthright_only_plan: incomplete view:")
    assert message in response.error


def _complete_mover() -> Case:
    """A Finance-to-Engineering mover whose view exercises every JML entry check: standing
    birthright memberships, a human account carrying them and a requested assignment, with one
    usage record per provisioned pair. The oracle accepts it (the test below pins that)."""
    case = jml_case(
        "j",
        HrEventType.mover,
        effective_on=day(5),
        before=attributes("Finance"),
        after=attributes("Engineering"),
        held=[
            membership("m_all", "u_j", "br_all", source="birthright"),
            membership("m_fin", "u_j", "br_fin", source="birthright"),
        ],
    )
    records = make_view(
        account("acct_j", "u_j"),
        entitlement("ent_rpt"),
        assignment("as_r", "acct_j", "ent_rpt"),
        *(usage(f"use_{e}", "acct_j", e, day(-5)) for e in ("ent_all", "ent_fin", "ent_rpt")),
    )
    view = case.view.model_copy(
        update={
            name: {**getattr(case.view, name), **getattr(records, name)}
            for name in ("accounts", "entitlements", "assignments", "usage")
        }
    )
    return case.model_copy(update={"view": view})


def _with(case: Case, collection: str, *records: Any) -> Case:
    """``case`` with ``records`` added to (or replacing their IDs in) a view collection."""
    merged = {**getattr(case.view, collection), **{r.id: r for r in records}}
    return case.model_copy(update={"view": case.view.model_copy(update={collection: merged})})


def _floor_incomplete_cases() -> list[tuple[Case, str]]:
    """Views the oracle's JML entry checks reject (``PlanContext``: ``check_complete`` and the
    event's identity), each one edit away from ``_complete_mover``, with a fragment of the
    oracle's complaint. Without the oracle's entry checks the floor would answer each one (or,
    with no event, refuse with a message of its own), since its grants don't depend on the
    broken record."""
    mover = _complete_mover()
    no_event = mover.model_copy(update={"view": mover.view.model_copy(update={"event": None})})
    requested = membership("m_all", "u_j", "br_all", source="request")
    controls = (control("mc_1", "u_j"), control("mc_2", "u_j"))
    return [
        (no_event, "plan_jml needs a CaseView with an HR event"),
        (_without(mover, "identities", "u_j"), "identity 'u_j' (identity_id of event hr_1)"),
        (_without(mover, "accounts", "acct_j"), "as_r (account acct_j)"),
        (_without(mover, "roles", "br_fin"), "m_fin is in role 'br_fin', which isn't in the view"),
        (_with(mover, "memberships", requested), "m_all has source 'request'"),
        (_without(mover, "usage", "use_ent_fin"), "(account acct_j, entitlement ent_fin)"),
        (_with(mover, "mitigating_controls", *controls), "u_j has several controls for SOD-001"),
    ]


def test_the_complete_mover_is_oracle_valid() -> None:
    case = _complete_mover()
    oracle_decide(case.view, Task.jml)
    assert [op["role_id"] for op in _operations("birthright_only_plan", case)] == ["br_eng"]
    assert _operations("birthright_only_plan", case) == _gold_grants(case)


@pytest.mark.parametrize("index", range(len(_floor_incomplete_cases())))
def test_birthright_only_plan_refuses_views_the_oracle_rejects_at_entry(index: int) -> None:
    """The floor runs the oracle's JML entry checks, so a view they reject is an error response
    carrying the oracle's own complaint, though the floor's grants don't depend on the broken
    record."""
    case, message = _floor_incomplete_cases()[index]
    with pytest.raises(OracleInputError) as refused:
        oracle_decide(case.view, Task.jml)
    assert message in str(refused.value)
    response = make_baseline("birthright_only_plan").respond(case)
    assert not response.ok and response.parsed is None
    assert response.error == f"baseline:birthright_only_plan: incomplete view: {refused.value}"


def _floor_rule_check_cases() -> list[tuple[Case, str]]:
    """Views the oracle refuses past its entry checks, on a fact the floor's own part of the
    plan can check with one public ``PlanContext`` call, with a fragment of the oracle's
    complaint. The first four don't break the floor's grants, so a floor that skipped the call
    would answer: a joiner or rehire already holding live access the grants would switch on
    (``reject_prior_access``), a conversion without ``before`` attributes (``before``) and a
    mover whose held new birthright role doesn't expand (``expansion_of``; the floor doesn't
    grant ``br_all``, which the mover holds). The last, a conversion with neither ``before`` nor
    ``after``, pins the order: gold reads ``before`` first, so a floor that read ``after`` first
    would refuse it with a different message."""
    prior = [membership("m_fin", "u_j", "br_fin", source="birthright")]
    prior_access = [
        (
            jml_case(
                "j", event_type, effective_on=day(4), after=attributes("Engineering"), held=prior
            ),
            f"{event_type} u_j already holds access that the plan's grants would switch on from "
            f"{day(4).isoformat()}: m_fin",
        )
        for event_type in (HrEventType.joiner, HrEventType.rehire)
    ]
    conversion = jml_case(
        "j",
        HrEventType.conversion,
        effective_on=day(3),
        after=attributes("Finance"),
        employment_type="contractor",
    )
    mover = jml_case(
        "j",
        HrEventType.mover,
        effective_on=day(5),
        before=attributes("Finance"),
        after=attributes("Engineering"),
        held=[membership("m_all", "u_j", "br_all", source="birthright")],
    )
    return [
        *prior_access,
        (conversion, "conversion event hr_1 has no before attributes"),
        (_without(mover, "entitlements", "ent_all"), "entitlement 'ent_all' is referenced"),
        (
            jml_case("j", HrEventType.conversion, employment_type="contractor"),
            "conversion event hr_1 has no before attributes",
        ),
    ]


@pytest.mark.parametrize("index", range(len(_floor_rule_check_cases())))
def test_birthright_only_plan_runs_the_oracle_checks_on_its_part_of_the_plan(index: int) -> None:
    """Past the entry checks, the floor makes the oracle's checks on the part of the plan it
    computes, in gold's order, so it refuses these views with the oracle's own message."""
    case, message = _floor_rule_check_cases()[index]
    with pytest.raises(OracleInputError) as refused:
        oracle_decide(case.view, Task.jml)
    assert message in str(refused.value)
    response = make_baseline("birthright_only_plan").respond(case)
    assert not response.ok and response.parsed is None
    assert response.error == f"baseline:birthright_only_plan: incomplete view: {refused.value}"


def test_birthright_only_plan_ignores_roles_it_does_not_grant() -> None:
    """Only granted roles must expand: a non-matching birthright role whose entitlement is
    missing doesn't stop the floor (the oracle doesn't read it either)."""
    case = _without(
        jml_case("j", HrEventType.joiner, after=attributes("Finance")), "entitlements", "ent_eng"
    )
    assert _operations("birthright_only_plan", case) == _gold_grants(case)
    assert [op["role_id"] for op in _gold_grants(case)] == ["br_all", "br_fin"]


def _gold_grants(case: Case) -> list[dict[str, Any]]:
    """The ``GRANT_ROLE`` operations of the oracle's plan for ``case``."""
    gold = oracle_decide(case.view, Task.jml)
    return [
        op.model_dump(mode="json", exclude_none=True)
        for op in gold.operations  # type: ignore[union-attr]
        if op.op is OpType.GRANT_ROLE
    ]


def test_birthright_only_plan_equals_gold_for_a_joiner() -> None:
    """JML-001 grants exactly the matching birthright, held or not, so the floor is perfect
    for joiners, including one already holding a matching role (the grant provisions)."""
    held = [membership("m_all", "u_j", "br_all", source="birthright")]
    for holdings in ([], held):
        case = jml_case(
            "j",
            HrEventType.joiner,
            effective_on=day(4),
            after=attributes("Engineering"),
            held=holdings,
        )
        gold = oracle_decide(case.view, Task.jml)
        expected = [op.model_dump(mode="json", exclude_none=True) for op in gold.operations]  # type: ignore[union-attr]
        assert _operations("birthright_only_plan", case) == expected
        assert [op["role_id"] for op in expected] == ["br_all", "br_eng"]


def test_birthright_only_plan_regrants_held_roles_for_a_rehire() -> None:
    held = [membership("m_all", "u_j", "br_all", source="birthright")]
    case = jml_case("j", HrEventType.rehire, after=attributes("Finance"), held=held)
    assert [op["role_id"] for op in _operations("birthright_only_plan", case)] == [
        "br_all",
        "br_fin",
    ]


@pytest.mark.parametrize(
    ("held", "granted"),
    [
        # Standing birthright already held: only the new department's role is granted.
        (
            [
                membership("m_all", "u_j", "br_all", source="birthright"),
                membership("m_fin", "u_j", "br_fin", source="birthright"),
            ],
            ["br_eng"],
        ),
        # Time-bound access to a birthright role would lapse, so it isn't "already held".
        (
            [membership("m_all", "u_j", "br_all", source="birthright", expires_on=day(60))],
            ["br_all", "br_eng"],
        ),
        # Held is judged on the move date: a membership starting after it doesn't count.
        (
            [membership("m_all", "u_j", "br_all", source="birthright", granted_on=day(9))],
            ["br_all", "br_eng"],
        ),
        # Another identity's membership is not this mover's.
        ([membership("m_all", "u_mgr", "br_all", source="birthright")], ["br_all", "br_eng"]),
    ],
)
def test_birthright_only_plan_skips_roles_a_mover_already_holds(
    held: list[Any], granted: list[str]
) -> None:
    case = jml_case(
        "j",
        HrEventType.mover,
        effective_on=day(5),
        before=attributes("Finance"),
        after=attributes("Engineering"),
        held=held,
    )
    operations = _operations("birthright_only_plan", case)
    assert [op["role_id"] for op in operations] == granted
    assert operations == _gold_grants(case)


def test_birthright_only_plan_matches_gold_grants_for_a_conversion() -> None:
    held = [
        membership("m_all", "u_j", "br_all", source="birthright"),
        membership("m_ctr", "u_j", "br_contractor", source="birthright"),
    ]
    case = jml_case(
        "j",
        HrEventType.conversion,
        effective_on=day(3),
        before=attributes("Finance", employment_type="contractor"),
        after=attributes("Finance"),
        held=held,
        employment_type="contractor",
    )
    operations = _operations("birthright_only_plan", case)
    assert operations == [
        {"op": "GRANT_ROLE", "identity_id": "u_j", "role_id": "br_fin", "effective": "2026-10-13"}
    ]
    assert operations == _gold_grants(case)


# --------------------------------------------------------------------------------------------
# Oracle
# --------------------------------------------------------------------------------------------


def _install_ideal(monkeypatch: pytest.MonkeyPatch, ideal_output: Callable[[Case], Any]) -> None:
    """Install a stand-in ``tasks.ideal`` module whose ``ideal_output`` is the given one."""
    module = types.ModuleType("identitybench.tasks.ideal")
    module.ideal_output = ideal_output  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "identitybench.tasks.ideal", module)


def _fake_ideal(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Install a stand-in ``tasks.ideal`` that answers like an always-escalate output and
    records the cases it saw."""
    seen: list[str] = []

    def ideal_output(case: Case) -> dict[str, Any]:
        seen.append(case.case_id)
        if case.task is Task.access_request:
            return {"decision": "ESCALATE", "rationale": f"ideal {case.case_id}"}
        if case.task is Task.access_review:
            items = case.view.campaign.items if case.view.campaign else ()
            return {"items": [{"item_id": i.item_id, "decision": "ESCALATE"} for i in items]}
        return {"operations": []}

    _install_ideal(monkeypatch, ideal_output)
    return seen


def test_oracle_returns_what_ideal_output_returns(
    cases: dict[Task, Case], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_ideal(monkeypatch)
    response = make_baseline("oracle").respond(cases[Task.access_request])
    assert response.parsed == {"decision": "ESCALATE", "rationale": "ideal req"}
    assert response.text == json.dumps(response.parsed)
    assert seen == ["req"]


def test_a_failing_case_is_an_error_response_not_an_exception(
    cases: dict[Task, Case], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad case must not abort a run (the runner cancels every cell on an exception): it
    comes back as an error response, which the parser scores INVALID."""

    def mismatched(case: Case) -> dict[str, Any]:
        raise ValueError(f"case {case.case_id}: review gold for task access_request")

    _install_ideal(monkeypatch, mismatched)
    response = make_baseline("oracle").respond(cases[Task.access_request])
    assert response.parsed is None and response.text is None
    assert response.error == (
        "baseline:oracle: failed on case req: ValueError: case req: review gold for task "
        "access_request"
    )

    _install_ideal(monkeypatch, lambda case: {"decision": "APPROVE", "facts": {"a", "b"}})
    response = make_baseline("oracle").respond(cases[Task.access_request])
    assert response.parsed is None
    assert response.error is not None
    assert response.error.startswith("baseline:oracle: failed on case req: TypeError:")


def _with_oracle_gold(case: Case) -> Case:
    return case.model_copy(update={"gold": oracle_decide(case.view, case.task)})


def test_oracle_returns_real_ideal_outputs(cases: dict[Task, Case]) -> None:
    ideal = pytest.importorskip("identitybench.tasks.ideal")
    parse = pytest.importorskip("identitybench.runner.parse")
    oracle = make_baseline("oracle")
    graded = [
        _with_oracle_gold(cases[Task.access_request]),
        _with_oracle_gold(cases[Task.access_review]),
        _with_oracle_gold(
            jml_case("j", HrEventType.joiner, effective_on=day(2), after=attributes("Finance"))
        ),
    ]
    for case in graded:
        response = oracle.respond(case)
        assert response.ok
        assert response.parsed == ideal.ideal_output(case)
        assert parse.parse_response(case, response).ok
        output = OUTPUT_MODELS[case.task].model_validate(response.parsed)
        if case.task is Task.access_request:
            assert output.decision == case.gold.decision  # type: ignore[union-attr]
        elif case.task is Task.access_review:
            assert [i.decision for i in output.items] == [  # type: ignore[union-attr]
                g.decision
                for g in case.gold.items  # type: ignore[union-attr]
            ]
        else:
            assert tuple(output.operations) == case.gold.operations  # type: ignore[union-attr]


# --------------------------------------------------------------------------------------------
# World-built cases (once the case builders exist)
# --------------------------------------------------------------------------------------------


def _check_world_cases(cases: list[Case], probe: Probe) -> None:
    assert cases
    baselines = [make_baseline(name) for name in NON_PROBE]
    baselines.append(make_baseline("surface_probe", probe=probe))
    for case in cases:
        for baseline in baselines:
            if not baseline.supports(case.task) or (
                baseline.name == "surface_probe" and case.task not in probe.tasks
            ):
                continue
            response: ModelResponse = baseline.respond(case)
            assert response.ok, (baseline.name, case.case_id, response.error)
            OUTPUT_MODELS[case.task].model_validate(response.parsed)
            if baseline.name == "always_escalate":
                assert set(decisions(response.parsed or {})) == {"ESCALATE"}
            if baseline.name == "oracle" and case.task is not Task.jml:
                gold_labels = (
                    [label_of(g.decision) for g in case.gold.items]  # type: ignore[union-attr]
                    if case.task is Task.access_review
                    else [label_of(case.gold.decision)]  # type: ignore[union-attr]
                )
                assert [label_of(d) for d in decisions(response.parsed or {})] == gold_labels


def test_world_built_request_cases(probe: Probe) -> None:
    requests = pytest.importorskip("identitybench.tasks.requests")
    pytest.importorskip("identitybench.tasks.ideal")
    from identitybench.world.generator import build_world

    world = build_world("startup", 7)
    cases = requests.build_request_cases(world, split=Split.test, n=24, seed=3)
    _check_world_cases(cases, probe)


def test_world_built_review_cases(probe: Probe) -> None:
    reviews = pytest.importorskip("identitybench.tasks.reviews")
    pytest.importorskip("identitybench.tasks.ideal")
    from identitybench.world.generator import build_world

    world = build_world("startup", 7)
    cases = reviews.build_review_cases(world, split=Split.test, n_campaigns=2, seed=3)
    _check_world_cases(cases, probe)


def test_world_built_jml_cases(probe: Probe) -> None:
    jml = pytest.importorskip("identitybench.tasks.jml")
    pytest.importorskip("identitybench.tasks.ideal")
    from identitybench.world.generator import build_world

    world = build_world("startup", 7)
    counts = {event_type: 1 for event_type in HrEventType}
    cases = jml.build_jml_cases(world, split=Split.test, counts=counts, seed=3)
    _check_world_cases(cases, probe)


def test_world_built_variant_cases(probe: Probe) -> None:
    """Every baseline answers adversarial copies and twins, including ``surface_probe`` (whose
    features must classify the copies' ``attack_field`` provenance) refitted from a dev list
    that holds them, as the runner's whole-split dev read does."""
    pytest.importorskip("identitybench.tasks.adversarial")
    pytest.importorskip("identitybench.tasks.twins")
    pytest.importorskip("identitybench.tasks.requests")
    pytest.importorskip("identitybench.tasks.reviews")
    pytest.importorskip("identitybench.tasks.ideal")
    built = world_cases_with_variants()
    variants = built.adversarial + built.twins
    _check_world_cases(variants, probe)
    world_probe = train_probe(built.base)
    refit = make_baseline(
        "surface_probe", params=world_probe.to_json(), dev_cases=[*built.base, *variants]
    )
    for case in variants:
        response = refit.respond(case)
        assert response.ok, (case.case_id, case.variant, response.error)
