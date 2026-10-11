"""Expected-vs-actual comparison for the conformance scenarios (SPEC §6.3, §7.2, §7.4, §9.1).

The effect tables and thresholds below are transcribed from the policy text (SPEC §7.4 and
§7.5) rather than imported from the rule registry or ``policy.constants``, so the oracle's
effects and numbers are checked independently too.

Every ``compare_*`` function returns a list of human-readable mismatches (empty means the oracle
agrees with the scenario). ``consistency_problems`` checks a scenario's expectation against the
spec's own rules and against the facts in its view: precedence, the certificate table, the cap
arithmetic, which single-field rules must fire, control validity, and that every expected claim
holds. A typo in a YAML file therefore fails loudly before the oracle ever sees it, instead of
showing up later as an apparent oracle disagreement.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, timedelta

from identitybench.dates import cap_ending_on, effective_duration, is_valid_on, parse_effective
from identitybench.schemas.case import AccessRequest, CaseView, ReviewItem
from identitybench.schemas.enums import Effect, RequestDecision, ReviewDecision
from identitybench.schemas.evidence import canonical, check_claim, index_view
from identitybench.schemas.gold import (
    Certificate,
    Claim,
    ExceptionEval,
    Finding,
    JmlGold,
    RequestGold,
    ReviewGold,
    ReviewItemGold,
)
from identitybench.schemas.ops import REQUIRED_FIELDS, Operation, OpType
from identitybench.world.entities import (
    EmploymentType,
    HrEventType,
    MitigatingControl,
    RiskTier,
    TicketStatus,
    TicketType,
    max_risk,
)
from tests.conformance.loader import (
    HORIZON_PAD_DAYS,
    ClaimExpect,
    ExceptionExpect,
    JmlExpect,
    RequestExpect,
    ReviewExpect,
    ReviewItemExpect,
    Scenario,
    SimulationExpect,
    expand_role,
)
from tests.conformance.scores import score_request_output
from tests.conformance.simulation import simulation_consistency

_DENY, _ESC, _COND, _REVOKE = Effect.DENY, Effect.ESCALATE, Effect.CONDITION, Effect.REVOKE

REQUEST_EFFECTS: dict[str, Effect] = {
    "LIFE-001": _DENY,
    "LIFE-002": _ESC,
    "LIFE-003": _DENY,
    "LIFE-004": _COND,
    "ELIG-001": _DENY,
    "ELIG-002": _DENY,
    "ELIG-003": _DENY,
    "ELIG-004": _DENY,
    "ELIG-005": _DENY,
    **{f"SOD-00{i}": _DENY for i in range(1, 7)},
    "MIT-003": _COND,
    "APPR-001": _ESC,
    "APPR-002": _ESC,
    "APPR-003": _DENY,
    "APPR-004": _COND,
    "TEMP-001": _COND,
    "TEMP-002": _COND,
    "JUST-001": _ESC,
}
"""SPEC §7.4, column REQ (MIT-001 is an exception, never a finding)."""

REVIEW_EFFECTS: dict[str, Effect] = {
    "LIFE-001": _REVOKE,
    "LIFE-003": _REVOKE,
    "ELIG-001": _REVOKE,
    "ELIG-002": _REVOKE,
    "ELIG-003": _REVOKE,
    "ELIG-005": _REVOKE,
    **{f"SOD-00{i}": _ESC for i in range(1, 7)},
    "MIT-002": _ESC,
    "APPR-005": _ESC,
    "HYG-001": _REVOKE,
    "HYG-002": _REVOKE,
    "HYG-003": _ESC,
    "HYG-004": _REVOKE,
    "HYG-005": _REVOKE,
}
"""SPEC §7.4, column REV."""

SOD_RULES: frozenset[str] = frozenset(f"SOD-00{i}" for i in range(1, 7))
CAP_RULES: frozenset[str] = frozenset(r for r, e in REQUEST_EFFECTS.items() if e is _COND)
EXCEPTION_RULE = "MIT-001"
"""SPEC §7.4: MIT-001 is "exception" in both the REQ and REV columns."""

JML_EVENT_RULES: dict[HrEventType, str] = {
    HrEventType.joiner: "JML-001",
    HrEventType.mover: "JML-002",
    HrEventType.leaver: "JML-003",
    HrEventType.rehire: "JML-004",
    HrEventType.conversion: "JML-005",
    HrEventType.leave_start: "JML-006",
    HrEventType.leave_end: "JML-007",
}
"""SPEC §7.5 event table."""

DORMANCY_DAYS = 90
"""HYG-001: dormant iff as_of − clock start > 90 days."""
MOVE_GRACE_DAYS = 14
"""HYG-005 and JML-002: old birthright goes on move date + 14 days."""
PRIVILEGED_CAP_DAYS = 7
"""TEMP-001: privileged entitlements are capped at 7 days per grant."""
BREAK_GLASS_CAP_DAYS = 1
"""APPR-004: valid break-glass requests are capped at 1 day."""
MIT_RENEWAL_WINDOW_DAYS = 30
"""MIT-002: a mitigated conflict whose control expires on or before as-of + 30 days."""

ClaimKey = tuple[str, str, str]
OpKey = tuple[tuple[str, ...], date]


# --------------------------------------------------------------------------------------------
# Spec-derived expectations
# --------------------------------------------------------------------------------------------


def request_decision_for(findings: Iterable[str]) -> RequestDecision:
    """SPEC §7.2: any DENY rule -> DENY; otherwise any ESCALATE rule -> ESCALATE; else APPROVE."""
    effects = {REQUEST_EFFECTS[rule] for rule in findings}
    if _DENY in effects:
        return RequestDecision.DENY
    if _ESC in effects:
        return RequestDecision.ESCALATE
    return RequestDecision.APPROVE


def review_decision_for(findings: Iterable[str]) -> ReviewDecision:
    """SPEC §7.2: REVOKE beats ESCALATE, which beats CERTIFY."""
    effects = {REVIEW_EFFECTS[rule] for rule in findings}
    if _REVOKE in effects:
        return ReviewDecision.REVOKE
    if _ESC in effects:
        return ReviewDecision.ESCALATE
    return ReviewDecision.CERTIFY


def _findings_with_effect(
    findings: Iterable[str], effects: Mapping[str, Effect], wanted: Effect | None
) -> frozenset[str]:
    if wanted is None:
        return frozenset()
    return frozenset(rule for rule in findings if effects.get(rule) is wanted)


def derived_request_certificates(expect: RequestExpect) -> frozenset[str]:
    """SPEC §9.1: a DENY (ESCALATE) has one certificate per DENY (ESCALATE) finding.

    APPROVE certificates depend on which caps bind, so scenarios write them out explicitly.
    """
    wanted = {RequestDecision.DENY: _DENY, RequestDecision.ESCALATE: _ESC}.get(expect.decision)
    return _findings_with_effect(expect.findings, REQUEST_EFFECTS, wanted)


def expected_request_certificates(expect: RequestExpect) -> frozenset[str]:
    if expect.certificates is not None:
        return frozenset(expect.certificates)
    return derived_request_certificates(expect)


def derived_item_certificates(item: ReviewItemExpect) -> frozenset[str]:
    """SPEC §9.1: one certificate per REVOKE (ESCALATE) finding; CERTIFY has none."""
    wanted = {ReviewDecision.REVOKE: _REVOKE, ReviewDecision.ESCALATE: _ESC}.get(item.decision)
    return _findings_with_effect(item.findings, REVIEW_EFFECTS, wanted)


def expected_item_certificates(item: ReviewItemExpect) -> frozenset[str]:
    if item.certificates is not None:
        return frozenset(item.certificates)
    return derived_item_certificates(item)


# --------------------------------------------------------------------------------------------
# Facts read off a view
# --------------------------------------------------------------------------------------------


def conferred(view: CaseView, ref: str) -> frozenset[str]:
    """Entitlements a review item confers: a role's whole expansion, or one assignment's."""
    if ref in view.memberships:
        return expand_role(view.roles, view.memberships[ref].role_id)
    return frozenset({view.assignments[ref].entitlement_id})


def holder_of(view: CaseView, ref: str) -> str | None:
    """The identity a review item belongs to; None for service and orphaned accounts."""
    if ref in view.memberships:
        return view.memberships[ref].identity_id
    return view.accounts[view.assignments[ref].account_id].identity_id


def item_risk_tier(view: CaseView, ref: str) -> RiskTier | None:
    """SPEC §12.1: the tier of the entitlement an item confers; a role takes its highest tier."""
    tiers = [view.entitlements[e].risk_tier for e in sorted(conferred(view, ref))]
    return max_risk(tiers) if tiers else None


def control_for(view: CaseView, identity_id: str | None, rule: str) -> MitigatingControl | None:
    """The control for one (identity, rule) pair; SPEC §6.2 allows at most one."""
    for control in view.mitigating_controls.values():
        if control.identity_id == identity_id and control.rule_id == rule:
            return control
    return None


def control_is_active(view: CaseView, identity_id: str | None, rule: str) -> bool:
    """SPEC §6.3: a control authorizes an exception only on days it is valid."""
    control = control_for(view, identity_id, rule)
    return control is not None and is_valid_on(control.granted_on, control.expires_on, view.as_of)


def valid_break_glass(view: CaseView, request: AccessRequest) -> bool:
    """SPEC §7.4 APPR-003: ``ticket_ref`` names an open incident in the ticket list, assigned to
    the beneficiary, whose related applications include the entitlement's application."""
    if not request.is_break_glass or request.ticket_ref is None:
        return False
    ticket = view.tickets.get(request.ticket_ref)
    return (
        ticket is not None
        and ticket.type is TicketType.incident
        and ticket.status is TicketStatus.open
        and ticket.assignee_id == request.beneficiary_id
        and view.entitlements[request.entitlement_id].app_id in ticket.related_app_ids
    )


