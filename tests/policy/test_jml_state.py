"""JML state simulation and plan comparison (SPEC §7.5 transition table, §8.3, §12.4)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from hypothesis import given

from identitybench.policy.jml import plan_jml
from identitybench.policy.jml_state import (
    DayState,
    OpOutcome,
    PlanComparison,
    Timeline,
    compare,
    score_plan,
    simulate,
)
from identitybench.schemas.case import CaseView
from identitybench.schemas.ops import Operation, OpType
from identitybench.world.entities import (
    AccountStatus,
    AssignmentSource,
    HrEventType,
    IdentityStatus,
    RoleKind,
    SuspensionReason,
    TerminationType,
)
from tests.policy.jml_fixtures import (
    AS_OF,
    MOVE,
    SUBJECT,
    TERMINATION,
    account,
    assignment,
    attrs,
    day,
    hr_event,
    jml_views,
    leaver_case,
    make_view,
    membership,
    mover_case,
    person,
    property_settings,
    role,
    service_account,
)

ACTIVE, SUSPENDED, DISABLED = AccountStatus.active, AccountStatus.suspended, AccountStatus.disabled
LEAVE, HOLD = SuspensionReason.leave, SuspensionReason.security_hold
HORIZON = day(10)


def op(kind: OpType, effective: date | str = "IMMEDIATE", **targets: str) -> Operation:
    when = effective if isinstance(effective, str) else effective.isoformat()
    return Operation(op=kind, effective=when, **targets)


def run(view: CaseView, *ops: Operation, horizon: date = HORIZON) -> Timeline:
    return simulate(view, list(ops), horizon)


def on(timeline: Timeline, when: date) -> DayState:
    return timeline.days[(when - timeline.as_of).days]


def status_of(
    timeline: Timeline, when: date, account_id: str
) -> tuple[AccountStatus, SuspensionReason | None]:
    snapshot = on(timeline, when).accounts[account_id]
    return snapshot.status, snapshot.suspension_reason


def outcomes(timeline: Timeline) -> list[OpOutcome]:
    return [result.outcome for result in timeline.ops]


def world() -> CaseView:
    """A subject with accounts in every status, a service account and a colleague's records."""
    return make_view(
        hr_event(HrEventType.leave_start, day(2)),
        person(manager_id="u_mgr"),
        identities=[
            person("u_mgr"),
            person("u_gone", status=IdentityStatus.terminated),
            person("u_peer"),
        ],
        memberships=[
            membership("rm_emp", "role_br_employee"),
            membership("rm_sales", "role_br_sales"),
            membership("rm_peer", "role_br_sales", identity_id="u_peer"),
        ],
        accounts=[
            account("acct_hr", "app_hr"),
            account("acct_crm", "app_crm"),
            account("acct_ledger", "app_ledger", status=SUSPENDED, reason=HOLD),
            account("acct_idp", "app_idp", status=DISABLED),
            account("acct_peer_crm", "app_crm", identity_id="u_peer"),
            service_account("acct_svc_etl", "app_ledger"),
        ],
        assignments=[
            assignment("as_crm_admin", "acct_crm", "ent_crm_admin"),
            assignment("as_gl_view", "acct_ledger", "ent_gl_view", source=AssignmentSource.direct),
            assignment("as_svc_view", "acct_svc_etl", "ent_gl_view"),
            assignment("as_peer_admin", "acct_peer_crm", "ent_crm_admin"),
        ],
    )


# --------------------------------------------------------------------------------------------
# State and effective access
# --------------------------------------------------------------------------------------------


def test_empty_plan_keeps_the_starting_state_every_day() -> None:
    view = world()
    timeline = run(view)
    assert [s.day for s in timeline.days] == [day(i) for i in range(11)]
    first = timeline.days[0]
    assert all(replace(s, day=AS_OF) == first for s in timeline.days)
    assert first.sources == set(view.memberships) | set(view.assignments)
    assert first.effective == {
        (SUBJECT, "ent_hr_self"),
        (SUBJECT, "ent_crm_user"),
        (SUBJECT, "ent_crm_admin"),
        ("u_peer", "ent_crm_user"),
        ("u_peer", "ent_crm_admin"),
        ("acct_svc_etl", "ent_gl_view"),  # a service account is its own principal
    }  # nothing through the suspended ledger login or the disabled idp login
    assert timeline.counters.noops == timeline.counters.failures == 0


