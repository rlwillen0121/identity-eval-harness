"""Fixtures for the ``scoring.served`` tests in scoring and the judge: tool-use predictions on
real cases whose evidence cites a record only the tools served, and a context-mode run whose
outputs must stay byte for byte what they were before tool-use evidence was integrated.

Cases are cut from a generated startup world by the real view builder
(``tests.render.world_cases``), and a real ``render.tools.ToolEnvironment`` records each trace:
the agent asks for the beneficiary's manager chain, whose top (the CEO) is a world record the
view doesn't hold. Answers are ``tasks.ideal.ideal_output`` (a correct, fully grounded answer)
with extra evidence claims appended, so whether a unit is grounded turns on those claims alone.

JML cases from the same world get a trace of an agent that asked ``get_entitlement`` for a
world entitlement outside the view, in an application where the event's subject has an active
account, and a plan that is gold's plus a grant of it: excess access in the world, a fabricated
operation against the view alone. Others get the trace of an agent that listed a peer's access
(``list_access`` on an identity outside the view) and a plan copying one of the peer's
entitlements, whose ID the agent read only in the peer's assignment, to the event's subject.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from identitybench.render.tools import ToolEnvironment
from identitybench.schemas.case import Case
from identitybench.schemas.enums import Effect
from identitybench.schemas.evidence import index_view
from identitybench.schemas.gold import Certificate, Claim, Finding, RequestGold
from identitybench.schemas.ops import Operation, OpType
from identitybench.schemas.predictions import Prediction, ToolTrace
from identitybench.tasks.ideal import ideal_output
from identitybench.world.entities import (
    Account,
    AccountKind,
    AccountStatus,
    Assignment,
    AssignmentSource,
    RiskTier,
    Role,
    RoleKind,
    World,
)
from identitybench.world.generator import build_world
from tests.render.world_cases import jml_cases, request_cases
from tests.scoring.builders import (
    AS_OF,
    baseline_predictions,
    claim_dict,
    entitlement,
    identity,
    leaver_case,
    oracle_output,
    oracle_predictions,
    predict,
    request_case,
    request_output,
    request_view,
    review_case,
    review_output,
)

TOOL_CONFIG = "tooly"


class FakeServed:
    """A served index: the view's index plus the record it holds under each requested ID, with
    every call logged."""

    def __init__(self, records: Mapping[str, BaseModel] | None = None) -> None:
        self.records = dict(records or {})
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, case: Case, ids: Sequence[str], /) -> dict[str, BaseModel]:
        self.calls.append((case.case_id, tuple(ids)))
        index = index_view(case.view)
        index.update({i: self.records[i] for i in ids if i in self.records})
        return index


def trace(*fetched: str) -> ToolTrace:
    return ToolTrace(calls=1, fetched_ids=fetched, tool_names=("get_records",))


@functools.cache
def chain_world() -> World:
    return build_world("startup", 11)


@dataclass(frozen=True)
class ChainCase:
    """A request case, the trace of an agent that fetched its beneficiary's manager chain, and
    two world identities outside the view: one the tools served, one they never did."""

    case: Case
    trace: ToolTrace
    served_id: str
    """The top of the chain: returned by the tools, absent from the view."""
    unserved_id: str
    """A world identity neither in the view nor returned by any tool."""

    def title(self, identity_id: str) -> str:
        return chain_world().identities[identity_id].title


def chain_trace(case: Case, world: World, *, base: Case | None = None) -> ToolTrace:
    """The trace of one ``get_manager_chain`` call on the request's beneficiary."""
    request = case.view.request
    assert request is not None
    env = ToolEnvironment(case, world, base=base)
    env.call("get_manager_chain", {"identity_id": request.beneficiary_id})
    return ToolTrace(
        calls=1,
        fetched_ids=env.fetched_ids,
        tool_names=("get_manager_chain",),
        requested_ids=env.requested_ids,
    )


@functools.cache
def chain_cases() -> tuple[ChainCase, ...]:
    """The world's request cases with a gold certificate whose chain leaves the view."""
    world = chain_world()
    found: list[ChainCase] = []
    for case in request_cases(world, 6, 11):
        trace = chain_trace(case, world)
        view = index_view(case.view)
        outside = [i for i in trace.fetched_ids if i not in view]
        if not outside or not isinstance(case.gold, RequestGold) or not case.gold.certificates:
            continue
        unserved = next(
            i for i in sorted(world.identities) if i not in view and i not in trace.fetched_ids
        )
        found.append(ChainCase(case, trace, outside[-1], unserved))
    assert found, "no request case's manager chain leaves its view"
    return tuple(found)