# --------------------------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------------------------


def claim_key(claim: Claim | ClaimExpect) -> ClaimKey:
    return (claim.id, claim.field, canonical(claim.value))


def claim_sets_key(
    sets: Iterable[Iterable[Claim | ClaimExpect]],
) -> frozenset[frozenset[ClaimKey]]:
    """Claim sets compared as a set of sets: claim order and set order don't matter."""
    return frozenset(frozenset(claim_key(c) for c in claims) for claims in sets)


def _fmt_claim_sets(sets: frozenset[frozenset[ClaimKey]]) -> str:
    rendered = sorted(
        "[" + ", ".join(f"{i}.{f}={v}" for i, f, v in sorted(claims)) + "]" for claims in sets
    )
    return " | ".join(rendered) if rendered else "(none)"


def _fmt_duration(days: int | None) -> str:
    return "null (standing)" if days is None else f"{days} days"


def _exception_label(rule: str, for_rule: str, applies: bool) -> str:
    return f"{rule} for {for_rule} applies={str(applies).lower()}"


def op_key(op: Operation, as_of: date) -> OpKey:
    """(op, target fields) plus the effective *date*: ``IMMEDIATE`` equals the as-of date."""
    return (op.target_key(), parse_effective(op.effective, as_of))


def _fmt_op(key: OpKey) -> str:
    target, effective = key
    fields = REQUIRED_FIELDS[OpType(target[0])]
    args = " ".join(f"{name}={value}" for name, value in zip(fields, target[1:], strict=True))
    return f"{target[0]} {args} @ {effective.isoformat()}"