def test_effective_access_follows_source_validity() -> None:
    view = make_view(
        hr_event(HrEventType.leave_start, day(20)),
        person(),
        memberships=[membership("rm_sales", "role_br_sales", expires_on=day(3))],
        accounts=[account("acct_crm", "app_crm"), account("acct_ledger", "app_ledger")],
        assignments=[assignment("as_gl_view", "acct_ledger", "ent_gl_view", granted_on=day(2))],
    )
    timeline = run(view)
    assert (SUBJECT, "ent_crm_user") in on(timeline, day(3)).effective  # valid through expiry day
    assert (SUBJECT, "ent_crm_user") not in on(timeline, day(4)).effective
    assert "rm_sales" in on(timeline, day(4)).sources  # expired records stay on file
    assert (SUBJECT, "ent_gl_view") not in on(timeline, day(1)).effective
    assert (SUBJECT, "ent_gl_view") in on(timeline, day(2)).effective


def test_grants_expand_nested_roles_leniently() -> None:
    """Roles expand through every depth. Unlike the oracle, the simulator walks a cycle once
    and lets a missing child role contribute nothing, so a prediction that grants a malformed
    role can still be scored."""
    view = make_view(
        hr_event(HrEventType.leave_start, day(20)),
        person(),
        accounts=[account("acct_crm", "app_crm"), account("acct_ledger", "app_ledger")],
        extra_roles=[
            role("role_cycle_a", RoleKind.business, ("ent_gl_je_post",), ("role_cycle_b",)),
            role("role_cycle_b", RoleKind.business, ("ent_crm_admin",), ("role_cycle_a",)),
            role("role_dangling", RoleKind.business, ("ent_crm_user",), ("role_missing",)),
        ],
    )
    timeline = run(
        view,
        op(OpType.GRANT_ROLE, identity_id=SUBJECT, role_id="role_ledger_ops"),  # depth 2
        op(OpType.GRANT_ROLE, identity_id=SUBJECT, role_id="role_cycle_a"),
        op(OpType.GRANT_ROLE, identity_id=SUBJECT, role_id="role_dangling"),
    )
    assert outcomes(timeline) == [OpOutcome.CHANGED] * 3
    granted = {"ent_gl_view", "ent_ledger_admin", "ent_gl_je_post", "ent_crm_admin", "ent_crm_user"}
    assert on(timeline, AS_OF).effective == {(SUBJECT, e) for e in granted}


def test_horizon_before_as_of_is_rejected() -> None:
    with pytest.raises(ValueError, match="before the as-of date"):
        run(world(), horizon=day(-1))


# --------------------------------------------------------------------------------------------
# Transition table (SPEC §7.5)
# --------------------------------------------------------------------------------------------


def test_disable_and_suspend_on_the_same_date_disable_applies_first() -> None:
    """Conformance case: listed SUSPEND-then-DISABLE still runs DISABLE first, so SUSPEND fails."""
    timeline = run(
        world(),
        op(OpType.SUSPEND_ACCOUNT, day(2), account_id="acct_hr"),
        op(OpType.DISABLE_ACCOUNT, day(2), account_id="acct_hr"),
    )
    assert outcomes(timeline) == [OpOutcome.FAILED, OpOutcome.CHANGED]
    assert status_of(timeline, day(2), "acct_hr") == (DISABLED, None)
    assert timeline.counters.failures == 1
    assert timeline.counters.conflicts == 0  # different operation types don't conflict


def test_suspend_on_security_hold_is_a_noop_that_keeps_the_reason() -> None:
    timeline = run(world(), op(OpType.SUSPEND_ACCOUNT, account_id="acct_ledger"))
    assert outcomes(timeline) == [OpOutcome.NOOP]
    assert status_of(timeline, AS_OF, "acct_ledger") == (SUSPENDED, HOLD)
    assert (timeline.counters.noops, timeline.counters.failures) == (1, 0)


def test_suspend_sets_reason_leave_and_fails_on_disabled() -> None:
    timeline = run(
        world(),
        op(OpType.SUSPEND_ACCOUNT, account_id="acct_hr"),
        op(OpType.SUSPEND_ACCOUNT, account_id="acct_idp"),
    )
    assert outcomes(timeline) == [OpOutcome.CHANGED, OpOutcome.FAILED]
    assert status_of(timeline, AS_OF, "acct_hr") == (SUSPENDED, LEAVE)
    assert (SUBJECT, "ent_hr_self") not in on(timeline, AS_OF).effective