def with_claims(
    case: Case, claims: Sequence[Mapping[str, Any]], *, rationale: str = ""
) -> dict[str, Any]:
    """``ideal_output(case)`` with ``claims`` appended to its evidence (and ``rationale`` to
    its rationale)."""
    output = ideal_output(case)
    output["citations"]["evidence"] = [*output["citations"]["evidence"], *claims]
    if rationale:
        output["rationale"] = f"{output['rationale']} {rationale}"
    return output


def chain_prediction(
    chain: ChainCase,
    claims: Sequence[Mapping[str, Any]],
    *,
    traced: bool = True,
    rationale: str = "",
    config_id: str = TOOL_CONFIG,
) -> Prediction:
    """A correct answer to ``chain.case`` citing ``claims`` too, with the chain's trace (or
    none, as a context-mode answer)."""
    return predict(
        chain.case,
        with_claims(chain.case, claims, rationale=rationale),
        config_id=config_id,
        tool_trace=chain.trace if traced else None,
    )


def top_claim(chain: ChainCase, *, value: str | None = None, entity: str | None = None):
    """A claim on the title of the chain's top (or of ``entity``), true unless ``value``."""
    target = entity or chain.served_id
    return claim_dict(target, "title", value if value is not None else chain.title(target))


# --------------------------------------------------------------------------------------------
# Hand-built records outside ``leaver_case``'s view
# --------------------------------------------------------------------------------------------

ENT_WORLD = entitlement("ent_w", risk_tier=RiskTier.high)
ENT_CHILD = entitlement("ent_c", risk_tier=RiskTier.low)
ROLE_CHILD = Role(id="role_c", name="Child", kind=RoleKind.business, entitlements=("ent_c",))
ROLE_WORLD = Role(
    id="role_w",
    name="World role",
    kind=RoleKind.business,
    entitlements=("ent_w",),
    child_roles=("role_c",),
)
OUTSIDER_ACCOUNT = Account(
    id="acct_x",
    app_id="app_1",
    identity_id="u_x",
    kind=AccountKind.human,
    status=AccountStatus.active,
)
OUTSIDER_ASSIGNMENT = Assignment(
    id="as_x",
    account_id="acct_x",
    entitlement_id="ent_1",
    source=AssignmentSource.request,
    granted_on=AS_OF,
)
PEER_ASSIGNMENT = Assignment(
    id="as_w",
    account_id="acct_x",
    entitlement_id="ent_w",
    source=AssignmentSource.request,
    granted_on=AS_OF,
)
CLOSED_ACCOUNT = Account(
    id="acct_d",
    app_id="app_1",
    identity_id="u_x",
    kind=AccountKind.human,
    status=AccountStatus.disabled,
)
LEAVER_WORLD: Mapping[str, BaseModel] = {
    record.id: record
    for record in (
        ENT_WORLD,
        ENT_CHILD,
        ROLE_CHILD,
        ROLE_WORLD,
        identity("u_x"),
        OUTSIDER_ACCOUNT,
        OUTSIDER_ASSIGNMENT,
        PEER_ASSIGNMENT,
        CLOSED_ACCOUNT,
    )
}
"""Records outside ``builders.leaver_case``'s view, as a world would serve them: a role with a
child role and their entitlements, and an outsider with an active account holding the view's
entitlement and the world role's, and a disabled account."""


def leaver_world() -> FakeServed:
    return FakeServed(LEAVER_WORLD)


def now(op: OpType, **targets: str) -> Operation:
    return Operation(op=op, effective="IMMEDIATE", **targets)


def plan_plus(case: Case, *extra: Operation, **fields: Any) -> Prediction:
    """Gold's plan for ``case`` plus ``extra``, with ``fields`` (a trace) on the prediction."""
    output = oracle_output(case)
    output["operations"] += [op.model_dump(mode="json", exclude_none=True) for op in extra]
    return predict(case, output, **fields)