def _set_diff(label: str, expected: Iterable[str], actual: Sequence[str]) -> list[str]:
    want, got = set(expected), set(actual)
    problems: list[str] = []
    duplicated = sorted(k for k, n in Counter(actual).items() if n > 1)
    if duplicated:
        problems.append(f"{label}: duplicated {duplicated}")
    if want != got:
        problems.append(
            f"{label}: expected {sorted(want)}, got {sorted(got)} "
            f"(missing {sorted(want - got)}, unexpected {sorted(got - want)})"
        )
    return problems


def _findings_problems(
    label: str, expected: Iterable[str], findings: Sequence[Finding], effects: Mapping[str, Effect]
) -> list[str]:
    problems = _set_diff(f"{label}findings", expected, [f.rule for f in findings])
    for finding in findings:
        want = effects.get(finding.rule)
        if want is not None and finding.effect is not want:
            problems.append(
                f"{label}finding {finding.rule}: effect should be {want.value} (SPEC §7.4), "
                f"got {finding.effect.value}"
            )
    return problems


def _certificate_problems(
    label: str,
    expected_rules: Iterable[str],
    expect: RequestExpect | ReviewItemExpect,
    certificates: Sequence[Certificate],
) -> list[str]:
    problems = _set_diff(f"{label}certificates", expected_rules, [c.rule for c in certificates])
    by_rule = {c.rule: c for c in certificates}
    for rule, sets in sorted(expect.claim_sets.items()):
        certificate = by_rule.get(rule)
        if certificate is None:
            continue  # already reported as a missing certificate
        want, got = claim_sets_key(sets), claim_sets_key(certificate.claim_sets)
        if want != got:
            problems.append(
                f"{label}certificate {rule} claim_sets:\n"
                f"      expected {_fmt_claim_sets(want)}\n"
                f"      got      {_fmt_claim_sets(got)}"
            )
    for rule, claims in sorted(expect.claims_include.items()):
        certificate = by_rule.get(rule)
        if certificate is None:
            continue
        want_keys = frozenset(claim_key(c) for c in claims)
        got = claim_sets_key(certificate.claim_sets)
        if not got or any(not want_keys <= claim_set for claim_set in got):
            problems.append(
                f"{label}certificate {rule}: every claim set should include "
                f"{_fmt_claim_sets(frozenset({want_keys}))}\n"
                f"      got {_fmt_claim_sets(got)}"
            )
    return problems