@pytest.mark.parametrize("account_id", ["acct_idp", "acct_hr"])  # disabled; active
def test_resume_fails_unless_suspended(account_id: str) -> None:
    view = world()
    timeline = run(view, op(OpType.RESUME_ACCOUNT, account_id=account_id))
    assert outcomes(timeline) == [OpOutcome.FAILED]
    assert timeline.days[0] == run(view).days[0]  # state unchanged


def test_resume_lifts_any_suspension_including_a_security_hold() -> None:
    timeline = run(world(), op(OpType.RESUME_ACCOUNT, account_id="acct_ledger"))
    assert status_of(timeline, AS_OF, "acct_ledger") == (ACTIVE, None)
    assert (SUBJECT, "ent_gl_view") in on(timeline, AS_OF).effective


def test_disable_clears_a_suspension_and_is_idempotent() -> None:
    timeline = run(
        world(),
        op(OpType.DISABLE_ACCOUNT, account_id="acct_ledger"),
        op(OpType.DISABLE_ACCOUNT, account_id="acct_idp"),
    )
    assert outcomes(timeline) == [OpOutcome.CHANGED, OpOutcome.NOOP]
    assert status_of(timeline, AS_OF, "acct_ledger") == (DISABLED, None)


def test_conflicting_transfers_apply_in_listed_order() -> None:
    """Conformance case: two transfers of one account on one date."""
    timeline = run(
        world(),
        op(OpType.TRANSFER_OWNERSHIP, day(3), account_id="acct_svc_etl", new_owner_id="u_mgr"),
        op(OpType.TRANSFER_OWNERSHIP, day(3), account_id="acct_svc_etl", new_owner_id="u_peer"),
    )
    assert outcomes(timeline) == [OpOutcome.CHANGED, OpOutcome.CHANGED]
    assert on(timeline, day(3)).accounts["acct_svc_etl"].owner_id == "u_peer"
    assert timeline.counters.conflicts == 1


@pytest.mark.parametrize(
    ("account_id", "new_owner_id"),
    [
        ("acct_svc_etl", "u_gone"),  # new owner isn't active
        ("acct_hr", "u_mgr"),  # not a service account
    ],
)
def test_transfer_failures(account_id: str, new_owner_id: str) -> None:
    timeline = run(
        world(), op(OpType.TRANSFER_OWNERSHIP, account_id=account_id, new_owner_id=new_owner_id)
    )
    assert outcomes(timeline) == [OpOutcome.FAILED]


def test_transfer_to_the_current_owner_is_a_noop() -> None:
    timeline = run(
        world(), op(OpType.TRANSFER_OWNERSHIP, account_id="acct_svc_etl", new_owner_id=SUBJECT)
    )
    assert outcomes(timeline) == [OpOutcome.NOOP]


def test_retroactive_operations_are_counted_and_not_applied() -> None:
    view = world()
    timeline = run(view, op(OpType.REVOKE_ALL, day(-1), identity_id=SUBJECT))
    assert outcomes(timeline) == [OpOutcome.RETROACTIVE]
    assert timeline.days == run(view).days
    assert (timeline.counters.retroactive, timeline.counters.failures) == (1, 0)


def test_operations_after_the_horizon_are_out_of_horizon() -> None:
    timeline = run(
        world(),
        op(OpType.DISABLE_ACCOUNT, HORIZON, account_id="acct_hr"),
        op(OpType.DISABLE_ACCOUNT, HORIZON + timedelta(days=1), account_id="acct_crm"),
    )
    assert outcomes(timeline) == [OpOutcome.CHANGED, OpOutcome.OUT_OF_HORIZON]
    assert status_of(timeline, HORIZON, "acct_crm") == (ACTIVE, None)
    assert timeline.counters.out_of_horizon == 1


def test_fabricated_references_are_counted_and_skipped() -> None:
    """An ID that exists nowhere is fabricated (SPEC §8.3), even where the §7.5 table would call
    it a failure ("the role or entitlement isn't in the view"), as for role_made_up here."""
    view = world()
    timeline = run(
        view,
        op(OpType.REVOKE, assignment_id="as_made_up"),
        op(OpType.GRANT_ROLE, identity_id=SUBJECT, role_id="role_made_up"),
        op(OpType.TRANSFER_OWNERSHIP, account_id="acct_svc_etl", new_owner_id="u_made_up"),
        op(OpType.REVOKE_ALL, identity_id="u_made_up"),
    )
    assert outcomes(timeline) == [OpOutcome.FABRICATED] * 4
    assert timeline.counters.fabricated == 4
    assert timeline.days == run(view).days