def closing_plan(case: Case, **fields: Any) -> Prediction:
    """Gold's plan for ``builders.leaver_case`` plus disabling ``acct_d``, an account the world
    already disabled, fetched by the agent: a no-op, so the plan is exact, but only when it is
    simulated against the served records (against the view alone ``acct_d`` is fabricated)."""
    return plan_plus(
        case,
        now(OpType.DISABLE_ACCOUNT, account_id="acct_d"),
        tool_trace=trace("u_x", "acct_d"),
        **fields,
    )


# --------------------------------------------------------------------------------------------
# JML plans that grant a world entitlement the tools served
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GrantCase:
    """A JML case whose subject has an active account in an application with two world
    entitlements outside the view: one the agent fetched with ``get_entitlement``, one no tool
    ever returned."""

    case: Case
    trace: ToolTrace
    served_id: str
    unserved_id: str

    def prediction(self, entitlement_id: str | None = None, *, traced: bool = True) -> Prediction:
        """Gold's plan plus an immediate grant of ``entitlement_id`` (the served one by default)
        to the event's subject, with this case's trace (or none, as a context-mode answer)."""
        event = self.case.view.event
        assert event is not None
        grant = Operation(
            op=OpType.GRANT,
            identity_id=event.identity_id,
            entitlement_id=entitlement_id or self.served_id,
            effective="IMMEDIATE",
        )
        output = ideal_output(self.case)
        output["operations"] = [
            *output["operations"],
            grant.model_dump(mode="json", exclude_none=True),
        ]
        return predict(
            self.case,
            output,
            config_id=TOOL_CONFIG,
            tool_trace=self.trace if traced else None,
        )


@functools.cache
def grant_cases() -> tuple[GrantCase, ...]:
    """The world's JML cases whose subject has an active account in an application with at
    least two entitlements outside the view."""
    world = chain_world()
    found: list[GrantCase] = []
    for case in jml_cases(world, 2, 11):
        view = case.view
        event = view.event
        assert event is not None
        apps = sorted(
            account.app_id
            for account in view.accounts.values()
            if account.identity_id == event.identity_id
            and account.kind is AccountKind.human
            and account.status is AccountStatus.active
        )
        for app_id in apps:
            outside = sorted(
                e.id
                for e in world.entitlements.values()
                if e.app_id == app_id and e.id not in view.entitlements
            )
            if len(outside) < 2:
                continue
            env = ToolEnvironment(case, world)
            env.call("get_entitlement", {"entitlement_id": outside[0]})
            trace = ToolTrace(
                calls=1,
                fetched_ids=env.fetched_ids,
                tool_names=("get_entitlement",),
                requested_ids=env.requested_ids,
            )
            assert outside[0] in trace.fetched_ids and outside[1] not in trace.fetched_ids
            found.append(GrantCase(case, trace, outside[0], outside[1]))
            break
    assert found, "no JML case's subject has an account in an application the view cuts short"
    return tuple(found)


@dataclass(frozen=True)
class PeerCase:
    """A JML case and the trace of an agent that listed the access of a peer outside the view,
    which showed an assignment of an entitlement outside the view: the agent read that
    entitlement's ID in the assignment, and no tool returned its record."""

    case: Case
    trace: ToolTrace
    account_id: str
    """The peer's account holding the assignment."""
    entitlement_id: str

    def prediction(self, *extra: Operation) -> Prediction:
        """Gold's plan plus an immediate grant of the peer's entitlement to the event's
        subject, plus ``extra``, with this case's trace."""
        event = self.case.view.event
        assert event is not None
        grant = Operation(
            op=OpType.GRANT,
            identity_id=event.identity_id,
            entitlement_id=self.entitlement_id,
            effective="IMMEDIATE",
        )
        output = ideal_output(self.case)
        output["operations"] = [
            *output["operations"],
            *(op.model_dump(mode="json", exclude_none=True) for op in (grant, *extra)),
        ]
        return predict(self.case, output, config_id=TOOL_CONFIG, tool_trace=self.trace)