def _exception_problems(
    label: str, expected: Sequence[ExceptionExpect], actual: Sequence[ExceptionEval]
) -> list[str]:
    want = [_exception_label(e.rule, e.for_rule, e.applies) for e in expected]
    got = [_exception_label(e.rule, e.for_rule, e.applies) for e in actual]
    return _set_diff(f"{label}exceptions", want, got)


# --------------------------------------------------------------------------------------------
# Oracle comparisons
# --------------------------------------------------------------------------------------------


def compare_request(scenario: Scenario, gold: RequestGold) -> list[str]:
    """Mismatches between a request scenario's expectation and ``decide_request``'s gold."""
    expect = scenario.expect
    request = scenario.view.request
    assert isinstance(expect, RequestExpect) and request is not None
    problems: list[str] = []
    if gold.decision is not expect.decision:
        problems.append(f"decision: expected {expect.decision.value}, got {gold.decision.value}")
    want_days = (
        expect.effective_duration_days if expect.decision is RequestDecision.APPROVE else None
    )
    if gold.effective_duration_days != want_days:
        problems.append(
            f"effective_duration_days: expected {_fmt_duration(want_days)}, "
            f"got {_fmt_duration(gold.effective_duration_days)}"
        )
    problems += _findings_problems("", expect.findings, gold.findings, REQUEST_EFFECTS)
    problems += _exception_problems("", expect.exceptions, gold.exceptions)
    problems += _certificate_problems(
        "", expected_request_certificates(expect), expect, gold.certificates
    )
    tier = scenario.view.entitlements[request.entitlement_id].risk_tier
    if gold.risk_tier is not tier:
        problems.append(
            f"risk_tier: expected {tier.value} (the requested entitlement's tier, SPEC §12.1), "
            f"got {gold.risk_tier.value}"
        )
    return problems


def _item_problems(
    view: CaseView, item: ReviewItem, expect: ReviewItemExpect, gold: ReviewItemGold
) -> list[str]:
    label = f"item {item.item_id} ({item.ref}) "
    problems: list[str] = []
    if gold.decision is not expect.decision:
        problems.append(
            f"{label}decision: expected {expect.decision.value}, got {gold.decision.value}"
        )
    problems += _findings_problems(label, expect.findings, gold.findings, REVIEW_EFFECTS)
    problems += _certificate_problems(
        label, expected_item_certificates(expect), expect, gold.certificates
    )
    problems += _exception_problems(label, expect.exceptions, gold.exceptions)
    tier = item_risk_tier(view, item.ref)
    if tier is not None and gold.risk_tier is not tier:
        problems.append(
            f"{label}risk_tier: expected {tier.value} (highest tier the item confers, "
            f"SPEC §12.1), got {gold.risk_tier.value}"
        )
    return problems


def compare_review(scenario: Scenario, gold: ReviewGold) -> list[str]:
    """Mismatches between a review scenario's per-item expectations and ``decide_review``."""
    expect = scenario.expect
    campaign = scenario.view.campaign
    assert isinstance(expect, ReviewExpect) and campaign is not None
    by_id: dict[str, list[ReviewItemGold]] = {}
    for item_gold in gold.items:
        by_id.setdefault(item_gold.item_id, []).append(item_gold)
    problems: list[str] = []
    for item in campaign.items:
        golds = by_id.pop(item.item_id, [])
        if len(golds) != 1:
            problems.append(f"item {item.item_id}: expected one gold record, got {len(golds)}")
            continue
        problems += _item_problems(scenario.view, item, expect.items[item.ref], golds[0])
    if by_id:
        problems.append(f"gold has items that aren't in the campaign: {sorted(by_id)}")
    return problems