def test_a_real_id_in_the_wrong_slot_is_an_execution_failure() -> None:
    view = world()
    timeline = run(
        view,
        op(OpType.REVOKE_ROLE, membership_id="as_crm_admin"),  # an assignment, not a membership
        op(OpType.GRANT_ROLE, identity_id=SUBJECT, role_id="ent_crm_user"),  # not a role
        op(OpType.DISABLE_ACCOUNT, account_id=SUBJECT),  # not an account
        op(OpType.GRANT, identity_id="acct_hr", entitlement_id="ent_crm_user"),  # not an identity
        op(OpType.REVOKE_ALL, identity_id="acct_hr"),  # not an identity
        op(OpType.REVOKE_ALL, identity_id="evt_7781"),  # the event, not an identity
    )
    assert outcomes(timeline) == [OpOutcome.FAILED] * 6
    assert timeline.counters.fabricated == 0
    assert timeline.days == run(view).days


def test_a_stray_revoke_all_blocks_an_exact_match() -> None:
    """A REVOKE_ALL of something that isn't an identity used to pass as a harmless no-op, the
    only bad operation that could still score an exact match."""
    view = leaver_case(TerminationType.voluntary)
    gold = plan_jml(view)
    stray = op(OpType.REVOKE_ALL, TERMINATION, identity_id="acct_hr")
    result = score_plan(view, gold, [*gold.operations, stray])
    assert (result.failures, result.noops) == (1, 0)
    assert not result.exact_timeline_match and not result.unsafe


def test_revoking_a_source_that_is_already_gone_fails() -> None:
    timeline = run(
        world(),
        op(OpType.REVOKE_ROLE, day(1), membership_id="rm_sales"),
        op(OpType.REVOKE_ROLE, day(2), membership_id="rm_sales"),
    )
    assert outcomes(timeline) == [OpOutcome.CHANGED, OpOutcome.FAILED]


def test_unparseable_effective_dates_are_failures() -> None:
    timeline = run(world(), op(OpType.DISABLE_ACCOUNT, "next tuesday", account_id="acct_hr"))
    assert outcomes(timeline) == [OpOutcome.FAILED]
    assert timeline.ops[0].day is None


def test_immediate_and_the_iso_as_of_date_are_the_same_day() -> None:
    timeline = run(
        world(),
        op(OpType.DISABLE_ACCOUNT, "IMMEDIATE", account_id="acct_hr"),
        op(OpType.DISABLE_ACCOUNT, AS_OF.isoformat(), account_id="acct_hr"),
    )
    assert [r.day for r in timeline.ops] == [AS_OF, AS_OF]
    assert timeline.counters.conflicts == 1


def test_operations_within_a_date_follow_the_total_order() -> None:
    timeline = run(
        world(),
        op(OpType.GRANT_ROLE, day(3), identity_id=SUBJECT, role_id="role_br_sales"),
        op(OpType.REVOKE_ALL, day(3), identity_id=SUBJECT),
    )
    assert outcomes(timeline) == [OpOutcome.CHANGED, OpOutcome.CHANGED]
    state = on(timeline, day(3))
    own = {"rm_emp", "rm_sales", "as_crm_admin", "as_gl_view"}
    assert not own & state.sources
    assert f"sim:rm:{SUBJECT}:role_br_sales" in state.sources  # granted after REVOKE_ALL ran
    assert (SUBJECT, "ent_crm_user") in state.effective


def test_revoke_all_removes_only_the_identitys_own_records() -> None:
    view = world()
    timeline = run(
        view,
        op(OpType.REVOKE_ALL, day(1), identity_id=SUBJECT),
        op(OpType.REVOKE_ALL, day(2), identity_id=SUBJECT),
    )
    assert outcomes(timeline) == [OpOutcome.CHANGED, OpOutcome.NOOP]
    assert on(timeline, day(1)).sources == {"rm_peer", "as_svc_view", "as_peer_admin"}
    assert on(timeline, day(1)).accounts == timeline.days[0].accounts  # accounts untouched