@functools.cache
def peer_cases() -> tuple[PeerCase, ...]:
    """The world's JML cases with, for each, the first peer outside the view (by ID) whose
    ``list_access`` listing shows an assignment of an entitlement outside the view."""
    world = chain_world()
    found: list[PeerCase] = []
    for case in jml_cases(world, 2, 11):
        known = index_view(case.view)
        for peer_id in sorted(i for i in world.identities if i not in known):
            env = ToolEnvironment(case, world)
            env.call("list_access", {"identity_id": peer_id})
            fetched = env.fetched_ids
            listed = env.served_index(fetched)
            copied = next(
                (
                    record
                    for record_id in fetched
                    if isinstance(record := listed[record_id], Assignment)
                    and record_id not in known
                    and record.entitlement_id not in known
                    and record.entitlement_id not in fetched
                ),
                None,
            )
            if copied is None:
                continue
            trace = ToolTrace(
                calls=1,
                fetched_ids=fetched,
                tool_names=("list_access",),
                requested_ids=env.requested_ids,
            )
            found.append(PeerCase(case, trace, copied.account_id, copied.entitlement_id))
            break
    assert found, "no peer outside a JML case's view holds an entitlement outside it"
    return tuple(found)


# --------------------------------------------------------------------------------------------
# A context-mode run
# --------------------------------------------------------------------------------------------

ELIG_CERTIFICATE = Certificate(
    rule="ELIG-004", claim_sets=((Claim(id="ent_1", field="requestable", value=False),),)
)
"""A gold DENY's certificate: the requested entitlement isn't requestable."""


def context_cases() -> list[Case]:
    view = request_view(
        entitlements={"ent_1": entitlement(requestable=False)}, identities={"u_1": identity()}
    )
    deny = [
        request_case(
            case_id,
            "DENY",
            view=view,
            findings=(Finding(rule="ELIG-004", effect=Effect.DENY),),
            certificates=(ELIG_CERTIFICATE,),
        )
        for case_id in ("r1", "r2", "r3")
    ]
    approve = request_case(
        "r4",
        "APPROVE",
        duration=30,
        requested=30,
        view=request_view(requested=30, identities={"u_1": identity()}),
    )
    review = review_case(
        "c1", [("rv_1", "REVOKE"), ("rv_2", "CERTIFY"), ("rv_3", "ESCALATE", RiskTier.high)]
    )
    return [*deny, approve, review, leaver_case("j1")]


def context_predictions(cases: list[Case]) -> list[Prediction]:
    """A context-mode model with grounded, ungrounded and false claims on every task, plus the
    always-escalate and oracle baselines. The model also has a second repeat of ``r1`` (another
    label) and of ``j1`` (a plan that stops after its first operation), so a summary holds
    ``ConfigScores.repeats`` with label and timeline disagreement."""
    by_id = {c.case_id: c for c in cases}
    true_claim = claim_dict("ent_1", "requestable", False)
    model = [
        predict(
            by_id["r1"],
            request_output("DENY", rules=["ELIG-004"], evidence=[true_claim], confidence=0.9),
            config_id="m",
        ),
        predict(
            by_id["r2"],
            request_output(
                "DENY",
                rules=["ELIG-004", "SOD-001"],
                evidence=[
                    claim_dict("ent_1", "requestable", True),
                    claim_dict("u_9", "status", "active"),
                ],
                confidence=0.7,
            ),
            config_id="m",
        ),
        predict(by_id["r3"], request_output("APPROVE", cap=10, confidence=0.4), config_id="m"),
        predict(
            by_id["r4"],
            request_output(
                "APPROVE",
                cap=30,
                evidence=[claim_dict("u_1", "title", "Analyst")],
                confidence=0.8,
            ),
            config_id="m",
        ),
        predict(
            by_id["c1"],
            review_output(
                [("rv_1", "REVOKE"), ("rv_2", "REVOKE"), ("rv_3", "CERTIFY")],
                citations={"rules": [], "evidence": [claim_dict("rv_1", "ref", "rm_rv_1")]},
            ),
            config_id="m",
        ),
        predict(by_id["j1"], oracle_output(by_id["j1"]), config_id="m"),
    ]
    plan = oracle_output(by_id["j1"])
    repeats = [
        predict(
            by_id["r1"], request_output("ESCALATE", confidence=0.5), config_id="m", sample_idx=1
        ),
        predict(
            by_id["j1"],
            {**plan, "operations": plan["operations"][:1]},
            config_id="m",
            sample_idx=1,
        ),
    ]
    return [
        *model,
        *repeats,
        *baseline_predictions(cases, "always_escalate"),
        *oracle_predictions(cases),
    ]