def expected_horizon_end(scenario: Scenario) -> date:
    """SPEC §8.3, computed from the scenario's expected (correct) plan."""
    expect, event = scenario.expect, scenario.view.event
    assert isinstance(expect, JmlExpect) and event is not None
    dates = [parse_effective(op.effective, scenario.view.as_of) for op in expect.operations]
    return max([event.effective_on, *dates]) + timedelta(days=HORIZON_PAD_DAYS)


def compare_jml(scenario: Scenario, gold: JmlGold) -> list[str]:
    """Mismatches between a JML scenario's expected operations and ``plan_jml``'s plan.

    Operations are compared as a multiset of (op, target fields, effective date), so order
    doesn't matter but a duplicated operation does.
    """
    expect = scenario.expect
    assert isinstance(expect, JmlExpect)
    as_of = scenario.view.as_of
    want = Counter(op_key(op, as_of) for op in expect.operations)
    got = Counter(op_key(op, as_of) for op in gold.operations)
    problems: list[str] = []
    missing, unexpected = want - got, got - want
    if missing or unexpected:
        lines = ["operations differ:"]
        lines += [f"      missing    {_fmt_op(k)}" for k in sorted(missing.elements())]
        lines += [f"      unexpected {_fmt_op(k)}" for k in sorted(unexpected.elements())]
        problems.append("\n".join(lines))
    problems += _set_diff("findings", expect.findings, [f.rule for f in gold.findings])
    horizon = expected_horizon_end(scenario)
    if gold.horizon_end != horizon:
        problems.append(
            f"horizon_end: expected {horizon.isoformat()} (SPEC §8.3), "
            f"got {gold.horizon_end.isoformat()}"
        )
    return problems


def describe(scenario: Scenario, problems: Sequence[str]) -> str:
    """Failure message: which scenario, what it tests, the spec sentence, and every mismatch."""
    lines = [
        f"conformance scenario {scenario.id!r} ({scenario.source}) disagrees with the oracle",
        f"  tests: {scenario.what}",
        f"  spec:  {scenario.spec}",
    ]
    if scenario.ambiguity:
        lines.append(
            f"  ambiguity: {', '.join(scenario.ambiguity)} (see tests/conformance/AMBIGUITIES.md)"
        )
    lines += [f"  - {problem}" for problem in problems]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# Scored outputs (Appendix A's table of outputs A-C)
# --------------------------------------------------------------------------------------------


def _claim(expect: ClaimExpect) -> Claim:
    value = expect.value.isoformat() if isinstance(expect.value, date) else expect.value
    return Claim(id=expect.id, field=expect.field, value=value)


def expected_request_gold(scenario: Scenario) -> RequestGold:
    """The gold record a request scenario expects. Scoring needs every certificate's claims, so
    each certificate must have its ``claim_sets`` written out."""
    expect, request = scenario.expect, scenario.view.request
    assert isinstance(expect, RequestExpect) and request is not None
    rules = sorted(expected_request_certificates(expect))
    unpinned = [rule for rule in rules if rule not in expect.claim_sets]
    if unpinned:
        raise ValueError(f"{scenario.id}: certificates {unpinned} have no claim_sets to score")
    approve = expect.decision is RequestDecision.APPROVE
    return RequestGold(
        decision=expect.decision,
        effective_duration_days=expect.effective_duration_days if approve else None,
        risk_tier=scenario.view.entitlements[request.entitlement_id].risk_tier,
        findings=tuple(Finding(rule=r, effect=REQUEST_EFFECTS[r]) for r in expect.findings),
        exceptions=tuple(
            ExceptionEval.model_validate({"rule": e.rule, "for": e.for_rule, "applies": e.applies})
            for e in expect.exceptions
        ),
        certificates=tuple(
            Certificate(
                rule=rule,
                claim_sets=tuple(
                    tuple(_claim(c) for c in claims) for claims in expect.claim_sets[rule]
                ),
            )
            for rule in rules
        ),
    )


def scored_output_problems(scenario: Scenario) -> list[str]:
    """Each scored output's expected scores must be what the SPEC §12 and §13.1 definitions give
    against the scenario's expected gold. Only the scores a scenario writes are compared."""
    if not scenario.outputs:
        return []
    expect = scenario.expect
    assert isinstance(expect, RequestExpect)
    unpinned = sorted(expected_request_certificates(expect) - set(expect.claim_sets))
    if unpinned:
        return [f"scored outputs need claim_sets for every certificate; missing {unpinned}"]
    gold = expected_request_gold(scenario)
    problems: list[str] = []
    for scored in scenario.outputs:
        actual = score_request_output(scenario.view, gold, scored.output)
        for name in sorted(scored.expect.model_fields_set):
            want, got = getattr(scored.expect, name), getattr(actual, name)
            if want != got:
                problems.append(
                    f"output {scored.label}: expected {name}={want!r}, but the SPEC §12/§13.1 "
                    f"definitions give {got!r}"
                )
    return problems