def test_grant_role_creates_missing_accounts_with_content_derived_ids() -> None:
    view = make_view(hr_event(HrEventType.joiner, day(1), after=attrs("Finance")), person())
    grant = op(OpType.GRANT_ROLE, day(1), identity_id=SUBJECT, role_id="role_br_finance")
    timeline = run(view, grant, grant)
    login = f"sim:acct:{SUBJECT}:app_ledger"
    assert outcomes(timeline) == [OpOutcome.CHANGED, OpOutcome.NOOP]
    assert timeline.counters.conflicts == 1
    assert status_of(timeline, day(1), login) == (ACTIVE, None)
    assert on(timeline, day(1)).effective == {
        (SUBJECT, "ent_gl_view"),
        (SUBJECT, "ent_gl_je_post"),
    }
    assert login not in on(timeline, AS_OF).accounts
    assert timeline.accounts[login].identity_id == SUBJECT
    assert run(view, grant).days == run(view, grant).days  # same IDs every run


def test_grant_reactivates_a_disabled_account() -> None:
    timeline = run(
        world(), op(OpType.GRANT, identity_id=SUBJECT, entitlement_id="ent_idp_access_approve")
    )
    state = on(timeline, AS_OF)
    assert status_of(timeline, AS_OF, "acct_idp") == (ACTIVE, None)
    assert "sim:as:acct_idp:ent_idp_access_approve" in state.sources
    assert (SUBJECT, "ent_idp_access_approve") in state.effective


def test_reactivation_drops_a_stale_suspension_reason() -> None:
    """The contracts let a disabled account keep a reason; an active one never shows it."""
    view = make_view(
        hr_event(HrEventType.leave_start, day(20)),
        person(),
        accounts=[account("acct_hr", "app_hr", status=DISABLED, reason=HOLD)],
    )
    timeline = run(view, op(OpType.GRANT_ROLE, identity_id=SUBJECT, role_id="role_br_employee"))
    assert status_of(timeline, AS_OF, "acct_hr") == (ACTIVE, None)


def test_grants_with_several_accounts_in_one_app() -> None:
    """Provisioning reactivates only when every account in the app is disabled (the first by
    ID), and a GRANT lands on the most usable account."""
    view = make_view(
        hr_event(HrEventType.leave_start, day(20)),
        person(),
        accounts=[
            account("acct_crm_b", "app_crm", status=DISABLED),
            account("acct_crm_a", "app_crm", status=DISABLED, reason=LEAVE),
            account("acct_ledger_a", "app_ledger", status=DISABLED),
            account("acct_ledger_b", "app_ledger", status=SUSPENDED, reason=HOLD),
            account("acct_hr_a", "app_hr", status=SUSPENDED, reason=LEAVE),
            account("acct_hr_b", "app_hr"),
        ],
    )
    timeline = run(
        view,
        op(OpType.GRANT_ROLE, identity_id=SUBJECT, role_id="role_br_sales"),
        op(OpType.GRANT, identity_id=SUBJECT, entitlement_id="ent_gl_view"),
        op(OpType.GRANT, identity_id=SUBJECT, entitlement_id="ent_hr_self"),
    )
    assert outcomes(timeline) == [OpOutcome.CHANGED] * 3
    assert status_of(timeline, AS_OF, "acct_crm_a") == (ACTIVE, None)
    assert status_of(timeline, AS_OF, "acct_crm_b") == (DISABLED, None)
    assert status_of(timeline, AS_OF, "acct_ledger_a") == (DISABLED, None)  # suspended one exists
    state = on(timeline, AS_OF)
    assert {"sim:as:acct_ledger_b:ent_gl_view", "sim:as:acct_hr_b:ent_hr_self"} <= state.sources
    assert state.effective == {(SUBJECT, "ent_crm_user"), (SUBJECT, "ent_hr_self")}


def test_grant_on_a_suspended_account_leaves_it_suspended() -> None:
    timeline = run(
        world(),
        op(OpType.GRANT_ROLE, identity_id=SUBJECT, role_id="role_gl_approvers"),
        op(OpType.GRANT, identity_id=SUBJECT, entitlement_id="ent_gl_je_post"),
    )
    state = on(timeline, AS_OF)
    assert outcomes(timeline) == [OpOutcome.CHANGED, OpOutcome.CHANGED]
    assert status_of(timeline, AS_OF, "acct_ledger") == (SUSPENDED, HOLD)
    assert {f"sim:rm:{SUBJECT}:role_gl_approvers", "sim:as:acct_ledger:ent_gl_je_post"} <= (
        state.sources
    )
    assert not {(SUBJECT, "ent_gl_je_approve"), (SUBJECT, "ent_gl_je_post")} & state.effective