# --------------------------------------------------------------------------------------------
# Self-consistency of the expectations (runs without the oracle)
# --------------------------------------------------------------------------------------------


def _claim_problems(
    view: CaseView,
    expect: RequestExpect | ReviewItemExpect,
    certificates: frozenset[str],
    label: str = "",
) -> list[str]:
    """Expected claims belong to certificates and hold in the view, as ``check_claim`` reads it
    (SPEC §8.1); a certificate pins exact claim sets or included claims, not both."""
    problems: list[str] = []
    for kind, rules in (
        ("claim_sets", expect.claim_sets),
        ("claims_include", expect.claims_include),
    ):
        stray = sorted(set(rules) - certificates)
        if stray:
            problems.append(f"{label}{kind} given for non-certificate rules {stray}")
    both = sorted(set(expect.claim_sets) & set(expect.claims_include))
    if both:
        problems.append(f"{label}claim_sets and claims_include both given for {both}")
    claims: list[ClaimExpect] = [c for sets in expect.claim_sets.values() for s in sets for c in s]
    claims += [c for included in expect.claims_include.values() for c in included]
    index = index_view(view)
    false_claims = [
        f"{c.id}.{c.field}={canonical(c.value)}"
        for c in claims
        if not check_claim(index, c.id, c.field, c.value)
    ]
    if false_claims:
        problems.append(
            f"{label}expected claims that don't hold in the view (SPEC §8.1): {false_claims}"
        )
    return problems


def _cap_applicability(view: CaseView, request: AccessRequest, expect: RequestExpect) -> list[str]:
    """Rules whose predicate is one field (or the break-glass ticket) must fire exactly when it
    holds. Under reading A1 that is independent of the final decision."""
    beneficiary = view.identities[request.beneficiary_id]
    entitlement = view.entitlements[request.entitlement_id]
    valid = valid_break_glass(view, request)
    applies: dict[str, tuple[bool, str]] = {
        "LIFE-004": (
            beneficiary.employment_type is EmploymentType.contractor,
            "LIFE-004 applies to every contractor request",
        ),
        "TEMP-001": (entitlement.privileged, "TEMP-001 applies iff the entitlement is privileged"),
        "TEMP-002": (
            entitlement.max_duration_days is not None,
            "TEMP-002 applies iff the entitlement has a max duration",
        ),
        "APPR-004": (valid, "APPR-004 applies iff the break-glass request is valid"),
        "APPR-003": (
            request.is_break_glass and not valid,
            "APPR-003 applies iff a break-glass request's ticket_ref isn't an open incident "
            "assigned to the beneficiary and related to the entitlement's application",
        ),
    }
    findings = set(expect.findings)
    return [
        f"{reason} (SPEC §7.4), so it should {'' if holds else 'not '}be a finding"
        for rule, (holds, reason) in applies.items()
        if (rule in findings) != holds
    ]


def _cap_values(
    view: CaseView, request: AccessRequest, expect: RequestExpect
) -> tuple[dict[str, int], list[str]]:
    """Each applicable cap in days, computed from the view (SPEC §6.3 and §7.4)."""
    beneficiary = view.identities[request.beneficiary_id]
    entitlement = view.entitlements[request.entitlement_id]
    caps: dict[str, int] = {}
    problems: list[str] = []
    for rule in sorted(set(expect.findings) & CAP_RULES):
        if rule == "LIFE-004":
            if beneficiary.contract_end_date is None:
                problems.append("LIFE-004 needs the contractor's contract_end_date")
                continue
            caps[rule] = cap_ending_on(beneficiary.contract_end_date, view.as_of)
        elif rule == "MIT-003":
            expiries = [
                control.expires_on
                for e in expect.exceptions
                if e.applies
                and (control := control_for(view, request.beneficiary_id, e.for_rule)) is not None
            ]
            if not expiries:
                problems.append("MIT-003 needs the control that MIT-001 applied")
                continue
            caps[rule] = min(cap_ending_on(end, view.as_of) for end in expiries)
        elif rule == "TEMP-001":
            caps[rule] = PRIVILEGED_CAP_DAYS
        elif rule == "TEMP-002":
            if entitlement.max_duration_days is None:
                problems.append("TEMP-002 needs the entitlement's max_duration_days")
                continue
            caps[rule] = entitlement.max_duration_days
        else:
            caps[rule] = BREAK_GLASS_CAP_DAYS
    return caps, problems


def _approve_consistency(
    request: AccessRequest, expect: RequestExpect, caps: Mapping[str, int]
) -> list[str]:
    """SPEC §6.3 effective duration and the §9.1 certificate rule for an APPROVE."""
    problems: list[str] = []
    if expect.certificates is None:
        problems.append("APPROVE scenarios must list certificates (binding caps, SPEC §9.1)")
    requested = request.requested_duration_days
    want = effective_duration(requested, caps.values())
    if expect.effective_duration_days != want:
        problems.append(
            f"effective_duration_days should be {_fmt_duration(want)}: min(requested "
            f"{_fmt_duration(requested)}, caps {dict(sorted(caps.items()))}) (SPEC §6.3)"
        )
    binds = want is not None and (requested is None or want < requested)
    binding = {rule for rule, days in caps.items() if binds and days == want}
    if expect.certificates is not None and set(expect.certificates) != binding:
        problems.append(
            f"APPROVE certificates should be the binding caps {sorted(binding)} (SPEC §9.1: "
            "'Each binding cap rule. Tied caps are alternatives.')"
        )
    return problems


def _request_consistency(scenario: Scenario, expect: RequestExpect) -> list[str]:
    view, request = scenario.view, scenario.view.request
    assert request is not None
    unknown = sorted(set(expect.findings) - set(REQUEST_EFFECTS))
    if unknown:
        return [f"findings {unknown} have no request effect in SPEC §7.4"]
    problems: list[str] = []
    if expect.decision is not request_decision_for(expect.findings):
        problems.append(
            f"decision {expect.decision.value} contradicts SPEC §7.2 precedence over findings "
            f"{sorted(expect.findings)}"
        )
    problems += _cap_applicability(view, request, expect)
    caps, cap_problems = _cap_values(view, request, expect)
    problems += cap_problems
    if expect.decision is RequestDecision.APPROVE:
        if not cap_problems:
            problems += _approve_consistency(request, expect, caps)
    else:
        if expect.effective_duration_days is not None:
            problems.append("effective_duration_days is only set for APPROVE")
        if expect.certificates is not None and set(expect.certificates) != set(
            derived_request_certificates(expect)
        ):
            problems.append("explicit certificates contradict the SPEC §9.1 certificate rule")
    problems += _exception_consistency(view, request.beneficiary_id, expect.exceptions, expect)
    mitigated = any(e.applies for e in expect.exceptions)
    if mitigated != ("MIT-003" in expect.findings):
        problems.append("MIT-003 applies iff MIT-001 excused a conflict (SPEC §7.4)")
    problems += _near_miss_consistency(expect.near_miss, expect.findings, REQUEST_EFFECTS)
    problems += _claim_problems(view, expect, expected_request_certificates(expect))
    problems += scored_output_problems(scenario)
    return problems


def _exception_consistency(
    view: CaseView,
    holder: str | None,
    exceptions: Sequence[ExceptionExpect],
    expect: RequestExpect | ReviewItemExpect,
) -> list[str]:
    """MIT-001 is evaluated for every SoD conflict in play (SPEC §9.1, reading A2), and it
    applies exactly when the holder's control for that rule is valid on the as-of date."""
    problems: list[str] = []
    findings = set(expect.findings)
    for exception in exceptions:
        rule = exception.for_rule
        if exception.rule != EXCEPTION_RULE or rule not in SOD_RULES:
            problems.append(f"unexpected exception {exception.rule} for {rule}")
            continue
        if exception.applies and rule in findings:
            problems.append(f"MIT-001 applies, so {rule} can't be a finding")
        elif not exception.applies and rule not in findings:
            problems.append(f"MIT-001 doesn't apply, so {rule} must be a finding")
        active = control_is_active(view, holder, rule)
        if exception.applies != active:
            problems.append(
                f"MIT-001 for {rule}: applies should be {str(active).lower()}, since {holder} "
                f"{'has' if active else 'has no'} control for it valid on the as-of date "
                "(SPEC §6.3)"
            )
    evaluated = {e.for_rule for e in exceptions}
    for rule in sorted(findings & SOD_RULES):
        if rule not in evaluated:
            problems.append(f"{rule} fired, so MIT-001 must be listed as evaluated for it")
    return problems