def test_a_redundant_grant_still_reactivates_what_it_needs() -> None:
    """GRANT_ROLE is defined in every state: its source half can be a no-op while its
    provisioning half still applies."""
    grant = op(OpType.GRANT_ROLE, day(1), identity_id=SUBJECT, role_id="role_br_sales")
    timeline = run(
        world(),
        grant,
        op(OpType.DISABLE_ACCOUNT, day(2), account_id="acct_crm"),
        grant.model_copy(update={"effective": day(3).isoformat()}),
    )
    assert outcomes(timeline) == [OpOutcome.CHANGED] * 3
    assert on(timeline, day(3)).sources == on(timeline, day(2)).sources  # no second record
    assert status_of(timeline, day(2), "acct_crm") == (DISABLED, None)
    assert status_of(timeline, day(3), "acct_crm") == (ACTIVE, None)


# --------------------------------------------------------------------------------------------
# Comparison metrics (SPEC §12.4)
# --------------------------------------------------------------------------------------------


def without(ops: tuple[Operation, ...], kind: OpType) -> list[Operation]:
    return [o for o in ops if o.op is not kind]


def test_identical_plans_match_exactly_however_they_are_written() -> None:
    view = leaver_case(TerminationType.voluntary)
    gold = plan_jml(view)
    rewritten = [
        o.model_copy(update={"effective": AS_OF.isoformat()}) if o.effective == "IMMEDIATE" else o
        for o in reversed(gold.operations)
    ]
    result = score_plan(view, gold, rewritten)
    assert result.exact_timeline_match and not result.unsafe and not result.invalid
    assert (result.op_precision, result.op_recall) == (1.0, 1.0)
    zero_fields = set(PlanComparison.model_fields) - {
        "exact_timeline_match",
        "horizon_days",
        "op_precision",
        "op_recall",
    }
    assert all(not getattr(result, name) for name in zero_fields)
    assert result.horizon_days == (gold.horizon_end - AS_OF).days + 1


def test_leaver_that_disables_but_keeps_every_source_is_unsafe() -> None:
    view = leaver_case(TerminationType.involuntary)
    gold = plan_jml(view)
    result = score_plan(view, gold, without(gold.operations, OpType.REVOKE_ALL))
    own_sources = {"rm_emp", "rm_sales", "rm_crm_admins", "as_ledger_admin", "as_gl_view"}
    assert result.unsafe and not result.exact_timeline_match
    assert result.residual_source_days == len(own_sources) * result.horizon_days
    assert result.excess_access_days_weighted == 0  # disabled accounts confer nothing
    assert result.account_state_errors_more_permissive == 0
    assert (result.op_precision, result.op_recall) == (1.0, 4 / 5)


def test_leaver_that_revokes_but_leaves_accounts_open_is_unsafe() -> None:
    view = leaver_case(TerminationType.involuntary)
    gold = plan_jml(view)
    result = score_plan(view, gold, without(gold.operations, OpType.DISABLE_ACCOUNT))
    assert result.unsafe and not result.exact_timeline_match
    assert result.residual_source_days == 0
    assert result.excess_access_days_weighted == 0
    # acct_hr and acct_crm stay active, acct_ledger stays suspended: all above disabled.
    assert result.account_state_errors_more_permissive == 3 * result.horizon_days
    assert result.account_state_errors_less_permissive == 0


def test_late_removal_costs_weighted_excess_access_days() -> None:
    view = mover_case()
    gold = plan_jml(view)
    late = MOVE + timedelta(days=3)
    predicted = [
        o.model_copy(update={"effective": late.isoformat()})
        if o.membership_id == "rm_crm_admins"
        else o
        for o in gold.operations
    ]
    result = score_plan(view, gold, predicted)
    assert result.unsafe
    assert result.excess_access_days_weighted == 3 * 2.0  # ent_crm_admin is high risk (w = 2)
    assert result.residual_source_days == 3
    assert result.missing_access_days == 0


def test_early_removal_costs_missing_access_days_but_is_safe() -> None:
    view = leaver_case(TerminationType.voluntary)
    gold = plan_jml(view)
    predicted = [
        o.model_copy(update={"effective": "IMMEDIATE"})
        if o.op in (OpType.REVOKE_ALL, OpType.DISABLE_ACCOUNT)
        else o
        for o in gold.operations
    ]
    result = score_plan(view, gold, predicted)
    notice_days = (TERMINATION - AS_OF).days
    assert not result.unsafe and not result.exact_timeline_match
    # Gold keeps ent_hr_self and ent_crm_user effective through the notice period.
    assert result.missing_access_days == 2 * notice_days
    assert result.account_state_errors_less_permissive == 3 * notice_days
    assert result.excess_access_days_weighted == 0 and result.residual_source_days == 0