def _near_miss_consistency(
    near_miss: Iterable[str], findings: Iterable[str], effects: Mapping[str, Effect]
) -> list[str]:
    problems: list[str] = []
    for rule in near_miss:
        if rule not in effects:
            problems.append(f"near_miss {rule} has no effect for this task")
        if rule in findings:
            problems.append(f"near_miss {rule} is also a finding")
    return problems


def _mit002_consistency(view: CaseView, holder: str | None, item: ReviewItemExpect) -> list[str]:
    """SPEC §7.4 MIT-002: an item of a mitigated conflict escalates iff the control expires on
    or before as-of + 30 days."""
    window_end = view.as_of + timedelta(days=MIT_RENEWAL_WINDOW_DAYS)
    renewal_due = False
    for exception in item.exceptions:
        control = control_for(view, holder, exception.for_rule)
        if exception.applies and control is not None:
            renewal_due = renewal_due or control.expires_on <= window_end
    if renewal_due and "MIT-002" not in item.findings:
        return [
            "MIT-002 should be a finding: a mitigated conflict's control expires on or "
            "before as-of + 30 days"
        ]
    if not renewal_due and "MIT-002" in item.findings:
        return [
            "MIT-002 needs a mitigated conflict (MIT-001 applies=true) whose control "
            "expires on or before as-of + 30 days"
        ]
    return []


def _review_consistency(scenario: Scenario, expect: ReviewExpect) -> list[str]:
    view, campaign = scenario.view, scenario.view.campaign
    assert campaign is not None
    refs = {item.ref for item in campaign.items}
    expected = set(expect.items)
    problems: list[str] = []
    if refs != expected:
        problems.append(
            f"expectations must cover exactly the campaign items: missing "
            f"{sorted(refs - expected)}, extra {sorted(expected - refs)}"
        )
    for ref, item in sorted(expect.items.items()):
        unknown = sorted(set(item.findings) - set(REVIEW_EFFECTS))
        if unknown:
            problems.append(f"{ref}: findings {unknown} have no review effect in SPEC §7.4")
            continue
        if item.decision is not review_decision_for(item.findings):
            problems.append(f"{ref}: decision contradicts SPEC §7.2 precedence")
        if item.certificates is not None and set(item.certificates) != set(
            derived_item_certificates(item)
        ):
            problems.append(f"{ref}: explicit certificates contradict SPEC §9.1")
        problems += [
            f"{ref}: {p}"
            for p in _near_miss_consistency(item.near_miss, item.findings, REVIEW_EFFECTS)
        ]
        if ref not in refs:
            continue
        holder = holder_of(view, ref)
        problems += [
            f"{ref}: {p}" for p in _exception_consistency(view, holder, item.exceptions, item)
        ]
        problems += [f"{ref}: {p}" for p in _mit002_consistency(view, holder, item)]
        problems += _claim_problems(view, item, expected_item_certificates(item), f"{ref}: ")
    return problems


def _jml_consistency(scenario: Scenario, expect: JmlExpect) -> list[str]:
    event = scenario.view.event
    assert event is not None
    problems: list[str] = []
    rule = JML_EVENT_RULES[event.type]
    if list(expect.findings) != [rule]:
        problems.append(f"a {event.type.value} event's finding is {rule} (SPEC §7.5)")
    view = scenario.view
    pools: dict[str, Mapping[str, object]] = {
        "identity_id": view.identities,
        "role_id": view.roles,
        "membership_id": view.memberships,
        "entitlement_id": view.entitlements,
        "assignment_id": view.assignments,
        "account_id": view.accounts,
        "new_owner_id": view.identities,
    }
    for op in expect.operations:
        if parse_effective(op.effective, view.as_of) < view.as_of:
            problems.append(f"gold operation {op.op.value} is retroactive (SPEC §7.5)")
        for field in REQUIRED_FIELDS[op.op]:
            value = getattr(op, field)
            if value not in pools[field]:
                problems.append(f"gold operation {op.op.value}: {field} {value} is not in the view")
    return problems


def consistency_problems(scenario: Scenario) -> list[str]:
    """Problems with a scenario's expectation that the spec and its view alone can detect."""
    expect = scenario.expect
    if isinstance(expect, RequestExpect):
        return _request_consistency(scenario, expect)
    if isinstance(expect, ReviewExpect):
        return _review_consistency(scenario, expect)
    if isinstance(expect, SimulationExpect):
        return simulation_consistency(scenario, expect)
    return _jml_consistency(scenario, expect)