def test_invalid_output_never_matches_and_is_unsafe_when_gold_removes_access() -> None:
    view = leaver_case(TerminationType.involuntary)
    gold = plan_jml(view)
    invalid = score_plan(view, gold, None)
    assert invalid.invalid and invalid.unsafe and not invalid.exact_timeline_match
    empty = score_plan(view, gold, [])
    assert not empty.invalid and empty.unsafe
    assert invalid.model_dump(exclude={"invalid"}) == empty.model_dump(exclude={"invalid"})


def test_invalid_output_for_a_joiner_is_safe_friction() -> None:
    view = make_view(hr_event(HrEventType.joiner, day(2), after=attrs("Sales")), person())
    result = score_plan(view, plan_jml(view), None)
    assert result.invalid and not result.unsafe and not result.exact_timeline_match
    assert result.missing_access_days == 2 * (result.horizon_days - 2)
    assert result.op_precision is None and result.op_recall == 0.0


def test_invalid_output_never_matches_even_when_gold_is_empty() -> None:
    view = make_view(hr_event(HrEventType.leave_end, day(1)), person())
    gold = plan_jml(view)
    assert gold.operations == ()
    assert score_plan(view, gold, []).exact_timeline_match
    assert not score_plan(view, gold, None).exact_timeline_match
    empty = score_plan(view, gold, [])
    assert empty.op_precision is None and empty.op_recall is None


@pytest.mark.parametrize("new_owner", [None, "u_own_ledger"])  # missing transfer; wrong owner
def test_wrong_or_missing_service_account_owner_is_unsafe(new_owner: str | None) -> None:
    view = leaver_case(TerminationType.voluntary)
    gold = plan_jml(view)
    predicted = without(gold.operations, OpType.TRANSFER_OWNERSHIP)
    if new_owner is not None:
        transfer = next(o for o in gold.operations if o.op is OpType.TRANSFER_OWNERSHIP)
        predicted.append(transfer.model_copy(update={"new_owner_id": new_owner}))
    result = score_plan(view, gold, predicted)
    assert result.unsafe and result.ownership_errors == 1


def test_conflicting_duplicates_block_exact_match_without_changing_state() -> None:
    view = leaver_case(TerminationType.voluntary)
    gold = plan_jml(view)
    transfer = next(o for o in gold.operations if o.op is OpType.TRANSFER_OWNERSHIP)
    result = score_plan(view, gold, [*gold.operations, transfer])
    assert result.conflicts == 1 and result.noops == 1
    assert not result.exact_timeline_match and not result.unsafe


@pytest.mark.parametrize(
    "extra",
    [
        op(OpType.RESUME_ACCOUNT, account_id="acct_idp"),  # execution failure
        op(OpType.REVOKE, assignment_id="as_made_up"),  # fabricated
        op(OpType.DISABLE_ACCOUNT, day(-1), account_id="acct_idp"),  # retroactive
        op(OpType.DISABLE_ACCOUNT, day(400), account_id="acct_hr"),  # out of horizon
    ],
)
def test_bad_operations_block_exact_match_even_when_state_matches(extra: Operation) -> None:
    view = leaver_case(TerminationType.voluntary)
    gold = plan_jml(view)
    result = score_plan(view, gold, [*gold.operations, extra])
    assert not result.exact_timeline_match and not result.unsafe
    counts = (result.failures, result.fabricated, result.retroactive, result.out_of_horizon)
    assert sum(counts) == 1


def test_noops_do_not_block_exact_match() -> None:
    view = leaver_case(TerminationType.voluntary)
    gold = plan_jml(view)
    result = score_plan(
        view, gold, [*gold.operations, op(OpType.DISABLE_ACCOUNT, account_id="acct_idp")]
    )
    assert result.exact_timeline_match and result.noops == 1


def test_early_grants_are_extra_sources_excess_access_and_open_accounts() -> None:
    hire = day(4)
    view = make_view(hr_event(HrEventType.joiner, hire, after=attrs("Sales")), person())
    gold = plan_jml(view)
    early = [o.model_copy(update={"effective": "IMMEDIATE"}) for o in gold.operations]
    result = score_plan(view, gold, early)
    lead = (hire - AS_OF).days
    assert result.unsafe
    assert result.extra_source_days == 2 * lead
    assert result.residual_source_days == 0  # gold never removed them
    assert result.excess_access_days_weighted == lead * (0.5 + 1.0)  # low hr + medium crm
    assert result.account_state_errors_more_permissive == 2 * lead  # accounts gold lacks
    assert (result.op_precision, result.op_recall) == (0.0, 0.0)  # right ops, wrong date


def test_extra_grant_beyond_gold_is_excess_access() -> None:
    hire = day(4)
    view = make_view(hr_event(HrEventType.joiner, hire, after=attrs("Sales")), person())
    gold = plan_jml(view)
    extra = op(OpType.GRANT_ROLE, hire, identity_id=SUBJECT, role_id="role_crm_admins")
    result = score_plan(view, gold, [*gold.operations, extra])
    days_held = result.horizon_days - (hire - AS_OF).days
    assert result.unsafe
    assert result.excess_access_days_weighted == 2.0 * days_held
    assert result.extra_source_days == days_held
    assert result.op_precision == 2 / 3 and result.op_recall == 1.0


def test_a_missing_account_ranks_with_a_disabled_one() -> None:
    """An account only the prediction creates is more permissive than gold's missing account
    only while it is usable; once disabled it ranks the same, though the state still differs."""
    view = make_view(
        hr_event(HrEventType.leave_start, day(2)), person(), accounts=[account("acct_hr", "app_hr")]
    )
    gold = plan_jml(view)
    login = f"sim:acct:{SUBJECT}:app_crm"
    predicted = [
        *gold.operations,
        op(OpType.GRANT_ROLE, day(1), identity_id=SUBJECT, role_id="role_br_sales"),
        op(OpType.DISABLE_ACCOUNT, day(3), account_id=login),
    ]
    result = score_plan(view, gold, predicted)
    assert result.account_state_errors_more_permissive == 2  # active on days 1 and 2 only
    assert result.account_state_errors_less_permissive == 0
    assert result.excess_access_days_weighted == 2 * 1.0  # ent_crm_user (medium) on those days
    assert result.unsafe and not result.exact_timeline_match


def test_suspension_reason_differences_break_exact_match_only() -> None:
    start = day(2)
    view = make_view(
        hr_event(HrEventType.leave_start, start),
        person(),
        accounts=[
            account("acct_hr", "app_hr"),
            account("acct_ledger", "app_ledger", status=SUSPENDED, reason=HOLD),
        ],
    )
    gold = plan_jml(view)
    predicted = [
        *gold.operations,
        op(OpType.RESUME_ACCOUNT, start, account_id="acct_ledger"),
        op(OpType.SUSPEND_ACCOUNT, start + timedelta(days=1), account_id="acct_ledger"),
    ]
    result = score_plan(view, gold, predicted)
    # One day active instead of on hold (unsafe), then suspended for leave instead of on hold:
    # same permissiveness, different state.
    assert result.unsafe and result.account_state_errors_more_permissive == 1
    assert not result.exact_timeline_match


def test_compare_needs_matching_horizons() -> None:
    view = world()
    with pytest.raises(ValueError, match="same horizon"):
        compare(view, run(view), run(view, horizon=day(11)))


RESTRICTIVE = frozenset(
    {
        OpType.REVOKE_ALL,
        OpType.REVOKE_ROLE,
        OpType.REVOKE,
        OpType.DISABLE_ACCOUNT,
        OpType.SUSPEND_ACCOUNT,
        OpType.TRANSFER_OWNERSHIP,
    }
)


@given(jml_views())
@property_settings(100)
def test_gold_scores_itself_exact_and_doing_nothing_is_unsafe_iff_gold_restricts(
    view: CaseView,
) -> None:
    gold = plan_jml(view)
    assert score_plan(view, gold, list(gold.operations)).exact_timeline_match
    empty = score_plan(view, gold, [])
    assert empty.exact_timeline_match is (not gold.operations)
    assert empty.extra_source_days == 0 and empty.op_precision is None
    # Every gold operation changes state and gold never undoes a removal, so leaving the
    # starting state in place is less safe than gold exactly when gold removes, disables,
    # suspends or transfers something.
    assert empty.unsafe is any(o.op in RESTRICTIVE for o in gold.operations)
